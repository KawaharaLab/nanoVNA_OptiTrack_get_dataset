# -*- coding: utf-8 -*-
"""
データセット(CSV)とウェブカメラ動画の時刻同期(後処理)
================================================================================

sync_optitrack_nanovna.py が出力する CSV には、各サンプルの絶対時刻(WallClock,
ローカル時刻・ミリ秒精度)が入っている。一方、動画は Windows 標準「カメラ」アプリで
別途録画・保存する(同じ PC の時計を共有する)。

本スクリプトは、CSV の WallClock と「動画の録画開始時刻」を突き合わせて、各データ行が
その動画の何秒目(video_time_sec)に当たるかを算出する。動画自体はここでは撮らない
(=時刻のみを共有して同期する)。

同期の考え方:
    video_time_sec = (行の WallClock) - (動画の録画開始時刻)
  → データ行 i は、動画の先頭から video_time_sec[i] 秒の位置に対応する。

動画の録画開始時刻(絶対時刻)の決め方(上から順に採用):
  1) --video-start "YYYY-MM-DD HH:MM:SS[.fff]" で明示指定した場合はそれを使う。
  2) Windows カメラのファイル名 WIN_YYYYMMDD_HH_MM_SS_Pro.mp4 から録画開始時刻を読む
     (ローカル時刻・秒精度。最も確実)。
  3) 動画メタデータの creation_time(ffprobe。多くは UTC なのでローカルへ変換)。
  4) 上記が使えなければファイルの作成時刻(Windows: st_ctime。最後の手段・要注意)。

出力:
  - <csv>_video_synced.csv : 元の全列 + video_time_sec 列(+ 範囲外フラグ)を付けた CSV。
  - --extract-frames DIR   : 各データ行(または --extract-every で間引いた行)に対応する
                             動画フレームを PNG で書き出す(ffmpeg が必要)。

必要なもの:
  - Python 3.8+(標準ライブラリのみで CSV 同期は動作)
  - ffmpeg / ffprobe(任意。動画長の確認・メタデータ読み取り・フレーム抽出に使用。
    PATH に無ければ --ffmpeg / --ffprobe で場所を指定できる)

使い方:
  ■ GUI(ファイルをパスで選ぶ) — 引数なしで起動、または --gui:
      python sync_video_with_dataset.py
    「参照」ボタンでデータセット CSV と動画ファイルのパスを選び、[同期実行]。

  ■ CLI(コマンドライン):
    # ファイル名から録画開始時刻を自動判定して同期 CSV を作る
    python sync_video_with_dataset.py --csv sync_dataset.csv --video WIN_20260727_15_30_45_Pro.mp4

    # 録画開始時刻を明示し、10 行ごとに対応フレームも書き出す
    python sync_video_with_dataset.py --csv data.csv --video clip.mp4 \
        --video-start "2026-07-27 15:30:45.000" --extract-frames frames --extract-every 10
"""

import os
import re
import csv
import sys
import json
import shutil
import argparse
import subprocess
import datetime


# CSV の絶対時刻列(sync_optitrack_nanovna.py が書き出す形式)
DEFAULT_WALLCLOCK_COL = "WallClock"
DEFAULT_TIMESTAMP_COL = "Timestamp"


class SyncError(Exception):
    """同期処理で想定内の失敗(入力不備・時刻判定不能など)を表す。
    CLI は標準エラーへ、GUI はダイアログへ、このメッセージをそのまま出す。"""

# WallClock / --video-start の解釈に使う日時フォーマット候補(上から順に試す)
_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
]

# Windows 標準カメラの既定ファイル名: WIN_YYYYMMDD_HH_MM_SS[_Pro].mp4
_WIN_CAM_RE = re.compile(r"WIN_(\d{4})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})")


def parse_local_datetime(text):
    """"YYYY-MM-DD HH:MM:SS[.fff]"(または T 区切り)をローカル naive datetime に解釈する。"""
    text = text.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError("日時として解釈できません: {!r}".format(text))


