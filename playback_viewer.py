# -*- coding: utf-8 -*-
"""
取得済みデータセット + 動画 再生ビューア(ポストホック)
================================================================================

sync_optitrack_nanovna.py で取得した CSV(7 マーカー座標 + leftbody/rightbody の S11)と、
連動録画した動画(WIN_YYYYMMDD_HH_MM_SS_Pro.mp4)を、時刻同期して再生する GUI。
view_marker_impedances/viewer_3marker.py を土台に、再生エンジンを作り替えている。

上半身・下半身のどちらで計測した CSV かは位置列の名前から自動判定する(BODY_LAYOUTS):
  上半身: chest / R,L_upperarm / R,L_joint / R,L_forearm → 角度は左右の肘
  下半身: waist / R,L_thigh    / R,L_knee  / R,L_shin    → 角度は左右の膝
判定に失敗するときだけ --body upper / --body lower で明示指定する。

表示内容:
  - 3D マーカー散布 + ボーン + 左右の関節角度(肘 or 膝。数値。3D ビュー内に表示)
  - 左右 body のスミスチャート(見出しは Impedance。点の座標は Γ = S11 だが、
    スミスチャートはそれをインピーダンス Z = R + jX(Z0 = 50Ω 基準)として読む図)
  - 動画パネル(--video 指定時)

単一周波数(1 点)モードで測った CSV にも対応する。周波数が 1 つしか無いときは
「掃引の形」を描けないので、次のように表示を切り替える:
  - スミスチャート … 左右で 1 枚にまとめ、その上に左右の Γ(インピーダンス)を重ねて
    描く(掃引モードのように軌跡が重なって判別できなくなる心配が無いため)。点は
    現在フレームの ★ 1 つずつで、色で左右を区別する。Z の数値はチャート下に 2 行で
    並べ、行頭の ★(マーカーと同色)を凡例代わりにする。時間変化を軌跡として見たい
    ときは --trail N(直近 N 行を線でつなぐ)を付ける
  - 左右インピーダンス相対誤差 … 横軸を周波数ではなく時間にした全区間のグラフ
  - 周波数スライダー … 出さない(読み取り対象はその 1 点に固定)
列名は "leftbody_S11_Real_13.56" のように MHz サフィックス付きが現行形式だが、
サフィックスの無い旧形式("leftbody_S11_Real")も読める(周波数は --single-freq で指定)。
起動時に、検出した S11 列の周波数(何点あるか)を標準出力へ出す。1 点のつもりなのに
スミスチャートに多数の点が並ぶときは、まずこのログで CSV 側の点数を確認すること。

★同期の考え方(重要)★
  マーカー座標とスミスチャートは「同じ CSV 行」から描くので常に同期している。
  動画は連続的に(シークせずに)デコードして再生し、その時刻に対応する CSV 行を
  video_time_sec から求めてマーカー/スミスを更新する。
    - 動画パネル … 経過した実時間に対応するフレームを表示。描画が間に合わないときは
      間のフレームを cap.grab() で読み飛ばして実時間再生を保つ(シークはしない)。
    - マーカー/スミス/関節角度 … 対応する CSV 行が変わったときに更新。ただし更新レートは
      PANEL_UPDATE_MAX_HZ で頭打ちにする。
  → 「マーカーとスミスは同期・動画はそれに同期しつつ滑らか」を実現する。

★描画の高速化(重要)★
  単一周波数(1 点)モードでは CSV が動画より速く進む(例 54 Hz > 24 fps)ため、
  行が変わるたびに図全体を再描画していると 1 フレームあたり数百 ms かかり、再生が
  まったく追いつかない(映像がとびとびになる)。本ビューアは次のように描画する:
    - 動くアーティストは animated=True にし、静的な背景(3D の箱・スミス格子・
      時系列グラフ・目盛)はキャッシュして blit で合成する。
    - 画面へ転送するのは変化する領域だけ(_blit_regions)。
    - 動画フレームは表示サイズまで OpenCV で縮小してから interpolation='nearest' で描く
      (imshow 既定の再サンプルは 1 フレームあたり ~15 ms かかる)。縮小先は元映像の
      縦横比を保つ(パネルの箱にそのまま合わせると映像が引き伸ばされる)。
    - ウィジェット(スライダー・フレーム番号)の更新でも図全体の再描画が走らないよう、
      set_val() ではなく表示アーティストを直接書き換える。

CSV に video_time_sec 列(sync_video_with_dataset.py が付与)があればそれを使う。
無い場合でも、CSV に WallClock 列があり動画ファイル名が WIN_YYYYMMDD_HH_MM_SS(_Pro).mp4 なら、
録画開始時刻をファイル名から読んで video_time_sec = WallClock - 録画開始 を内部計算する
(--video-start "YYYY-MM-DD HH:MM:SS[.fff]" で明示指定も可)。

使い方:
  # 同期済み CSV(video_time_sec 付き)+ 動画
  python playback_viewer.py sync_dataset_video_synced.csv --video WIN_20260727_15_30_45_Pro.mp4

  # 生の CSV(WallClock 付き)+ 動画(video_time_sec はファイル名から内部計算)
  python playback_viewer.py sync_dataset.csv --video WIN_20260727_15_30_45_Pro.mp4

  # 動画なし(マーカー/スミスのみ再生)
  python playback_viewer.py sync_dataset.csv

必要環境: Python 3.8+, pandas, numpy, matplotlib(, 動画表示に opencv-python)
"""

import re
import sys
import json
import time
import argparse
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import Button, Slider, TextBox
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import Bbox, TransformedBbox
from matplotlib.patches import Rectangle
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 (projection='3d' 登録用)


# ------------------------------------------------------------------ #
# 引数
# ------------------------------------------------------------------ #
_default_csv = Path(__file__).parent / 'sync_dataset.csv'
parser = argparse.ArgumentParser(
    description='取得済みデータセット + 動画の同期再生ビューア')
parser.add_argument('csv', nargs='?', default=str(_default_csv),
                    help='CSV ファイルパス(絶対/相対)')
parser.add_argument('--video', default=None,
                    help='同期再生する動画ファイル(WIN_YYYYMMDD_HH_MM_SS_Pro.mp4 等)')
parser.add_argument('--video-start', default=None,
                    help='動画の録画開始時刻 "YYYY-MM-DD HH:MM:SS[.fff]"'
                         '(video_time_sec 列が無く WallClock から計算する場合の明示指定)')
parser.add_argument('--speed', type=float, default=1.0,
                    help='動画なし再生時の速度倍率(既定 1.0)')
parser.add_argument('--single-freq', type=float, default=None,
                    help='単一周波数(1点)モードで測った古い CSV(列名に MHz が付かない '
                         '"leftbody_S11_Real" 形式)を開くときの周波数[MHz]。'
                         '表示ラベルにのみ使う(例: --single-freq 13.56)')
parser.add_argument('--trail', type=int, default=0, metavar='N',
                    help='単一周波数(1点)モードのスミスチャートに、その周波数の Γ が'
                         '時間とともに動いた軌跡を直近 N 行ぶん描く(既定 0 = 描かない'
                         '=現在フレームの 1 点だけを表示)。例: --trail 300')
parser.add_argument('--body', choices=['auto', 'upper', 'lower'], default='auto',
                    help='計測部位(上半身/下半身)。既定 auto は CSV の位置列名から自動判定する'
                         '(upper: chest/R_upperarm/... , lower: waist/R_thigh/...)。'
                         '自動判定に失敗するときだけ明示指定する')
args = parser.parse_args()

CSV_PATH = Path(args.csv)
if not CSV_PATH.exists():
    sys.exit(f'[Error] CSV not found: {CSV_PATH}')

# ------------------------------------------------------------------ #
# ヘッダーを読み、計測部位(上半身/下半身)・S11(leftbody/rightbody)列と Z 列・
# 動画同期列を検出
# ------------------------------------------------------------------ #
_header_cols = pd.read_csv(CSV_PATH, nrows=0).columns.tolist()

# ------------------------------------------------------------------ #
# 計測部位(上半身 / 下半身)ごとの表示レイアウト
#   sync_optitrack_nanovna.py の MARKER_GROUPS と同じ 7 点構成に対応する。
#   どちらで測った CSV かは位置列の名前(chest_X があるか waist_X があるか)で判る
#   ため、既定では列名から自動判定する(--body で明示指定も可)。
#   order  : 3D ビューに描くマーカー(凡例の並び)
#   bones  : つなぐ線 (a, b, 色)
#   angles : 3 点のなす角として表示する関節 (ラベル, (端, 関節, 端))
# ------------------------------------------------------------------ #
BODY_LAYOUTS = {
    'upper': dict(
        label='Upper Body',
        order=['R_upperarm', 'R_joint', 'R_forearm', 'chest',
               'L_upperarm', 'L_joint', 'L_forearm'],
        bones=[
            ('chest',      'R_upperarm', 'gray'),
            ('R_upperarm', 'R_joint',    'red'),
            ('R_joint',    'R_forearm',  'red'),
            ('chest',      'L_upperarm', 'gray'),
            ('L_upperarm', 'L_joint',    'blue'),
            ('L_joint',    'L_forearm',  'blue'),
        ],
        angles=[
            ('R Elbow', ('R_upperarm', 'R_joint', 'R_forearm')),
            ('L Elbow', ('L_upperarm', 'L_joint', 'L_forearm')),
        ],
    ),
    'lower': dict(
        label='Lower Body',
        order=['R_thigh', 'R_knee', 'R_shin', 'waist',
               'L_thigh', 'L_knee', 'L_shin'],
        bones=[
            ('waist',   'R_thigh', 'gray'),
            ('R_thigh', 'R_knee',  'red'),
            ('R_knee',  'R_shin',  'red'),
            ('waist',   'L_thigh', 'gray'),
            ('L_thigh', 'L_knee',  'blue'),
            ('L_knee',  'L_shin',  'blue'),
        ],
        angles=[
            ('R Knee', ('R_thigh', 'R_knee', 'R_shin')),
            ('L Knee', ('L_thigh', 'L_knee', 'L_shin')),
        ],
    ),
}


def _missing_marker_cols(part, cols):
    """その部位のマーカーのうち、CSV に X/Y/Z 列が揃っていないものを返す。"""
    have = set(cols)
    return [nm for nm in BODY_LAYOUTS[part]['order']
            if not all(f'{nm}_{ax}' in have for ax in ('X', 'Y', 'Z'))]


