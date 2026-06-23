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

VNA 接続(COM ポート):
  計測する S パラメータは S11 固定。VNA を接続する COM ポートは GUI のドロップダウンで
  選択する。「ポート再検索」ボタンで一覧を更新でき、後から VNA を挿しても再起動不要。
  選択ポートが開けない(存在しない/他ソフトが占有)場合は、クラッシュさせず
  messagebox.showerror で警告を表示する。

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
import collections
from datetime import datetime

import serial  # pip install pyserial
from serial.tools import list_ports  # 利用可能な COM ポートの列挙に使用

import numpy as np          # pip install numpy
import skrf as rf           # pip install scikit-rf (S パラメータ -> インピーダンス変換)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib            # pip install matplotlib
matplotlib.use("TkAgg")      # tkinter への埋め込みバックエンド
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


# =============================================================================
# コンソール出力ポリシー
# =============================================================================
# 計測中の細かいログはコンソールに出さず GUI ログに集約する。
# CONSOLE_VERBOSE=True にすると、補助的な [INFO]/自動判別結果などもコンソールに出す。
# (重要イベント[計測開始/保存完了/エラー]とエラー詳細は常にコンソールへ出力する)
CONSOLE_VERBOSE = False


def _dbg(*args):
    """補助ログ。CONSOLE_VERBOSE のときのみコンソール出力する。"""
    if CONSOLE_VERBOSE:
        print(*args)


# =============================================================================
# 設定（ここを環境に合わせて変更してください）
# =============================================================================

# --- JNCRadio VNA 3G (シリアル) ---
NANOVNA_PORT   = "COM3"        # Windows のデバイスマネージャーで確認した COM 番号
NANOVNA_BAUD   = 115200        # ボーレート(JNCRadio VNA 3G は 115200 を推奨)
Z0             = 50.0          # 特性インピーダンス [Ω]

# --- 掃引(スイープ)範囲の既定値 ---
# 実際の掃引条件は GUI の入力欄(開始/終了周波数・点数)で変更でき、[計測開始]時に
# その値が VNA に設定される。下記はその「GUI 初期値 兼 既定値」。
# 一定範囲を掃引して 101 点分の複素 S11 を一括取得し、服(メアンダコイル)を動かした
# ときに「どの帯域で整合がずれるか」を Z11 のスイープとして記録・可視化する。
SWEEP_START_HZ = 12_500_000    # 掃引開始 [Hz] (12.5 MHz)
SWEEP_STOP_HZ  = 14_500_000    # 掃引終了 [Hz] (14.5 MHz)
SCAN_POINTS    = 101           # 掃引点数(GUI 初期値)
# GUI の点数スピンボックスの範囲(1 刻みで任意指定可能)
POINTS_MIN = 1                 # 最小点数(1 = 単一周波数ピンポイント測定)
POINTS_MAX = 100_000           # 上限(実用上の安全上限。実機の制約に応じて調整)

# 単一周波数(1点)モードでデバイスへ実際に投げる scan の点数。
# 点数 1 の縮退 scan はファームによっては不安定なため、同一周波数を数点掃引して
# 平均し、論理的には 1 点として扱う(start==stop の極小掃引)。
SINGLE_DEVICE_POINTS = 2

# 単一周波数モードのリアルタイムグラフ(時系列)で保持する直近サンプル数。
TIME_PLOT_WINDOW = 300

# 掃引周波数グリッド[Hz]を作るヘルパ。CSV ヘッダー・skrf 変換・グラフ横軸の共通基準。
def make_freq_grid(start_hz, stop_hz, points):
    return np.linspace(float(start_hz), float(stop_hz), int(points))


# 既定グリッド(初期グラフ描画などに使用)
FREQ_GRID_HZ = make_freq_grid(SWEEP_START_HZ, SWEEP_STOP_HZ, SCAN_POINTS)

# 参考用の中心周波数(グラフの縦線マーカーに使用)。
TARGET_FREQ_HZ = 13_560_000    # 13.56 MHz

# --- 計測する S パラメータ(S11 固定) ---
# 本機(NanoVNA 系)のコンソール `scan {start} {stop} {pts} {outmask}` は outmask の
# ビットで出力内容を選ぶ。S11(ポート1の反射)は bit1(=2)。
# 本システムは S11 固定で計測する。
VNA_SPARAM  = "S11"
VNA_OUTMASK = 2

# --- VNA 未接続テストモード(OptiTrack 単体テスト用) ---
# COM ポート一覧に「None(VNAなしテストモード)」を追加する。これを選んで計測開始すると、
# VNA の接続・初期化を一切スキップし、OptiTrack の座標だけを取得する。
# VNA 由来のカラム(S11_Real, S11_Imag, Z_R, Z_X)にはダミー値 0 を入れて同期させる。
NO_VNA_DISPLAY = "None（VNAなしテストモード）"  # ドロップダウンの表示名
TEST_MODE = "__TEST_NO_VNA__"                  # get_selected_port が返す内部センチネル
TEST_MODE_INTERVAL_SEC = 0.1                    # テストモードの1サンプル間隔[秒](律速が無いため)

# 使用する COM ポートは GUI のドロップダウンで選択する(下記は初期選択の候補)。
# 起動時にこの値が一覧に存在すれば初期選択される。無ければ一覧の先頭を選ぶ。

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

# 位置情報の先頭 10 列(タイムスタンプ + 3 部位 × XYZ)
_POSITION_HEADER = ["Timestamp",
                    "UpperArm_X", "UpperArm_Y", "UpperArm_Z",
                    "Joint_X",    "Joint_Y",    "Joint_Z",
                    "Forearm_X",  "Forearm_Y",  "Forearm_Z"]