def video_start_from_filename(video_path):
    """Windows カメラのファイル名から録画開始時刻(ローカル naive datetime)を読む。無ければ None。"""
    name = os.path.basename(video_path)
    m = _WIN_CAM_RE.search(name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
    try:
        return datetime.datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


def _run(cmd):
    """外部コマンドを実行し (returncode, stdout, stderr) を返す。失敗しても例外は投げない。"""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    except Exception as e:
        return 1, "", str(e)


def ffprobe_info(video_path, ffprobe):
    """
    ffprobe で動画の (duration_sec, creation_local_datetime) を取得する。
    取得できない項目は None。ffprobe が無ければ (None, None)。
    """
    if not ffprobe:
        return None, None
    rc, out, _ = _run([ffprobe, "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", video_path])
    if rc != 0 or not out:
        return None, None
    try:
        info = json.loads(out)
    except ValueError:
        return None, None

    fmt = info.get("format", {}) or {}
    duration = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        pass

    # creation_time は多くの場合 UTC("...Z")。ローカル naive へ変換する。
    creation_local = None
    tags = fmt.get("tags", {}) or {}
    ctime = tags.get("creation_time")
    if not ctime:
        for st in info.get("streams", []) or []:
            ctime = (st.get("tags", {}) or {}).get("creation_time")
            if ctime:
                break
    if ctime:
        creation_local = _parse_iso_utc_to_local(ctime)
    return duration, creation_local


def _parse_iso_utc_to_local(text):
    """ISO8601(末尾 Z=UTC を含みうる)をローカル naive datetime に変換する。失敗時 None。"""
    text = text.strip()
    try:
        if text.endswith("Z"):
            dt = datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
            dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone().replace(tzinfo=None)
        # タイムゾーン情報が無ければローカルとみなす
        return parse_local_datetime(text)
    except (ValueError, TypeError):
        return None


def file_creation_local(video_path):
    """ファイルの作成時刻(Windows: st_ctime)をローカル naive datetime で返す。最後の手段。"""
    try:
        st = os.stat(video_path)
    except OSError:
        return None
    # Windows では st_ctime が作成時刻。他 OS では変化時刻になる点に注意。
    return datetime.datetime.fromtimestamp(st.st_ctime)


def determine_video_start(video_path, explicit, ffprobe):
    """
    動画の録画開始時刻(ローカル naive datetime)と、その判定根拠(説明文字列)を返す。
    優先順: 明示指定 > ファイル名 > メタデータ creation_time > ファイル作成時刻。
    """
    if explicit:
        return parse_local_datetime(explicit), "明示指定(--video-start)"

    from_name = video_start_from_filename(video_path)
    if from_name is not None:
        return from_name, "ファイル名(WIN_YYYYMMDD_HH_MM_SS)"

    _, creation = ffprobe_info(video_path, ffprobe)
    if creation is not None:
        return creation, "動画メタデータ creation_time(ffprobe)"

    fc = file_creation_local(video_path)
    if fc is not None:
        return fc, "ファイル作成時刻(st_ctime。目安・要確認)"

    return None, None


def read_wallclocks(csv_path, wallclock_col, timestamp_col, meta_start_wall):
    """
    CSV を読み、(fieldnames, rows, wall_datetimes) を返す。
    - 通常は WallClock 列を絶対時刻として解釈する。
    - WallClock 列が無い古い CSV では、meta.json の first_sample_wall(または計測開始時刻)を
      基準に Timestamp(経過秒)を足して絶対時刻を復元する(meta_start_wall 指定時のみ)。
    """
    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    has_wall = wallclock_col in fieldnames
    walls = []
    if has_wall:
        for r in rows:
            raw = (r.get(wallclock_col) or "").strip()
            walls.append(parse_local_datetime(raw) if raw else None)
        return fieldnames, rows, walls

    # フォールバック: WallClock が無い → meta の基準時刻 + Timestamp(経過秒)
    if meta_start_wall is None:
        raise SyncError(
            "CSV に '{}' 列がありません。sync_optitrack_nanovna.py の新しい版(カメラ同期 ON)で"
            "取得し直すか、同じ場所に meta.json を用意してください。".format(wallclock_col))
    if timestamp_col not in fieldnames:
        raise SyncError(
            "'{}' 列も '{}' 列も無いため絶対時刻を復元できません。".format(
                wallclock_col, timestamp_col))
    base = meta_start_wall
    for r in rows:
        try:
            elapsed = float(r.get(timestamp_col) or "")
            walls.append(base + datetime.timedelta(seconds=elapsed))
        except ValueError:
            walls.append(None)
    return fieldnames, rows, walls


def load_meta_start_wall(csv_path):
    """<csv>.meta.json があれば first_sample_wall(無ければ meas_start_wall)を返す。無ければ None。"""
    meta_path = os.path.splitext(csv_path)[0] + ".meta.json"
    if not os.path.isfile(meta_path):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    for key in ("first_sample_wall", "meas_start_wall"):
        val = meta.get(key)
        if val:
            try:
                return parse_local_datetime(val)
            except ValueError:
                pass
    return None


def extract_frames(video_path, jobs, out_dir, ffmpeg, log=print):
    """
    jobs = [(row_index, video_time_sec), ...] の各時刻の 1 フレームを PNG で書き出す。
    書き出したフレーム数を返す。ffmpeg が無ければ 0。
    """
    if not ffmpeg:
        log("[警告] ffmpeg が見つからないためフレーム抽出をスキップします。")
        return 0
    os.makedirs(out_dir, exist_ok=True)
    n_ok = 0
    for idx, t in jobs:
        out_png = os.path.join(out_dir, "row{:06d}_{:09.3f}s.png".format(idx, t))
        # -ss を -i の前に置いて高速シーク。1 フレームだけ取り出す。
        rc, _, err = _run([ffmpeg, "-y", "-loglevel", "error",
                           "-ss", "{:.3f}".format(t), "-i", video_path,
                           "-frames:v", "1", out_png])
        if rc == 0 and os.path.isfile(out_png):
            n_ok += 1
        else:
            log("[警告] row {} (t={:.3f}s) のフレーム抽出に失敗: {}".format(idx, t, err.strip()))
    return n_ok


def run_sync(csv_path, video_path, out_path=None, video_start=None,
             wallclock_col=DEFAULT_WALLCLOCK_COL, timestamp_col=DEFAULT_TIMESTAMP_COL,
             extract_frames_dir=None, extract_every=1,
             ffmpeg=None, ffprobe=None, log=print):
    """
    CSV(WallClock)と動画(録画開始時刻)を突き合わせて同期 CSV を書き出す共通処理。
    CLI と GUI の双方から呼ぶ。進捗は log(str) で通知する。
    想定内の失敗は SyncError を送出する。戻り値は (out_path, stats dict)。

    引数はコマンドライン/GUI のどちらからでも同じ意味:
      csv_path, video_path : 入力パス(必須)
      out_path             : 出力 CSV(None で <csv>_video_synced.csv)
      video_start          : 録画開始時刻の明示指定(None で自動判定)
      extract_frames_dir   : フレーム PNG の出力先(None で抽出しない。ffmpeg 必須)
      extract_every        : フレーム抽出を N 行ごとに間引く
      ffmpeg, ffprobe      : 実行パス(None で PATH から検索)
    """
    if not csv_path or not os.path.isfile(csv_path):
        raise SyncError("データセット CSV が見つかりません: {}".format(csv_path or "(未指定)"))
    if not video_path or not os.path.isfile(video_path):
        raise SyncError("動画ファイルが見つかりません: {}".format(video_path or "(未指定)"))

    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    ffprobe = ffprobe or shutil.which("ffprobe")

    # 1) 動画の録画開始時刻を決める
    vstart, source = determine_video_start(video_path, video_start or None, ffprobe)
    if vstart is None:
        raise SyncError(
            "動画の録画開始時刻を判定できませんでした。\n"
            "「録画開始時刻」を YYYY-MM-DD HH:MM:SS で明示指定するか、\n"
            "Windows カメラの既定ファイル名(WIN_YYYYMMDD_HH_MM_SS_Pro.mp4)のままにしてください。")
    log("動画の録画開始時刻: {}  [判定根拠: {}]".format(
        vstart.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], source))

    # 2) 動画長(範囲外の行を検出するため。ffprobe が無ければ None)
    duration, _ = ffprobe_info(video_path, ffprobe)
    if duration is not None:
        log("動画長: {:.3f} 秒".format(duration))
    else:
        log("動画長: 不明(ffprobe 無し。範囲外チェックは省略します)")

    # 3) CSV を読み、各行の絶対時刻を得る
    meta_start = load_meta_start_wall(csv_path)
    fieldnames, rows, walls = read_wallclocks(
        csv_path, wallclock_col, timestamp_col, meta_start)

    # 4) 各行の video_time_sec を算出
    out_fields = list(fieldnames) + ["video_time_sec", "in_video"]
    out_path = out_path or (os.path.splitext(csv_path)[0] + "_video_synced.csv")

    n_in = n_before = n_after = n_bad = 0
    jobs = []  # フレーム抽出対象 [(row_index, t)]
    for i, (r, w) in enumerate(zip(rows, walls)):
        if w is None:
            r["video_time_sec"] = ""
            r["in_video"] = "0"
            n_bad += 1
            continue
        t = (w - vstart).total_seconds()
        r["video_time_sec"] = "{:.3f}".format(t)
        in_video = t >= 0 and (duration is None or t <= duration)
        r["in_video"] = "1" if in_video else "0"
        if t < 0:
            n_before += 1
        elif duration is not None and t > duration:
            n_after += 1
        else:
            n_in += 1
        if in_video and extract_frames_dir and (i % max(1, extract_every) == 0):
            jobs.append((i, t))

    # 5) 同期済み CSV を書き出す
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(rows)

    log("-" * 56)
    log("同期 CSV を書き出しました: {}".format(out_path))
    log("  行数: {} / 動画範囲内: {} / 録画開始より前: {} / 動画長より後: {} / 時刻不明: {}".format(
        len(rows), n_in, n_before, n_after, n_bad))
    if n_in == 0:
        log("  [警告] 動画範囲内の行がありません。録画開始時刻や動画ファイルが正しいか確認してください。")

    stats = {"rows": len(rows), "in_video": n_in, "before": n_before,
             "after": n_after, "bad": n_bad, "frames_ok": 0, "frames_total": 0}

    # 6) 任意: 対応フレームを書き出す
    if extract_frames_dir:
        log("-" * 56)
        log("フレーム抽出: {} 行ぶん(出力先: {})".format(len(jobs), extract_frames_dir))
        n_ok = extract_frames(video_path, jobs, extract_frames_dir, ffmpeg, log=log)
        log("  抽出できたフレーム: {} / {}".format(n_ok, len(jobs)))
        stats["frames_ok"] = n_ok
        stats["frames_total"] = len(jobs)

    return out_path, stats