def _detect_body_part(cols):
    """
    CSV の位置列名から計測部位を判定する。
    上半身(chest/R_upperarm/...)と下半身(waist/R_thigh/...)は列名が重ならないため、
    7 点すべての X/Y/Z が揃っている方を採用する。
    """
    complete = [p for p in BODY_LAYOUTS if not _missing_marker_cols(p, cols)]
    if len(complete) == 1:
        return complete[0]
    if len(complete) > 1:      # 両方揃う CSV は想定外だが、その場合は上半身を優先
        return 'upper'
    return None


if args.body == 'auto':
    BODY_PART = _detect_body_part(_header_cols)
    if BODY_PART is None:
        detail = '; '.join(
            '{}: 不足 {}'.format(BODY_LAYOUTS[p]['label'],
                                 ', '.join(_missing_marker_cols(p, _header_cols)))
            for p in BODY_LAYOUTS)
        sys.exit('[Error] CSV の位置列から計測部位を判定できませんでした。'
                 '--body upper / --body lower で明示指定してください。\n'
                 f'        ({detail})')
else:
    BODY_PART = args.body
    missing = _missing_marker_cols(BODY_PART, _header_cols)
    if missing:
        sys.exit(f'[Error] --body {BODY_PART} を指定しましたが、CSV に次のマーカー列が'
                 f'ありません: {", ".join(missing)}')

LAYOUT = BODY_LAYOUTS[BODY_PART]
# (表示名, CSV の列名プレフィックス)。本ビューアでは両者は同じ。
MARKERS = [(nm, nm) for nm in LAYOUT['order']]
print(f'[Info] 計測部位: {LAYOUT["label"]} ({BODY_PART}) '
      f'/ マーカー: {", ".join(LAYOUT["order"])}')

# 列名の MHz サフィックスは「無い」場合もある: 単一周波数(1点)モードで測った古い CSV は
# "leftbody_S11_Real" のようにサフィックスが付かない(現在の計測スクリプトは 1 点でも
# "leftbody_S11_Real_13.56" と付ける)。どちらの形式でも読めるようにする。
# サフィックスが無い列の周波数は CSV から判らないので、--single-freq があればそれを、
# 無ければ「不明」を表す _FREQ_UNKNOWN を周波数キーに使う(表示ラベル用途のみ)。
_FREQ_UNKNOWN = float('nan')
_UNLABELED_FREQ = float(args.single_freq) if args.single_freq is not None else _FREQ_UNKNOWN
# NaN は辞書キーにできない(NaN != NaN)ので、内部のキーには実数の番兵を使う。
_UNLABELED_KEY = -1.0

_s11_re = re.compile(r'^(leftbody|rightbody)_S11_(Real|Imag)(?:_([0-9.]+))?$')
_s11_map = {}
for col in _header_cols:
    m = _s11_re.match(col)
    if not m:
        continue
    side, comp, freq_str = m.groups()
    key = float(freq_str) if freq_str else _UNLABELED_KEY
    _s11_map.setdefault(side, {}).setdefault(key, {})[comp] = col

SIDES = [s for s in ('leftbody', 'rightbody') if s in _s11_map]
HAS_SMITH = len(SIDES) > 0

_z_re = re.compile(r'^(leftbody|rightbody)_Z_(R|X)(?:_([0-9.]+))?$')
_z_map = {}
for col in _header_cols:
    m = _z_re.match(col)
    if not m:
        continue
    side, comp, freq_str = m.groups()
    key = float(freq_str) if freq_str else _UNLABELED_KEY
    _z_map.setdefault(side, {}).setdefault(key, {})[comp] = col

freqs = {}
_real_cols = {}
_imag_cols = {}
_zr_cols = {}
_zx_cols = {}
s11_usecols = []
for side in SIDES:
    freq_list = sorted(f for f, d in _s11_map[side].items() if 'Real' in d and 'Imag' in d)
    # サフィックス無し(=周波数不明)のキーは、表示用の周波数に読み替える
    freqs[side] = np.array([_UNLABELED_FREQ if f == _UNLABELED_KEY else f
                            for f in freq_list], dtype=float)
    _real_cols[side] = [_s11_map[side][f]['Real'] for f in freq_list]
    _imag_cols[side] = [_s11_map[side][f]['Imag'] for f in freq_list]
    s11_usecols += _real_cols[side] + _imag_cols[side]
    z_entry = _z_map.get(side, {})
    _zr_cols[side] = [z_entry.get(f, {}).get('R') for f in freq_list]
    _zx_cols[side] = [z_entry.get(f, {}).get('X') for f in freq_list]
    s11_usecols += [c for c in _zr_cols[side] if c is not None]
    s11_usecols += [c for c in _zx_cols[side] if c is not None]

# 単一周波数(1点)モードの判定: どの side も周波数が 1 つだけ。
# このとき「掃引の形」は描けないので、表示を次のように切り替える:
#   スミスチャート  … その 1 点の Γ(現在フレームの値)だけを ★ で示す
#                     (--trail N を付けたときだけ直近 N 行の時間軌跡も描く)
#   左右インピーダンス相対誤差 … 周波数軸ではなく時間軸のグラフ
#   周波数スライダー … 選ぶ余地が無いので出さない
SINGLE_FREQ = HAS_SMITH and all(len(freqs[s]) == 1 for s in SIDES)
# 単一周波数モードでスミスチャートに残す時間軌跡の長さ[行数]。0 = 軌跡を描かない。
# 既定を 0 にしてあるのは、1 点しか測っていないのにスミスチャート上へ多数の点が
# 並ぶと「複数の周波数を測ったトレース」と紛らわしいため。時間変化を見たいときは
# --trail 300 のように明示する。
SMITH_TRAIL_FRAMES = max(0, int(args.trail))


def fmt_freq_mhz(f, prec=2):
    """周波数[MHz]の表示。列名にサフィックスが無い古い CSV では不明(?)になる。"""
    return '?' if not np.isfinite(f) else '{:.{p}f}'.format(f, p=prec)


# ------------------------------------------------------------------ #
# 検出した S11 列の周波数を起動時に報告する。
#   「1 点しか測っていないはずなのにスミスチャートに複数の点が出る」ときの切り分け用。
#   ここで 2 点以上と出るなら、原因は表示側ではなく CSV に複数の周波数列があること。
# ------------------------------------------------------------------ #
if HAS_SMITH:
    for _side in SIDES:
        _fs = freqs[_side]
        if len(_fs) == 1:
            _desc = '1 点 ({} MHz)'.format(fmt_freq_mhz(_fs[0]))
        else:
            _desc = '{} 点 ({} … {} MHz)'.format(
                len(_fs), fmt_freq_mhz(_fs[0]), fmt_freq_mhz(_fs[-1]))
        print('[Info] {} の S11 列: {}'.format(_side, _desc))
    print('[Info] 表示モード: {}'.format(
        '単一周波数(1点)' if SINGLE_FREQ else '掃引'))
    if SINGLE_FREQ:
        print('[Info] スミスチャートは 1 枚にまとめ、左右のインピーダンス(Γ)を'
              '重ねて表示します(青=Left / 赤=Right)。')
    if SINGLE_FREQ and SMITH_TRAIL_FRAMES > 0:
        print('[Info] スミスチャートに直近 {} 行の時間軌跡を描きます'
              '(--trail 0 で現在値の 1 点だけになります)'.format(SMITH_TRAIL_FRAMES))
    elif not SINGLE_FREQ:
        print('[Info] スミスチャートには上記の全周波数を結んだ掃引トレースを描きます。'
              '1 点だけ測ったつもりなら CSV の列名を確認してください。')
else:
    print('[Info] S11 列が見つかりませんでした(スミスチャートは表示しません)。')


VIDEO_TIME_COL = 'video_time_sec'
VIDEO_INRANGE_COL = 'in_video'
WALLCLOCK_COL = 'WallClock'

extra_cols = []
if VIDEO_TIME_COL in _header_cols:
    extra_cols.append(VIDEO_TIME_COL)
if VIDEO_INRANGE_COL in _header_cols:
    extra_cols.append(VIDEO_INRANGE_COL)
if WALLCLOCK_COL in _header_cols:
    extra_cols.append(WALLCLOCK_COL)

usecols = (['Timestamp'] + [f'{prefix}_{ax}' for _, prefix in MARKERS for ax in ('X', 'Y', 'Z')]
           + s11_usecols + extra_cols)
usecols = [c for c in dict.fromkeys(usecols) if c in _header_cols]
df = pd.read_csv(CSV_PATH, usecols=usecols)

timestamps = df['Timestamp'].to_numpy(dtype=float)
pos = {name: df[[f'{prefix}_X', f'{prefix}_Y', f'{prefix}_Z']].to_numpy(dtype=float)
       for name, prefix in MARKERS}
n_frames = len(timestamps)
if n_frames < 2:
    sys.exit('[Error] CSV must contain at least 2 rows.')
dt_ms = float(np.mean(np.diff(timestamps)) * 1000)


# ------------------------------------------------------------------ #
# 動画同期時刻 video_time_sec を用意する
#   1) CSV に video_time_sec 列があればそれを使う
#   2) 無ければ WallClock 列 + 録画開始時刻(--video-start か 動画ファイル名 WIN_...)から計算
# ------------------------------------------------------------------ #
_WIN_CAM_RE = re.compile(r'WIN_(\d{4})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})')
_DT_FORMATS = ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
               "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]


def _parse_dt(text):
    for fmt in _DT_FORMATS:
        try:
            return datetime.datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    return None


