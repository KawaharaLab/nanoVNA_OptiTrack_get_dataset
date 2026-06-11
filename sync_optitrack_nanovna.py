# -*- coding: utf-8 -*-
"""
OptiTrack (Motive 3.1 / NatNet) + JNCRadio VNA 3G 同期計測スクリプト
================================================================================

同一 Windows PC 上で動作する Motive から NatNet 経由でラベル付きマーカーの座標を
受信しつつ、USB シリアル接続された JNCRadio VNA 3G から特定単一周波数の S11 を
連続取得し、両者を同期して CSV に保存する。

計測対象は腕に取り付けた 3 個の対象(リジッドボディ または ラベル付きマーカー):
  1. 上腕   (Upper Arm)
  2. 関節   (Joint / 肘など)
  3. 前腕   (Forearm)
各対象の [X, Y, Z] 座標を同時に取得し、S11 / インピーダンスと紐付けて 1 行に記録する。

★ID の自動判別(キャリブレーション)★
  Motive はセッションごとに ID を自動採番するため実行のたびに ID が変わる。本スクリプトは
  名前付けや ID 指定を不要にするため、計測開始時の最初の有効フレームで 3 対象の「高さ」を
  比較し、一番高い=上腕 / 中間=関節 / 一番低い=前腕 として ID と部位の対応を自動確定する。
  以後はその ID を固定して座標を取得する(設定は HEIGHT_AXIS_INDEX / OBJECT_SOURCE)。

動作確認環境:
  - Windows 10/11, Python 3.8+
  - Motive 3.1.0 Beta 2 / NatNet 4.x (127.0.0.1, Unicast)
  - OptiTrack NatNet SDK 同梱の NatNetClient.py / MoCapData.py / DataDescriptions.py

GUI / スレッド構成:
  - tkinter GUI(メインスレッド): [計測開始]/[計測終了] ボタン。GUI は固まらない。
  - 計測ワーカー(バックグラウンド): VNA から S11 を取得し、3 対象の最新座標と結合して
    メモリ上(self.rows)へ蓄積する。
  - NatNet 受信(SDK 内部スレッド): new_frame_with_data_listener が 3 対象の最新座標を更新。
    最初の有効フレームで「開始時の高さ」による自動判別を実行する。
  - 共有変数は threading.Lock() で保護してスレッド安全に読み書きする。
  - 終了時(終了ボタン / ウィンドウ×)は必ずクリーンアップを実行する:
      VNA のクローズ → NatNet の shutdown() → 全スレッドの join。
    これにより COM ポート / UDP ソケット / スレッドが確実に解放され、
    次回実行時にハングしない。

操作の流れ:
  1. プログラム起動で GUI が立ち上がる。
  2. [計測開始] で計測スタート(最初の有効フレームで自動判別が走る)。
  3. [計測終了] で計測停止 → 保存ダイアログでファイル名指定 → CSV 保存 → 終了。

CSV ヘッダー:
  [Timestamp,
   UpperArm_X, UpperArm_Y, UpperArm_Z,
   Joint_X,    Joint_Y,    Joint_Z,
   Forearm_X,  Forearm_Y,  Forearm_Z,
   S11_Real, S11_Imag, Z_R, Z_X]
"""

import os
import csv
import sys
import time
import queue
import threading
from datetime import datetime

import serial  # pip install pyserial

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# =============================================================================
# 設定（ここを環境に合わせて変更してください）
# =============================================================================

# --- JNCRadio VNA 3G (シリアル) ---
NANOVNA_PORT   = "COM3"        # Windows のデバイスマネージャーで確認した COM 番号
NANOVNA_BAUD   = 115200        # ボーレート(JNCRadio VNA 3G は 115200 を推奨)
TARGET_FREQ_HZ = 13_560_000    # 取得したい単一周波数 [Hz] (例: 13.56 MHz)
SCAN_POINTS    = 11            # 1回の scan の掃引点数(本機の最小は 11)。同一周波数で平均する
Z0             = 50.0          # 特性インピーダンス [Ω]

# --- OptiTrack (NatNet) ---
NATNET_SERVER_IP = "127.0.0.1"  # 同一PCなので localhost (Motive と同じ PC)
NATNET_LOCAL_IP  = "127.0.0.1"  # 同一PCなので localhost
NATNET_USE_MULTICAST = False    # 同一PCループバックは Unicast 推奨

