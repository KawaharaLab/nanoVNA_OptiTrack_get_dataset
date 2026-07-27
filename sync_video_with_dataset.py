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

使用例:
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
        raise SystemExit(
            "エラー: CSV に '{}' 列がありません。sync_optitrack_nanovna.py の新しい版で"
            "取得し直すか、--video-start と併せて meta.json を用意してください。".format(wallclock_col))
    if timestamp_col not in fieldnames:
        raise SystemExit(
            "エラー: '{}' 列も '{}' 列も無いため絶対時刻を復元できません。".format(
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


def extract_frames(video_path, jobs, out_dir, ffmpeg):
    """
    jobs = [(row_index, video_time_sec), ...] の各時刻の 1 フレームを PNG で書き出す。
    書き出したフレーム数を返す。ffmpeg が無ければ 0。
    """
    if not ffmpeg:
        print("[警告] ffmpeg が見つからないためフレーム抽出をスキップします。")
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
            print("[警告] row {} (t={:.3f}s) のフレーム抽出に失敗: {}".format(idx, t, err.strip()))
    return n_ok


def main():
    ap = argparse.ArgumentParser(
        description="データセット CSV とウェブカメラ動画を時刻(WallClock)で同期する後処理ツール。")
    ap.add_argument("--csv", required=True, help="sync_optitrack_nanovna.py が出力した CSV")
    ap.add_argument("--video", required=True, help="Windows 標準カメラで録画した動画ファイル")
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

    if not os.path.isfile(args.csv):
        raise SystemExit("エラー: CSV が見つかりません: {}".format(args.csv))
    if not os.path.isfile(args.video):
        raise SystemExit("エラー: 動画が見つかりません: {}".format(args.video))

    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    ffprobe = args.ffprobe or shutil.which("ffprobe")

    # 1) 動画の録画開始時刻を決める
    video_start, source = determine_video_start(args.video, args.video_start, ffprobe)
    if video_start is None:
        raise SystemExit(
            "エラー: 動画の録画開始時刻を判定できませんでした。\n"
            "  --video-start \"YYYY-MM-DD HH:MM:SS\" で明示指定するか、\n"
            "  Windows カメラの既定ファイル名(WIN_YYYYMMDD_HH_MM_SS_Pro.mp4)のままにしてください。")
    print("動画の録画開始時刻: {}  [判定根拠: {}]".format(
        video_start.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], source))

    # 2) 動画長(範囲外の行を検出するため。ffprobe が無ければ None)
    duration, _ = ffprobe_info(args.video, ffprobe)
    if duration is not None:
        print("動画長: {:.3f} 秒".format(duration))
    else:
        print("動画長: 不明(ffprobe 無し。範囲外チェックは省略します)")

    # 3) CSV を読み、各行の絶対時刻を得る
    meta_start = load_meta_start_wall(args.csv)
    fieldnames, rows, walls = read_wallclocks(
        args.csv, args.wallclock_col, args.timestamp_col, meta_start)

    # 4) 各行の video_time_sec を算出
    out_fields = list(fieldnames) + ["video_time_sec", "in_video"]
    out_path = args.out or (os.path.splitext(args.csv)[0] + "_video_synced.csv")

    n_in = n_before = n_after = n_bad = 0
    jobs = []  # フレーム抽出対象 [(row_index, t)]
    for i, (r, w) in enumerate(zip(rows, walls)):
        if w is None:
            r["video_time_sec"] = ""
            r["in_video"] = "0"
            n_bad += 1
            continue
        t = (w - video_start).total_seconds()
        r["video_time_sec"] = "{:.3f}".format(t)
        in_video = t >= 0 and (duration is None or t <= duration)
        r["in_video"] = "1" if in_video else "0"
        if t < 0:
            n_before += 1
        elif duration is not None and t > duration:
            n_after += 1
        else:
            n_in += 1
        if in_video and args.extract_frames and (i % max(1, args.extract_every) == 0):
            jobs.append((i, t))

    # 5) 同期済み CSV を書き出す
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        writer.writerows(rows)

    print("-" * 60)
    print("同期 CSV を書き出しました: {}".format(out_path))
    print("  行数: {} / 動画範囲内: {} / 録画開始より前: {} / 動画長より後: {} / 時刻不明: {}".format(
        len(rows), n_in, n_before, n_after, n_bad))
    if n_in == 0:
        print("  [警告] 動画範囲内の行がありません。録画開始時刻(--video-start)や"
              "動画ファイルが正しいか確認してください。")

    # 6) 任意: 対応フレームを書き出す
    if args.extract_frames:
        print("-" * 60)
        print("フレーム抽出: {} 行ぶん(出力先: {})".format(len(jobs), args.extract_frames))
        n_ok = extract_frames(args.video, jobs, args.extract_frames, ffmpeg)
        print("  抽出できたフレーム: {} / {}".format(n_ok, len(jobs)))


if __name__ == "__main__":
    main()