def _video_start_from_name(path):
    m = _WIN_CAM_RE.search(Path(path).name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = (int(g) for g in m.groups())
    try:
        return datetime.datetime(y, mo, d, hh, mm, ss)
    except ValueError:
        return None


def _video_start_from_meta(csv_path):
    """
    CSV と同じ場所の <csv名>.meta.json から録画開始時刻(video_start_wall)を読む。
    これはミリ秒精度なので、ファイル名(秒精度)より正確に同期できる。無ければ None。
    """
    meta_path = Path(csv_path).with_name(Path(csv_path).stem + '.meta.json')
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    val = meta.get('video_start_wall')
    return _parse_dt(str(val)) if val else None


def _compute_video_time_from_wallclock(video_path):
    """WallClock 列 + 録画開始時刻から video_time_sec を計算する。失敗時 None。"""
    if WALLCLOCK_COL not in df.columns:
        return None, None
    start = None
    if args.video_start:
        start = _parse_dt(args.video_start)
    if start is None and video_path:
        start = _video_start_from_name(video_path)
    if start is None:
        return None, None
    walls = [_parse_dt(str(w)) for w in df[WALLCLOCK_COL].to_numpy()]
    vts = np.array([(w - start).total_seconds() if w is not None else np.nan
                    for w in walls], dtype=float)
    return vts, start


# ------------------------------------------------------------------ #
# 動画キャプチャを開く(--video 指定時のみ)
# ------------------------------------------------------------------ #
video_cap = None
video_time_sec = None
video_in_range = None
video_fps = 30.0
VIDEO_SIZE = (640, 480)   # 映像の (幅, 高さ)。開いたあと実際の値で上書きする
if args.video:
    video_path = Path(args.video)
    if not video_path.exists():
        sys.exit(f'[Error] Video not found: {video_path}')
    try:
        import cv2
    except ImportError:
        sys.exit('[Error] 動画表示には opencv-python が必要です: pip install opencv-python')

    # video_time_sec を決める優先順位(ずれ対策で「精密な録画開始時刻」を最優先):
    #   1) --video-start(明示) + WallClock  -> WallClock からミリ秒精度で計算
    #   2) meta.json の video_start_wall + WallClock -> ミリ秒精度で計算(ファイル名より正確)
    #   3) CSV の video_time_sec 列(sync_video_with_dataset.py が付けた値)
    #   4) 動画ファイル名 WIN_...(秒精度) + WallClock -> 計算(±1秒の系統誤差が残りうる)
    _explicit_start = _parse_dt(args.video_start) if args.video_start else None
    _meta_start = _video_start_from_meta(CSV_PATH)
    _precise_start = _explicit_start or _meta_start
    if _precise_start is not None and WALLCLOCK_COL in df.columns:
        _walls = [_parse_dt(str(w)) for w in df[WALLCLOCK_COL].to_numpy()]
        video_time_sec = np.array(
            [(w - _precise_start).total_seconds() if w is not None else np.nan
             for w in _walls], dtype=float)
        _how = '明示指定' if _explicit_start else 'meta.json'
        src = f'WallClock - 録画開始({_precise_start}, {_how}・ミリ秒精度)'
    elif VIDEO_TIME_COL in df.columns:
        video_time_sec = df[VIDEO_TIME_COL].to_numpy(dtype=float)
        src = 'CSV の video_time_sec 列'
    else:
        video_time_sec, vstart = _compute_video_time_from_wallclock(str(video_path))
        if video_time_sec is None:
            sys.exit(f'[Error] CSV に "{VIDEO_TIME_COL}" 列が無く、WallClock からも計算できません。\n'
                     f'        sync_video_with_dataset.py で video_time_sec を付けるか、\n'
                     f'        --video-start で録画開始時刻を指定してください。')
        src = f'WallClock - 録画開始({vstart}, ファイル名・秒精度)'
    if VIDEO_INRANGE_COL in df.columns:
        video_in_range = df[VIDEO_INRANGE_COL].to_numpy().astype(bool)
    else:
        video_in_range = np.isfinite(video_time_sec)

    video_cap = cv2.VideoCapture(str(video_path))
    if not video_cap.isOpened():
        sys.exit(f'[Error] Failed to open video: {video_path}')
    fps = video_cap.get(cv2.CAP_PROP_FPS)
    if fps and 1 < fps <= 240:
        video_fps = float(fps)

    # --- 実効 FPS の測り直し ---
    # 本ビューアは「表示すべきフレーム番号 = 経過した実時間 × video_fps」で再生する。
    # そのため video_fps(= CAP_PROP_FPS の宣言値)が実際とずれていると再生速度がずれる:
    # 宣言値が実効値より低いとスロー再生、高いと早送りになる。GoPro/スマホ/Windows カメラ
    # の mp4 は、可変フレームレート(VFR)やメタデータの取りこぼしで CAP_PROP_FPS が実効値と
    # 食い違うことがある。そこで先頭を少しデコードし、各フレームの実時刻(POS_MSEC)から
    # 実効 FPS を測り直す。宣言値と 2% 以上ずれていれば実測値を採用する
    # (POS_MSEC が取れない/不正な環境では宣言値のまま)。測り終えたら先頭へ巻き戻す。
    _fps_src = 'CAP_PROP_FPS(宣言値)'
    try:
        _ms_list = []
        for _ in range(90):
            if not video_cap.grab():
                break
            _ms = video_cap.get(cv2.CAP_PROP_POS_MSEC)
            if _ms is not None and _ms > 0:
                _ms_list.append(float(_ms))
        if len(_ms_list) >= 10 and _ms_list[-1] > _ms_list[0]:
            _meas_fps = (len(_ms_list) - 1) * 1000.0 / (_ms_list[-1] - _ms_list[0])
            if 1.0 < _meas_fps <= 240.0 and abs(_meas_fps - video_fps) / video_fps > 0.02:
                video_fps = _meas_fps
                _fps_src = 'POS_MSEC 実測(宣言値とズレのため補正)'
    except Exception:
        pass
    finally:
        try:
            video_cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        except Exception:
            pass

    # 映像の縦横比。動画パネルの初期プレースホルダをこの比で作らないと、
    # 正方形のダミー画像に合わせて軸の箱が正方形に固定され、映像が縦に伸びる。
    _vw = int(video_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    _vh = int(video_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if _vw > 0 and _vh > 0:
        VIDEO_SIZE = (_vw, _vh)
    print(f'[Info] video_time_sec のソース: {src} / 動画FPS: {video_fps:.3f} '
          f'({_fps_src}) / サイズ: {VIDEO_SIZE[0]}x{VIDEO_SIZE[1]}')
HAS_VIDEO = video_cap is not None

# 動画同期の対象となる行(in_range かつ video_time_sec が有限)の範囲
if HAS_VIDEO:
    _valid = video_in_range & np.isfinite(video_time_sec)
    if not _valid.any():
        sys.exit('[Error] 動画範囲内の行がありません(video_time_sec / in_video を確認)。')
    _vts_full = np.where(np.isfinite(video_time_sec), video_time_sec, np.inf)
    T_START = float(np.nanmin(video_time_sec[_valid]))
    T_END = float(np.nanmax(video_time_sec[_valid]))


def row_for_video_time(t):
    """動画時刻 t[s] に対応する CSV 行(video_time_sec が t 以下で最も大きい行)。"""
    idx = int(np.searchsorted(_vts_full, t, side='right') - 1)
    return int(np.clip(idx, 0, n_frames - 1))


# ------------------------------------------------------------------ #
# S11(Γ)/ Z を配列化
# ------------------------------------------------------------------ #
gamma_real = {side: df[_real_cols[side]].to_numpy() for side in SIDES}
gamma_imag = {side: df[_imag_cols[side]].to_numpy() for side in SIDES}
Z0 = 50.0
z_real = {}
z_react = {}
for side in SIDES:
    n_f = len(freqs[side])
    zr_arr = np.full((n_frames, n_f), np.nan)
    zx_arr = np.full((n_frames, n_f), np.nan)
    for i, col in enumerate(_zr_cols[side]):
        if col is not None:
            zr_arr[:, i] = df[col].to_numpy()
    for i, col in enumerate(_zx_cols[side]):
        if col is not None:
            zx_arr[:, i] = df[col].to_numpy()
    gamma_c = gamma_real[side] + 1j * gamma_imag[side]
    z_calc = Z0 * (1 + gamma_c) / (1 - gamma_c + 1e-15)
    zr_arr[np.isnan(zr_arr)] = z_calc.real[np.isnan(zr_arr)]
    zx_arr[np.isnan(zx_arr)] = z_calc.imag[np.isnan(zx_arr)]
    z_real[side] = zr_arr
    z_react[side] = zx_arr


def nearest_freq_idx(side, target_freq):
    return int(np.argmin(np.abs(freqs[side] - target_freq)))


# ------------------------------------------------------------------ #
# 関節角度(上半身=肘 / 下半身=膝)を事前計算
# ------------------------------------------------------------------ #
def joint_angles(prox, joint, dist):
    """3 点のなす角[deg]を全フレームぶん返す(関節 joint が頂点)。"""
    v1 = prox - joint
    v2 = dist - joint
    cos_a = np.einsum('ij,ij->i', v1, v2) / (
        np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1) + 1e-12)
    return np.degrees(np.arccos(np.clip(cos_a, -1.0, 1.0)))


# 右(index 0) / 左(index 1) の順。ラベルは部位に応じて "R Elbow" / "R Knee" など。
ANGLE_LABEL_R, _r_names = LAYOUT['angles'][0]
ANGLE_LABEL_L, _l_names = LAYOUT['angles'][1]
angles = {
    'R': joint_angles(*(pos[n] for n in _r_names)),
    'L': joint_angles(*(pos[n] for n in _l_names)),
}


def fmt_angle(v):
    return f'{v:.1f}°' if np.isfinite(v) else 'N/A'


# ------------------------------------------------------------------ #
# 図中の文字サイズ
#   離れた位置から見たり資料に貼ったりするため、図中の文字は既定の 2 倍で描く。
#   個々の fontsize は fs() を通し、目盛や凡例など明示していない文字は rcParams の
#   基準サイズ(既定 10 pt)を上げることでまとめて追従させる。倍率を変えたいときは
#   FONT_SCALE だけを書き換えればよい。
# ------------------------------------------------------------------ #
FONT_SCALE = 2.0
plt.rcParams['font.size'] = 10.0 * FONT_SCALE
# フォントは Helvetica(regular)。Helvetica が無い環境では、字幅がほぼ同じ Arial に、
# それも無ければ DejaVu Sans にフォールバックする(Windows は Arial があるので実質同じ見た目)。
plt.rcParams['font.family'] = 'sans-serif'
plt.rcParams['font.sans-serif'] = ['Helvetica', 'Arial', 'DejaVu Sans']
plt.rcParams['font.weight'] = 'normal'


def fs(size):
    """図中の文字サイズ[pt](FONT_SCALE 倍して返す)。"""
    return size * FONT_SCALE


# ------------------------------------------------------------------ #
# レイアウト
#   画面を大きく左右 2 分割する。それぞれの塊の背景に薄いカードを敷き、塊の間に隙間を
#   空けることで「どの表示が関連しているか」を一目で判断できるようにする。
#     ・左カラム … 上に動画 / 下に 3D マーカービュー(縦積み)。
#                  動画なしのときは 3D が左カラム全体を使う。
#     ・右カラム … スミスチャート + 左右インピーダンスの相対誤差(従来どおり)。
#     ・最下段  … 操作系(Play / 時間スライダー / フレーム番号)。
#   各カラム/サブ領域の x・y 範囲をここで一元的に決め、カードも中身もこれを基準に置く。
# ------------------------------------------------------------------ #
fig = plt.figure(figsize=(16, 9))
fig.suptitle(f'Dataset + Video Playback Viewer  ({LAYOUT["label"]})',
             fontsize=fs(14), fontweight='bold', y=0.965)

# 上段カードの縦範囲
_TOP_Y0, _TOP_Y1 = 0.130, 0.915

# 左右カラムの x 範囲(画面をほぼ半分ずつ)
_L_x0, _L_x1 = 0.010, 0.492               # 左カラム(動画 + 3D を 1 枠にまとめる)
_R_x0, _R_x1 = 0.506, 0.990               # 右カラム(スミス + 誤差)
_C_x0, _C_x1 = _R_x0, _R_x1               # 右カラムの別名(スミス/誤差コードが参照)

# 左カラムは「動画 + 3D」を 1 つの枠(カード)にまとめる。枠の中を上から
#   [動画] / [時刻・関節角度の 1 行] / [3D の箱] / [マーカー凡例]
# の順に置く。3D をできるだけ大きくするため、ラベル類は箱の外の細い 1 行・帯に収め、
# 3D の箱には残りを目いっぱい使わせる(以前は箱の上にヘッダー 2 行ぶんを空けていた)。
_B_x0, _B_x1 = _L_x0, _L_x1
if HAS_VIDEO:
    _A_x0, _A_x1, _A_y0, _A_y1 = _L_x0, _L_x1, 0.590, _TOP_Y1   # 動画サブ領域(上)
    _HDR_Y = 0.562                                              # 時刻・関節角度(1 行)
    _ax3d_top = 0.548                                           # 3D の箱の上端
else:
    _A_x0 = _A_x1 = None                                        # 動画なし
    _HDR_Y = _TOP_Y1 - 0.028                                    # 3D が左全体を使う
    _ax3d_top = _TOP_Y1 - 0.056

# 塊を示す背景カードは、いちばん背面(zorder 最小)の全面 axes に描く。
# 静的なので blit の背景ビットマップに一度だけ取り込まれ、以後は描き直されない。
CARD_FILL, CARD_EDGE = '#f4f5f7', '#ccd0d6'
_bg_ax = fig.add_axes([0.0, 0.0, 1.0, 1.0], zorder=-100)
_bg_ax.set_xlim(0, 1)
_bg_ax.set_ylim(0, 1)
_bg_ax.axis('off')
_bg_ax.set_facecolor('none')


def _add_card(x0, x1, y0, y1):
    _bg_ax.add_patch(Rectangle(
        (x0, y0), x1 - x0, y1 - y0, transform=_bg_ax.transAxes,
        facecolor=CARD_FILL, edgecolor=CARD_EDGE, linewidth=1.3, zorder=-100))


_add_card(_L_x0, _L_x1, _TOP_Y0, _TOP_Y1)      # 左カード(動画 + 3D を 1 枠に)
if HAS_SMITH:
    _add_card(_C_x0, _C_x1, _TOP_Y0, _TOP_Y1)  # スミス+誤差カード(右)
_BOT_CARD = (0.010, 0.014, 0.990, 0.108)       # 操作系カード (x0, y0, x1, y1)
_add_card(*_BOT_CARD)

# --- 左: 動画パネル ---
if HAS_VIDEO:
    # 動画パネルは左カラムの使える矩形の中に、映像の縦横比を保った最大サイズで中央配置する。
    # 箱を固定比で置くと、16:9 の映像では上下に大きな余白ができて間延びし、縦長(スマホ)の
    # 映像では左右が余る。実際の映像比 (VIDEO_SIZE) に合わせて箱そのものを作れば、映像が
    # 箱いっぱいに描かれ、余白は左カラムの周囲だけ(中央寄せで左右対称)になる。
    # 動画カード(左・上)の内側(余白を少し取る)を使える矩形にする。
    # 上端側は動画タイトル("Video t = ...")のぶんを空ける。
    _LC = (_A_x0 + 0.016, _A_y0 + 0.020, (_A_x1 - _A_x0) - 0.032,
           (_A_y1 - _A_y0) - 0.075)
    _fig_w_in, _fig_h_in = fig.get_size_inches()
    _av_w_in, _av_h_in = _LC[2] * _fig_w_in, _LC[3] * _fig_h_in
    _vasp = VIDEO_SIZE[0] / max(VIDEO_SIZE[1], 1)      # 映像の 幅/高さ
    if _av_w_in / _vasp <= _av_h_in:                   # 幅で決まる(横長の映像)
        _bw_in, _bh_in = _av_w_in, _av_w_in / _vasp
    else:                                              # 高さで決まる(縦長の映像)
        _bh_in, _bw_in = _av_h_in, _av_h_in * _vasp
    _bw, _bh = _bw_in / _fig_w_in, _bh_in / _fig_h_in
    _bx = _LC[0] + (_LC[2] - _bw) / 2.0
    _by = _LC[1] + (_LC[3] - _bh) / 2.0
    ax_video = fig.add_axes([_bx, _by, _bw, _bh])
    ax_video.axis('off')
    # プレースホルダは必ず映像と同じ縦横比で作る。imshow は aspect='equal' なので
    # 軸の箱がこの画像の比に合わせて縮められる。正方形のダミーだと箱が正方形に決まり、
    # 以後そこへ映像を流し込むと縦に引き伸ばされて見える。
    # interpolation='nearest': 既定の 'antialiased' は縮小のたびに重い再サンプルが
    # 走る(実測 ~15 ms/フレーム)。表示サイズへの縮小は _set_video_image が OpenCV で
    # 済ませるので、ここは等倍描画で足りる。
    video_img = ax_video.imshow(
        np.zeros((VIDEO_SIZE[1], VIDEO_SIZE[0], 3), dtype=np.uint8),
        animated=True, interpolation='nearest')
    video_title = ax_video.text(0.5, 1.02, '', transform=ax_video.transAxes,
                                ha='center', va='bottom', fontsize=fs(10), fontweight='bold',
                                animated=True)
else:
    ax_video = None

# --- 左下: 3D マーカービュー ---
# 3D の箱をできるだけ大きくするため、時刻・関節角度は箱の“外・上”の 1 行(_HDR_Y)に、
# マーカー凡例は箱の下の細い帯に置き、箱そのものは残りを目いっぱい使う(上端=_ax3d_top)。
# 時刻・関節角度は axes の外なので、そのままだと blit 領域(axes の bbox)から外れて更新
# されない。_blit_regions にこの 1 行を覆う矩形(_HDR_REGION)を足して転送対象に含める。
_ax3d_x = _B_x0 + 0.040
_ax3d_w = (_B_x1 - _B_x0) - 0.080
_ax3d_y = _TOP_Y0 + 0.062                       # 下: 凡例(2 段)のぶんを空ける
_ax3d_h = _ax3d_top - _ax3d_y                   # 上: 時刻・関節角度の 1 行のぶんを空ける
_AX3D_RECT = [_ax3d_x, _ax3d_y, _ax3d_w, _ax3d_h]
# 時刻・関節角度の 1 行を覆う blit 転送矩形(図座標)。
_HDR_REGION = (_B_x0, _HDR_Y - 0.020, _B_x1, _HDR_Y + 0.020)
ax = fig.add_axes(_AX3D_RECT, projection='3d')

# --- 3D 軸設定 ---
all_pts = np.vstack(list(pos.values()))
pad = 0.05


def _axis_limits(arr):
    lo, hi = np.nanmin(arr), np.nanmax(arr)
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return -1.0, 1.0
    if lo == hi:
        return lo - 1.0, hi + 1.0
    return lo - pad, hi + pad


ax.set_xlim(*_axis_limits(all_pts[:, 0]))
ax.set_ylim(*_axis_limits(all_pts[:, 1]))
ax.set_zlim(*_axis_limits(all_pts[:, 2]))
# 座標まわりはすっきりさせる: 数値(目盛ラベル)も軸名(X/Y/Z)も表示しない。
# マーカーとボーンで体の向きは読めるので、軸の文字は情報が薄いわりに、凡例や箱の縁と
# ぶつかって“ごちゃつき”の原因になっていた。奥行きの手掛かりとして目盛線(グリッド)は残す。
ax.set_xlabel(''); ax.set_ylabel(''); ax.set_zlabel('')
for _axis in (ax.xaxis, ax.yaxis, ax.zaxis):
    _axis.set_major_locator(MaxNLocator(4))
ax.set_xticklabels([]); ax.set_yticklabels([]); ax.set_zticklabels([])
# 軸名を消したぶん、箱の中の立方体をさらに大きく見せる(既定は周囲の余白が多い)。
# 古い matplotlib は zoom 引数が無いので無視する。
try:
    ax.set_box_aspect(None, zoom=1.3)
except TypeError:
    pass

MARKER_STYLE = {
    # --- 上半身 ---
    'R_upperarm': dict(marker='o', color='red',         trail='salmon',     label='R UpperArm'),
    'R_joint':    dict(marker='s', color='darkorange',  trail='moccasin',   label='R Joint'),
    'R_forearm':  dict(marker='^', color='firebrick',   trail='lightcoral', label='R Forearm'),
    'chest':      dict(marker='D', color='dimgray',     trail='lightgray',  label='Chest'),
    'L_upperarm': dict(marker='o', color='blue',        trail='skyblue',    label='L UpperArm'),
    'L_joint':    dict(marker='s', color='deepskyblue', trail='powderblue', label='L Joint'),
    'L_forearm':  dict(marker='^', color='navy',        trail='lightsteelblue', label='L Forearm'),
    # --- 下半身(上半身と同じ配色ルール: 右=赤系 / 左=青系 / 中心=灰) ---
    'R_thigh':    dict(marker='o', color='red',         trail='salmon',     label='R Thigh'),
    'R_knee':     dict(marker='s', color='darkorange',  trail='moccasin',   label='R Knee'),
    'R_shin':     dict(marker='^', color='firebrick',   trail='lightcoral', label='R Shin'),
    'waist':      dict(marker='D', color='dimgray',     trail='lightgray',  label='Waist'),
    'L_thigh':    dict(marker='o', color='blue',        trail='skyblue',    label='L Thigh'),
    'L_knee':     dict(marker='s', color='deepskyblue', trail='powderblue', label='L Knee'),
    'L_shin':     dict(marker='^', color='navy',        trail='lightsteelblue', label='L Shin'),
}
for name, _ in MARKERS:
    st = MARKER_STYLE[name]
    ax.plot(*pos[name].T, color=st['trail'], alpha=0.35, lw=0.8, zorder=1)

# 毎フレーム動かすアーティストは animated=True にして blit で描く(_blit_all を参照)。
# 静的な軌跡・格子・目盛は背景としてキャッシュされ、再描画されない。
pts = {}
for name, _ in MARKERS:
    st = MARKER_STYLE[name]
    pt, = ax.plot([], [], [], linestyle='None', marker=st['marker'], color=st['color'],
                  ms=9, label=st['label'], zorder=5, animated=True)
    pts[name] = pt

BONES = LAYOUT['bones']
bone_lines = [ax.plot([], [], [], '-', color=c, lw=2.2, zorder=4, animated=True)[0]
              for _, _, c in BONES]

# マーカー凡例は 3D ビューの下(3D カードの下端)に横並びで置く。枠(囲み)は付けず、
# 薄めの文字にして“ごちゃつき”を抑える。左カラムは横に広いので 4 列に収める。
ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.02), ncol=4,
          fontsize=fs(6.0), columnspacing=1.2, labelspacing=0.4,
          handletextpad=0.3, frameon=False)