# --- 自動判別(キャリブレーション)方式 ---
# Motive 側で名前付けや ID 設定をしなくても、計測開始時の「高さ」で 3 つの対象を
# 自動的に upperarm / joint / forearm へ割り当てる。
#   計測開始時の最初の有効フレームで 3 対象の高さ(座標)を降順ソートし、
#     1番高い  -> UpperArm(上腕)
#     2番目    -> Joint(関節)
#     1番低い  -> Forearm(前腕)
#   として ID と部位の対応を確定し、以後はその ID を固定して座標を取得する。
#
# 高さに使う座標軸のインデックス: 0=X, 1=Y, 2=Z。
# NatNet(Motive)の既定配信は Y-Up のため「高さ = Y(=1)」。
# Motive を Z-Up でストリーミングしている場合は 2 に変更する。
HEIGHT_AXIS_INDEX = 1

# 判別に使うフレームデータのソース:
#   "rigid_body"     : リジッドボディのみを対象にする
#   "labeled_marker" : ラベル付きマーカーのみを対象にする
#   "auto"           : リジッドボディがあればそれを、無ければラベル付きマーカーを使う
OBJECT_SOURCE = "auto"

# キャリブレーションに必要な対象数(上腕・関節・前腕の 3 個)。
EXPECTED_OBJECT_COUNT = 3

# 自動判別(キャリブレーション)が完了するまで待つ最大秒数。
CALIBRATION_TIMEOUT_SEC = 10.0

# オクルージョン(隠れ)状態のマーカーは座標が無効なため更新をスキップし、
# 直前の有効値を保持する。
SKIP_OCCLUDED = True

# ストリーミング途絶の警告しきい値 [秒](この時間フレームが来なければ警告)
STREAM_STALE_SEC = 2.0

# --- 出力 ---
OUTPUT_CSV = "sync_dataset.csv"
CSV_HEADER = ["Timestamp",
              "UpperArm_X", "UpperArm_Y", "UpperArm_Z",
              "Joint_X",    "Joint_Y",    "Joint_Z",
              "Forearm_X",  "Forearm_Y",  "Forearm_Z",
              "S11_Real", "S11_Imag", "Z_R", "Z_X"]


PART_NAMES = ("UpperArm", "Joint", "Forearm")


# =============================================================================
# 自動判別(キャリブレーション): 開始時の高さで ID -> 部位 を確定する
# =============================================================================
#
# 計測開始時の最初の有効フレームで 3 対象の高さ(HEIGHT_AXIS_INDEX 軸の座標)を
# 降順ソートし、ID -> 部位(UpperArm/Joint/Forearm) の対応表を一度だけ作る。
# 確定後は _id_to_part を固定し、毎フレーム ID 一致で座標を取り出す。
_calib_lock = threading.Lock()
_id_to_part = {}                      # obj_id(int) -> "UpperArm"/"Joint"/"Forearm"
_calibrated = threading.Event()       # 自動判別が完了したら set


def collect_frame_objects(mocap):
    """
    現在フレームの MoCapData から、判別/取得対象の (obj_id, pos) のリストを得る。
    OBJECT_SOURCE に従い、リジッドボディ または ラベル付きマーカー から収集する。
    SKIP_OCCLUDED=True の場合、トラッキング無効/オクルージョン中の対象は除外する。
    """
    objects = []

    # --- リジッドボディ ---
    if OBJECT_SOURCE in ("rigid_body", "auto"):
        rb_data = getattr(mocap, "rigid_body_data", None)
        for rb in getattr(rb_data, "rigid_body_list", None) or []:
            if SKIP_OCCLUDED and not getattr(rb, "tracking_valid", True):
                continue
            objects.append((int(rb.id_num), rb.pos))

    # --- ラベル付きマーカー(auto はリジッドボディが無いときだけ使う) ---
    use_lm = OBJECT_SOURCE == "labeled_marker" or \
        (OBJECT_SOURCE == "auto" and not objects)
    if use_lm:
        lm_data = getattr(mocap, "labeled_marker_data", None)
        for m in getattr(lm_data, "labeled_marker_list", None) or []:
            if SKIP_OCCLUDED and (getattr(m, "param", 0) & 0x01):
                continue
            objects.append((int(m.id_num), m.pos))

    return objects


# キャリブレーション時の「対象数が合わない」警告を間引くための直近出力時刻
_calib_warn_time = [0.0]


