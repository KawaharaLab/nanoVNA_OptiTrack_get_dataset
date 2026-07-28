# -*- coding: utf-8 -*-
"""
取得済みデータセット + 動画 再生ビューア(ポストホック)
================================================================================

sync_optitrack_nanovna.py で取得した CSV(7 マーカー座標 + leftbody/rightbody の S11)と、
連動録画した動画(WIN_YYYYMMDD_HH_MM_SS_Pro.mp4)を、時刻同期して再生する GUI。
view_marker_impedances/viewer_3marker.py を土台に、再生エンジンを作り替えている。

表示内容:
  - 3D マーカー散布 + ボーン(右腕/左腕/体幹) + 左右の肘角度(数値 + 時系列)
  - 左右 body の S11 スミスチャート
  - 動画パネル(--video 指定時)

★同期の考え方(重要)★
  マーカー座標とスミスチャートは「同じ CSV 行」から描くので常に同期している。
  一方、動画は VNA(= CSV 行)より遥かに高 FPS。行ごとに動画を video_time_sec へシークすると
  多数の動画フレームを飛ばして「とびとび」になる。これを避けるため、本ビューアは
  【動画のフレームを順番に(飛ばさず)連続再生】し、その時刻に対応する CSV 行を
  video_time_sec から求めてマーカー/スミスを更新する。
    - 動画パネル … 毎フレーム更新(blit で高速描画)。飛ばさないので滑らか。
    - マーカー/スミス/肘角度 … 対応する CSV 行が変わったときだけ全体を再描画
      (VNA レートで段階的に切り替わる)。
  → 「マーカーとスミスは同期・動画はそれに同期しつつ滑らか」を実現する。

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
import argparse
import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import Button, Slider, TextBox
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
args = parser.parse_args()

CSV_PATH = Path(args.csv)
if not CSV_PATH.exists():
    sys.exit(f'[Error] CSV not found: {CSV_PATH}')

MARKERS = [
    ('R_upperarm', 'R_upperarm'),
    ('R_joint',    'R_joint'),
    ('R_forearm',  'R_forearm'),
    ('chest',      'chest'),
    ('L_upperarm', 'L_upperarm'),
    ('L_joint',    'L_joint'),
    ('L_forearm',  'L_forearm'),
]

# ------------------------------------------------------------------ #
# ヘッダーを読み、S11(leftbody/rightbody)列と Z 列、動画同期列を検出
# ------------------------------------------------------------------ #
_header_cols = pd.read_csv(CSV_PATH, nrows=0).columns.tolist()
_s11_re = re.compile(r'^(leftbody|rightbody)_S11_(Real|Imag)_([0-9.]+)$')
_s11_map = {}
for col in _header_cols:
    m = _s11_re.match(col)
    if not m:
        continue
    side, comp, freq_str = m.groups()
    _s11_map.setdefault(side, {}).setdefault(float(freq_str), {})[comp] = col

SIDES = [s for s in ('leftbody', 'rightbody') if s in _s11_map]
HAS_SMITH = len(SIDES) > 0

_z_re = re.compile(r'^(leftbody|rightbody)_Z_(R|X)_([0-9.]+)$')
_z_map = {}
for col in _header_cols:
    m = _z_re.match(col)
    if not m:
        continue
    side, comp, freq_str = m.groups()
    _z_map.setdefault(side, {}).setdefault(float(freq_str), {})[comp] = col

freqs = {}
_real_cols = {}
_imag_cols = {}
_zr_cols = {}
_zx_cols = {}
s11_usecols = []
for side in SIDES:
    freq_list = sorted(f for f, d in _s11_map[side].items() if 'Real' in d and 'Imag' in d)
    freqs[side] = np.array(freq_list)
    _real_cols[side] = [_s11_map[side][f]['Real'] for f in freq_list]
    _imag_cols[side] = [_s11_map[side][f]['Imag'] for f in freq_list]
    s11_usecols += _real_cols[side] + _imag_cols[side]
    z_entry = _z_map.get(side, {})
    _zr_cols[side] = [z_entry.get(f, {}).get('R') for f in freq_list]
    _zx_cols[side] = [z_entry.get(f, {}).get('X') for f in freq_list]
    s11_usecols += [c for c in _zr_cols[side] if c is not None]
    s11_usecols += [c for c in _zx_cols[side] if c is not None]

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
    print(f'[Info] video_time_sec のソース: {src} / 動画FPS: {video_fps:.1f}')
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
# 肘角度を事前計算
# ------------------------------------------------------------------ #
def elbow_angles(upper, joint, fore):
    v1 = upper - joint
    v2 = fore - joint
    cos_a = np.einsum('ij,ij->i', v1, v2) / (
        np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1) + 1e-12)
    return np.degrees(np.arccos(np.clip(cos_a, -1.0, 1.0)))


angles = {
    'R': elbow_angles(pos['R_upperarm'], pos['R_joint'], pos['R_forearm']),
    'L': elbow_angles(pos['L_upperarm'], pos['L_joint'], pos['L_forearm']),
}


def fmt_angle(v):
    return f'{v:.1f}°' if np.isfinite(v) else 'N/A'


# ------------------------------------------------------------------ #
# レイアウト
#   左 : 動画パネル
#   中央上 : 3D マーカービュー / 中央下 : 肘角度の時系列
#   右 : スミスチャート 2 つ(縦積み) / その下 : 周波数スライダー
#   下部(全幅): 時間(フレーム)スライダー ＋ Play / フレーム番号入力
# ------------------------------------------------------------------ #
fig = plt.figure(figsize=(16, 9))
fig.suptitle('Dataset + Video Playback Viewer', fontsize=14, fontweight='bold')

# --- 左: 動画パネル ---
if HAS_VIDEO:
    ax_video = fig.add_axes([0.015, 0.30, 0.31, 0.60])
    ax_video.axis('off')
    video_img = ax_video.imshow(np.zeros((10, 10, 3), dtype=np.uint8), animated=True)
    video_title = ax_video.text(0.5, 1.02, '', transform=ax_video.transAxes,
                                ha='center', va='bottom', fontsize=10, fontweight='bold')
else:
    ax_video = None

# --- 中央上: 3D マーカービュー ---
ax = fig.add_axes([0.35, 0.50, 0.30, 0.44], projection='3d')

# --- 中央下: 肘角度の時系列(3D の真下) ---
ax_ang = fig.add_axes([0.37, 0.29, 0.26, 0.16])
ax_ang.plot(timestamps, angles['R'], color='crimson',   lw=1.2, alpha=0.85, label='R elbow')
ax_ang.plot(timestamps, angles['L'], color='royalblue', lw=1.2, alpha=0.85, label='L elbow')
ax_ang.set_ylabel('Elbow (°)', fontsize=8)
ax_ang.set_xlabel('Time (s)', fontsize=8)
ax_ang.set_xlim(timestamps[0], timestamps[-1])
all_angles = np.concatenate([angles['R'], angles['L']])
if np.any(np.isfinite(all_angles)):
    ax_ang.set_ylim(max(0, np.nanmin(all_angles) - 5), min(180, np.nanmax(all_angles) + 5))
ax_ang.tick_params(labelsize=7)
ax_ang.grid(True, lw=0.4, alpha=0.5)
ax_ang.legend(loc='upper right', fontsize=7, ncol=2)
vline = ax_ang.axvline(timestamps[0], color='black', lw=1.2, alpha=0.7)

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
ax.set_xlabel('X'); ax.set_ylabel('Y'); ax.set_zlabel('Z')

MARKER_STYLE = {
    'R_upperarm': dict(marker='o', color='red',         trail='salmon',     label='R UpperArm'),
    'R_joint':    dict(marker='s', color='darkorange',  trail='moccasin',   label='R Joint'),
    'R_forearm':  dict(marker='^', color='firebrick',   trail='lightcoral', label='R Forearm'),
    'chest':      dict(marker='D', color='dimgray',     trail='lightgray',  label='Chest'),
    'L_upperarm': dict(marker='o', color='blue',        trail='skyblue',    label='L UpperArm'),
    'L_joint':    dict(marker='s', color='deepskyblue', trail='powderblue', label='L Joint'),
    'L_forearm':  dict(marker='^', color='navy',        trail='lightsteelblue', label='L Forearm'),
}
for name, _ in MARKERS:
    st = MARKER_STYLE[name]
    ax.plot(*pos[name].T, color=st['trail'], alpha=0.35, lw=0.8, zorder=1)

pts = {}
for name, _ in MARKERS:
    st = MARKER_STYLE[name]
    pt, = ax.plot([], [], [], linestyle='None', marker=st['marker'], color=st['color'],
                  ms=9, label=st['label'], zorder=5)
    pts[name] = pt

BONES = [
    ('chest',      'R_upperarm', 'gray'),
    ('R_upperarm', 'R_joint',    'red'),
    ('R_joint',    'R_forearm',  'red'),
    ('chest',      'L_upperarm', 'gray'),
    ('L_upperarm', 'L_joint',    'blue'),
    ('L_joint',    'L_forearm',  'blue'),
]
bone_lines = [ax.plot([], [], [], '-', color=c, lw=2.2, zorder=4)[0] for _, _, c in BONES]

ax.legend(loc='upper right', fontsize=7, ncol=2)
time_txt    = ax.text2D(0.02, 0.96, '', transform=ax.transAxes, fontsize=10)
angle_txt_r = ax.text2D(0.02, 0.90, '', transform=ax.transAxes, fontsize=12,
                        color='crimson', fontweight='bold')
angle_txt_l = ax.text2D(0.02, 0.84, '', transform=ax.transAxes, fontsize=12,
                        color='royalblue', fontweight='bold')

# ------------------------------------------------------------------ #
# スミスチャート(右: 縦積み 2 つ)
# ------------------------------------------------------------------ #
SMITH_STYLE = {
    'leftbody':  dict(color='royalblue', label='Left Body'),
    'rightbody': dict(color='crimson',   label='Right Body'),
}
# 右カラムに 2 つ縦積み。タイトルは各チャート上(1 行)、Z 読み出しは各チャート下に置く。
# 1 つ目の Z テキスト(ztext_y)と 2 つ目のタイトル(title_y)が重ならないよう間隔を確保する。
SMITH_BLOCK = {
    'leftbody':  dict(title_y=0.950, rect=[0.685, 0.655, 0.29, 0.25], ztext_y=0.638),
    'rightbody': dict(title_y=0.575, rect=[0.685, 0.285, 0.29, 0.25], ztext_y=0.268),
}
_SMITH_CX = 0.685 + 0.29 / 2.0  # スミスチャート中心の figure x 座標(タイトル/Z テキスト用)


def draw_smith_grid(ax_s):
    theta = np.linspace(0, 2 * np.pi, 240)
    ax_s.plot(np.cos(theta), np.sin(theta), color='black', lw=1.2, zorder=1)
    ax_s.plot([-1, 1], [0, 0], color='gray', lw=0.6, zorder=1)
    x_sweep = np.linspace(-500, 500, 1500)
    for r in (0.0, 0.2, 0.5, 1.0, 2.0, 5.0):
        z = r + 1j * x_sweep
        g = (z - 1) / (z + 1)
        ax_s.plot(g.real, g.imag, color='gray', lw=0.5, alpha=0.6, zorder=1)
    r_sweep = np.linspace(0, 500, 1500)
    for x in (0.2, 0.5, 1.0, 2.0, 5.0):
        for sign in (1, -1):
            z = r_sweep + 1j * sign * x
            g = (z - 1) / (z + 1)
            ax_s.plot(g.real, g.imag, color='gray', lw=0.4, alpha=0.5, zorder=1)
    ax_s.set_xlim(-1.08, 1.08)
    ax_s.set_ylim(-1.08, 1.08)
    ax_s.set_aspect('equal')
    ax_s.axis('off')


smith_lines = {}
smith_start_pts = {}
freq_marker_pts = {}
freq_txt = {}
if HAS_SMITH:
    for side in SIDES:
        st = SMITH_STYLE[side]
        blk = SMITH_BLOCK[side]
        axs = fig.add_axes(blk['rect'])
        draw_smith_grid(axs)
        fmin, fmax, npts = freqs[side].min(), freqs[side].max(), len(freqs[side])
        # タイトルは 1 行に収める(2 行にすると下段チャートのタイトルと重なりやすいため)
        fig.text(_SMITH_CX, blk['title_y'],
                 f'{st["label"]} S11  {fmin:.1f}-{fmax:.1f} MHz ({npts}pt)',
                 fontsize=9.5, fontweight='bold', color=st['color'], ha='center', va='top')
        line, = axs.plot([], [], '-', color=st['color'], lw=1.3, zorder=3)
        start_pt, = axs.plot([], [], marker='o', color=st['color'], ms=5, zorder=4)
        marker_pt, = axs.plot([], [], marker='*', color='gold', ms=16,
                              markeredgecolor=st['color'], markeredgewidth=1.3, zorder=6)
        txt = fig.text(_SMITH_CX, blk['ztext_y'], '', fontsize=9.5,
                       ha='center', va='top', color=st['color'], fontweight='bold')
        smith_lines[side] = line
        smith_start_pts[side] = start_pt
        freq_marker_pts[side] = marker_pt
        freq_txt[side] = txt

# ------------------------------------------------------------------ #
# ウィジェット
#   時間(フレーム)スライダー: 下部に全幅で配置(動画・3D・スミスの下)
#   周波数スライダー: スミスチャート 2 つの下(右側)に配置
# ------------------------------------------------------------------ #
ax_slider = fig.add_axes([0.10, 0.115, 0.84, 0.025])
slider = Slider(ax_slider, 'Time', 0, n_frames - 1,
                valinit=0, valstep=1, color='steelblue')

ax_btn = fig.add_axes([0.06, 0.035, 0.10, 0.05])
btn_play = Button(ax_btn, 'Play', color='0.85', hovercolor='0.70')

ax_time_box = fig.add_axes([0.27, 0.04, 0.10, 0.035])
time_box = TextBox(ax_time_box, f'Frame(0-{n_frames - 1}) ', initial='0')

state = {'playing': False, 'frame': 0, 'updating': False, 'freq': None,
         'bg': None, 'vframe': 0}

DEFAULT_FREQ_MHZ = 13.56
freq_slider = None
if HAS_SMITH:
    _all_freqs = np.concatenate([freqs[side] for side in SIDES])
    _fmin, _fmax = float(_all_freqs.min()), float(_all_freqs.max())
    _fstep = float(np.min([np.diff(freqs[side]).min() for side in SIDES if len(freqs[side]) > 1] or [0.01]))
    _finit = float(np.clip(DEFAULT_FREQ_MHZ, _fmin, _fmax))
    ax_freq = fig.add_axes([0.70, 0.215, 0.26, 0.02])
    freq_slider = Slider(ax_freq, 'Freq (MHz)', _fmin, _fmax,
                         valinit=_finit, valstep=_fstep, color='seagreen', valfmt='%.2f')
    state['freq'] = _finit


def update_freq_display():
    if not HAS_SMITH or state['freq'] is None:
        return
    idx = state['frame']
    target = state['freq']
    for side in SIDES:
        fi = nearest_freq_idx(side, target)
        actual_f = freqs[side][fi]
        gr = gamma_real[side][idx, fi]
        gi = gamma_imag[side][idx, fi]
        zr = z_real[side][idx, fi]
        zx = z_react[side][idx, fi]
        if np.isfinite(gr) and np.isfinite(gi):
            freq_marker_pts[side].set_data(np.array([gr]), np.array([gi]))
        else:
            freq_marker_pts[side].set_data(np.array([]), np.array([]))
        if np.isfinite(zr) and np.isfinite(zx):
            sign = '+' if zx >= 0 else '-'
            freq_txt[side].set_text(f'@{actual_f:.2f} MHz: Z = {zr:.1f} {sign} j{abs(zx):.1f} Ω')
        else:
            freq_txt[side].set_text(f'@{actual_f:.2f} MHz: Z = N/A')


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
    angle_txt_r.set_text(f'R Elbow: {fmt_angle(angles["R"][idx])}')
    angle_txt_l.set_text(f'L Elbow: {fmt_angle(angles["L"][idx])}')
    vline.set_xdata([timestamps[idx], timestamps[idx]])
    if not state['updating']:
        state['updating'] = True
        slider.set_val(idx)
        state['updating'] = False
    time_box.set_val(str(idx))
    if HAS_SMITH:
        for side in SIDES:
            re_vals = gamma_real[side][idx]
            im_vals = gamma_imag[side][idx]
            mask = np.isfinite(re_vals) & np.isfinite(im_vals)
            smith_lines[side].set_data(re_vals[mask], im_vals[mask])
            if mask.any():
                smith_start_pts[side].set_data([re_vals[mask][0]], [im_vals[mask][0]])
            else:
                smith_start_pts[side].set_data([], [])
        update_freq_display()


def full_redraw(idx):
    """マーカー/スミス/角度を更新して全体を再描画し、動画 blit 用の背景を取り直す。"""
    draw_markers_smith(idx)
    fig.canvas.draw()
    if HAS_VIDEO:
        state['bg'] = fig.canvas.copy_from_bbox(ax_video.bbox)


def blit_video():
    """動画パネルだけを高速に更新(全体再描画しない)。"""
    if state['bg'] is None:
        return
    fig.canvas.restore_region(state['bg'])
    ax_video.draw_artist(video_img)
    fig.canvas.blit(ax_video.bbox)
    fig.canvas.flush_events()


# ------------------------------------------------------------------ #
# 動画: フレーム番号で駆動(フレームを飛ばさず順次読み。時刻ずれが累積しない)
#   録画は「実時間で一定 FPS」で書かれているため、フレーム番号 k の実時刻 = k / video_fps。
#   これを video_time_sec(= WallClock - 録画開始 = 実経過時間)と突き合わせて行を選ぶので、
#   宣言 FPS の誤差や POS_MSEC のクセに依存せず、映像とマーカー/スミスが同期し続ける。
# ------------------------------------------------------------------ #
if HAS_VIDEO:
    import cv2


def _set_video_image(frame_bgr, t_sec):
    rgb = frame_bgr[:, :, ::-1]
    video_img.set_data(rgb)
    video_img.set_extent((0, rgb.shape[1], rgb.shape[0], 0))
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
if HAS_VIDEO:
    _interval_ms = max(5, int(1000.0 / video_fps))
else:
    _interval_ms = max(5, int(dt_ms / max(args.speed, 1e-6)))

timer = fig.canvas.new_timer(interval=_interval_ms)


def _tick_video():
    if not state['playing']:
        return
    ret, frame = video_cap.read()          # 次のフレームを 1 枚だけ読む(飛ばさない)
    if not ret or frame is None:
        _stop_play(); return
    state['vframe'] += 1
    t_vid = state['vframe'] / video_fps     # フレーム番号 -> 実時刻(録画が実時間 FPS なので正確)
    if t_vid > T_END + 1.0 / video_fps:
        _stop_play(); return
    _set_video_image(frame, t_vid)
    idx = row_for_video_time(t_vid)         # その時刻に対応する CSV 行
    if idx != state['frame'] or state['bg'] is None:
        full_redraw(idx)                    # 行が変わった: マーカー/スミス更新 + 背景取り直し
    else:
        blit_video()                        # 同じ行: 動画だけ高速更新


def _tick_novideo():
    if not state['playing']:
        return
    nxt = state['frame'] + 1
    if nxt >= n_frames:
        _stop_play(); return
    full_redraw(nxt)


def _start_play():
    state['playing'] = True
    btn_play.label.set_text('Pause')
    if HAS_VIDEO:
        seek_to_row(state['frame'])         # 現在行の位置へ動画を合わせてから連続再生
    timer.start()


def _stop_play():
    state['playing'] = False
    btn_play.label.set_text('Play')
    timer.stop()


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