# 時刻・関節角度(上半身=肘 / 下半身=膝)は 3D の箱の外(上)の 1 行に置く(箱の中だと
# マーカーと重なり、上に 2 行取ると箱が小さくなるため)。時刻は控えめなグレー、
# 関節角度は左右の色でそれぞれ示す。フレーム数や角度が大きい実データでも枠(左カード)に
# 収まるよう、regular(細字)で少し小さめにし、左側の角度の右端がカード右端(_L_x1)を
# 越えないよう位置を決める。
time_txt    = fig.text(_B_x0 + 0.016, _HDR_Y, '', fontsize=fs(6.5), va='center',
                       color='0.35', animated=True)
angle_txt_r = fig.text(_B_x0 + 0.180, _HDR_Y, '', fontsize=fs(7.5), va='center',
                       color='crimson', animated=True)
angle_txt_l = fig.text(_B_x0 + 0.320, _HDR_Y, '', fontsize=fs(7.5), va='center',
                       color='royalblue', animated=True)

# ------------------------------------------------------------------ #
# スミスチャート(右上)
#   掃引モード       … 左右 body で 1 枚ずつ、横に 2 つ並べる(掃引トレースが重なると
#                      どちらの軌跡か判らなくなるため)
#   単一周波数モード … 各 body の点は 1 つだけで重なる心配が無いので、1 枚にまとめて
#                      左右のインピーダンス(Γ)を同じチャート上に重ねて表示する
# ------------------------------------------------------------------ #
SMITH_STYLE = {
    'leftbody':  dict(color='royalblue', label='Left Body'),
    'rightbody': dict(color='crimson',   label='Right Body'),
}
SMITH_MERGED = SINGLE_FREQ