def try_calibrate(objects):
    """
    まだ未確定であれば、与えられた (obj_id, pos) 群から ID->部位 を確定する。
    高さ(HEIGHT_AXIS_INDEX 軸)の降順で UpperArm > Joint > Forearm に割り当てる。
    確定できたら True、まだ条件を満たさなければ False を返す。
    """
    if len(objects) < EXPECTED_OBJECT_COUNT:
        return False

    if len(objects) > EXPECTED_OBJECT_COUNT:
        # 対象が多すぎると高さ順の中間(Joint)が一意に決まらないので確定しない。
        now = time.time()
        if now - _calib_warn_time[0] > 2.0:
            _calib_warn_time[0] = now
            print("[WARN] 有効な対象が {} 個あります(期待値 {})。"
                  "余分な対象を Motive 側で外すか OBJECT_SOURCE を見直してください。"
                  .format(len(objects), EXPECTED_OBJECT_COUNT))
        return False

    # 高さ(指定軸の座標)で降順ソート: [0]=最も高い, [-1]=最も低い
    ordered = sorted(
        objects, key=lambda t: t[1][HEIGHT_AXIS_INDEX], reverse=True)
    mapping = {
        ordered[0][0]: "UpperArm",   # 一番高い  -> 上腕
        ordered[1][0]: "Joint",      # 2番目     -> 関節
        ordered[2][0]: "Forearm",    # 一番低い  -> 前腕
    }

    with _calib_lock:
        _id_to_part.clear()
        _id_to_part.update(mapping)
    _calibrated.set()

    axis_name = {0: "X", 1: "Y", 2: "Z"}.get(HEIGHT_AXIS_INDEX, "?")
    print("[INFO] 自動判別 完了(開始時の高さ {} 軸で降順):".format(axis_name))
    for part, (oid, pos) in zip(("UpperArm", "Joint", "Forearm"), ordered):
        print("       {:8s} <- id={}  (高さ {}={:.3f})".format(
            part, oid, axis_name, pos[HEIGHT_AXIS_INDEX]))
    return True


def get_id_to_part():
    """フレームコールバックから呼ぶ: 確定済み ID->部位 マップのスナップショット。"""
    with _calib_lock:
        return dict(_id_to_part)


# =============================================================================
# スレッド間共有: 3 マーカーの最新 OptiTrack 座標
# =============================================================================

# Lock で保護される共有状態。NatNet コールバック(書き込み)と
# VNA ループ(読み出し)の双方からアクセスされる。各部位ごとに {x,y,z,valid} を保持。
_pos_lock = threading.Lock()
_latest_positions = {
    "UpperArm": {"x": None, "y": None, "z": None, "valid": False},
    "Joint":    {"x": None, "y": None, "z": None, "valid": False},
    "Forearm":  {"x": None, "y": None, "z": None, "valid": False},
}

# 最後にフレームを受信した時刻(ストリーミング途絶検知用)。Lock 下で更新。
_last_frame_time = [0.0]

# 全スレッド共通の停止フラグ
_stop_event = threading.Event()


def update_latest_position(name, x, y, z):
    """NatNet コールバックから呼ばれ、指定部位の最新座標をスレッド安全に更新する。"""
    with _pos_lock:
        slot = _latest_positions[name]
        slot["x"] = x
        slot["y"] = y
        slot["z"] = z
        slot["valid"] = True


def mark_frame_received():
    """フレーム受信時刻を更新(ストリーミング生存確認用)。"""
    with _pos_lock:
        _last_frame_time[0] = time.time()


def read_latest_positions():
    """
    VNA ループから呼ばれ、3 マーカーすべての最新座標のスナップショットを
    同一ロック下で一括取得する。
    戻り値: {"UpperArm": (x,y,z,valid), "Joint": (...), "Forearm": (...)}
    """
    snapshot = {}
    with _pos_lock:
        for name, slot in _latest_positions.items():
            snapshot[name] = (slot["x"], slot["y"], slot["z"], slot["valid"])
    return snapshot


def seconds_since_last_frame():
    with _pos_lock:
        last = _last_frame_time[0]
    if last == 0.0:
        return None  # まだ一度も受信していない
    return time.time() - last