# =============================================================================
# GUI(ファイルをパスで選んで同期する)
# =============================================================================

def launch_gui(prefill=None, run=True):
    """
    tkinter でファイル選択 GUI を開く。prefill は argparse の Namespace(任意)で、
    起動時に各欄へ初期値を入れる。tkinter はここでのみ import する
    (CLI しか使わない環境で tkinter 依存を持ち込まないため)。
    run=False のときは mainloop を回さずに App インスタンスを返す(テスト/埋め込み用)。
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import queue
    import threading

    VIDEO_TYPES = [("動画ファイル", "*.mp4 *.mov *.avi *.mkv *.wmv *.m4v"),
                   ("すべてのファイル", "*.*")]
    CSV_TYPES = [("CSV ファイル", "*.csv"), ("すべてのファイル", "*.*")]

    class SyncApp(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("動画 × データセット 時刻同期")
            self.geometry("840x580")
            self.minsize(700, 480)
            self._q = queue.Queue()
            self._running = False
            self._build()
            if prefill is not None:
                self._apply_prefill(prefill)
            self.after(100, self._poll)

        # ---- 1 行ぶんの「ラベル + パス入力 + 参照ボタン」を作る ----
        def _path_row(self, parent, label, browse_cmd, width=44):
            # grid で「ラベル(固定) | 入力欄(伸縮) | 参照(固定)」の 3 列に分ける。
            # 入力欄だけ weight を付けて伸ばし、参照ボタン列を必ず確保する。
            row = ttk.Frame(parent)
            row.pack(fill="x", padx=8, pady=4)
            row.columnconfigure(1, weight=1)
            ttk.Label(row, text=label, width=16).grid(row=0, column=0, sticky="w")
            var = tk.StringVar()
            ent = ttk.Entry(row, textvariable=var, width=width)
            ent.grid(row=0, column=1, sticky="ew", padx=(0, 6))
            ttk.Button(row, text="参照…", command=browse_cmd).grid(row=0, column=2)
            return var

        def _build(self):
            ttk.Label(
                self,
                text=("データセット CSV と、Windows 標準カメラで撮った動画のパスを選び、[同期実行] を押します。\n"
                      "各行に video_time_sec(動画の何秒目か)を付けた同期 CSV を書き出します。"),
                justify="left").pack(anchor="w", padx=8, pady=(8, 2))

            self.csv_var = self._path_row(self, "データセット CSV:", self._browse_csv)
            self.video_var = self._path_row(self, "動画ファイル:", self._browse_video)
            self.out_var = self._path_row(self, "出力 CSV(任意):", self._browse_out)
            ttk.Label(self, text="  ※ 出力 CSV を空欄にすると <データセット名>_video_synced.csv になります。",
                      foreground="#666").pack(anchor="w", padx=8)

            # 録画開始時刻(任意)
            vs = ttk.Frame(self)
            vs.pack(fill="x", padx=8, pady=4)
            ttk.Label(vs, text="録画開始時刻(任意):", width=16).pack(side="left")
            self.vstart_var = tk.StringVar()
            ttk.Entry(vs, textvariable=self.vstart_var, width=26).pack(side="left", padx=(0, 6))
            ttk.Label(vs, text='空欄で自動判定(ファイル名 WIN_… 等)。例: 2026-07-27 15:30:45',
                      foreground="#666").pack(side="left")

            # フレーム抽出(任意)
            fr = ttk.LabelFrame(self, text="対応フレームの書き出し(任意・ffmpeg が必要)")
            fr.pack(fill="x", padx=8, pady=6)
            self.frames_on = tk.BooleanVar(value=False)
            ttk.Checkbutton(fr, text="対応する動画フレームを PNG で書き出す",
                            variable=self.frames_on, command=self._toggle_frames).pack(anchor="w", padx=6, pady=2)
            fr2 = ttk.Frame(fr)
            fr2.pack(fill="x", padx=6, pady=2)
            # grid: ラベル | 入力欄(伸縮) | 参照 | N 行ごと: | スピン
            fr2.columnconfigure(1, weight=1)
            ttk.Label(fr2, text="出力先フォルダ:", width=14).grid(row=0, column=0, sticky="w")
            self.framesdir_var = tk.StringVar()
            self.framesdir_ent = ttk.Entry(fr2, textvariable=self.framesdir_var, width=40)
            self.framesdir_ent.grid(row=0, column=1, sticky="ew", padx=(0, 6))
            self.framesdir_btn = ttk.Button(fr2, text="参照…", command=self._browse_framesdir)
            self.framesdir_btn.grid(row=0, column=2)
            ttk.Label(fr2, text="N 行ごと:").grid(row=0, column=3, padx=(8, 2))
            self.every_var = tk.StringVar(value="1")
            self.every_spin = ttk.Spinbox(fr2, textvariable=self.every_var, from_=1, to=100000, width=7)
            self.every_spin.grid(row=0, column=4)
            self._toggle_frames()

            # ツールの状態(ffmpeg/ffprobe)
            ff = shutil.which("ffmpeg")
            fp = shutil.which("ffprobe")
            tool = "ffmpeg: {} / ffprobe: {}".format(
                ff or "見つかりません", fp or "見つかりません")
            ttk.Label(self, text=tool, foreground="#06c").pack(anchor="w", padx=8, pady=(0, 2))

            # 実行ボタン
            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=8, pady=4)
            self.run_btn = ttk.Button(bar, text="同期実行", command=self._on_run)
            self.run_btn.pack(side="left")

            # ログ
            logf = ttk.Frame(self)
            logf.pack(fill="both", expand=True, padx=8, pady=(2, 8))
            self.log = tk.Text(logf, height=10, state="disabled", wrap="word")
            sb = ttk.Scrollbar(logf, command=self.log.yview)
            self.log.configure(yscrollcommand=sb.set)
            self.log.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")

        def _apply_prefill(self, ns):
            if getattr(ns, "csv", None):
                self.csv_var.set(ns.csv)
            if getattr(ns, "video", None):
                self.video_var.set(ns.video)
            if getattr(ns, "out", None):
                self.out_var.set(ns.out)
            if getattr(ns, "video_start", None):
                self.vstart_var.set(ns.video_start)
            if getattr(ns, "extract_frames", None):
                self.frames_on.set(True)
                self.framesdir_var.set(ns.extract_frames)
                self._toggle_frames()
            if getattr(ns, "extract_every", None):
                self.every_var.set(str(ns.extract_every))

        def _toggle_frames(self):
            state = "normal" if self.frames_on.get() else "disabled"
            self.framesdir_ent.configure(state=state)
            self.framesdir_btn.configure(state=state)
            self.every_spin.configure(state=state)

        # ---- 参照ダイアログ ----
        def _browse_csv(self):
            p = filedialog.askopenfilename(title="データセット CSV を選択", filetypes=CSV_TYPES)
            if p:
                self.csv_var.set(p)

        def _browse_video(self):
            p = filedialog.askopenfilename(title="動画ファイルを選択", filetypes=VIDEO_TYPES)
            if p:
                self.video_var.set(p)

        def _browse_out(self):
            init = ""
            if self.csv_var.get():
                init = os.path.splitext(os.path.basename(self.csv_var.get()))[0] + "_video_synced.csv"
            p = filedialog.asksaveasfilename(title="出力 CSV の保存先",
                                             defaultextension=".csv",
                                             initialfile=init, filetypes=CSV_TYPES)
            if p:
                self.out_var.set(p)

        def _browse_framesdir(self):
            p = filedialog.askdirectory(title="フレームの出力先フォルダを選択")
            if p:
                self.framesdir_var.set(p)

        # ---- ログ(メインスレッドのみ) ----
        def _log(self, text):
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")

        # ---- 実行(ワーカースレッドで run_sync を回す) ----
        def _on_run(self):
            if self._running:
                return
            csv_path = self.csv_var.get().strip()
            video_path = self.video_var.get().strip()
            if not csv_path or not video_path:
                messagebox.showerror("入力不足", "データセット CSV と動画ファイルのパスを両方指定してください。")
                return
            try:
                every = max(1, int(float(self.every_var.get())))
            except ValueError:
                every = 1
            out_path = self.out_var.get().strip() or None
            vstart = self.vstart_var.get().strip() or None
            frames_dir = (self.framesdir_var.get().strip() or None) if self.frames_on.get() else None
            if self.frames_on.get() and not frames_dir:
                messagebox.showerror("入力不足", "フレーム書き出しの出力先フォルダを指定してください。")
                return

            self._running = True
            self.run_btn.configure(state="disabled")
            self._log("=" * 56)
            self._log("同期を開始します…")

            def worker():
                try:
                    out, stats = run_sync(
                        csv_path, video_path, out_path, vstart,
                        DEFAULT_WALLCLOCK_COL, DEFAULT_TIMESTAMP_COL,
                        frames_dir, every, None, None,
                        log=lambda m: self._q.put(("log", m)))
                    self._q.put(("done", (out, stats)))
                except SyncError as e:
                    self._q.put(("error", str(e)))
                except Exception as e:  # 想定外は種別付きで表示
                    self._q.put(("error", "予期せぬエラー: {!r}".format(e)))

            threading.Thread(target=worker, name="SyncWorker", daemon=True).start()

        # ---- ワーカーからのメッセージを取り込む ----
        def _poll(self):
            try:
                while True:
                    kind, val = self._q.get_nowait()
                    if kind == "log":
                        self._log(val)
                    elif kind == "done":
                        out, stats = val
                        self._running = False
                        self.run_btn.configure(state="normal")
                        self._log("完了しました。")
                        messagebox.showinfo(
                            "同期完了",
                            "同期 CSV を書き出しました:\n{}\n\n"
                            "行数 {} / 動画範囲内 {}".format(out, stats["rows"], stats["in_video"]))
                    elif kind == "error":
                        self._running = False
                        self.run_btn.configure(state="normal")
                        self._log("[エラー] " + val)
                        messagebox.showerror("エラー", val)
            except queue.Empty:
                pass
            self.after(100, self._poll)

    app = SyncApp()
    if not run:
        return app
    app.mainloop()


def main():
    ap = argparse.ArgumentParser(
        description="データセット CSV とウェブカメラ動画を時刻(WallClock)で同期する後処理ツール。"
                    "引数なしで起動、または --gui で GUI(ファイル選択)を開く。")
    ap.add_argument("--gui", action="store_true",
                    help="GUI(ファイル選択)で指定して実行する(引数なしでも GUI が開く)")
    ap.add_argument("--csv", default=None, help="sync_optitrack_nanovna.py が出力した CSV")
    ap.add_argument("--video", default=None, help="Windows 標準カメラで録画した動画ファイル")
    ap.add_argument("--out", default=None,
                    help="出力 CSV(既定: <csv>_video_synced.csv)")
    ap.add_argument("--video-start", default=None,
                    help='動画の録画開始時刻を明示指定 "YYYY-MM-DD HH:MM:SS[.fff]"')
    ap.add_argument("--wallclock-col", default=DEFAULT_WALLCLOCK_COL,
                    help="絶対時刻の列名(既定: WallClock)")
    ap.add_argument("--timestamp-col", default=DEFAULT_TIMESTAMP_COL,
                    help="経過秒の列名(WallClock 欠落時のフォールバック用。既定: Timestamp)")
    ap.add_argument("--extract-frames", default=None, metavar="DIR",
                    help="対応する動画フレームを PNG で書き出す出力先ディレクトリ")
    ap.add_argument("--extract-every", type=int, default=1, metavar="N",
                    help="フレーム抽出を N 行ごとに間引く(既定: 1=全行)")
    ap.add_argument("--ffmpeg", default=None, help="ffmpeg の実行パス(既定: PATH から検索)")
    ap.add_argument("--ffprobe", default=None, help="ffprobe の実行パス(既定: PATH から検索)")
    args = ap.parse_args()

    # GUI モード: --gui 指定時、または CSV/動画のどちらも未指定のとき
    if args.gui or (not args.csv and not args.video):
        launch_gui(prefill=args)
        return

    # CLI モード: 両方のパスが必要
    if not args.csv or not args.video:
        ap.error("--csv と --video の両方を指定してください(GUI を使うなら --gui または引数なし)。")

    try:
        run_sync(args.csv, args.video, args.out, args.video_start,
                 args.wallclock_col, args.timestamp_col,
                 args.extract_frames, args.extract_every,
                 args.ffmpeg, args.ffprobe, log=print)
    except SyncError as e:
        raise SystemExit("エラー: {}".format(e))


if __name__ == "__main__":
    main()