if SMITH_MERGED:
    # 1 枚に統合。2 枚ぶんの領域を使えるのでチャートを大きく取り、Z 読み出しは
    # チャート下に左右 2 行で並べる(cx / rect は左右で同じものを指す)。
    # チャート周りは文字が込み合いやすいので、行数を増やさないことを優先する:
    #   タイトルは 1 行(--trail 時のみ 2 行目)、凡例の箱は作らず Z の行頭に ★ を付けて
    #   兼ねさせ、周波数はタイトルにだけ書く(Z の行や相対誤差グラフでは繰り返さない)。
    # 右カラムを上下 2 段(スミス上・相対誤差下)でしっかり埋めるため、スミスは
    # 大きめに取る。aspect='equal' なので実際の円径は箱の短辺で決まる(ここでは高さ)。
    # title_y(円の上)/ ztext_y(円の下 2 行)は箱に合わせた値にする。
    _MERGED_RECT = [0.598, 0.45, 0.300, 0.40]
    _MERGED_CX = 0.748
    SMITH_BLOCK = {
        'leftbody':  dict(rect=_MERGED_RECT, cx=_MERGED_CX, title_y=0.892, ztext_y=0.435),
        'rightbody': dict(rect=_MERGED_RECT, cx=_MERGED_CX, title_y=0.892, ztext_y=0.398),
    }
else:
    # 右カラム(カード C)に 2 つ左右並び。各ブロックに中心 x(cx)を持たせ、
    # タイトルは上・Z 読み出しは下。カード上端に収まるようタイトルを少し下げる。
    SMITH_BLOCK = {
        'leftbody':  dict(rect=[0.537, 0.585, 0.185, 0.235], cx=0.6295,
                          title_y=0.892, ztext_y=0.568),
        'rightbody': dict(rect=[0.772, 0.585, 0.185, 0.235], cx=0.8645,
                          title_y=0.892, ztext_y=0.568),
    }


SMITH_GRID_R = (0.2, 0.5, 1.0, 2.0, 5.0)   # 定抵抗円 r = R/Z0
SMITH_GRID_X = (0.2, 0.5, 1.0, 2.0, 5.0)   # 定リアクタンス円 x = X/Z0 (±)


def smith_r_circle(r, n=241):
    """
    定抵抗円(r = const)を Γ 平面上の厳密な円として返す。
    z = r + jx を Γ=(z-1)/(z+1) で写すと、中心 (r/(1+r), 0)・半径 1/(1+r) の完全な円になる。
    (x を線形に振って写像すると Γ 上での点間隔が不均一になり、Γ=-1 付近が折れ線に見えてしまう)
    """
    t = np.linspace(0.0, 2.0 * np.pi, n)
    c = r / (1.0 + r)
    rad = 1.0 / (1.0 + r)
    return c + rad * np.cos(t), rad * np.sin(t)


def smith_x_arc(x, n=241):
    """
    定リアクタンス円(x = const)のうち、単位円の内側にある弧だけを厳密に返す。

    Γ 平面では中心 (1, 1/x)・半径 1/|x| の円になるが、そのうち意味があるのは
    r=0(単位円上) から r=∞(Γ=1) までの弧だけで、全周を描くと単位円の外へはみ出す。
    そこで両端の角度を求め、単位円の内側を通る向き(x>0 なら反時計回り、x<0 なら
    時計回り)に角度を等分して描く。こうすると弧長方向に点が均等に並ぶため、
    r を線形に振る方式と違い r≈0 付近(単位円の近く)でもカクつかない。
    """
    cy = 1.0 / x                 # 円の中心 (1, 1/x)
    rad = 1.0 / abs(x)           # 半径
    g0 = (1j * x - 1.0) / (1j * x + 1.0)                 # r=0 側の端点(単位円上)
    th0 = float(np.arctan2(g0.imag - cy, g0.real - 1.0))
    th1 = float(np.arctan2(-cy, 0.0))                    # r=∞ 側の端点 Γ=1
    dth = th1 - th0
    if x > 0:                    # 上半分: 反時計回りが単位円の内側を通る
        while dth <= 0.0:
            dth += 2.0 * np.pi
    else:                        # 下半分: 時計回り
        while dth >= 0.0:
            dth -= 2.0 * np.pi
    t = th0 + dth * np.linspace(0.0, 1.0, n)
    return 1.0 + rad * np.cos(t), cy + rad * np.sin(t)


def draw_smith_grid(ax_s, n=241):
    """単位円・実軸(x=0)・定抵抗円・定リアクタンス弧からなるスミスチャートのグリッドを描く。"""
    theta = np.linspace(0, 2 * np.pi, 361)
    ax_s.plot(np.cos(theta), np.sin(theta), color='black', lw=1.2, zorder=1)  # r=0 = 単位円
    ax_s.plot([-1, 1], [0, 0], color='gray', lw=0.6, zorder=1)                # x=0 = 実軸
    for r in SMITH_GRID_R:
        gx, gy = smith_r_circle(r, n)
        ax_s.plot(gx, gy, color='gray', lw=0.5, alpha=0.6, zorder=1)
    for x in SMITH_GRID_X:
        for sign in (1, -1):
            gx, gy = smith_x_arc(sign * x, n)
            ax_s.plot(gx, gy, color='gray', lw=0.4, alpha=0.5, zorder=1)
    ax_s.set_xlim(-1.08, 1.08)
    ax_s.set_ylim(-1.08, 1.08)
    ax_s.set_aspect('equal')
    ax_s.axis('off')


smith_lines = {}
smith_start_pts = {}
freq_marker_pts = {}
freq_txt = {}
if HAS_SMITH:
    # 統合表示では格子を 1 回だけ描き、その軸を左右で共有する。
    merged_ax = None
    if SMITH_MERGED:
        merged_ax = fig.add_axes(SMITH_BLOCK[SIDES[0]]['rect'])
        draw_smith_grid(merged_ax)
        # (matplotlib の既定フォントは日本語を持たないので図中の文字は英語にする)
        # 見出しは「インピーダンス」にする: 点の座標は Γ = S11 だが、スミスチャートは
        # その Γ をインピーダンス(Z = 50Ω 基準)として読む図であり、下に出している
        # 数値も Z なので、S11 と書くと読み取る量と食い違って見える。
        # 既定(--trail 0)は 1 行だけにし、軌跡を描くときだけ断り書きを 2 行目に足す。
        title = f'Impedance @ {fmt_freq_mhz(freqs[SIDES[0]][0])} MHz (1 pt)'
        if SMITH_TRAIL_FRAMES > 0:
            title += f'\ntrail: last {SMITH_TRAIL_FRAMES} rows'
        fig.text(SMITH_BLOCK[SIDES[0]]['cx'], SMITH_BLOCK[SIDES[0]]['title_y'], title,
                 fontsize=fs(9.5), fontweight='bold', color='black', ha='center', va='top')
    for side in SIDES:
        st = SMITH_STYLE[side]
        blk = SMITH_BLOCK[side]
        if merged_ax is not None:
            axs = merged_ax
        else:
            axs = fig.add_axes(blk['rect'])
            draw_smith_grid(axs)
            fmin, fmax, npts = freqs[side].min(), freqs[side].max(), len(freqs[side])
            title = (f'{st["label"]} Impedance\n'
                     f'{fmt_freq_mhz(fmin, 1)}-{fmt_freq_mhz(fmax, 1)} MHz ({npts}pt)')
            fig.text(blk['cx'], blk['title_y'], title, fontsize=fs(9), fontweight='bold',
                     color=st['color'], ha='center', va='top')
        line, = axs.plot([], [], '-', color=st['color'], lw=1.3, zorder=3, animated=True)
        start_pt, = axs.plot([], [], marker='o', color=st['color'], ms=5, zorder=4,
                             animated=True)
        # 統合表示では左右を色で見分ける必要があるので、★ の塗りを body の色にする
        # (別チャートなら塗りは金色固定でよく、縁の色で body を示している)。
        mk_style = (dict(color=st['color'], ms=17, markeredgecolor='black',
                         markeredgewidth=0.8)
                    if merged_ax is not None else
                    dict(color='gold', ms=16, markeredgecolor=st['color'],
                         markeredgewidth=1.3))
        marker_pt, = axs.plot([], [], marker='*', zorder=6, animated=True, **mk_style)
        txt = fig.text(blk['cx'], blk['ztext_y'], '', fontsize=fs(9),
                       ha='center', va='top', color=st['color'], fontweight='bold',
                       animated=True)
        smith_lines[side] = line
        smith_start_pts[side] = start_pt
        freq_marker_pts[side] = marker_pt
        freq_txt[side] = txt