def reset_runtime_state():
    """
    1 回の計測を開始する前に、全スレッド共有の状態を初期化する。
    (停止フラグ・自動判別結果・最新座標・受信時刻をクリアする)
    GUI で計測をやり直す場合に、前回の状態が残らないようにする。
    """
    _stop_event.clear()
    _calibrated.clear()
    with _calib_lock:
        _id_to_part.clear()
    with _pos_lock:
        for slot in _latest_positions.values():
            slot["x"] = None
            slot["y"] = None
            slot["z"] = None
            slot["valid"] = False
        _last_frame_time[0] = 0.0
    _calib_warn_time[0] = 0.0


# =============================================================================
# インピーダンス変換
# =============================================================================

def s11_to_impedance(s11, z0=Z0):
    """
    反射係数 S11 (複素数) から負荷インピーダンス Z = R + jX を計算する。
        Z = Z0 * (1 + S11) / (1 - S11)
    戻り値: (R, X)  実部・虚部 [Ω]
    """
    denom = (1.0 - s11)
    if denom == 0:
        return float("inf"), float("inf")  # 完全反射(開放相当)
    z = z0 * (1.0 + s11) / denom
    return z.real, z.imag


# =============================================================================
# JNCRadio VNA 3G シリアル制御
# =============================================================================

class NanoVNA:
    """
    JNCRadio VNA 3G (NanoVNA 互換) のコンソールコマンドを扱う薄いラッパ。

    本機のシリアルは「コマンド\\r を送ると、エコー → 結果行 → プロンプト 'ch> '」
    の順で応答する(マニュアル §7)。scan コマンドで毎回フレッシュな掃引を実行する。

        scan {start(Hz)} {stop(Hz)} [points] [outmask]
        outmask=2 -> 各掃引点の S11 データ(real imag)のみを出力
    """

    PROMPT = b"ch> "

    def __init__(self, port, baud=115200, timeout=2.0):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # 連続出力を止めてプロンプト状態を確定させる
        self._send_raw("pause")
        self._read_until_prompt()

    def _send_raw(self, cmd):
        """コマンドを CR 終端で送信する(本機の終端は <CR>)。"""
        self.ser.write((cmd + "\r").encode("ascii"))
        self.ser.flush()

    def _read_until_prompt(self, max_wait=3.0):
        """プロンプト 'ch> ' が現れるまで読み、受信テキスト全体を返す。"""
        buf = bytearray()
        deadline = time.time() + max_wait
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if self.PROMPT in buf:
                    break
            else:
                if self.PROMPT in buf:
                    break
        return buf.decode("ascii", errors="ignore")

    @staticmethod
    def _parse_s11_lines(text):
        """
        受信テキストから S11 の (real, imag) ペア群を抽出する。
        outmask=2 の各データ行は "real imag" の2列。2 float に解釈できない行
        (コマンドエコー・プロンプト等)はスキップする。
        """
        pairs = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            if len(tokens) < 2:
                continue
            try:
                real = float(tokens[0])
                imag = float(tokens[1])
            except ValueError:
                continue
            pairs.append((real, imag))
        return pairs

    def measure_s11(self):
        """
        単一周波数 TARGET_FREQ_HZ で 1 回 scan を実行し、S11(複素数)を返す。
        SCAN_POINTS 点はすべて同一周波数(start==stop)なので平均してノイズを抑える。
        取得失敗時は None を返す。
        """
        cmd = "scan {start} {stop} {pts} 2".format(
            start=int(TARGET_FREQ_HZ),
            stop=int(TARGET_FREQ_HZ),
            pts=int(SCAN_POINTS),
        )
        self.ser.reset_input_buffer()
        self._send_raw(cmd)
        text = self._read_until_prompt()
        pairs = self._parse_s11_lines(text)
        if not pairs:
            return None
        avg_real = sum(p[0] for p in pairs) / len(pairs)
        avg_imag = sum(p[1] for p in pairs) / len(pairs)
        return complex(avg_real, avg_imag)

    def close(self):
        try:
            self._send_raw("resume")   # 掃引を再開して画面表示を元に戻す
            time.sleep(0.05)
        except Exception:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


# =============================================================================
# OptiTrack / NatNet
# =============================================================================

