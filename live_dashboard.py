# -*- coding: utf-8 -*-
"""
計測中のライブ・ダッシュボード(別ウィンドウ)
================================================================================

view_marker_impedances/viewer_3marker.py のレイアウトを「計測中のライブ表示」に対応させたもの:
  - 3D マーカー散布 + ボーン(右腕・左腕・体幹) + 左右の肘角度(テキスト + 時系列)
  - 左右 body の S11 スミスチャート
  - webカメラのライブ映像パネル(カメラ使用時のみ)

【最大の狙い】表示を nanoVNA の掃引レートに律速させない。
本ダッシュボードは専用の高速タイマー(interval_ms ごと)で、共有状態から
「最新のマーカー座標・最新のカメラフレーム・各 VNA の最新掃引」を直接読み取って描画する。
コンバイナ(全 VNA が揃うまで待つ CSV 記録用ループ)とは独立して動くため、
OptiTrack とカメラの表示は掃引レートに関係なく滑らかに更新される。

親(計測 GUI)の tkinter mainloop に相乗りする Toplevel として実装する
(別 mainloop を立てないのでイベントループ競合が起きない)。
"""

import time
import collections

import numpy as np

import tkinter as tk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 (projection='3d' 登録のため)


# viewer_3marker.py と同じマーカー定義・表示スタイル・ボーン
MARKER_STYLE = {
    'R_upperarm': dict(marker='o', color='red',         label='R UpperArm'),
    'R_joint':    dict(marker='s', color='darkorange',  label='R Joint'),
    'R_forearm':  dict(marker='^', color='firebrick',   label='R Forearm'),
    'chest':      dict(marker='D', color='dimgray',     label='Chest'),
    'L_upperarm': dict(marker='o', color='blue',        label='L UpperArm'),
    'L_joint':    dict(marker='s', color='deepskyblue', label='L Joint'),
    'L_forearm':  dict(marker='^', color='navy',        label='L Forearm'),
}
MARKER_ORDER = ['R_upperarm', 'R_joint', 'R_forearm', 'chest',
                'L_upperarm', 'L_joint', 'L_forearm']
BONES = [
    ('chest',      'R_upperarm', 'gray'),
    ('R_upperarm', 'R_joint',    'red'),
    ('R_joint',    'R_forearm',  'red'),
    ('chest',      'L_upperarm', 'gray'),
    ('L_upperarm', 'L_joint',    'blue'),
    ('L_joint',    'L_forearm',  'blue'),
]

# スミスチャートのサイド別スタイル(既知の名前は色を固定、未知はパレットから)
SMITH_STYLE = {
    'leftbody':  dict(color='royalblue', label='Left Body'),
    'rightbody': dict(color='crimson',   label='Right Body'),
}
_FALLBACK_COLORS = ['seagreen', 'darkorange', 'purple', 'teal', 'brown']


def _elbow_angle(upper, joint, fore):
    """UpperArm→Joint と Forearm→Joint のなす角[deg]。座標が無効なら NaN。"""
    if upper is None or joint is None or fore is None:
        return float('nan')
    v1 = np.asarray(upper, float) - np.asarray(joint, float)
    v2 = np.asarray(fore, float) - np.asarray(joint, float)
    n1 = np.linalg.norm(v1)
    n2 = np.linalg.norm(v2)
    if n1 < 1e-9 or n2 < 1e-9:
        return float('nan')
    cos_a = float(np.dot(v1, v2) / (n1 * n2))
    return float(np.degrees(np.arccos(np.clip(cos_a, -1.0, 1.0))))


def draw_smith_grid(ax_s):
    """定抵抗円・定リアクタンス円のグリッドと単位円を描く(viewer と同じ)。
    ライブ表示では毎フレーム全体を再描画するため、点数は控えめにして描画負荷を抑える。"""
    theta = np.linspace(0, 2 * np.pi, 240)
    ax_s.plot(np.cos(theta), np.sin(theta), color='black', lw=1.2, zorder=1)
    ax_s.plot([-1, 1], [0, 0], color='gray', lw=0.6, zorder=1)
    x_sweep = np.linspace(-500, 500, 800)
    for r in (0.0, 0.2, 0.5, 1.0, 2.0, 5.0):
        z = r + 1j * x_sweep
        g = (z - 1) / (z + 1)
        ax_s.plot(g.real, g.imag, color='gray', lw=0.5, alpha=0.6, zorder=1)
    r_sweep = np.linspace(0, 500, 800)
    for x in (0.2, 0.5, 1.0, 2.0, 5.0):
        for sign in (1, -1):
            z = r_sweep + 1j * sign * x
            g = (z - 1) / (z + 1)
            ax_s.plot(g.real, g.imag, color='gray', lw=0.4, alpha=0.5, zorder=1)
    ax_s.set_xlim(-1.08, 1.08)
    ax_s.set_ylim(-1.08, 1.08)
    ax_s.set_aspect('equal')
    ax_s.axis('off')