# ------------------------------------------------------------------ #
# 左右インピーダンスの相対誤差(右基準)グラフ(スミスチャート 2 つの下)
#   各周波数で rel(f) = |Z_left(f) - Z_right(f)| / |Z_right(f)| * 100 [%]。
#   右(rightbody)を基準(分母)にした、左右インピーダンスの相対誤差。フレームごとに更新する。
# ------------------------------------------------------------------ #
HAS_ERR = HAS_SMITH and ('leftbody' in SIDES) and ('rightbody' in SIDES)
err_line = None
err_vline = None
if HAS_ERR:
    _nerr = min(len(freqs['leftbody']), len(freqs['rightbody']))
    err_freqs = np.asarray(freqs['leftbody'][:_nerr], dtype=float)
    # 全フレームの相対誤差から y 上限を決める(外れ値は 99 パーセンタイルで抑制)
    _zl_all = z_real['leftbody'][:, :_nerr] + 1j * z_react['leftbody'][:, :_nerr]
    _zr_all = z_real['rightbody'][:, :_nerr] + 1j * z_react['rightbody'][:, :_nerr]
    _rel_all = np.abs(_zl_all - _zr_all) / (np.abs(_zr_all) + 1e-12) * 100.0
    _emax = np.nanpercentile(_rel_all, 99) if np.any(np.isfinite(_rel_all)) else 100.0
    # 相対誤差グラフはスミスの下(カード C の下部)に置く。掃引モードはスミス 2 枚ぶんの
    # 幅を使うので、単一周波数モード(1 枚)より横に広げてカードいっぱいに描く。
    if SINGLE_FREQ:
        ax_err = fig.add_axes([0.575, 0.180, 0.350, 0.150])
    else:
        ax_err = fig.add_axes([0.545, 0.180, 0.400, 0.150])
    ax_err.set_ylim(0, max(float(_emax) * 1.1, 1.0))
    # 縦軸ラベル(回転文字)は枠の外・左隣のカードへ張り出しやすいので置かず、
    # 単位 [%] はタイトルに入れる。縦軸は目盛の数字だけで足りる。
    ax_err.tick_params(labelsize=fs(7))
    # 文字が大きいので目盛の数字は少なめにする(重なり防止)
    ax_err.yaxis.set_major_locator(MaxNLocator(3))
    ax_err.xaxis.set_major_locator(MaxNLocator(6))
    ax_err.grid(True, lw=0.4, alpha=0.5)
    if SINGLE_FREQ:
        # 周波数軸が 1 点しか無い(=線が引けない)ので、全フレームの推移を時間軸で描き、
        # 現在フレームを縦線で示す。
        _rel_series = _rel_all[:, 0]
        ax_err.plot(timestamps, _rel_series, color='purple', lw=1.0, alpha=0.9)
        err_vline = ax_err.axvline(timestamps[0], color='black', lw=1.2, alpha=0.7,
                                   animated=True)
        ax_err.set_xlim(float(timestamps[0]), float(timestamps[-1]))
        ax_err.set_xlabel('Time (s)', fontsize=fs(8))
        # 周波数はスミスチャートのタイトルに出ているので、ここでは繰り返さない
        # (スミスの下は文字が込み合うため)。
        ax_err.set_title('L vs R impedance rel. error [%] (ref: Right)',
                         fontsize=fs(8.5), fontweight='bold')
    else:
        (err_line,) = ax_err.plot([], [], color='purple', lw=1.4, animated=True)
        ax_err.set_xlim(float(err_freqs.min()), float(err_freqs.max()))
        ax_err.set_xlabel('Frequency (MHz)', fontsize=fs(8))
        ax_err.set_title('L vs R impedance rel. error [%] (ref: Right)',
                         fontsize=fs(8.5), fontweight='bold')


def update_err_display(idx):
    """
    左右インピーダンスの相対誤差(右基準)を更新する。
    掃引モード: 現在フレームの誤差を周波数ごとにプロットする。
    単一周波数モード: 全フレームの時系列は固定表示なので、現在位置の縦線だけ動かす。
    """
    if not HAS_ERR:
        return
    if SINGLE_FREQ:
        err_vline.set_xdata([timestamps[idx], timestamps[idx]])
        return
    zl = z_real['leftbody'][idx, :_nerr] + 1j * z_react['leftbody'][idx, :_nerr]
    zr = z_real['rightbody'][idx, :_nerr] + 1j * z_react['rightbody'][idx, :_nerr]
    rel = np.abs(zl - zr) / (np.abs(zr) + 1e-12) * 100.0
    mask = np.isfinite(rel)
    err_line.set_data(err_freqs[mask], rel[mask])

# ------------------------------------------------------------------ #
# ウィジェット
#   Play ボタン / 時間(フレーム)スライダー / フレーム番号入力: 画面最下部に横 1 列で並べる。
#     以前は「全幅スライダー」の下にボタンとフレーム入力を別の段で置いており、下部が
#     2 段ぶんの高さを占めていた。1 行にまとめて縦を節約し、そのぶん上のパネル(動画・
#     3D・相対誤差グラフ)を下へ広げて大きく表示する。3 つは縦中心(y≈0.063)を揃える。
#   周波数スライダー: スミスチャートの下(右側)に配置(掃引モードのみ)
# ------------------------------------------------------------------ #
ax_slider = fig.add_axes([0.165, 0.054, 0.49, 0.028])
slider = Slider(ax_slider, 'Time', 0, n_frames - 1,
                valinit=0, valstep=1, color='steelblue')
# set_val のたびに canvas.draw_idle()(図全体の再描画)が走らないようにする。
# スライダーの表示は _blit_all() が animated アーティストとして描き直す。
slider.drawon = False
slider.poly.set_animated(True)
slider._handle.set_animated(True)
slider.valtext.set_animated(True)

ax_btn = fig.add_axes([0.022, 0.042, 0.072, 0.052])
btn_play = Button(ax_btn, 'Play', color='0.85', hovercolor='0.70')
btn_play.label.set_animated(True)

# ラベルは短く 'Frame ' にする(2 倍サイズの文字だと 'Frame(0-N)' が左へ伸びて
# 時間スライダーやその値表示と重なるため。有効範囲はスライダーで判る)。
ax_time_box = fig.add_axes([0.905, 0.046, 0.055, 0.045])
time_box = TextBox(ax_time_box, 'Frame ', initial='0')
time_box.text_disp.set_animated(True)

state = {'playing': False, 'frame': 0, 'updating': False, 'freq': None,
         'bg': None, 'vframe': 0, 'vshape': None,
         'last_panel': 0.0,      # 3D/スミス等を最後に更新した時刻(間引き用)
         # 実時間再生の基準(再生開始時刻と、そのときの動画フレーム番号 / CSV 行)
         'play_t0': 0.0, 'play_vf0': 0, 'play_row0': 0,
         'regions': None}   # blit する領域(初回に算出してキャッシュ)

# 動画あり再生で「動画以外のパネル(3D・スミス・角度・スライダー)」を更新する上限レート。
# 動画は実時間どおり更新するが、全パネルの描き直しは 1 回 40〜50 ms かかるため、
# 毎フレーム行うと動画のフレーム間隔(24 fps なら 41.7 ms)を使い切ってしまい、
# フレームを落として映像がカクつく。実測では 10 Hz で映像 21.6/24 fps(取りこぼし 9%)、
# 15 Hz では 15〜17 fps(同 28〜38%)。下げると映像が滑らかに、上げるとパネルが
# 動画によく追従する(単一周波数モードでも CSV の全行が描かれるわけではない)。
PANEL_UPDATE_MAX_HZ = 10
PANEL_MIN_INTERVAL = 1.0 / PANEL_UPDATE_MAX_HZ

DEFAULT_FREQ_MHZ = 13.56
freq_slider = None
if SINGLE_FREQ:
    # 周波数が 1 つしか無いので選ぶ余地が無い(スライダーは出さない)。
    # 読み取り対象はその 1 点に固定する。
    state['freq'] = float(freqs[SIDES[0]][0])
elif HAS_SMITH:
    _all_freqs = np.concatenate([freqs[side] for side in SIDES])
    _fmin, _fmax = float(_all_freqs.min()), float(_all_freqs.max())
    _fstep = float(np.min([np.diff(freqs[side]).min() for side in SIDES if len(freqs[side]) > 1] or [0.01]))
    _finit = float(np.clip(DEFAULT_FREQ_MHZ, _fmin, _fmax))
    # スミスチャート(上)と相対誤差グラフ(下)の間の余白に置く。読み取り対象の周波数を
    # 選ぶスライダーなので、スミスの ★ / Z 読み出しの近くにあるほうが対応が判りやすい。
    ax_freq = fig.add_axes([0.635, 0.455, 0.230, 0.022])
    freq_slider = Slider(ax_freq, 'Freq (MHz)', _fmin, _fmax,
                         valinit=_finit, valstep=_fstep, color='seagreen', valfmt='%.2f')
    state['freq'] = _finit