def _fmt_mhz(hz):
    """周波数[Hz]を CSV 列名用の MHz 表記(末尾ゼロを省いた短い形)にする。例: 12.5, 12.51, 12.505"""
    # 点数が多い(刻みが細かい)グリッドでも列名が重複しないよう 4 桁(=100Hz)精度で表記
    s = "{:.4f}".format(hz / 1e6)
    return s.rstrip("0").rstrip(".")


def build_csv_header(freq_grid_hz):
    """
    位置情報 + S パラメータ/インピーダンス列を並べた CSV ヘッダーを作る。
    - 単一周波数(1 点)のとき: 周波数サフィックスを付けないシンプルな
      [..., S11_Real, S11_Imag, Z_R, Z_X] にフォールバックする(test2.csv 互換)。
    - 2 点以上のとき: 各掃引周波数ごとに 4 列(S11_Real_<MHz> ...)を動的生成する。
    """
    cols = list(_POSITION_HEADER)
    if len(freq_grid_hz) <= 1:
        cols += ["S11_Real", "S11_Imag", "Z_R", "Z_X"]
    else:
        for hz in freq_grid_hz:
            lbl = _fmt_mhz(hz)
            cols += ["S11_Real_{}".format(lbl), "S11_Imag_{}".format(lbl),
                     "Z_R_{}".format(lbl), "Z_X_{}".format(lbl)]
    return cols