def _ensure_natnet_on_path():
    """
    NatNetClient.py を import できるよう、よくある配置場所を sys.path に追加する。
      1) このスクリプトと同じフォルダ
      2) 同梱 SDK: ./NatNetSDK/Samples/PythonClient
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        here,
        os.path.join(here, "NatNetSDK", "Samples", "PythonClient"),
    ]
    for d in candidates:
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)


def _on_frame_with_data(data_dict):
    """
    NatNetClient の new_frame_with_data_listener コールバック。
    1 フレームごとに呼ばれ、data_dict["mocap_data"] に MoCapData が入る。

    未確定なら最初の有効フレームで「開始時の高さ」によって ID->部位 を自動判別し、
    確定後は固定した ID->部位 マップで各対象の座標を更新する。
    """
    mark_frame_received()

    mocap = data_dict.get("mocap_data")
    if mocap is None:
        return

    objects = collect_frame_objects(mocap)

    # まだ判別前なら、このフレームで確定を試みる。
    if not _calibrated.is_set():
        if not try_calibrate(objects):
            return  # まだ条件を満たさない(対象数が揃っていない等)

    # 確定済みの ID->部位 マップで座標を更新(2フレーム目以降はこのパスのみ)。
    mapping = get_id_to_part()
    for oid, pos in objects:
        part = mapping.get(oid)
        if part is None:
            continue  # 判別対象外の ID
        update_latest_position(part, float(pos[0]), float(pos[1]), float(pos[2]))


def start_natnet():
    """
    NatNetClient を起動し、フレーム受信コールバックを登録する。
    成功すると streaming_client を返す。失敗時は None。
    """
    _ensure_natnet_on_path()
    try:
        from NatNetClient import NatNetClient
    except ImportError as e:
        print("[ERROR] NatNetClient を import できません: {}".format(e))
        print("        OptiTrack NatNet SDK 同梱の NatNetClient.py / MoCapData.py /")
        print("        DataDescriptions.py を、このスクリプトと同じフォルダ、または")
        print("        ./NatNetSDK/Samples/PythonClient/ に配置してください。")
        return None

    client = NatNetClient()
    client.set_client_address(NATNET_LOCAL_IP)
    client.set_server_address(NATNET_SERVER_IP)
    client.set_use_multicast(NATNET_USE_MULTICAST)

    # フレームごとに MoCapData を受け取るコールバックを登録
    client.new_frame_with_data_listener = _on_frame_with_data

    # 受信開始("d" = data + command スレッド)
    try:
        is_running = client.run("d")
    except Exception as e:
        print("[ERROR] NatNetClient.run() で例外: {}".format(e))
        return None

    if not is_running:
        print("[ERROR] NatNetClient の起動に失敗しました。")
        print("        Motive の Data Streaming(127.0.0.1 / Unicast / Labeled Markers)を確認してください。")
        try:
            client.shutdown()
        except Exception:
            pass
        return None

    # サーバ接続確認(数秒待ってバージョン応答が来るか)
    time.sleep(1.0)
    app_name = getattr(client, "get_application_name", lambda: "")() or ""
    if app_name and app_name != "Not Set":
        print("[INFO] Motive へ接続しました (server app: {}).".format(app_name))
    else:
        print("[INFO] NatNet 起動。Motive からのフレーム受信待ち...")
        print("       (まだサーバ応答なし。Motive の Data Streaming 設定を確認してください)")

    # 自動判別(キャリブレーション)の完了待ちは呼び出し側(ワーカー)が
    # 停止フラグを見ながら中断可能な形で行う。ここではブロックしない。
    return client


def shutdown_natnet(client):
    """
    NatNetClient を安全に停止する。受信スレッドと UDP ソケットを確実に解放する。
    SDK の shutdown() は内部で sockets.close() とスレッド join を行うが、
    run() が途中失敗していると属性が無いことがあるため try/except で頑健にする。
    """
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as e:
        print("[WARN] NatNet shutdown 中に例外(無視): {}".format(e))
    # SDK のスレッドが残っていれば join を試みる(取りこぼし防止)。
    for attr in ("data_thread", "command_thread"):
        th = getattr(client, attr, None)
        if th is not None:
            try:
                if th.is_alive():
                    th.join(timeout=2.0)
            except Exception:
                pass


# =============================================================================
# メイン: VNA サンプリングループ + CSV 書き込み
# =============================================================================

def _fmt_xyz(snap, name):
    """部位の (x,y,z) を CSV セル 3 個へ整形。未受信なら空欄。"""
    x, y, z, valid = snap[name]
    if not valid:
        return ["", "", ""]
    return ["{:.6f}".format(x), "{:.6f}".format(y), "{:.6f}".format(z)]


def _short_xyz(snap, name):
    x, y, z, valid = snap[name]
    if not valid:
        return "(----,----,----)"
    return "({:.3f},{:.3f},{:.3f})".format(x, y, z)


# =============================================================================
# 計測コントローラ: バックグラウンドで VNA + OptiTrack を回しデータを蓄積する
# =============================================================================

class MeasurementController:
    """
    計測のライフサイクル(接続→ループ→停止→クリーンアップ)を管理する。
    GUI(メインスレッド)から start()/request_stop()/cleanup()/save_csv() を呼ぶ。
    実際の計測は専用のワーカースレッドで行うため、GUI は固まらない。

    GUI へは out_queue 経由で (種別, 値) のメッセージを送る:
      ("status", str)   状態テキスト
      ("sample", int)   蓄積サンプル数
      ("error",  str)   致命的エラー(計測継続不可)
      ("finished", None) ワーカーループが自然終了/停止した
    """

    def __init__(self, out_queue):
        self.out_queue = out_queue
        self.vna = None
        self.client = None
        self.worker = None
        self.rows = []
        self.rows_lock = threading.Lock()
        self.sample_count = 0
        self._cleaned = False
        self._cleanup_lock = threading.Lock()

    # ---- GUI へ通知 ----
    def _post(self, kind, value=None):
        try:
            self.out_queue.put_nowait((kind, value))
        except Exception:
            pass

    # ---- 開始 ----
    def start(self):
        """共有状態を初期化し、計測ワーカースレッドを起動する。"""
        reset_runtime_state()
        with self.rows_lock:
            self.rows = []
        self.sample_count = 0
        self._cleaned = False
        self.worker = threading.Thread(
            target=self._worker_loop, name="MeasurementWorker", daemon=True)
        self.worker.start()

    # ---- ワーカースレッド本体 ----
    def _worker_loop(self):
        # 1) VNA 接続
        try:
            self.vna = NanoVNA(NANOVNA_PORT, NANOVNA_BAUD)
        except serial.SerialException as e:
            self._post("error",
                       "VNA シリアル接続に失敗: {}\nCOM 番号や他ソフトの占有を確認してください。"
                       .format(e))
            self._post("finished")
            return
        except Exception as e:
            self._post("error", "VNA 初期化中に予期せぬ例外: {}".format(e))
            self._post("finished")
            return
        self._post("status", "VNA 接続 OK: {} @ {}bps, {:.3f} MHz".format(
            NANOVNA_PORT, NANOVNA_BAUD, TARGET_FREQ_HZ / 1e6))

        # 2) NatNet 開始(ブロックしない)
        self.client = start_natnet()
        if self.client is None:
            self._post("status", "OptiTrack なしで継続(座標列は空欄になります)。")
        else:
            self._post("status", "OptiTrack 受信開始。最初の有効フレームで自動判別します...")

        # 3) 自動判別の完了を「停止フラグを見ながら」中断可能に待つ
        if self.client is not None:
            deadline = time.time() + CALIBRATION_TIMEOUT_SEC
            while not _stop_event.is_set() and not _calibrated.is_set():
                if time.time() > deadline:
                    self._post("status",
                               "[警告] 自動判別が完了しません。3対象のトラッキング/配信設定を確認してください。")
                    break
                time.sleep(0.05)
            if _calibrated.is_set():
                mapping = get_id_to_part()
                part_to_id = {p: i for i, p in mapping.items()}
                self._post("status", "自動判別 完了: " + " / ".join(
                    "{}=id{}".format(p, part_to_id.get(p, "?")) for p in PART_NAMES))

        # 4) 計測ループ(メモリへ蓄積)
        self._post("status", "計測中...")
        last_stale_warn = 0.0
        while not _stop_event.is_set():
            try:
                s11 = self.vna.measure_s11()
            except serial.SerialException as e:
                self._post("error", "VNA シリアル通信が切断されました: {}".format(e))
                break
            except Exception as e:
                self._post("error", "VNA 取得中に例外: {}".format(e))
                break
            if s11 is None:
                continue  # パース失敗はスキップ

            if self.client is not None:
                elapsed = seconds_since_last_frame()
                now = time.time()
                if (elapsed is not None and elapsed > STREAM_STALE_SEC
                        and (now - last_stale_warn) > STREAM_STALE_SEC):
                    last_stale_warn = now
                    self._post("status",
                               "[警告] OptiTrack フレームが {:.1f}s 途絶(座標が古い可能性)".format(elapsed))

            snap = read_latest_positions()
            z_r, z_x = s11_to_impedance(s11, Z0)
            ts = datetime.now().isoformat(timespec="milliseconds")
            row = (
                [ts]
                + _fmt_xyz(snap, "UpperArm")
                + _fmt_xyz(snap, "Joint")
                + _fmt_xyz(snap, "Forearm")
                + ["{:.6f}".format(s11.real), "{:.6f}".format(s11.imag),
                   "{:.4f}".format(z_r), "{:.4f}".format(z_x)]
            )
            with self.rows_lock:
                self.rows.append(row)
                self.sample_count += 1
                count = self.sample_count
            self._post("sample", count)

        self._post("finished")

    # ---- 停止要求(GUI から。即座に戻る) ----
    def request_stop(self):
        _stop_event.set()

    # ---- クリーンアップ(冪等。VNA/NatNet/スレッドを確実に解放) ----
    def cleanup(self):
        """
        計測を完全に停止し、リソースを確実に解放する。
        どのスレッドから何度呼ばれても安全(冪等)。
        ※ ワーカースレッド自身からは呼ばないこと(自分を join できないため)。
        """
        with self._cleanup_lock:
            if self._cleaned:
                return
            _stop_event.set()

            # 1) ワーカースレッドの終了を待つ
            if self.worker is not None and self.worker.is_alive():
                if self.worker is not threading.current_thread():
                    self.worker.join(timeout=8.0)

            # 2) VNA(シリアル)を閉じる -> COM ポート解放
            if self.vna is not None:
                try:
                    self.vna.close()
                except Exception as e:
                    print("[WARN] VNA close 中に例外(無視): {}".format(e))
                self.vna = None

            # 3) NatNet を停止 -> UDP ソケット/受信スレッド解放
            if self.client is not None:
                shutdown_natnet(self.client)
                self.client = None

            self._cleaned = True

    # ---- CSV 保存 ----
    def save_csv(self, path):
        """蓄積済みデータを CSV に書き出す。書き出した行数を返す。"""
        with self.rows_lock:
            rows = list(self.rows)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
        return len(rows)

    def get_sample_count(self):
        with self.rows_lock:
            return self.sample_count


# =============================================================================
# GUI (tkinter)
# =============================================================================

class App(tk.Tk):
    """計測開始/終了ボタンと状態表示を持つシンプルな GUI。"""

    def __init__(self):
        super().__init__()
        self.title("OptiTrack × VNA 同期計測")
        self.geometry("560x420")
        self.minsize(480, 360)

        self.msg_queue = queue.Queue()
        self.controller = None
        self.measuring = False
        self._finalizing = False  # 停止→保存→終了の処理中フラグ

        self._build_widgets()

        # ウィンドウの × でも必ずクリーンアップして終了する
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        # GUI メッセージのポーリング開始
        self.after(100, self._poll_messages)

    # ---- ウィジェット構築 ----
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 6}

        info = ttk.Label(
            self,
            text=("VNA: {}  /  周波数: {:.3f} MHz  /  高さ軸: {}\n"
                  "[計測開始]を押すと最初の有効フレームで自動判別します。").format(
                NANOVNA_PORT, TARGET_FREQ_HZ / 1e6,
                {0: "X", 1: "Y", 2: "Z"}.get(HEIGHT_AXIS_INDEX, "?")),
            justify="left")
        info.pack(anchor="w", **pad)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", **pad)
        self.start_btn = ttk.Button(
            btn_frame, text="計測開始", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(
            btn_frame, text="計測終了 / 保存", command=self.on_stop,
            state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        status_frame = ttk.Frame(self)
        status_frame.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="待機中")
        ttk.Label(status_frame, text="状態:").pack(side="left")
        ttk.Label(status_frame, textvariable=self.status_var,
                  foreground="#0a6").pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="サンプル数: 0")
        ttk.Label(status_frame, textvariable=self.count_var).pack(side="right")

        # ログ表示
        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, height=12, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---- ログ出力 ----
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- 計測開始 ----
    def on_start(self):
        if self.measuring:
            return
        self.measuring = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("接続中...")
        self.count_var.set("サンプル数: 0")
        self._log("計測を開始します。")
        self.controller = MeasurementController(self.msg_queue)
        self.controller.start()

    # ---- 計測終了 -> 保存 -> 終了 ----
    def on_stop(self):
        if not self.measuring or self._finalizing:
            return
        self._finalizing = True
        self.stop_btn.configure(state="disabled")
        self.status_var.set("停止処理中...")
        self._log("計測を停止しています...")

        # 停止要求(即時に戻る)。クリーンアップは GUI を固めないよう別スレッドで。
        self.controller.request_stop()

        def _finalize():
            try:
                self.controller.cleanup()
            finally:
                # メインスレッドで保存ダイアログを出すため通知
                self.msg_queue.put(("ready_to_save", None))

        threading.Thread(target=_finalize, name="Finalizer",
                          daemon=True).start()

    # ---- 保存ダイアログ -> 保存 -> 終了 ----
    def _do_save_and_exit(self):
        n = self.controller.get_sample_count() if self.controller else 0
        self.status_var.set("保存先を選択してください")
        path = filedialog.asksaveasfilename(
            title="計測データの保存先",
            defaultextension=".csv",
            initialfile=OUTPUT_CSV,
            filetypes=[("CSV ファイル", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if path:
            try:
                written = self.controller.save_csv(path)
                self._log("保存しました: {} ({} サンプル)".format(path, written))
                messagebox.showinfo(
                    "保存完了", "{} サンプルを保存しました。\n{}".format(written, path))
            except Exception as e:
                self._log("保存に失敗: {}".format(e))
                messagebox.showerror("保存エラー", "保存に失敗しました:\n{}".format(e))
        else:
            # キャンセル時: データ破棄の確認
            if n > 0 and not messagebox.askyesno(
                    "確認", "保存をキャンセルしました。{} サンプルを破棄して終了しますか?".format(n)):
                # 終了を取りやめ -> 保存ダイアログを再表示できるよう finalizing 解除しない。
                # ここでは再度保存を促す。
                self._do_save_and_exit()
                return
        self.destroy()

    # ---- ウィンドウ × ----
    def on_window_close(self):
        if self._finalizing:
            return
        if self.measuring:
            if not messagebox.askokcancel(
                    "終了確認", "計測中です。停止して終了しますか?"):
                return
            self._finalizing = True
            self.status_var.set("停止処理中...")
            self._log("ウィンドウを閉じています。クリーンアップ中...")

            def _finalize_close():
                try:
                    if self.controller:
                        self.controller.request_stop()
                        self.controller.cleanup()
                finally:
                    self.msg_queue.put(("ready_to_save", None))
            threading.Thread(target=_finalize_close, name="FinalizerClose",
                             daemon=True).start()
        else:
            self.destroy()

    # ---- GUI メッセージのポーリング ----
    def _poll_messages(self):
        try:
            while True:
                kind, value = self.msg_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(value)
                    self._log(value)
                elif kind == "sample":
                    self.count_var.set("サンプル数: {}".format(value))
                elif kind == "error":
                    self._log("[エラー] " + str(value))
                    messagebox.showerror("エラー", str(value))
                    # 致命的エラー: 計測を畳んでボタンを戻す
                    self._recover_after_error()
                elif kind == "finished":
                    self._log("計測ループ終了。")
                elif kind == "ready_to_save":
                    self.status_var.set("停止しました")
                    self.count_var.set("サンプル数: {}".format(
                        self.controller.get_sample_count() if self.controller else 0))
                    self._do_save_and_exit()
                    return  # ウィンドウ破棄後に after を再登録しない
        except queue.Empty:
            pass
        # ウィンドウが破棄されていれば再ポーリングしない
        try:
            if self.winfo_exists():
                self.after(100, self._poll_messages)
        except tk.TclError:
            pass

    def _recover_after_error(self):
        """致命的エラー後、計測状態を解除して再度開始できるようにする。"""
        if self._finalizing:
            return

        def _cl():
            if self.controller:
                self.controller.cleanup()
        threading.Thread(target=_cl, daemon=True).start()
        self.measuring = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("エラーで停止。再開できます")


def main():
    app = App()
    try:
        app.mainloop()
    finally:
        # mainloop を抜けたら(=ウィンドウ破棄後)、念のため最終クリーンアップ。
        if app.controller is not None:
            try:
                app.controller.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    main()
