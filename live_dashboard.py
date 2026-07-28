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

        # blit 描画(遅延対策):
        #   マーカー/ボーン/角度線/スミス線/カメラ映像を animated=True にし、静的背景(3D箱・
        #   スミス格子・軸)を 1 度だけ描いてキャッシュ。毎ティックは背景復元 + draw_artist + blit
        #   だけで済ませ、重い全体再描画(~0.24s)を「3D範囲拡大/回転/リサイズ時のみ」に限定する。
        #   → カメラ映像もマーカーもスミスも ~30fps で滑らかに、低遅延で更新される。
        self._blit_bg = None            # 静的背景のキャッシュ(draw_event で取り直す)
        self._fast_ms = max(15, min(self.interval_ms, 33))  # 更新間隔(~30fps)
        self._video_shape = None        # 直近フレーム形状(extent 再設定の判定用)

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
            self._video_img = self.ax_video.imshow(np.zeros((10, 10, 3), dtype=np.uint8),
                                                   animated=True)
            self._video_title = self.ax_video.text(
                0.5, 1.02, 'Camera', transform=self.ax_video.transAxes,
                ha='center', va='bottom', fontsize=9, fontweight='bold')
        else:
            self.ax3d = self.fig.add_axes([0.04, 0.34, 0.56, 0.60], projection='3d')
            self.ax_video = None

        self.ax3d.set_xlabel('X'); self.ax3d.set_ylabel('Y'); self.ax3d.set_zlabel('Z')
        self.ax3d.set_xlim(-1, 1); self.ax3d.set_ylim(-1, 1); self.ax3d.set_zlim(-1, 1)

        # 現在フレームのマーカー点(animated=True: blit で毎ティック描画する)
        self._pts = {}
        for name in MARKER_ORDER:
            st = MARKER_STYLE[name]
            pt, = self.ax3d.plot([], [], [], linestyle='None', marker=st['marker'],
                                 color=st['color'], ms=9, label=st['label'], zorder=5,
                                 animated=True)
            self._pts[name] = pt
        # ボーン
        self._bone_lines = [
            self.ax3d.plot([], [], [], '-', color=c, lw=2.2, zorder=4, animated=True)[0]
            for _, _, c in BONES]
        self.ax3d.legend(loc='upper right', fontsize=7, ncol=2)
        self._time_txt = self.ax3d.text2D(0.02, 0.96, '', transform=self.ax3d.transAxes,
                                          fontsize=10, animated=True)
        self._angle_txt_r = self.ax3d.text2D(0.02, 0.90, '', transform=self.ax3d.transAxes,
                                             fontsize=12, color='crimson', fontweight='bold',
                                             animated=True)
        self._angle_txt_l = self.ax3d.text2D(0.02, 0.84, '', transform=self.ax3d.transAxes,
                                             fontsize=12, color='royalblue', fontweight='bold',
                                             animated=True)

        # --- 肘角度の時系列 ---
        # x 軸は「現在からの相対秒(-history_sec〜0)」で固定する。データ側を (t-now) で流すことで
        # 軸スクロールによる全体再描画を避け、線だけ animated=True で blit する。
        self.ax_ang = self.fig.add_axes([0.05, 0.06, 0.55, 0.20])
        (self._ang_line_r,) = self.ax_ang.plot([], [], color='crimson', lw=1.2,
                                               label='R elbow', animated=True)
        (self._ang_line_l,) = self.ax_ang.plot([], [], color='royalblue', lw=1.2,
                                               label='L elbow', animated=True)
        self.ax_ang.set_ylabel('Elbow (deg)', fontsize=8)
        self.ax_ang.set_xlabel('Time (s, now=0)', fontsize=8)
        self.ax_ang.set_ylim(0, 180)
        self.ax_ang.set_xlim(-self.history_sec, 0)
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
            line, = axs.plot([], [], '-', color=st['color'], lw=1.3, zorder=3, animated=True)
            start_pt, = axs.plot([], [], marker='o', color=st['color'], ms=5, zorder=4,
                                 animated=True)
            self._smith_lines[i] = line
            self._smith_start[i] = start_pt

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.win)
        self._tkwidget = self.canvas.get_tk_widget()
        self._tkwidget.pack(fill="both", expand=True)

        # blit で毎ティック描画する animated アーティストの一覧(描画順=下→上)
        self._anim = list(self._pts.values()) + list(self._bone_lines)
        for i in sorted(self._smith_lines):
            self._anim.append(self._smith_lines[i])
            self._anim.append(self._smith_start[i])
        self._anim += [self._ang_line_r, self._ang_line_l,
                       self._time_txt, self._angle_txt_r, self._angle_txt_l]
        if self.has_video:
            self._anim.append(self._video_img)

        # 全体描画が起きるたび(初回/範囲拡大/回転/リサイズ)に静的背景を取り直す。
        self.canvas.mpl_connect('draw_event', self._on_draw)
        self.canvas.draw()

    # ------------------------------------------------------------------ #
    # 3D 軸範囲を見えた点を含むよう広げる
    # ------------------------------------------------------------------ #
    def _grow_limits(self, pts_xyz):
        """見えた点を含むよう 3D 範囲を広げる。範囲が実際に変わったら True を返す
        (True のときだけ背景の取り直し=全体再描画が必要になる)。"""
        if not pts_xyz:
            return False
        arr = np.asarray(pts_xyz, float)
        lo = arr.min(axis=0)
        hi = arr.max(axis=0)
        new = [float(lo[0]), float(hi[0]), float(lo[1]), float(hi[1]), float(lo[2]), float(hi[2])]
        if self._lim is not None:
            new = [min(self._lim[0], new[0]), max(self._lim[1], new[1]),
                   min(self._lim[2], new[2]), max(self._lim[3], new[3]),
                   min(self._lim[4], new[4]), max(self._lim[5], new[5])]
        if self._lim == new:
            return False
        self._lim = new
        pad = 0.05
        if new[1] + pad > new[0] - pad:
            self.ax3d.set_xlim(new[0] - pad, new[1] + pad)
        if new[3] + pad > new[2] - pad:
            self.ax3d.set_ylim(new[2] - pad, new[3] + pad)
        if new[5] + pad > new[4] - pad:
            self.ax3d.set_zlim(new[4] - pad, new[5] + pad)
        return True

    # ------------------------------------------------------------------ #
    # 定期更新(高速タイマー。掃引レートに非依存)
    # ------------------------------------------------------------------ #
    def _schedule(self, delay_ms=None):
        if self._closed:
            return
        if delay_ms is None:
            delay_ms = self._fast_ms
        self._after_id = self.win.after(int(delay_ms), self._tick)

    def _on_draw(self, _event):
        """全体描画のたびに静的背景を保存する(animated アーティストは含まれない)。"""
        try:
            self._blit_bg = self.canvas.copy_from_bbox(self.fig.bbox)
        except Exception:
            self._blit_bg = None

    def _tick(self):
        if self._closed:
            return
        t0 = time.perf_counter()
        try:
            grew = self._refresh()   # 最新値を各 animated アーティストへ(set_data。安価)
            # 背景が無い/3D範囲や映像サイズが変わったときだけ重い全体再描画で背景を取り直す。
            if self._blit_bg is None or grew:
                self.canvas.draw()   # -> draw_event -> _on_draw が背景を保存
            # 毎ティック: 背景復元 + animated アーティストのみ描画 + 各軸領域を blit(低コスト)。
            if self._blit_bg is not None:
                self.canvas.restore_region(self._blit_bg)
                axes_seen = []
                for art in self._anim:
                    ax = art.axes
                    try:
                        ax.draw_artist(art)
                    except Exception:
                        pass
                    if ax not in axes_seen:
                        axes_seen.append(ax)
                for ax in axes_seen:
                    self.canvas.blit(ax.bbox)
                # blit 結果を画面へ反映する。flush_events(~19ms・入力処理も走り重い)ではなく
                # update_idletasks(再描画のみ・入力は処理しない=軽く再入もしない)を使う。
                try:
                    self._tkwidget.update_idletasks()
                except Exception:
                    pass
        except Exception:
            # 描画中の一時的な例外で計測を止めない(次のティックで回復)
            pass
        # 次ティックを適応的に予約する: 1 フレーム(_fast_ms)の残り時間だけ待つ。
        # 描画が _fast_ms を超えても最低 5ms は空けて他のイベント処理に譲る(ビジーループ防止)。
        work_ms = (time.perf_counter() - t0) * 1000.0
        self._schedule(max(5, self._fast_ms - work_ms))

    def _refresh(self):
        """最新の共有状態を各 animated アーティストへ反映する。3D 範囲が広がったら True。"""
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
        grew = self._grow_limits(seen)

        # --- 肘角度(テキスト + 相対時系列) ---
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
        # x 軸は固定(-history_sec〜0)。データを (t-now) で流すので軸再描画は不要。
        t_arr = np.fromiter(self._hist_t, float) - now
        self._ang_line_r.set_data(t_arr, np.fromiter(self._hist_r, float))
        self._ang_line_l.set_data(t_arr, np.fromiter(self._hist_l, float))

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
                if self._video_shape != rgb.shape:
                    self._video_img.set_extent((0, rgb.shape[1], rgb.shape[0], 0))
                    self._video_shape = rgb.shape
                    self._video_title.set_text('Camera (live)')
                    grew = True   # extent 変更は背景の取り直しが必要
        return grew

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