def update_freq_display():
    if not HAS_SMITH or state['freq'] is None:
        return
    idx = state['frame']
    target = state['freq']
    for side in SIDES:
        # 単一周波数モードでは選択肢が 1 つだけ(周波数が不明な古い CSV でも 0 に固定)
        fi = 0 if SINGLE_FREQ else nearest_freq_idx(side, target)
        actual_f = freqs[side][fi]
        gr = gamma_real[side][idx, fi]
        gi = gamma_imag[side][idx, fi]
        zr = z_real[side][idx, fi]
        zx = z_react[side][idx, fi]
        if np.isfinite(gr) and np.isfinite(gi):
            freq_marker_pts[side].set_data(np.array([gr]), np.array([gi]))
        else:
            freq_marker_pts[side].set_data(np.array([]), np.array([]))
        # 統合表示は 1 枚に左右が重なっているので、どちらの Z かを行頭で示す。
        # 行頭の ★ はチャート上のマーカーと同色なので、これが凡例を兼ねる
        # (周波数はチャートのタイトルに出ているので繰り返さない)。
        # ★ は mathtext($\bigstar$)で描く。Helvetica/Arial には ★(U+2605)が無く、
        # そのまま文字として置くと豆腐(□)になるため。mathtext なら本文フォントに依らず
        # 描け、色も本文と同じ(= body の色)になる。
        # 掃引モードは 2 枚のチャートが近接しており、1 行に収めると隣の読み出しと
        # ぶつかるので周波数と Z を 2 行に分ける。
        if SMITH_MERGED:
            head, sep = r'$\bigstar$ ' + SMITH_STYLE[side]['label'], ': '
        else:
            head, sep = f'@{fmt_freq_mhz(actual_f)} MHz', '\n'
        if np.isfinite(zr) and np.isfinite(zx):
            sign = '+' if zx >= 0 else '-'
            freq_txt[side].set_text(f'{head}{sep}Z = {zr:.1f} {sign} j{abs(zx):.1f} Ω')
        else:
            freq_txt[side].set_text(f'{head}{sep}Z = N/A')


# ------------------------------------------------------------------ #
# マーカー/スミス/角度の更新(CSV 行が変わったときだけ全体を再描画)
# ------------------------------------------------------------------ #
def draw_markers_smith(idx):
    idx = int(np.clip(idx, 0, n_frames - 1))
    state['frame'] = idx
    cur = {name: pos[name][idx] for name, _ in MARKERS}
    for name, pt in pts.items():
        p = cur[name]
        pt.set_data(np.array([p[0]]), np.array([p[1]]))
        pt.set_3d_properties(np.array([p[2]]))
    for line, (a, b, _c) in zip(bone_lines, BONES):
        pa, pb = cur[a], cur[b]
        line.set_data(np.array([pa[0], pb[0]]), np.array([pa[1], pb[1]]))
        line.set_3d_properties(np.array([pa[2], pb[2]]))
    time_txt.set_text(f'Time: {timestamps[idx]:.3f} s   [{idx + 1} / {n_frames}]')
    angle_txt_r.set_text(f'{ANGLE_LABEL_R}: {fmt_angle(angles["R"][idx])}')
    angle_txt_l.set_text(f'{ANGLE_LABEL_L}: {fmt_angle(angles["L"][idx])}')
    # ---- ウィジェットの表示更新 ----
    # TextBox.set_val() は 'submit' オブザーバまで発火するため、そのまま呼ぶと
    # on_time_submit → seek_to_row に再入し、行が変わるたびに動画を再シークして
    # しまう(mp4 のシークはキーフレーム単位なので映像がとびとびになる)。さらに
    # TextBox._rendercursor() は canvas.draw() を同期実行するため、行が変わるたびに
    # 図全体が再描画されて再生が追いつかなくなる。
    # → 表示テキストだけを直接書き換える(オブザーバも再描画も起こさない)。
    state['updating'] = True
    try:
        slider.set_val(idx)          # drawon=False なので再描画は起きない
        time_box.text_disp.set_text(str(idx))
    finally:
        state['updating'] = False
    if HAS_SMITH:
        for side in SIDES:
            if SINGLE_FREQ and SMITH_TRAIL_FRAMES <= 0:
                # 1 点しか測っていないので、チャートに出す点も 1 つだけにする。
                # 現在フレームの値は ★(update_freq_display)が描くので、ここでは
                # 掃引トレースも軌跡の始点 ○ も描かない。
                smith_lines[side].set_data([], [])
                smith_start_pts[side].set_data([], [])
                continue
            if SINGLE_FREQ:
                # --trail N 指定時のみ。掃引トレースは引けないので、代わりにその周波数の
                # Γ が時間とともにどう動いたか(直近 SMITH_TRAIL_FRAMES 行の軌跡)を描き、
                # 軌跡の始点を ○ で、現在値を ★(update_freq_display)で示す。
                lo = max(0, idx - SMITH_TRAIL_FRAMES + 1)
                re_vals = gamma_real[side][lo:idx + 1, 0]
                im_vals = gamma_imag[side][lo:idx + 1, 0]
            else:
                re_vals = gamma_real[side][idx]
                im_vals = gamma_imag[side][idx]
            mask = np.isfinite(re_vals) & np.isfinite(im_vals)
            smith_lines[side].set_data(re_vals[mask], im_vals[mask])
            if mask.any():
                smith_start_pts[side].set_data([re_vals[mask][0]], [im_vals[mask][0]])
            else:
                smith_start_pts[side].set_data([], [])
        update_freq_display()
        update_err_display(idx)


# ------------------------------------------------------------------ #
# 描画(blit)
#   毎フレーム図全体を再描画すると、相対誤差の時系列(数千点)や 3D の箱・
#   スミス格子まで描き直すことになり 1 回あたり数百 ms かかる。単一周波数モードでは
#   CSV の行が動画フレームより速く進む(例 54 Hz > 24 fps)ため、行が変わるたびに
#   全体再描画していると再生が追いつかず、映像がとびとびになる。
#   → 静的な部分は背景としてキャッシュし、動くアーティストだけ描き直して blit する。
# ------------------------------------------------------------------ #
# 毎フレーム描き直すアーティスト(描画順=下→上)
_ANIM = list(pts.values()) + list(bone_lines) + [time_txt, angle_txt_r, angle_txt_l]
if HAS_SMITH:
    for _side in SIDES:
        _ANIM += [smith_lines[_side], smith_start_pts[_side],
                  freq_marker_pts[_side], freq_txt[_side]]
if HAS_ERR:
    _ANIM.append(err_vline if SINGLE_FREQ else err_line)
# 動画パネルは毎フレーム更新する(他のパネルは PANEL_UPDATE_MAX_HZ で間引く)。
# 時刻テキスト(video_title)は軸の外にあり動画領域の blit に含まれないので、毎フレーム
# 描いても画面には出ない。テキスト描画は 1〜2 ms かかるためここには入れない。
_ANIM_VIDEO = [video_img] if HAS_VIDEO else []
if HAS_VIDEO:
    _ANIM += [video_img, video_title]
_ANIM += [slider.poly, slider._handle, slider.valtext, time_box.text_disp,
          btn_play.label]   # Play/Pause の表示も blit で更新する


# blit の結果を画面へ反映する方法。Tk バックエンドでは flush_events() が
# canvas.update()(= 入力イベントも含む全イベント処理)を呼ぶため 1 回あたり数十 ms
# かかることがある。再描画だけを処理する update_idletasks() で十分かつ大幅に軽い。
_tk_widget = None
if hasattr(fig.canvas, 'get_tk_widget'):
    try:
        _tk_widget = fig.canvas.get_tk_widget()
    except Exception:
        _tk_widget = None


def _flush():
    if _tk_widget is not None:
        _tk_widget.update_idletasks()
    else:
        fig.canvas.flush_events()


def _draw_animated():
    """動くアーティストだけを現在のレンダラで描く(図全体の再描画はしない)。"""
    for art in _ANIM:
        try:
            # fig.text() で作ったアーティストは axes を持たないので figure に描かせる
            (art.axes or fig).draw_artist(art)
        except Exception:
            pass               # 一時的な描画例外で再生を止めない


def _on_draw(_event=None):
    """
    図全体が描かれたとき(初回/リサイズ/3D 回転)に静的背景をキャッシュし直す。
    全体描画では animated アーティストは描かれないので、ここで描いて画面へ出す。
    """
    try:
        state['bg'] = fig.canvas.copy_from_bbox(fig.bbox)
    except Exception:
        state['bg'] = None
        return
    state['regions'] = None      # リサイズ等で軸の位置が変わっている可能性
    _draw_animated()
    fig.canvas.blit(fig.bbox)


fig.canvas.mpl_connect('draw_event', _on_draw)


def _blit_regions():
    """
    画面へ転送する領域(= 動くアーティストがある軸 + 図直下のテキスト周辺)を返す。

    図全体を blit すると余白まで毎回転送することになり、この図(1600x900)では
    1 回あたり 25〜30 ms かかる。必要な領域だけに絞ると半分以下で済む。
    """
    regs = []
    seen = []
    for art in _ANIM:
        a = art.axes
        if a is not None and a not in seen:
            seen.append(a)
            regs.append(a.bbox)
    # fig.text() で置いた Z 読み出しはどの軸にも属さないので、スミス 2 枚と
    # その下のテキストをまとめて覆う矩形(図座標)を 1 つ足す。
    if HAS_SMITH:
        x0 = min(SMITH_BLOCK[s]['rect'][0] for s in SIDES) - 0.01
        x1 = max(SMITH_BLOCK[s]['rect'][0] + SMITH_BLOCK[s]['rect'][2] for s in SIDES) + 0.01
        y0 = min(SMITH_BLOCK[s]['ztext_y'] for s in SIDES) - 0.04
        y1 = max(SMITH_BLOCK[s]['rect'][1] + SMITH_BLOCK[s]['rect'][3] for s in SIDES) + 0.01
        regs.append(TransformedBbox(Bbox([[x0, y0], [x1, y1]]), fig.transFigure))
    # 3D の時刻・関節角度は箱の外(上)のヘッダー帯に fig.text で置いているので、
    # その帯を覆う矩形(図座標)を足して blit の転送対象に含める。
    hx0, hy0, hx1, hy1 = _HDR_REGION
    regs.append(TransformedBbox(Bbox([[hx0, hy0], [hx1, hy1]]), fig.transFigure))
    # 動画パネルのタイトル(軸の上)も入るよう、動画軸だけ少し上へ広げる
    if HAS_VIDEO:
        bb = ax_video.bbox
        regs.append(Bbox([[bb.x0, bb.y0], [bb.x1, bb.y1 + 0.04 * fig.bbox.height]]))
    return regs


def _blit_all():
    """背景を復元し、動くアーティストだけ描き直して画面へ反映する。"""
    if state['bg'] is None:
        fig.canvas.draw()      # -> draw_event -> _on_draw が背景の保存まで行う
        return
    fig.canvas.restore_region(state['bg'])
    _draw_animated()
    if state['regions'] is None:
        state['regions'] = _blit_regions()
    for reg in state['regions']:
        fig.canvas.blit(reg)
    _flush()


