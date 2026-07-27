# -*- coding: utf-8 -*-
"""
ウェブカメラの録画 + 最新フレーム共有(OptiTrack/VNA と連動)
================================================================================

計測開始と連動して webカメラ(OpenCV)を開き、専用スレッドで連続的にフレームを読み取る:
  - 最新フレームをロック下で保持する(ライブ・ダッシュボードの表示用)。
  - 動画ファイル(mp4)へ書き出す(計測後の同期表示・確認用)。

録画の内部タイムラインが実時間とほぼ一致するよう、書き出し FPS は「起動直後に実測した
キャプチャ FPS」を用いる(取得できない環境では既定 30)。動画とデータセットの厳密な突き合わせは
CSV の WallClock 列と録画開始時刻(start_wall)で行う(sync_video_with_dataset.py)。

cv2 は本モジュールでのみ import する(カメラを使わない計測では OpenCV 依存を持ち込まない)。
"""

import time
import threading

try:
    import cv2
except ImportError:  # OpenCV 未導入でも import 自体は失敗させない
    cv2 = None

import numpy as np


class CameraError(Exception):
    """カメラのオープン/初期化に失敗したことを表す。"""


class CameraRecorder:
    """
    1 台の webカメラをキャプチャし、最新フレーム共有 + 動画書き出しを行う。

    使い方:
        rec = CameraRecorder(index=0, out_path="clip.mp4")
        rec.start()                      # カメラを開いてスレッド開始(失敗時 CameraError)
        frame = rec.get_latest_frame()   # 最新 BGR フレーム(未取得なら None)
        info = rec.stop()                # スレッド停止・ファイルクローズ。録画情報 dict を返す
    """

    def __init__(self, index=0, out_path=None, fps=None, use_dshow=True):
        self.index = int(index)
        self.out_path = out_path
        self.forced_fps = fps          # None なら実測 / 取得値を使う
        self.use_dshow = bool(use_dshow)

        self._cap = None
        self._writer = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None            # 最新 BGR フレーム(np.ndarray)

        # 録画情報(stop() で確定)
        self.start_wall = None         # 録画開始のエポック秒(WallClock 突き合わせ用)
        self.width = None
        self.height = None
        self.fps = None
        self.frame_count = 0

    # ---- 起動: カメラを開き、キャプチャスレッドを立ち上げる ----
    def start(self):
        if cv2 is None:
            raise CameraError(
                "OpenCV(cv2)が見つかりません。`pip install opencv-python` を実行してください。")

        api = cv2.CAP_DSHOW if self.use_dshow else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.index, api)
        if not cap.isOpened():
            # DSHOW で開けない環境向けに既定バックエンドで再試行
            cap = cv2.VideoCapture(self.index)
        if not cap.isOpened():
            raise CameraError("カメラ(index={})を開けませんでした。接続と使用中プロセスを確認してください。".format(self.index))

        # 最初の 1 フレームでサイズを確定する
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise CameraError("カメラ(index={})からフレームを取得できませんでした。".format(self.index))
        self.height, self.width = frame.shape[0], frame.shape[1]

        # 書き出し FPS を決める: 明示指定 > 実測 > デバイス申告 > 既定30
        fps = self.forced_fps or self._measure_fps(cap) or cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 1 or fps > 240:
            fps = 30.0
        self.fps = float(fps)

        # 動画ファイルを開く(out_path 指定時)
        if self.out_path:
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            self._writer = cv2.VideoWriter(
                self.out_path, fourcc, self.fps, (self.width, self.height))
            if not self._writer.isOpened():
                cap.release()
                raise CameraError("動画ファイルを開けませんでした: {}".format(self.out_path))

        self._cap = cap
        with self._lock:
            self._latest = frame
        self.frame_count = 0
        self.start_wall = time.time()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="CameraRecorder", daemon=True)
        self._thread.start()

    def _measure_fps(self, cap, n=15):
        """起動直後に n フレーム読み取り、実測 FPS を推定する。失敗時 None。"""
        t0 = time.time()
        got = 0
        for _ in range(n):
            ok, _f = cap.read()
            if ok:
                got += 1
        dt = time.time() - t0
        if got >= 2 and dt > 0:
            return got / dt
        return None

    # ---- キャプチャスレッド本体 ----
    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if not ok or frame is None:
                if self._stop.wait(0.01):
                    break
                continue
            with self._lock:
                self._latest = frame
            if self._writer is not None:
                try:
                    self._writer.write(frame)
                    self.frame_count += 1
                except Exception:
                    pass

    # ---- 最新フレーム(BGR)を返す。未取得なら None ----
    def get_latest_frame(self):
        with self._lock:
            f = self._latest
            return None if f is None else f.copy()

    # ---- 停止: スレッド join・ファイルクローズ。録画情報 dict を返す ----
    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception:
                pass
            self._writer = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        duration = self.frame_count / self.fps if (self.fps and self.frame_count) else 0.0
        return {
            "path": self.out_path,
            "start_wall": self.start_wall,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "frames": self.frame_count,
            "duration": duration,
        }


def list_camera_indices(max_index=5, use_dshow=True):
    """
    0..max_index-1 のカメラ番号のうち、開けるものを列挙して返す。
    GUI のカメラ選択肢を作るために使う(短時間オープン→即クローズ)。
    """
    if cv2 is None:
        return []
    api = cv2.CAP_DSHOW if use_dshow else cv2.CAP_ANY
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, api)
        try:
            if cap.isOpened():
                found.append(i)
        finally:
            cap.release()
    return found