class LiveDashboard:
    """
    計測中に開くライブ表示ウィンドウ(Toplevel)。

    データは以下のコールバックで「その瞬間の最新値」を取得する(掃引レート非依存):
      get_positions() -> {marker_name: (x, y, z, valid)}   (OptiTrack 受信スレッド由来)
      get_sweep(i)    -> s11 の複素 np.ndarray または None  (VNA リーダースレッド由来)
      get_frame()     -> BGR の np.ndarray または None       (カメラスレッド由来)
    """

    def __init__(self, parent, channel_names, freq_grid_hz,
                 get_positions, get_sweep, get_frame=None,
                 has_video=False, interval_ms=50, history_sec=30.0,
                 on_close=None):
        self.parent = parent
        self.channel_names = list(channel_names)
        self.freq_grid_hz = np.asarray(freq_grid_hz, dtype=float)
        self.get_positions = get_positions
        self.get_sweep = get_sweep
        self.get_frame = get_frame
        self.has_video = bool(has_video)
        self.interval_ms = int(interval_ms)
        self.history_sec = float(history_sec)
        self.on_close = on_close

        self._closed = False
        self._after_id = None
        self._t0 = time.perf_counter()
        # 肘角度の時系列履歴(t, R, L)
        self._hist_t = collections.deque()
        self._hist_r = collections.deque()
        self._hist_l = collections.deque()
        # 3D 軸の表示範囲(見えた点を包含するよう広げる)
        self._lim = None  # [xmin,xmax,ymin,ymax,zmin,zmax]

        self.win = tk.Toplevel(parent)
        self.win.title("ライブ表示（3Dマーカー / 肘角度 / スミス / カメラ）")
        self.win.geometry("1360x820")
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        self._build_figure()
        self._schedule()

    # ------------------------------------------------------------------ #
    # 図の構築
    # ------------------------------------------------------------------ #
    def _build_figure(self):
        self.fig = Figure(figsize=(13.6, 8.2), dpi=100)
        self.fig.suptitle("Live: 7 Marker Motion + Impedance Smith Chart",
                          fontsize=13, fontweight='bold')

        # --- 3D ビュー ---
        if self.has_video:
            self.ax3d = self.fig.add_axes([0.02, 0.34, 0.30, 0.60], projection='3d')
            self.ax_video = self.fig.add_axes([0.34, 0.40, 0.30, 0.52])
            self.ax_video.axis('off')
            self._video_img = self.ax_video.imshow(np.zeros((10, 10, 3), dtype=np.uint8))
            self._video_title = self.ax_video.text(
                0.5, 1.02, 'Camera', transform=self.ax_video.transAxes,
                ha='center', va='bottom', fontsize=9, fontweight='bold')
        else:
            self.ax3d = self.fig.add_axes([0.04, 0.34, 0.56, 0.60], projection='3d')
            self.ax_video = None

        self.ax3d.set_xlabel('X'); self.ax3d.set_ylabel('Y'); self.ax3d.set_zlabel('Z')
        self.ax3d.set_xlim(-1, 1); self.ax3d.set_ylim(-1, 1); self.ax3d.set_zlim(-1, 1)

        # 現在フレームのマーカー点
        self._pts = {}
        for name in MARKER_ORDER:
            st = MARKER_STYLE[name]
            pt, = self.ax3d.plot([], [], [], linestyle='None', marker=st['marker'],
                                 color=st['color'], ms=9, label=st['label'], zorder=5)
            self._pts[name] = pt
        # ボーン
        self._bone_lines = [
            self.ax3d.plot([], [], [], '-', color=c, lw=2.2, zorder=4)[0]
            for _, _, c in BONES]
        self.ax3d.legend(loc='upper right', fontsize=7, ncol=2)
        self._time_txt = self.ax3d.text2D(0.02, 0.96, '', transform=self.ax3d.transAxes, fontsize=10)
        self._angle_txt_r = self.ax3d.text2D(0.02, 0.90, '', transform=self.ax3d.transAxes,
                                             fontsize=12, color='crimson', fontweight='bold')
        self._angle_txt_l = self.ax3d.text2D(0.02, 0.84, '', transform=self.ax3d.transAxes,
                                             fontsize=12, color='royalblue', fontweight='bold')

        # --- 肘角度の時系列 ---
        self.ax_ang = self.fig.add_axes([0.05, 0.06, 0.55, 0.20])
        (self._ang_line_r,) = self.ax_ang.plot([], [], color='crimson', lw=1.2, label='R elbow')
        (self._ang_line_l,) = self.ax_ang.plot([], [], color='royalblue', lw=1.2, label='L elbow')
        self.ax_ang.set_ylabel('Elbow (°)', fontsize=8)
        self.ax_ang.set_xlabel('Time (s)', fontsize=8)
        self.ax_ang.set_ylim(0, 180)
        self.ax_ang.set_xlim(0, self.history_sec)
        self.ax_ang.tick_params(labelsize=7)
        self.ax_ang.grid(True, lw=0.4, alpha=0.5)
        self.ax_ang.legend(loc='upper right', fontsize=7, ncol=2)

        # --- スミスチャート(チャンネルぶん) ---
        self._smith_lines = {}
        self._smith_start = {}
        n = len(self.channel_names)
        if self.freq_grid_hz.size:
            fmin = self.freq_grid_hz.min() / 1e6
            fmax = self.freq_grid_hz.max() / 1e6
        else:
            fmin = fmax = float('nan')
        # 縦に n 個並べる(右側 30% 幅)
        for i, name in enumerate(self.channel_names):
            st = SMITH_STYLE.get(name, dict(color=_FALLBACK_COLORS[i % len(_FALLBACK_COLORS)],
                                            label=name))
            h = 0.86 / n
            bottom = 0.06 + (n - 1 - i) * h
            axs = self.fig.add_axes([0.66, bottom + 0.06, 0.30, h - 0.10])
            draw_smith_grid(axs)
            axs.set_title('{}  S11 Smith  ({:.2f}-{:.2f} MHz)'.format(st['label'], fmin, fmax),
                          fontsize=9, color=st['color'], fontweight='bold')
            line, = axs.plot([], [], '-', color=st['color'], lw=1.3, zorder=3)
            start_pt, = axs.plot([], [], marker='o', color=st['color'], ms=5, zorder=4)
            self._smith_lines[i] = line
            self._smith_start[i] = start_pt

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.win)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

    # ------------------------------------------------------------------ #
    # 3D 軸範囲を見えた点を含むよう広げる
    # ------------------------------------------------------------------ #
    def _grow_limits(self, pts_xyz):
        if not pts_xyz:
            return
        arr = np.asarray(pts_xyz, float)
        lo = arr.min(axis=0)
        hi = arr.max(axis=0)
        if self._lim is None:
            self._lim = [lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]]
        else:
            self._lim[0] = min(self._lim[0], lo[0]); self._lim[1] = max(self._lim[1], hi[0])
            self._lim[2] = min(self._lim[2], lo[1]); self._lim[3] = max(self._lim[3], hi[1])
            self._lim[4] = min(self._lim[4], lo[2]); self._lim[5] = max(self._lim[5], hi[2])
        pad = 0.05
        xr = (self._lim[0] - pad, self._lim[1] + pad)
        yr = (self._lim[2] - pad, self._lim[3] + pad)
        zr = (self._lim[4] - pad, self._lim[5] + pad)
        if xr[1] > xr[0]:
            self.ax3d.set_xlim(*xr)
        if yr[1] > yr[0]:
            self.ax3d.set_ylim(*yr)
        if zr[1] > zr[0]:
            self.ax3d.set_zlim(*zr)

    # ------------------------------------------------------------------ #
    # 定期更新(高速タイマー。掃引レートに非依存)
    # ------------------------------------------------------------------ #
    def _schedule(self):
        if self._closed:
            return
        self._after_id = self.win.after(self.interval_ms, self._tick)

    def _tick(self):
        if self._closed:
            return
        try:
            self._update()
            # draw_idle ではなく同期 draw を使う。描画完了後に次を予約することで、
            # 「描画(数百ms)より短い間隔で次ティックが溜まって再入・ビジーループ」になるのを防ぐ。
            self.canvas.draw()
        except Exception:
            # 描画中の一時的な例外で計測を止めない(次のティックで回復)
            pass
        self._schedule()

    def _update(self):
        now = time.perf_counter() - self._t0

        # --- マーカー座標 ---
        snap = self.get_positions() if self.get_positions else {}
        cur = {}
        seen = []
        for name in MARKER_ORDER:
            v = snap.get(name)
            if v is not None and len(v) >= 4 and v[3] and all(np.isfinite(v[:3])):
                cur[name] = (float(v[0]), float(v[1]), float(v[2]))
                seen.append(cur[name])
            else:
                cur[name] = None

        for name, pt in self._pts.items():
            p = cur.get(name)
            if p is None:
                pt.set_data(np.array([]), np.array([]))
                pt.set_3d_properties(np.array([]))
            else:
                pt.set_data(np.array([p[0]]), np.array([p[1]]))
                pt.set_3d_properties(np.array([p[2]]))
        for line, (a, b, _c) in zip(self._bone_lines, BONES):
            pa, pb = cur.get(a), cur.get(b)
            if pa is None or pb is None:
                line.set_data(np.array([]), np.array([]))
                line.set_3d_properties(np.array([]))
            else:
                line.set_data(np.array([pa[0], pb[0]]), np.array([pa[1], pb[1]]))
                line.set_3d_properties(np.array([pa[2], pb[2]]))
        self._grow_limits(seen)

        # --- 肘角度 ---
        ang_r = _elbow_angle(cur.get('R_upperarm'), cur.get('R_joint'), cur.get('R_forearm'))
        ang_l = _elbow_angle(cur.get('L_upperarm'), cur.get('L_joint'), cur.get('L_forearm'))
        self._time_txt.set_text('Time: {:.2f} s'.format(now))
        self._angle_txt_r.set_text('R Elbow: {}'.format(
            '{:.1f}°'.format(ang_r) if np.isfinite(ang_r) else 'N/A'))
        self._angle_txt_l.set_text('L Elbow: {}'.format(
            '{:.1f}°'.format(ang_l) if np.isfinite(ang_l) else 'N/A'))

        self._hist_t.append(now)
        self._hist_r.append(ang_r)
        self._hist_l.append(ang_l)
        while self._hist_t and (now - self._hist_t[0]) > self.history_sec:
            self._hist_t.popleft(); self._hist_r.popleft(); self._hist_l.popleft()
        t_arr = np.fromiter(self._hist_t, float)
        self._ang_line_r.set_data(t_arr, np.fromiter(self._hist_r, float))
        self._ang_line_l.set_data(t_arr, np.fromiter(self._hist_l, float))
        if t_arr.size:
            lo = max(0.0, now - self.history_sec)
            self.ax_ang.set_xlim(lo, max(lo + self.history_sec, now))

        # --- スミスチャート(各チャンネルの最新掃引 Γ=S11) ---
        for i, line in self._smith_lines.items():
            s11 = self.get_sweep(i) if self.get_sweep else None
            if s11 is None:
                continue
            s11 = np.asarray(s11)
            re = s11.real
            im = s11.imag
            mask = np.isfinite(re) & np.isfinite(im)
            line.set_data(re[mask], im[mask])
            if mask.any():
                self._smith_start[i].set_data([re[mask][0]], [im[mask][0]])
            else:
                self._smith_start[i].set_data([], [])

        # --- カメラ映像 ---
        if self.has_video and self.get_frame is not None:
            frame = self.get_frame()
            if frame is not None:
                rgb = frame[:, :, ::-1]  # BGR -> RGB
                self._video_img.set_data(rgb)
                self._video_img.set_extent((0, rgb.shape[1], rgb.shape[0], 0))
                self._video_title.set_text('Camera (live)')
            else:
                self._video_title.set_text('Camera: no frame')

    # ------------------------------------------------------------------ #
    # クローズ
    # ------------------------------------------------------------------ #
    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.win.destroy()
        except Exception:
            pass
        if self.on_close is not None:
            try:
                self.on_close()
            except Exception:
                pass