# 既定ヘッダー(参考。実際は計測条件に応じてコントローラが動的生成する)
CSV_HEADER = build_csv_header(FREQ_GRID_HZ)


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
            _dbg("[WARN] 有効な対象が {} 個あります(期待値 {})。"
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
    _dbg("[INFO] 自動判別 完了(開始時の高さ {} 軸で降順):".format(axis_name))
    for part, (oid, pos) in zip(("UpperArm", "Joint", "Forearm"), ordered):
        _dbg("       {:8s} <- id={}  (高さ {}={:.3f})".format(
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

# --- FPS 計測用の累計カウンタ ---
# OptiTrack: NatNet コールバック(受信スレッド)で 1 フレームごとに +1
# NanoVNA  : 計測ワーカーで 1 サンプル(1 掃引)ごとに +1
# どちらも reset_runtime_state() で 0 にリセットする。
_fps_lock = threading.Lock()
_opti_frame_count = [0]   # OptiTrack 受信フレーム累計
_vna_sample_count = [0]   # NanoVNA 取得サンプル累計


def incr_opti_frame():
    with _fps_lock:
        _opti_frame_count[0] += 1


def incr_vna_sample():
    with _fps_lock:
        _vna_sample_count[0] += 1


def read_fps_counters():
    """(OptiTrack 累計, NanoVNA 累計) を返す。"""
    with _fps_lock:
        return _opti_frame_count[0], _vna_sample_count[0]


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
    with _fps_lock:
        _opti_frame_count[0] = 0
        _vna_sample_count[0] = 0


# =============================================================================
# インピーダンス変換 (scikit-rf)
# =============================================================================

def s11_sweep_to_z(s11_array, freqs_hz=FREQ_GRID_HZ, z0=Z0):
    """
    掃引した複素 S11 配列(101点)を scikit-rf の Network に変換し、
    50Ω 基準のインピーダンス Z11 を求めて (Z_R配列, Z_X配列) を返す。

    skrf の Network は s を (周波数点数, ポート数, ポート数) で持つため、
    1 ポート(S11 のみ)では (N, 1, 1) に整形する。ntwk.z[:, 0, 0] が Z11。
    """
    s = np.asarray(s11_array, dtype=complex).reshape(-1, 1, 1)
    freq = rf.Frequency.from_f(np.asarray(freqs_hz, dtype=float), unit="Hz")
    ntwk = rf.Network(frequency=freq, s=s, z0=z0)
    z11 = ntwk.z[:, 0, 0]
    return z11.real, z11.imag


# =============================================================================
# JNCRadio VNA 3G シリアル制御
# =============================================================================

def list_serial_ports():
    """
    PC が認識しているシリアル(COM)ポートを列挙して返す。
    戻り値: [(device, description), ...] を device 名でソートしたもの。
            例: [("COM3", "USB Serial Device (COM3)"), ...]
    取得に失敗した場合は空リストを返す(GUI 側で「ポートなし」を表示)。
    """
    try:
        ports = list(list_ports.comports())
    except Exception:
        return []
    out = [(p.device, (p.description or "")) for p in ports]
    out.sort(key=lambda t: t[0])
    return out


class NanoVNA:
    """
    JNCRadio VNA 3G (NanoVNA 互換) のコンソールコマンドを扱う薄いラッパ。

    本機のシリアルは「コマンド\\r を送ると、エコー → 結果行 → プロンプト 'ch> '」
    の順で応答する(マニュアル §7)。scan コマンドで毎回フレッシュな掃引を実行する。

        scan {start(Hz)} {stop(Hz)} [points] [outmask]
        outmask は出力内容のビット指定: bit0(=1) 周波数, bit1(=2) S11, bit2(=4) S21
        本クラスは「周波数 + S11」を出力(outmask=3)する。

    掃引条件(開始/終了周波数・点数)は GUI から渡され、初期化時に sweep コマンドで
    デバイスへ設定する。
    """

    PROMPT = b"ch> "

    def __init__(self, port, baud=115200, timeout=2.0,
                 start_hz=SWEEP_START_HZ, stop_hz=SWEEP_STOP_HZ,
                 points=SCAN_POINTS):
        # 計測は S11 固定。scan には周波数ビット(1)を足して "周波数+S11"(=3)を出力させる。
        self.sparam = VNA_SPARAM
        self.scan_outmask = (1 | VNA_OUTMASK)  # 1(周波数) | 2(S11) = 3
        # 掃引条件(GUI で指定された値)
        self.start_hz = int(start_hz)
        self.stop_hz = int(stop_hz)
        self.points = int(points)
        # 単一周波数(ピンポイント)モード判定: 点数 1 または 開始==終了
        self.single = (self.points <= 1) or (self.start_hz == self.stop_hz)
        if self.single:
            # 論理点数は 1、周波数は開始値に揃える
            self.points = 1
            self.stop_hz = self.start_hz
        self.ser = serial.Serial(port, baud, timeout=timeout)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # 連続出力を止めてプロンプト状態を確定させる
        self._send_raw("pause")
        self._read_until_prompt()
        # 掃引範囲・点数をデバイスへ設定(初期化コマンド)
        self.setup_sweep()

    def setup_sweep(self):
        """
        デバイスの掃引条件(開始/終了周波数・点数)を設定する。
        NanoVNA 系コンソールの `sweep {start} {stop} {points}` を発行する。
        単一周波数モードでは縮退(1点)を避け、開始==終了で SINGLE_DEVICE_POINTS 点に設定する。
        ファームが当該書式に対応していなくても、毎回の scan が範囲を指定するため
        計測自体は継続できる。
        """
        if self.single:
            dev_stop, dev_points = self.start_hz, SINGLE_DEVICE_POINTS
        else:
            dev_stop, dev_points = self.stop_hz, self.points
        cmd = "sweep {start} {stop} {points}".format(
            start=self.start_hz, stop=dev_stop, points=dev_points)
        self.ser.reset_input_buffer()
        self._send_raw(cmd)
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
    def _parse_scan_lines(text):
        """
        outmask=3("周波数 S11_real S11_imag")の scan 出力をパースする。
        各データ行は "freq real imag" の 3 列。3 float に解釈できない行
        (コマンドエコー・プロンプト等)はスキップする。
        戻り値: [(freq_hz, real, imag), ...]
        """
        triples = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            if len(tokens) < 3:
                continue
            try:
                freq = float(tokens[0])
                real = float(tokens[1])
                imag = float(tokens[2])
            except ValueError:
                continue
            triples.append((freq, real, imag))
        return triples

    def measure_sweep(self):
        """
        設定された開始〜終了周波数を points 点で 1 回 scan し、
        掃引全点の複素 S11 を一括取得する。
        戻り値: (freqs_hz[np.ndarray], s11[np.ndarray(complex)]) / 失敗時 None

        単一周波数モードでは start==stop の極小掃引(SINGLE_DEVICE_POINTS 点)を行い、
        同一周波数の点を平均して論理的に 1 点へ集約する(npts=1 を保証)。
        """
        if self.single:
            dev_stop, dev_points = self.start_hz, SINGLE_DEVICE_POINTS
        else:
            dev_stop, dev_points = self.stop_hz, self.points
        cmd = "scan {start} {stop} {pts} {mask}".format(
            start=self.start_hz,
            stop=dev_stop,
            pts=dev_points,
            mask=int(self.scan_outmask),
        )
        self.ser.reset_input_buffer()
        self._send_raw(cmd)
        # 点数が多いと出力が長いので読み取り待ちを長めにする
        text = self._read_until_prompt(max_wait=8.0)
        triples = self._parse_scan_lines(text)
        if not triples:
            return None

        if self.single:
            # 同一周波数の全点を平均し、論理的に 1 点として返す
            re = float(np.mean([t[1] for t in triples]))
            im = float(np.mean([t[2] for t in triples]))
            return (np.array([float(self.start_hz)], dtype=float),
                    np.array([complex(re, im)], dtype=complex))

        freqs = np.array([t[0] for t in triples], dtype=float)
        s11 = np.array([complex(t[1], t[2]) for t in triples], dtype=complex)
        return freqs, s11

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
    incr_opti_frame()  # OptiTrack FPS 計測: 受信フレームをカウント

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
    # 毎フレームのコンソール出力(MoCap Frame ダンプ等)を抑制する
    try:
        client.set_print_level(0)
    except Exception:
        pass

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
        _dbg("[INFO] Motive へ接続しました (server app: {}).".format(app_name))
    else:
        _dbg("[INFO] NatNet 起動。Motive からのフレーム受信待ち...")
        _dbg("       (まだサーバ応答なし。Motive の Data Streaming 設定を確認してください)")

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
        _dbg("[WARN] NatNet shutdown 中に例外(無視): {}".format(e))
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
        # [計測開始]時に GUI から渡される計測条件(既定値で初期化)
        self.com_port = NANOVNA_PORT
        self.start_hz = SWEEP_START_HZ
        self.stop_hz = SWEEP_STOP_HZ
        self.points = SCAN_POINTS
        self.freq_grid_hz = FREQ_GRID_HZ
        self.csv_header = CSV_HEADER
        self.use_optitrack = True   # False で OptiTrack を使わない(VNA のみ計測)

    # ---- GUI へ通知 ----
    def _post(self, kind, value=None):
        try:
            self.out_queue.put_nowait((kind, value))
        except Exception:
            pass

    # ---- 開始 ----
    def start(self, com_port, start_hz, stop_hz, points, use_optitrack=True):
        """
        共有状態を初期化し、計測ワーカースレッドを起動する。
        com_port は GUI で選択された接続先 COM ポート(例 "COM3")。
        start_hz/stop_hz/points は GUI で指定された掃引条件。
        use_optitrack=False のときは OptiTrack(NatNet)を使わず VNA のみ計測する。
        この条件から周波数グリッドと CSV ヘッダーを動的に生成する。
        """
        self.com_port = com_port
        self.use_optitrack = bool(use_optitrack)
        self.start_hz = int(start_hz)
        self.stop_hz = int(stop_hz)
        self.points = int(points)
        # 単一周波数(ピンポイント)モードの正規化: 点数 1 または 開始==終了 -> 1 点に揃える
        if self.points <= 1 or self.start_hz == self.stop_hz:
            self.points = 1
            self.stop_hz = self.start_hz
        self.freq_grid_hz = make_freq_grid(self.start_hz, self.stop_hz, self.points)
        self.csv_header = build_csv_header(self.freq_grid_hz)

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
        test_mode = (self.com_port == TEST_MODE)

        # 1) VNA 接続(S11 固定)。テストモードなら接続・初期化を完全にスキップする。
        if test_mode:
            self.vna = None
            self._post("status",
                       "【VNAなしテストモード】VNA 接続をスキップしました（VNA 列は 0）。")
        else:
            # COM ポートが開けない場合は friendly なエラーを通知。
            # SerialException / FileNotFoundError はいずれも OSError のサブクラス。
            try:
                # GUI で指定された掃引条件を反映して接続・初期化
                self.vna = NanoVNA(self.com_port, NANOVNA_BAUD,
                                   start_hz=self.start_hz, stop_hz=self.stop_hz,
                                   points=self.points)
            except OSError as e:
                self._post("error",
                           "VNAの接続に失敗しました。\n"
                           "COM番号({})が正しいか、他のソフトが占有していないか確認してください。\n"
                           "（VNA を後から挿した場合は「ポート再検索」で一覧を更新できます）\n"
                           "\n詳細: {}".format(self.com_port, e))
                self._post("finished")
                return
            except Exception as e:
                self._post("error", "VNA 初期化中に予期せぬ例外: {}".format(e))
                self._post("finished")
                return
            self._post("status",
                       "VNA 接続 OK: {} @ {}bps / 掃引 {:.3f}-{:.3f} MHz {}点を記録 ({})".format(
                           self.com_port, NANOVNA_BAUD,
                           self.start_hz / 1e6, self.stop_hz / 1e6, self.points,
                           VNA_SPARAM))

        # 2) NatNet 開始(ブロックしない)。OptiTrack を使わないモードでは丸ごとスキップ。
        if not self.use_optitrack:
            self.client = None
            self._post("status",
                       "【OptiTrack なしモード】NatNet を使用しません(VNA のみ計測)。座標列は空欄になります。")
        else:
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

        # 4) 計測ループ(メモリへ蓄積 + グラフ更新)
        self._post("status", "計測中...")
        last_stale_warn = 0.0
        npts = self.points
        while not _stop_event.is_set():
            if test_mode:
                # VNA は使わない。全点ダミー 0。間隔をあけてサンプリング。
                s11_arr = np.zeros(npts, dtype=complex)
                z_r = np.zeros(npts)
                z_x = np.zeros(npts)
                # _stop_event.wait() は停止時に即座に抜けられる中断可能な待機。
                if _stop_event.wait(TEST_MODE_INTERVAL_SEC):
                    break
            else:
                try:
                    result = self.vna.measure_sweep()
                except serial.SerialException as e:
                    self._post("error", "VNA シリアル通信が切断されました: {}".format(e))
                    break
                except Exception as e:
                    self._post("error", "VNA 取得中に例外: {}".format(e))
                    break
                if result is None:
                    continue  # パース失敗はスキップ
                freqs, s11_arr = result
                if len(s11_arr) != npts:
                    # 期待点数と異なる掃引はヘッダーと整合しないのでスキップ(警告は間引く)
                    now = time.time()
                    if now - last_stale_warn > 2.0:
                        last_stale_warn = now
                        self._post("status",
                                   "[警告] 掃引点数が {} 点でした(期待 {} 点)。スキップします。".format(
                                       len(s11_arr), npts))
                    continue
                # scikit-rf で全点をまとめて Z11 に変換(この計測の周波数グリッドで)
                z_r, z_x = s11_sweep_to_z(s11_arr, self.freq_grid_hz, Z0)

            if self.client is not None:
                elapsed = seconds_since_last_frame()
                now = time.time()
                if (elapsed is not None and elapsed > STREAM_STALE_SEC
                        and (now - last_stale_warn) > STREAM_STALE_SEC):
                    last_stale_warn = now
                    self._post("status",
                               "[警告] OptiTrack フレームが {:.1f}s 途絶(座標が古い可能性)".format(elapsed))

            snap = read_latest_positions()
            ts = datetime.now().isoformat(timespec="milliseconds")

            # 各掃引点の (S11_Real, S11_Imag, Z_R, Z_X) を 1 行に展開
            vna_cols = []
            for i in range(npts):
                vna_cols += ["{:.6f}".format(s11_arr[i].real),
                             "{:.6f}".format(s11_arr[i].imag),
                             "{:.4f}".format(z_r[i]),
                             "{:.4f}".format(z_x[i])]
            row = (
                [ts]
                + _fmt_xyz(snap, "UpperArm")
                + _fmt_xyz(snap, "Joint")
                + _fmt_xyz(snap, "Forearm")
                + vna_cols
            )
            with self.rows_lock:
                self.rows.append(row)
                self.sample_count += 1
                count = self.sample_count
            incr_vna_sample()  # NanoVNA FPS 計測: 取得サンプルをカウント
            self._post("sample", count)
            # リアルタイムグラフ用に最新の Z スイープを送る(GUI 側で間引いて描画)
            self._post("plot", (np.asarray(z_r), np.asarray(z_x)))

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
                    _dbg("[WARN] VNA close 中に例外(無視): {}".format(e))
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
            writer.writerow(self.csv_header)  # 計測条件に応じて動的生成したヘッダー
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
        self.geometry("820x720")
        self.minsize(640, 560)

        self.msg_queue = queue.Queue()
        self.controller = None
        self.measuring = False
        self._finalizing = False  # 停止→保存→終了の処理中フラグ
        self._latest_plot = None  # 直近の (z_r, z_x)。poll ごとに1回だけ描画する
        self._counter_active = False  # コンソールの \r カウンタ行が出ているか

        # --- FPS 計測の状態(GUI=メインスレッドで 1 秒ごとに算出) ---
        self._latest_count = 0          # 直近のサンプル数
        self._opti_fps = 0.0            # 直近 1 秒の OptiTrack FPS
        self._vna_fps = 0.0             # 直近 1 秒の NanoVNA FPS
        self._fps_win_start = None      # FPS 計算ウィンドウの起点時刻
        self._fps_win_opti = 0          # ウィンドウ起点での OptiTrack 累計
        self._fps_win_vna = 0           # ウィンドウ起点での NanoVNA 累計
        self._meas_start_time = None    # 計測全体の開始時刻(平均 FPS 用)
        self._meas_stop_time = None     # 計測全体の停止時刻(平均 FPS 用)

        # グラフ横軸(周波数 MHz)。掃引モードでは周波数グリッド、単一周波数モードでは
        # 時系列(直近サンプル)に切り替える。
        self._freq_mhz = FREQ_GRID_HZ / 1e6
        self._plot_mode = "sweep"   # "sweep"(周波数軸) または "time"(時系列)
        # 単一周波数モードの時系列バッファ(直近 TIME_PLOT_WINDOW サンプル)
        self._tr_zr = collections.deque(maxlen=TIME_PLOT_WINDOW)
        self._tr_zx = collections.deque(maxlen=TIME_PLOT_WINDOW)

        self._build_widgets()

        # 全ウィジェット(self.log を含む)の構築後に COM ポートを取得する。
        # (構築前に呼ぶと self._log() が self.log を参照できず AttributeError になる)
        count = self._scan_ports()
        if count > 0:
            self._log("COM ポートを検出しました（{} 件）。先頭のポートを選択しました。"
                      .format(count))
        else:
            self._log("利用可能な COM ポートが見つかりませんでした。"
                      "VNA を接続して[ポート再検索]を押すか、"
                      "「{}」で OptiTrack 単体テストができます。".format(NO_VNA_DISPLAY))

        # ウィンドウの × でも必ずクリーンアップして終了する
        self.protocol("WM_DELETE_WINDOW", self.on_window_close)
        # GUI メッセージのポーリング開始
        self.after(100, self._poll_messages)

    # ---- ウィジェット構築 ----
    def _build_widgets(self):
        pad = {"padx": 8, "pady": 6}

        info = ttk.Label(
            self,
            text=("計測: {}（固定）  /  高さ軸: {}\n"
                  "COM ポートと掃引条件を設定して[計測開始]。最初の有効フレームで自動判別します。"
                  ).format(
                VNA_SPARAM,
                {0: "X", 1: "Y", 2: "Z"}.get(HEIGHT_AXIS_INDEX, "?")),
            justify="left")
        info.pack(anchor="w", **pad)

        # --- VNA 接続先 COM ポート選択 + 再検索 ---
        port_frame = ttk.Frame(self)
        port_frame.pack(fill="x", **pad)
        ttk.Label(port_frame, text="COM Port:").pack(side="left")
        self.port_var = tk.StringVar(value="")
        self.port_combo = ttk.Combobox(
            port_frame, textvariable=self.port_var,
            values=[], state="readonly", width=34)
        self.port_combo.pack(side="left", padx=6)
        self.rescan_btn = ttk.Button(
            port_frame, text="ポート再検索", command=self.on_rescan_ports)
        self.rescan_btn.pack(side="left", padx=4)

        # 表示名 -> デバイス名("COM3") の対応表。実際の接続にはデバイス名を使う。
        # 実際のスキャンは __init__ で全ウィジェット(self.log を含む)構築後に行う。
        self._port_display_to_device = {}

        # --- VNA 掃引条件(開始/終了周波数・点数)の設定 ---
        sweep_frame = ttk.Frame(self)
        sweep_frame.pack(fill="x", **pad)
        ttk.Label(sweep_frame, text="開始[MHz]:").pack(side="left")
        self.start_var = tk.StringVar(value="{:g}".format(SWEEP_START_HZ / 1e6))
        self.start_entry = ttk.Entry(sweep_frame, textvariable=self.start_var, width=8)
        self.start_entry.pack(side="left", padx=(2, 8))
        ttk.Label(sweep_frame, text="終了[MHz]:").pack(side="left")
        self.stop_var = tk.StringVar(value="{:g}".format(SWEEP_STOP_HZ / 1e6))
        self.stop_entry = ttk.Entry(sweep_frame, textvariable=self.stop_var, width=8)
        self.stop_entry.pack(side="left", padx=(2, 8))
        ttk.Label(sweep_frame, text="点数:").pack(side="left")
        self.points_var = tk.StringVar(value=str(SCAN_POINTS))
        # 1 刻みで増減できるスピンボックス(直接入力も可)
        self.points_spin = ttk.Spinbox(
            sweep_frame, textvariable=self.points_var,
            from_=POINTS_MIN, to=POINTS_MAX, increment=1, width=8)
        self.points_spin.pack(side="left", padx=2)
        ttk.Label(sweep_frame, text="(1刻みで指定可)").pack(side="left", padx=2)

        # --- 計測モード(OptiTrack を使うか) ---
        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill="x", **pad)
        self.optitrack_var = tk.BooleanVar(value=True)
        self.optitrack_chk = ttk.Checkbutton(
            opt_frame,
            text="OptiTrack を使う（OFF にすると VNA のみ計測：Motive 未接続でも可）",
            variable=self.optitrack_var)
        self.optitrack_chk.pack(side="left")

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

        # --- サンプリングレート(FPS)表示 ---
        fps_frame = ttk.Frame(self)
        fps_frame.pack(fill="x", **pad)
        self.fps_var = tk.StringVar(value="OptiTrack: -- FPS / NanoVNA: -- FPS")
        ttk.Label(fps_frame, text="取得レート:").pack(side="left")
        ttk.Label(fps_frame, textvariable=self.fps_var,
                  foreground="#06c").pack(side="left", padx=6)

        # --- リアルタイムグラフ(周波数 vs インピーダンス) ---
        self._build_plot(pad)

        # ログ表示(グラフを広く取るため小さめ)
        log_frame = ttk.Frame(self)
        log_frame.pack(fill="x", **pad)
        self.log = tk.Text(log_frame, height=6, state="disabled", wrap="word")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ---- リアルタイムグラフの構築 ----
    def _build_plot(self, pad):
        plot_frame = ttk.LabelFrame(self, text="周波数 vs インピーダンス (Z11)")
        plot_frame.pack(fill="both", expand=True, **pad)

        self.fig = Figure(figsize=(6.4, 3.4), dpi=100)
        self.ax = self.fig.add_subplot(111)

        # matplotlib の既定フォントは日本語グリフを持たないため、グラフ内は ASCII で表記する
        zeros = np.zeros_like(self._freq_mhz)
        (self.line_zr,) = self.ax.plot(
            self._freq_mhz, zeros, color="#d62728", label="Z_R (Resistance)")
        (self.line_zx,) = self.ax.plot(
            self._freq_mhz, zeros, color="#1f77b4", label="Z_X (Reactance)")

        # 参考: 中心周波数(13.56MHz)の縦線(単一周波数=時系列モードでは隠す)
        self._target_vline = self.ax.axvline(
            TARGET_FREQ_HZ / 1e6, color="#888888", linestyle="--", linewidth=0.8)

        self.ax.set_xlabel("Frequency [MHz]")
        self.ax.set_ylabel("Impedance [Ω]")
        self.ax.set_xlim(SWEEP_START_HZ / 1e6, SWEEP_STOP_HZ / 1e6)
        self.ax.set_ylim(-100, 100)
        self.ax.grid(True, alpha=0.3)
        self.ax.legend(loc="upper right", fontsize=9)
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

    def _reconfigure_plot(self, start_hz, stop_hz, points):
        """
        掃引条件の変更にあわせてグラフを作り直す。
        - 2 点以上: 横軸=周波数[MHz] のスイープ波形
        - 1 点(単一周波数): 横軸=直近サンプル の時系列スクロールチャート
        """
        if points <= 1:
            # 単一周波数 -> 時系列モード
            self._plot_mode = "time"
            self._tr_zr.clear()
            self._tr_zx.clear()
            self.line_zr.set_data([], [])
            self.line_zx.set_data([], [])
            self._target_vline.set_visible(False)
            self.ax.set_xlabel("Recent samples")
            self.ax.set_title("Single freq {:.4f} MHz (time series)".format(
                start_hz / 1e6), fontsize=9)
            self.ax.set_xlim(0, TIME_PLOT_WINDOW)
            self.ax.set_ylim(-100, 100)
        else:
            # スイープモード -> 横軸=周波数
            self._plot_mode = "sweep"
            self._freq_mhz = make_freq_grid(start_hz, stop_hz, points) / 1e6
            zeros = np.zeros_like(self._freq_mhz)
            self.line_zr.set_data(self._freq_mhz, zeros)
            self.line_zx.set_data(self._freq_mhz, zeros)
            self._target_vline.set_visible(True)
            self.ax.set_xlabel("Frequency [MHz]")
            self.ax.set_title("")
            self.ax.set_xlim(start_hz / 1e6, stop_hz / 1e6)
            self.ax.set_ylim(-100, 100)
        self.canvas.draw_idle()

    def _redraw_plot(self, z_r, z_x):
        """最新の Z データでグラフを更新する(メインスレッドから呼ぶこと)。"""
        if self._plot_mode == "time":
            # 単一周波数: 先頭(唯一)の値を時系列バッファへ追加してスクロール表示
            if len(z_r) == 0:
                return
            self._tr_zr.append(float(z_r[0]))
            self._tr_zx.append(float(z_x[0]))
            n = len(self._tr_zr)
            xs = range(n)
            self.line_zr.set_data(xs, list(self._tr_zr))
            self.line_zx.set_data(xs, list(self._tr_zx))
            self.ax.set_xlim(max(0, n - TIME_PLOT_WINDOW), max(TIME_PLOT_WINDOW, n))
            allv = np.array(list(self._tr_zr) + list(self._tr_zx), dtype=float)
        else:
            # スイープ: 点数が一致しない場合はスキップ(再構成直後など)
            if len(z_r) != len(self._freq_mhz):
                return
            self.line_zr.set_ydata(z_r)
            self.line_zx.set_ydata(z_x)
            allv = np.concatenate([np.asarray(z_r, float), np.asarray(z_x, float)])

        # 有限値のみで y 範囲を自動調整(全反射で inf になっても破綻しないように)
        finite = allv[np.isfinite(allv)]
        if finite.size:
            lo, hi = float(finite.min()), float(finite.max())
            if lo == hi:
                lo, hi = lo - 1.0, hi + 1.0
            margin = (hi - lo) * 0.1
            self.ax.set_ylim(lo - margin, hi + margin)
        self.canvas.draw_idle()

    # ---- COM ポート一覧のスキャン/再検索 ----
    def _scan_ports(self):
        """
        利用可能な COM ポートを取得して Combobox に反映する。
        末尾には常に「VNAなしテストモード」を追加する。
        選択は次のとおり:
          - 実ポートが 1 件以上 -> 先頭の実ポート(current(0))を選択
          - 実ポートが 0 件     -> 「VNAなしテストモード」を選択
        ログは出さず、見つかった実ポート数を返す(出力は呼び出し側が行う)。
        """
        ports = list_serial_ports()  # [(device, description), ...]
        self._port_display_to_device = {}
        displays = []
        for dev, desc in ports:
            disp = "{} - {}".format(dev, desc) if desc else dev
            displays.append(disp)
            self._port_display_to_device[disp] = dev

        # 末尾に「VNAなしテストモード」を必ず追加(実機が無くてもテスト可能にする)
        displays.append(NO_VNA_DISPLAY)
        self._port_display_to_device[NO_VNA_DISPLAY] = TEST_MODE

        self.port_combo.configure(values=displays)

        if ports:
            # 実ポートあり: 先頭の実ポートを選択(displays[0] は最初の実ポート)
            self.port_combo.current(0)
        else:
            # 実ポートなし: テストモードをデフォルト選択
            self.port_combo.current(displays.index(NO_VNA_DISPLAY))

        return len(ports)

    def get_selected_port(self):
        """Combobox の選択表示名から、実際のデバイス名("COM3")を返す。"""
        disp = self.port_var.get()
        return self._port_display_to_device.get(disp, disp)

    def on_rescan_ports(self):
        """[ポート再検索] ボタン: COM ポート一覧を更新する。"""
        if self.measuring:
            return  # 計測中は変更しない
        count = self._scan_ports()
        if count > 0:
            self._log("COM ポートを再検索しました（{} 件）。".format(count))
        else:
            self._log("COM ポートを再検索しましたが、利用可能なポートはありません。"
                      "「{}」で OptiTrack 単体テストができます。".format(NO_VNA_DISPLAY))

    # ---- ログ出力 ----
    def _log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---- コンソール出力(重要イベントのみ。毎フレームは出さない) ----
    def _console_event(self, text):
        """重要イベントを通常の1行でコンソールに残す。"""
        self._finalize_counter_line()
        print(text, flush=True)

    def _console_counter(self):
        """計測中の取得数と FPS を「同じ1行」で上書き更新する(\\r)。"""
        print("\r[計測中] 取得数: {} 件 (OptiTrack: {:.1f} Hz, NanoVNA: {:.1f} Hz)   ".format(
            self._latest_count, self._opti_fps, self._vna_fps),
            end="", flush=True)
        self._counter_active = True

    def _finalize_counter_line(self):
        """\\r で更新中のカウンタ行があれば、改行して確定させる。"""
        if self._counter_active:
            print("", flush=True)
            self._counter_active = False

    # ---- 掃引条件の読み取り・検証 ----
    def _read_sweep_config(self):
        """
        GUI の開始/終了周波数(MHz)と点数を読み取り検証する。
        正常なら (start_hz, stop_hz, points) を返し、不正なら警告して None を返す。
        単一周波数(開始==終了、または点数 1)は許可し、(points=1, stop=start) に正規化する。
        """
        try:
            start_mhz = float(self.start_var.get())
            stop_mhz = float(self.stop_var.get())
        except ValueError:
            messagebox.showerror("入力エラー",
                                 "開始/終了周波数は数値(MHz)で入力してください。")
            return None
        # 点数は 1 刻みの任意整数。小数を入れられても四捨五入で整数化する。
        try:
            points = int(round(float(self.points_var.get())))
        except ValueError:
            messagebox.showerror("入力エラー", "点数は整数で入力してください。")
            return None

        if start_mhz <= 0 or stop_mhz <= 0:
            messagebox.showerror("入力エラー", "周波数は正の値にしてください。")
            return None
        if start_mhz > stop_mhz:
            messagebox.showerror(
                "入力エラー", "開始周波数は終了周波数以下にしてください。\n"
                "(開始==終了 にすると単一周波数測定になります)")
            return None
        if points < POINTS_MIN or points > POINTS_MAX:
            messagebox.showerror(
                "入力エラー",
                "点数は {} 〜 {} の範囲で指定してください。".format(
                    POINTS_MIN, POINTS_MAX))
            return None

        start_hz = int(round(start_mhz * 1e6))
        stop_hz = int(round(stop_mhz * 1e6))
        # 単一周波数モードの正規化: 点数 1 または 開始==終了 -> (points=1, stop=start)
        if points <= 1 or start_hz == stop_hz:
            points = 1
            stop_hz = start_hz
        return start_hz, stop_hz, points

    # ---- 計測開始 ----
    def on_start(self):
        if self.measuring:
            return
        # 開始時点で選択されている COM ポートを読み込む
        com_port = self.get_selected_port()
        if not com_port:
            messagebox.showerror(
                "COM ポート未選択",
                "VNA の COM ポートが選択されていません。\n"
                "VNA を接続し、[ポート再検索] で一覧を更新してから選択してください。")
            return

        # 掃引条件(開始/終了周波数・点数)を読み取り検証
        cfg = self._read_sweep_config()
        if cfg is None:
            return
        start_hz, stop_hz, points = cfg
        use_optitrack = bool(self.optitrack_var.get())

        # 新しい掃引条件にあわせてグラフ横軸を作り直す
        self._reconfigure_plot(start_hz, stop_hz, points)

        self.measuring = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.port_combo.configure(state="disabled")   # 計測中は変更不可
        self.rescan_btn.configure(state="disabled")
        self.start_entry.configure(state="disabled")
        self.stop_entry.configure(state="disabled")
        self.points_spin.configure(state="disabled")
        self.optitrack_chk.configure(state="disabled")
        self.status_var.set("接続中...")
        self.count_var.set("サンプル数: 0")
        # FPS 計測の初期化
        self._latest_count = 0
        self._opti_fps = 0.0
        self._vna_fps = 0.0
        now = time.time()
        self._meas_start_time = now
        self._meas_stop_time = None
        self._fps_win_start = now
        self._fps_win_opti = 0
        self._fps_win_vna = 0
        self.fps_var.set("OptiTrack: -- FPS / NanoVNA: -- FPS")
        if points <= 1:
            sweep_desc = "単一周波数 {:.4f} MHz (1点)".format(start_hz / 1e6)
        else:
            sweep_desc = "掃引 {:.3f}-{:.3f} MHz {}点".format(
                start_hz / 1e6, stop_hz / 1e6, points)
        opt_desc = "OptiTrack ON" if use_optitrack else "OptiTrack OFF(VNAのみ)"
        if com_port == TEST_MODE:
            msg = ("計測を開始しました。【VNAなしテストモード】OptiTrack のみ取得"
                   "（VNA 列は 0, {}, {}）。").format(sweep_desc, opt_desc)
        else:
            msg = "計測を開始しました。COM Port = {}（{}, {}, {}）".format(
                com_port, VNA_SPARAM, sweep_desc, opt_desc)
        self._log(msg)
        self._console_event("[計測開始] " + msg)  # 重要イベントはコンソールにも残す
        self.controller = MeasurementController(self.msg_queue)
        self.controller.start(com_port, start_hz, stop_hz, points, use_optitrack)

    # ---- 計測終了 -> 保存 -> 終了 ----
    def on_stop(self):
        if not self.measuring or self._finalizing:
            return
        self._finalizing = True
        self._meas_stop_time = time.time()  # 平均 FPS 算出用に停止時刻を記録
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
                self._console_event(
                    "[保存完了] {} 件を保存しました: {}".format(written, path))
                self._log_fps_summary()  # 平均 FPS サマリーを GUI/コンソールへ
                messagebox.showinfo(
                    "保存完了", "{} サンプルを保存しました。\n{}".format(written, path))
            except Exception as e:
                self._log("保存に失敗: {}".format(e))
                self._console_event("[保存エラー] {}".format(e))
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
            self._meas_stop_time = time.time()  # 平均 FPS 算出用に停止時刻を記録
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
        self._latest_plot = None   # この poll サイクルで届いた最新の Z スイープ
        got_sample = False         # この poll サイクルで新しいサンプルが届いたか
        try:
            while True:
                kind, value = self.msg_queue.get_nowait()
                if kind == "status":
                    # ステータスは GUI ログのみに出す(コンソールは汚さない)
                    self.status_var.set(value)
                    self._log(value)
                elif kind == "sample":
                    self.count_var.set("サンプル数: {}".format(value))
                    self._latest_count = value
                    got_sample = True
                elif kind == "plot":
                    # 最新値だけ保持し、描画はループ後に1回だけ行う(間引き)
                    self._latest_plot = value
                elif kind == "error":
                    self._finalize_counter_line()
                    self._log("[エラー] " + str(value))
                    self._console_event("[エラー] " + str(value))
                    messagebox.showerror("エラー", str(value))
                    # 致命的エラー: 計測を畳んでボタンを戻す
                    self._recover_after_error()
                elif kind == "finished":
                    self._finalize_counter_line()
                    self._log("計測ループ終了。")
                elif kind == "ready_to_save":
                    self._finalize_counter_line()
                    self.status_var.set("停止しました")
                    self.count_var.set("サンプル数: {}".format(
                        self.controller.get_sample_count() if self.controller else 0))
                    self._do_save_and_exit()
                    return  # ウィンドウ破棄後に after を再登録しない
        except queue.Empty:
            pass

        # --- FPS を約 1 秒ごとに算出して表示(GUI ラベル + コンソール) ---
        fps_updated = self._update_fps_if_due()

        # コンソールの \r 行は、FPS 更新時 か 新サンプル到着時に上書き更新する
        if self.measuring and (fps_updated or got_sample):
            self._console_counter()

        # 今サイクルに届いた最新スイープでグラフを 1 回だけ更新(描画負荷を抑制)
        if self._latest_plot is not None:
            z_r, z_x = self._latest_plot
            self._redraw_plot(z_r, z_x)
        # ウィンドウが破棄されていれば再ポーリングしない
        try:
            if self.winfo_exists():
                self.after(100, self._poll_messages)
        except tk.TclError:
            pass

    def _update_fps_if_due(self):
        """
        FPS 計算ウィンドウ(約1秒)が経過していれば、OptiTrack/NanoVNA の FPS を
        算出して GUI ラベルを更新する。更新したら True を返す。
        """
        if not self.measuring or self._fps_win_start is None:
            return False
        now = time.time()
        dt = now - self._fps_win_start
        if dt < 1.0:
            return False
        opti_total, vna_total = read_fps_counters()
        self._opti_fps = (opti_total - self._fps_win_opti) / dt
        self._vna_fps = (vna_total - self._fps_win_vna) / dt
        # 次ウィンドウへ
        self._fps_win_start = now
        self._fps_win_opti = opti_total
        self._fps_win_vna = vna_total
        self.fps_var.set("OptiTrack: {:.1f} FPS / NanoVNA: {:.1f} FPS".format(
            self._opti_fps, self._vna_fps))
        return True

    def _log_fps_summary(self):
        """計測全体の平均 FPS を GUI ログとコンソールへ出力する。"""
        opti_total, vna_total = read_fps_counters()
        start = self._meas_start_time
        stop = self._meas_stop_time or time.time()
        dur = (stop - start) if start else 0.0
        if dur > 0:
            avg_opti = opti_total / dur
            avg_vna = vna_total / dur
        else:
            avg_opti = avg_vna = 0.0
        summary = ("計測サマリー: 計測時間 {:.1f} 秒 / "
                   "OptiTrack 平均 {:.1f} FPS ({} フレーム) / "
                   "NanoVNA 平均 {:.1f} FPS ({} サンプル)").format(
            dur, avg_opti, opti_total, avg_vna, vna_total)
        self._log(summary)
        self._console_event("[サマリー] " + summary)

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
        self.port_combo.configure(state="readonly")  # ポート再選択を許可
        self.rescan_btn.configure(state="normal")
        self.start_entry.configure(state="normal")   # 掃引条件の再編集を許可
        self.stop_entry.configure(state="normal")
        self.points_spin.configure(state="normal")
        self.optitrack_chk.configure(state="normal")
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