def _blit_video_only():
    """
    動画パネルだけを更新する(動画領域だけ blit するので全面 blit よりずっと安価)。
    背景の復元は全面に効くが、画面へ転送するのは動画領域だけなので、他のパネルは
    前回 blit した内容がそのまま残る。
    """
    if state['bg'] is None:
        fig.canvas.draw()
        return
    fig.canvas.restore_region(state['bg'])
    for art in _ANIM_VIDEO:
        ax_video.draw_artist(art)
    fig.canvas.blit(ax_video.bbox)
    _flush()


def full_redraw(idx):
    """マーカー/スミス/角度/動画を更新して画面へ反映する。"""
    draw_markers_smith(idx)
    _blit_all()


# ツールバーの保存ボタン(fig.savefig)は通常の描画を行うため、animated アーティスト
# (マーカー・スミス・動画)が入らない。保存の間だけ animated を外して描かせる。
_orig_savefig = fig.savefig


def _savefig_with_animated(*a, **kw):
    for art in _ANIM:
        art.set_animated(False)
    try:
        return _orig_savefig(*a, **kw)
    finally:
        for art in _ANIM:
            art.set_animated(True)
        state['bg'] = None      # 背景を取り直す(保存時の描画で汚れているため)


fig.savefig = _savefig_with_animated


# ------------------------------------------------------------------ #
# 動画: フレーム番号で駆動(フレームを飛ばさず順次読み。時刻ずれが累積しない)
#   録画は「実時間で一定 FPS」で書かれているため、フレーム番号 k の実時刻 = k / video_fps。
#   これを video_time_sec(= WallClock - 録画開始 = 実経過時間)と突き合わせて行を選ぶので、
#   宣言 FPS の誤差や POS_MSEC のクセに依存せず、映像とマーカー/スミスが同期し続ける。
# ------------------------------------------------------------------ #
if HAS_VIDEO:
    import cv2


def _set_video_image(frame_bgr, t_sec):
    """
    動画フレームを表示用アーティストへ流し込む。

    速度のための工夫(ここが再生の重さの大半を占めるため):
      - 表示サイズへの縮小は OpenCV で行う。matplotlib の imshow は既定の補間
        ('antialiased')で描画のたびに重い再サンプルを行うため(実測 ~15 ms)、
        あらかじめ表示ピクセル数まで落として補間 'nearest' で描く(~3.5 ms)。
      - BGR→RGB は cvtColor で連続配列にする(逆順スライスのビューは後段が遅い)。
      - extent は映像サイズが変わったときだけ更新する(毎回呼ぶと軸の再計算が入る)。

    ★縦横比★ 縮小先は「軸の箱にそのまま合わせる」のではなく、元映像の縦横比を保った
    まま箱に収まる最大サイズにする。箱の縦横比は映像と一致しているとは限らないため、
    箱にそのまま合わせると映像が引き伸ばされてしまう。
    """
    h, w = frame_bgr.shape[:2]
    bb = ax_video.bbox
    scale = min(bb.width / max(w, 1), bb.height / max(h, 1), 1.0)  # 拡大はしない
    tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    if (tw, th) != (w, h):
        frame_bgr = cv2.resize(frame_bgr, (tw, th), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    video_img.set_data(rgb)
    if state.get('vshape') != rgb.shape:
        # extent が変わると軸の箱が縦横比に合わせて再配置されるので、背景を取り直す
        video_img.set_extent((0, rgb.shape[1], rgb.shape[0], 0))
        state['vshape'] = rgb.shape
        state['bg'] = None
        state['regions'] = None
    video_title.set_text(f'Video t = {t_sec:.2f} s')


def seek_to_row(idx):
    """行 idx(= その動画時刻)へ動画をシークし、フレーム番号カウンタを合わせて表示更新する。"""
    idx = int(np.clip(idx, 0, n_frames - 1))
    if HAS_VIDEO:
        t = _vts_full[idx]
        if not np.isfinite(t):
            t = T_START
        vf = max(0, int(round(t * video_fps)))
        try:
            video_cap.set(cv2.CAP_PROP_POS_FRAMES, vf)
            ret, frame = video_cap.read()
        except Exception:
            ret, frame = False, None
        if ret and frame is not None:
            # mp4 はキーフレーム単位でずれるため、実際に読めたフレーム番号に合わせる
            actual = int(round(video_cap.get(cv2.CAP_PROP_POS_FRAMES))) - 1
            state['vframe'] = actual if actual >= 0 else vf
            _set_video_image(frame, state['vframe'] / video_fps)
        else:
            state['vframe'] = vf
    full_redraw(idx)


# ------------------------------------------------------------------ #
# 再生タイマー
# ------------------------------------------------------------------ #
# タイマーは「描くべきものがあるか」を細かく確認するためのもので、1 ティック＝1 フレーム
# ではない(何を描くかは経過した実時間から決める)。matplotlib のタイマーはコールバックが
# 終わってから次を予約するため、間隔を動画のフレーム間隔(41.7 ms)にすると
# 「間隔 + 描画時間」が実周期になり、そのぶん再生が遅れてしまう。細かく回して調整する。
if HAS_VIDEO:
    # フレーム間隔(41.7 ms @24fps)より十分細かく回す。matplotlib のタイマーは
    # 「間隔 + コールバックの所要時間」が実周期になるため、間隔が大きいと重い
    # ティックのたびにフレームを落とすことになる。
    _interval_ms = 5
else:
    # CSV が高レート(単一周波数モードでは数十 Hz)でも描画は 30 fps 程度で足りる。
    _interval_ms = max(20, min(33, int(dt_ms / max(args.speed, 1e-6))))

timer = fig.canvas.new_timer(interval=_interval_ms)


def _tick_video():
    """
    再生タイマー本体。表示すべき動画フレームを「実時間」から決める。

    タイマーは 1 ティックごとに 1 枚描くのではなく、経過した実時間に対応する
    フレーム番号まで進める。描画が間に合わないときは途中のフレームを cap.grab() で
    読み飛ばす(デコード位置は連続のままなのでシークは発生しない)。
    こうしないと、1 ティック 1 枚固定では描画時間ぶんだけ再生が遅れ続け、
    映像と実時間がずれてカクついて見える。
    """
    if not state['playing']:
        return
    # いま表示すべきフレーム番号(録画は実時間で一定 FPS なので単純比例)
    target_vf = state['play_vf0'] + int(
        (time.perf_counter() - state['play_t0']) * video_fps)
    if target_vf <= state['vframe']:
        return                              # 次のフレームの時刻にまだ達していない
    # 間に合っていないぶんは grab() で読み飛ばす(retrieve しないので安価)
    while state['vframe'] + 1 < target_vf:
        if not video_cap.grab():
            _stop_play(); return
        state['vframe'] += 1
    ret, frame = video_cap.read()
    if not ret or frame is None:
        _stop_play(); return
    state['vframe'] += 1
    t_vid = state['vframe'] / video_fps     # フレーム番号 -> 実時刻(録画が実時間 FPS なので正確)
    if t_vid > T_END + 1.0 / video_fps:
        _stop_play(); return
    _set_video_image(frame, t_vid)
    idx = row_for_video_time(t_vid)         # その時刻に対応する CSV 行
    now = time.perf_counter()
    # 単一周波数モードでは CSV が動画より速く進む(例 54 Hz > 24 fps)ため、行が変わる
    # たびに全パネルを描き直すと 1 フレームの予算(1/動画FPS)を超えて再生が追いつかない。
    # → 動画は毎フレーム更新し、他のパネルは PANEL_UPDATE_MAX_HZ までに間引く。
    if state['bg'] is None or (idx != state['frame']
                               and now - state['last_panel'] >= PANEL_MIN_INTERVAL):
        state['last_panel'] = now
        full_redraw(idx)                    # マーカー/スミス/動画をまとめて更新
    else:
        _blit_video_only()                  # 動画パネルだけ更新(安価)


def _tick_novideo():
    if not state['playing']:
        return
    # 経過した実時間に対応する行へ進める(描画が間に合わないときは行を飛ばして
    # 実時間再生を保つ)。1 ティック 1 行だと、CSV が高レートのとき描画が間に合わず
    # スローモーション再生になってしまう。
    now = time.perf_counter()
    elapsed = (now - state['play_t0']) * max(args.speed, 1e-6)
    t_target = timestamps[state['play_row0']] + elapsed
    if t_target > timestamps[-1]:
        _stop_play(); return
    nxt = int(np.searchsorted(timestamps, t_target, side='right') - 1)
    nxt = int(np.clip(nxt, 0, n_frames - 1))
    if nxt != state['frame'] or state['bg'] is None:
        full_redraw(nxt)


def _start_play():
    state['playing'] = True
    btn_play.label.set_text('Pause')
    if HAS_VIDEO:
        seek_to_row(state['frame'])         # 現在行の位置へ動画を合わせてから連続再生
    # 実時間再生の基準(この時刻・このフレーム/行から、経過した実時間ぶん進める)
    state['play_t0'] = time.perf_counter()
    state['play_vf0'] = state['vframe']
    state['play_row0'] = state['frame']
    state['last_panel'] = 0.0
    timer.start()


def _stop_play():
    state['playing'] = False
    btn_play.label.set_text('Play')
    timer.stop()
    _blit_all()      # 停止後は再生ループが回らないのでここで表示を更新する


def on_btn(_event):
    (_stop_play if state['playing'] else _start_play)()


def on_slider(val):
    if state['updating']:
        return
    seek_to_row(int(val))


def on_freq_slider(val):
    state['freq'] = val
    update_freq_display()
    fig.canvas.draw_idle()


def on_time_submit(text):
    try:
        idx = int(round(float(text)))
    except ValueError:
        time_box.set_val(str(state['frame']))
        return
    seek_to_row(int(np.clip(idx, 0, n_frames - 1)))


timer.add_callback(_tick_video if HAS_VIDEO else _tick_novideo)
slider.on_changed(on_slider)
btn_play.on_clicked(on_btn)
time_box.on_submit(on_time_submit)
if freq_slider is not None:
    freq_slider.on_changed(on_freq_slider)

if HAS_VIDEO:
    def on_close(_event):
        try:
            video_cap.release()
        except Exception:
            pass
    fig.canvas.mpl_connect('close_event', on_close)

# 初期表示
if HAS_VIDEO:
    seek_to_row(row_for_video_time(T_START))
else:
    full_redraw(0)

plt.show()
