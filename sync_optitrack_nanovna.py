# -*- coding: utf-8 -*-
"""
OptiTrack (Motive 3.1 / NatNet) + JNCRadio VNA 3G 同期計測スクリプト
================================================================================

同一 Windows PC 上で動作する Motive から NatNet 経由でラベル付きマーカーの座標を
受信しつつ、USB シリアル接続された JNCRadio VNA 3G から特定単一周波数の S11 を
連続取得し、両者を同期して CSV に保存する。

計測対象は身体に取り付けた 7 個のラベル付きマーカー。上半身・下半身のどちらを計測するかは
GUI の「計測部位」で計測開始前に選ぶ(MARKER_GROUPS):
  上半身: chest, R_upperarm, R_joint, R_forearm, L_upperarm, L_joint, L_forearm
  下半身: waist, R_thigh,    R_knee,  R_shin,    L_thigh,    L_knee,  L_shin
各マーカーの [X, Y, Z] 座標を同時に取得し、S11 / インピーダンスと紐付けて 1 行に記録する。
CSV の位置列・ライブ表示・マーカー欠損判定は、選んだ部位の 7 点だけを対象にする。

★マーカーの識別(ラベル認識)★
  Motive 側で各マーカーにラベル(名前)を付け、マーカーセットとして配信する。本スクリプトは
  マーカーセット内の「並び順」を、選択中の部位のマーカー名リスト(MARKER_NAMES)に対応付けて
  各マーカーの座標を取得する(高さによる自動判別は廃止)。実際にどの順序・形式で配信されて
  いるかは、起動直後に出力される診断ログ(MARKER_DIAGNOSTIC_FRAMES)で確認できる。
  並び順は Motive 側のアセット定義に依存するため、部位ごとに MARKER_GROUPS の "names" を
  実際の配信順へ合わせておくこと。

★マーカー消失時の扱い★
  いずれかのマーカーが認識できない(オクルージョン/未検出)フレームでは、そのとき取得した
  VNA サンプルを CSV に記録せず自動的に破棄する。マーカーセット内の隠れマーカーは Motive から
  (0,0,0) で配信されるため、原点近傍を「未認識」とみなす(OCCLUSION_ORIGIN_EPS)。

動作確認環境:
  - Windows 10/11, Python 3.8+
  - Motive 3.1.0 Beta 2 / NatNet 4.x (127.0.0.1, Unicast)
  - OptiTrack NatNet SDK 同梱の NatNetClient.py / MoCapData.py / DataDescriptions.py

GUI / スレッド構成:
  - tkinter GUI(メインスレッド): [計測開始]/[計測終了] ボタン。GUI は固まらない。
  - VNA リーダー: 各 nanoVNA を掃引して最新の S11→Z11 を publish する。
      交互モード(既定): 1 本のスレッドが全チャンネルを 1 台ずつ順番に掃引する。
        → 2 台が同時に「掃引」しないので RF 干渉によるノイズを防ぐ(VNA_ALTERNATE_SWEEP)。
        ただし NanoVNA は掃引していない間も最後の周波数を出し続けるため、単一周波数
        (CW)モードでは交互にしても両機が同じ周波数を送信し続けてしまう。そこで
        scan の終端を退避周波数にして、掃引が終わると発振器が測定周波数から
        離れるようにする(VNA_PARK_* を参照)。
      並列モード: チャンネルごとに専用スレッドで同時掃引する(高速だが 2 台同時だと干渉)。
  - コンバイナ(バックグラウンド): 全チャンネルの最新掃引と 7 マーカー座標を結合して
    メモリ上(self.rows)へ蓄積する。全マーカーが認識できているサンプルのみ記録する。
  - NatNet 受信(SDK 内部スレッド): new_frame_with_data_listener が 7 マーカーの最新座標を更新。
    マーカーの marker_id をラベル名に対応付ける。未検出のマーカーは無効化する。
  - 共有変数は threading.Lock() で保護してスレッド安全に読み書きする。
  - 終了時(終了ボタン / ウィンドウ×)は必ずクリーンアップを実行する:
      VNA のクローズ → NatNet の shutdown() → 全スレッドの join。
    これにより COM ポート / UDP ソケット / スレッドが確実に解放され、
    次回実行時にハングしない。

操作の流れ:
  1. プログラム起動で GUI が立ち上がる。
  2. [計測開始] で計測スタート(全マーカーが揃ったサンプルのみ記録される)。
  3. [計測終了] で計測停止 → 保存ダイアログでファイル名指定 → CSV 保存 → 終了。

VNA 接続(COM ポート):
  計測する S パラメータは S11 固定。VNA を接続する COM ポートは GUI のドロップダウンで
  選択する。「ポート再検索」ボタンで一覧を更新でき、後から VNA を挿しても再起動不要。
  選択ポートが開けない(存在しない/他ソフトが占有)場合は、クラッシュさせず
  messagebox.showerror で警告を表示する。

★ウェブカメラ録画・ライブ表示・時刻同期★
  各サンプルには「計測開始からの経過秒(Timestamp)」に加えて「絶対時刻(WallClock,
  ローカル時刻・ミリ秒精度)」を記録する(カメラ同期 ON 時)。
  [計測開始]と連動して webカメラ(OpenCV)を録画開始し、計測終了・CSV 保存時に CSV と同じ
  フォルダへ WIN_YYYYMMDD_HH_MM_SS_Pro.mp4 として保存する。保存時には CSV の隣に
  <csv名>.meta.json(計測開始/終了の絶対時刻・動画情報)も書き出す。後処理で CSV の WallClock と
  動画の録画開始時刻を突き合わせれば、各データ行がその動画の何秒目かを算出できる
  (sync_video_with_dataset.py)。
  また計測中は別ウィンドウのライブ・ダッシュボード(live_dashboard.py)で 3D マーカー/肘角度/
  スミスチャート/カメラ映像を表示する。この表示は nanoVNA の掃引レートに律速されず、専用の
  高速タイマーで最新のマーカー座標・カメラフレーム・各 VNA の最新掃引を直接読んで更新する。

CSV ヘッダー(位置列は選択中の部位のマーカー順、VNA 列は VNA_CHANNEL_NAMES 順に自動生成。
下記は上半身を選んだ場合の例。下半身なら waist / R_thigh / R_knee / R_shin / ... になる):
  [Timestamp, WallClock,
   R_forearm_X,  R_forearm_Y,  R_forearm_Z,
   R_joint_X,    R_joint_Y,    R_joint_Z,
   R_upperarm_X, R_upperarm_Y, R_upperarm_Z,
   chest_X,      chest_Y,      chest_Z,
   L_forearm_X,  L_forearm_Y,  L_forearm_Z,
   L_joint_X,    L_joint_Y,    L_joint_Z,
   L_upperarm_X, L_upperarm_Y, L_upperarm_Z,
   leftbody_S11_Real_<MHz>,  ..., leftbody_Z_R_<MHz>,  leftbody_Z_X_<MHz>,
   rightbody_S11_Real_<MHz>, ..., rightbody_Z_R_<MHz>, rightbody_Z_X_<MHz>]
  (単一周波数(1点)モードでも MHz サフィックスは付く。列名にどの周波数かが残り、
   再生ビューア等の下流ツールが掃引モードと同じ規則で読めるようにするため)

計測中の GUI 表示: leftbody / rightbody それぞれのスミスチャート(S11=Γ をプロット)と、
「読み取り周波数」に最も近い掃引点のインピーダンス Z=R+jX の数値表示。
"""

import os
import csv
import sys
import time
import json
import queue
import shutil
import tempfile
import datetime
import threading
import collections

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

# --- ローカルモジュール(カメラ録画 / ライブ表示ダッシュボード) ---
# これらが読み込めない環境でも、カメラ/ライブ表示を使わない計測は続行できるようにする。
# (camera_recorder は cv2 を遅延 import するので、cv2 未導入でも import 自体は成功する)
_ensure_local_on_path = os.path.dirname(os.path.abspath(__file__))
if _ensure_local_on_path not in sys.path:
    sys.path.insert(0, _ensure_local_on_path)
try:
    import camera_recorder
except Exception as _e:          # pragma: no cover
    camera_recorder = None
    print("[WARN] camera_recorder を読み込めません(カメラ録画は無効): {}".format(_e))
try:
    import live_dashboard
except Exception as _e:          # pragma: no cover
    live_dashboard = None
    print("[WARN] live_dashboard を読み込めません(ライブ表示は無効): {}".format(_e))


# =============================================================================
# コンソール出力ポリシー
# =============================================================================
# 計測中の細かいログはコンソールに出さず GUI ログに集約する。
# CONSOLE_VERBOSE=True にすると、補助的な [INFO] などもコンソールに出す。
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

# --- VNA チャンネル(複数台の計測) ---
# nanoVNA を複数台つないで、それぞれ別の対象の S11 を計測する。ここに列挙した
# 名前ぶんだけ COM ポート選択欄が GUI に並ぶ(掃引の並行/交互は VNA_ALTERNATE_SWEEP 参照)。
# CSV 列は leftbody_..., rightbody_... のようにチャンネル名で区別されるので、
# 保存データからどちらの対象のインピーダンスかを判別できる。
# GUI ではチャンネル名ごとに COM ポートを選ぶ(どの COM がどちらの body かは画面で割り当てる)。
# 1 台だけ使う場合は ["leftbody"] のように 1 要素にする。
VNA_CHANNEL_NAMES = ["leftbody", "rightbody"]

# --- 交互計測(RF 干渉対策) ---
# 2 台の nanoVNA を「同時に」掃引すると、両機が同じ周波数帯で同時に送信するため RF が
# 干渉し、両方のインピーダンスに大きなノイズが乗る。これを防ぐため、既定では実機を
# 1 台ずつ順番に掃引する(交互計測)。掃引と掃引の間に待ち時間は入れないので、各台の FPS は
# 「並列同時掃引の約 1/実機台数」になる(=同時に送信しないための物理的な下限)。
# 片方をテストモードにすると実機は 1 台だけになり干渉しないので、その 1 台を全速で掃引する
# (テストモード側は掃引せずダミー 0 を埋めるだけ)。
#   True  : 1 本のスレッドで実機のみをラウンドロビン掃引(同時送信しない/推奨)
#   False : チャンネルごとに専用スレッドで並列掃引(高速だが 2 台同時だと干渉する)
VNA_ALTERNATE_SWEEP = True

# --- 掃引(スイープ)範囲の既定値 ---
# 実際の掃引条件は GUI の入力欄(開始/終了周波数・点数)で変更でき、[計測開始]時に
# その値が VNA に設定される。下記はその「GUI 初期値 兼 既定値」。
# 一定範囲を掃引して 101 点分の複素 S11 を一括取得し、服(メアンダコイル)を動かした
# ときに「どの帯域で整合がずれるか」を Z11 のスイープとして記録・可視化する。
SWEEP_START_HZ = 6_000_000     # 掃引開始 [Hz] (6 MHz)
SWEEP_STOP_HZ  = 20_000_000    # 掃引終了 [Hz] (20 MHz)
SCAN_POINTS    = 101           # 掃引点数(GUI 初期値)
# GUI の点数スピンボックスの範囲(1 刻みで任意指定可能)
POINTS_MIN = 1                 # 最小点数(1 = 単一周波数ピンポイント測定)
POINTS_MAX = 100_000           # 上限(実用上の安全上限。実機の制約に応じて調整)

# 単一周波数(1点)モードで「退避しない」ときにデバイスへ投げる scan の点数。
# 点数 1 の縮退 scan はファームによっては不安定なため、同一周波数を数点掃引する
# (start==stop の極小掃引)。先頭 1 点は整定前のことがあるので捨て、残りを平均して
# 論理的に 1 点にする。退避するときは「測定点 + 退避点」の 2 点固定になる(下記)。
SINGLE_DEVICE_POINTS = 3
# 上記のうち先頭から捨てる点数(整定待ち)。
SINGLE_DISCARD_POINTS = 1

# --- 単一周波数(CW)モードの相互干渉対策: 待機中の発振器の退避 -------------------
# 【なぜ必要か】NanoVNA は pause 後も Si5351 が最後に設定された周波数を出力し続ける。
# 掃引モードでは 2 台が同じ周波数に同時にいる瞬間はほぼ無いので実害が出ないが、
# 単一周波数モードでは両機が同じ周波数(例 13.56 MHz)に居座るため、交互計測にしても
# 「送信は常時同時」になる。2 台の基準クロック差 Δf ぶんだけ相手のキャリアが
# 受信 IF 内で回り、静止した被測定物でも Γ が Δf 周期でゆっくり回転する
# (左右で逆回りの同一周波数うなりとして観測される)。
#
# 【対策】掃引の"終端"を退避周波数にする: `scan {測定周波数} {退避周波数} 2 3`。
#   点0 = 測定周波数の測定値、点1 = 退避周波数(捨てる)。掃引が終わった時点で
#   発振器は退避周波数に残るので、追加のコマンドを送らずに退避できる。
#   → 1 サンプルあたりのシリアル往復は掃引モードと同じ 1 回だけ。往復を増やすと
#     応答の取りこぼしでコマンドとプロンプトがずれ、「VNA 無応答」で計測が
#     止まる事故につながるため、ホットパスでは追加コマンドを送らない。
#   (接続直後と復帰時だけは測定を伴わない `freq {Hz}` で退避する: NanoVNA-D の
#    cmd_freq は pause_sweep() + set_frequency() を実行するだけの軽いコマンド)
VNA_PARK_ENABLE = True
# 退避先 = 測定周波数 × VNA_PARK_RATIO。
# 【1 より大きくすること】scan は start > stop を "frequency range is invalid" として
# 拒否するため(NanoVNA-D の cmd_scan)、退避先は必ず測定周波数より上でなければ
# 「測定 → 退避」の 2 点掃引が成立しない。
# 黄金比(1.618)は整数比・整数分の 1 のどちらにもならないので、方形波出力の
# 奇数次高調波(1.618f, 4.854f, …)も、受信側 LO の奇数次高調波(f, 3f, 5f, …)との
# 差も、測定周波数から十分離れる。
VNA_PARK_RATIO = 1.618
VNA_PARK_MAX_HZ = 1_500_000_000  # 退避先の上限[Hz](NanoVNA-H4 の上限周波数)
VNA_PARK_MIN_MARGIN = 1.05       # 測定周波数のこの倍以上離れていないと退避しない

# IF 帯域(DSP の平均化窓)。None ならデバイスの現在設定のまま触らない。
# NanoVNA-D の `bandwidth {n}` は帯域 ≒ 1000/(n+1) [Hz]。n を大きくするほど
# 低ノイズだが 1 点あたりの所要時間が伸びる(=サンプリングレートが下がる)。
# 単一周波数モードは 1 掃引が軽いので、ノイズが気になるときは 1〜9 を試す。
VNA_IF_BANDWIDTH = None


def park_freq_for(meas_hz):
    """
    待機中に発振器を逃がす周波数[Hz]を返す(測定周波数 × VNA_PARK_RATIO)。
    機体の上限を超える場合は上限に丸め、それでも測定周波数に近すぎる(干渉を
    避けられない)ときは None を返して退避を諦める。
    """
    meas = float(meas_hz)
    park = min(int(meas * VNA_PARK_RATIO), int(VNA_PARK_MAX_HZ))
    if park < meas * VNA_PARK_MIN_MARGIN:
        return None
    return park


# 単一周波数(1点)モードのスミスチャートに軌跡として残す直近サンプル数。
# 1 点では掃引トレースが引けないので、直近サンプルの Γ を線でつないで
# 「その周波数の値が時間とともにどう動いたか」を見えるようにする。
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
TEST_MODE = "__TEST_NO_VNA__"                  # get_selected_ports が返す内部センチネル
TEST_MODE_INTERVAL_SEC = 0.1                    # テストモードの1サンプル間隔[秒](律速が無いため)

# 使用する COM ポートは GUI のドロップダウンで選択する(下記は初期選択の候補)。
# 起動時にこの値が一覧に存在すれば初期選択される。無ければ一覧の先頭を選ぶ。

# --- OptiTrack (NatNet) ---
NATNET_SERVER_IP = "127.0.0.1"  # 同一PCなので localhost (Motive と同じ PC)
NATNET_LOCAL_IP  = "127.0.0.1"  # 同一PCなので localhost
NATNET_USE_MULTICAST = False    # 同一PCループバックは Unicast 推奨

# --- マーカーの識別(ラベル認識)方式 ---
# Motive 側で 7 個のマーカーにラベル(名前)を付けて配信する。フレームデータにマーカー名は
# 含まれないため、本スクリプトは「マーカーの並び(marker_id または セット内 index)」を
# 選択中の部位のマーカー名リストに順番どおり対応付けて座標を取得する(高さによる自動判別は廃止)。
#
# 【重要】各部位の "names" の並び順が、実際の配信順と一致している必要がある。
# 実際の並びは起動直後の診断ログ(MARKER_DIAGNOSTIC_FRAMES)で確認し、必要ならここを並べ替える。
# 上半身と下半身では Motive のアセットが別なので、配信順もそれぞれ確認すること。
# CSV の位置列もこの順序で生成される。

# --- 計測部位(上半身 / 下半身)の定義 ---
# どちらを計測するかは GUI の「計測部位」で計測開始前に選ぶ。選んだ側の 7 点だけを
# 記録し、CSV 列・ライブ表示・マーカー欠損判定もその 7 点で行う。
# label      : GUI/ログの表示名
# names      : Motive の配信順に並べたマーカー名(= CSV の位置列の順序)
# 下半身の並びは上半身と同じ規則(中心 → 左右の付け根 → 関節 → 末端)で初期値を置いてある。
# 実際の配信順は Motive のアセット定義次第なので、診断ログを見て必ず確認・修正すること。
MARKER_GROUPS = {
    "upper": {
        "label": "上半身",
        "names": ["chest", "R_upperarm", "L_upperarm",
                  "R_joint", "R_forearm", "L_forearm", "L_joint"],
    },
    "lower": {
        "label": "下半身",
        "names": ["waist", "L_shin", "L_thigh",
                  "R_thigh", "L_knee", "R_knee", "R_shin"],
    },
}
# 起動時に選ばれている部位(GUI のラジオボタン初期値)
DEFAULT_MARKER_GROUP = "upper"

# 現在選択中の部位キー。GUI の[計測開始]時に set_marker_group() で更新される。
MARKER_GROUP = DEFAULT_MARKER_GROUP

# 旧設定名の互換: 選択中の部位のマーカー名(配信順)。set_marker_group() で切り替わる。
EXPECTED_MARKER_NAMES = list(MARKER_GROUPS[DEFAULT_MARKER_GROUP]["names"])

# 座標取得のソース:
#   "labeled_marker" : ラベル付きマーカーを marker_id(1..N)で MARKER_NAMES に対応付ける。
#                      → marker_id=i のマーカー を MARKER_NAMES[i-1] に割り当てる。
#   "marker_set"     : マーカーセット内の「並び順(index)」で対応付ける。
# Motive で個別マーカーにラベルを付けてアセット配信している場合は "labeled_marker"。
MARKER_SOURCE = "labeled_marker"

# labeled_marker のとき、対象アセットの model_id(id_num の上位16bit)。
# None なら model_id を問わず marker_id が 1..len(MARKER_NAMES) の範囲のものを使う。
# 余計なラベル付きマーカー(別アセット/点群)が混じる場合は、診断ログの model= の値を指定する。
# 上半身・下半身のアセットを同時に配信している場合は、ここを部位ごとに指定しないと
# 相手側のマーカーを拾ってしまうので注意(その場合は MARKER_GROUPS に model_id を持たせて
# set_marker_group() で切り替えるとよい)。
MARKER_MODEL_ID = None

# marker_set のとき、位置取得に使うマーカーセット名。
# 空文字 "" のときは、マーカー数が len(MARKER_NAMES) と一致するセットを
# 自動採用する(全点をまとめた "all" セットは除外)。
MARKER_SET_NAME = ""

# オクルージョン(隠れ)判定:
# 隠れマーカーは occluded ビット付き、または (0,0,0) で配信される。原点近傍
# (各軸の絶対値がこの値以内)や NaN を「未認識」とみなす。
OCCLUSION_ORIGIN_EPS = 1e-6

# 起動直後、受信フレームの構造(マーカーセット名・個数・各マーカー座標、
# ラベル付きマーカー、リジッドボディ)を GUI ログへ出力する回数。0 で無効。
# どの形式・並び順で配信されているかを確認し、EXPECTED_MARKER_NAMES / MARKER_SOURCE を
# 合わせるために使う。
MARKER_DIAGNOSTIC_FRAMES = 5

# ストリーミング途絶の警告しきい値 [秒](この時間フレームが来なければ警告)
STREAM_STALE_SEC = 2.0

# --- 出力 ---
OUTPUT_CSV = "sync_dataset.csv"

# 計測対象マーカー名(選択中の部位のマーカーを配信順で並べたもの)。
# 共有状態・CSV 列・スナップショットなどはすべてこの順序に従う。
# 計測開始時に set_marker_group() が選択された部位のものへ差し替える。
MARKER_NAMES = tuple(EXPECTED_MARKER_NAMES)

# 位置情報の先頭列(計測開始からの経過秒 [+ 絶対時刻] + 各マーカー × XYZ)を動的生成する。
# Timestamp 列の中身は「計測ループ開始(最初のサンプル取得直前)を 0 とした経過時間[秒]」。
# WallClock 列は同じサンプルの絶対時刻(ローカル時刻・ミリ秒精度: "YYYY-MM-DD HH:MM:SS.fff")。
# WallClock = 計測開始時の壁時計 + Timestamp となるように基準時刻から算出するため、両列は整合する。
# ウェブカメラ動画(Windows 標準カメラで別撮り)との同期は、この WallClock を基準に行う。
# WallClock 列は「カメラ撮影と同期する」を GUI で ON にしたときだけ付与する(include_wallclock)。
def build_position_header(include_wallclock=True):
    """位置情報の先頭列を作る。include_wallclock=True で Timestamp の次に WallClock を挿入する。"""
    cols = ["Timestamp"]
    if include_wallclock:
        cols.append("WallClock")
    for mk in MARKER_NAMES:
        cols += ["{}_X".format(mk), "{}_Y".format(mk), "{}_Z".format(mk)]
    return cols


# 既定の位置ヘッダー(参考用。実際は計測条件に応じてコントローラが動的生成する)
_POSITION_HEADER = build_position_header(True)


def _fmt_mhz(hz):
    """周波数[Hz]を CSV 列名用の MHz 表記(末尾ゼロを省いた短い形)にする。例: 12.5, 12.51, 12.505"""
    # 点数が多い(刻みが細かい)グリッドでも列名が重複しないよう 4 桁(=100Hz)精度で表記
    s = "{:.4f}".format(hz / 1e6)
    return s.rstrip("0").rstrip(".")


def _vna_columns_for(prefix, freq_grid_hz):
    """
    1 チャンネルぶんの S パラメータ/インピーダンス列名を返す(先頭に prefix_ を付ける)。

    単一周波数(1点)モードでも MHz サフィックスを必ず付ける。列名を掃引モードと同じ
    "<ch>_S11_Real_<MHz>" 形式に統一しておくと、
      ・どの周波数で測ったのかが CSV 自身に残る
      ・再生ビューア(playback_viewer.py)など下流ツールが列を同じ規則で見つけられる
    (以前は 1 点のときだけサフィックスを省いていたため、再生ビューアが
     インピーダンス列を 1 つも認識できずスミスチャートが出なかった)
    """
    cols = []
    for hz in freq_grid_hz:
        lbl = _fmt_mhz(hz)
        cols += ["{}_S11_Real_{}".format(prefix, lbl),
                 "{}_S11_Imag_{}".format(prefix, lbl),
                 "{}_Z_R_{}".format(prefix, lbl),
                 "{}_Z_X_{}".format(prefix, lbl)]
    return cols


def build_csv_header(freq_grid_hz, channel_names=VNA_CHANNEL_NAMES,
                     include_wallclock=True):
    """
    位置情報 + 各 VNA チャンネルの S パラメータ/インピーダンス列を並べた CSV ヘッダーを作る。
    - 位置列(Timestamp [+ WallClock] + 各マーカー XYZ)のあとに、チャンネルごとに 4 列 × 点数を並べる。
    - include_wallclock=True のときのみ絶対時刻列 WallClock を含める(カメラ同期 ON 時)。
    - 列名は "<チャンネル名>_S11_Real_<MHz>" のようにチャンネル名と周波数で区別する。
      (単一周波数(1点)モードでも MHz サフィックスを付ける: _vna_columns_for 参照)
    """
    cols = build_position_header(include_wallclock)
    for name in channel_names:
        cols += _vna_columns_for(name, freq_grid_hz)
    return cols


# 既定ヘッダー(参考。実際は計測条件に応じてコントローラが動的生成する)
CSV_HEADER = build_csv_header(FREQ_GRID_HZ)


# =============================================================================
# ラベル認識: マーカーの並び(marker_id / セット内 index)を名前に対応付けて座標を取り出す
# =============================================================================
#
# フレームデータにマーカー名は含まれない。ラベル付きマーカーは marker_id(1..N)、マーカーセットは
# セット内の並び順で毎フレーム配信されるため、EXPECTED_MARKER_NAMES にその並びを記述しておき、
# marker_id / index で名前へ対応付ける。隠れマーカーは occluded ビット付き or (0,0,0) で来る。

# 診断ログ(受信フレームの構造ダンプ)の残り出力回数。フレームコールバックから減算する。
_diag_remaining = [MARKER_DIAGNOSTIC_FRAMES]

# フレームコールバック(NatNet 受信スレッド)から GUI ログへ文字列を送るためのシンク。
# コントローラが計測開始時に設定する。未設定時は _dbg にフォールバックする。
_frame_log_sink = [None]


def set_frame_log_sink(fn):
    """フレーム受信スレッドからのログ出力先を設定する(None で解除)。"""
    _frame_log_sink[0] = fn


def _flog(msg):
    """フレーム受信スレッドからのログ。GUI ログへ送る(無ければコンソール)。"""
    sink = _frame_log_sink[0]
    if sink is not None:
        try:
            sink(msg)
            return
        except Exception:
            pass
    print(msg)


def _decode_name(raw):
    """マーカーセットの model_name(bytes/str)を文字列にする。"""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw) if raw is not None else ""


def _is_occluded_pos(pos):
    """位置が「未認識(隠れ)」か判定する。原点近傍 or NaN を未認識とみなす。"""
    try:
        x, y, z = float(pos[0]), float(pos[1]), float(pos[2])
    except (TypeError, ValueError, IndexError):
        return True
    if x != x or y != y or z != z:   # NaN チェック
        return True
    eps = OCCLUSION_ORIGIN_EPS
    return abs(x) <= eps and abs(y) <= eps and abs(z) <= eps


def _iter_marker_sets(mocap):
    """(セット名, MarkerData) の列を返す。"""
    ms = getattr(mocap, "marker_set_data", None)
    for md in getattr(ms, "marker_data_list", None) or []:
        yield _decode_name(getattr(md, "model_name", b"")), md


def select_marker_set(mocap):
    """
    座標取得に使うマーカーセットを選ぶ。
    - MARKER_SET_NAME が指定されていればその名前のセット。
    - 空文字なら、マーカー数が len(MARKER_NAMES) と一致するセット("all" は除外)。
    見つからなければ None。
    """
    want = len(MARKER_NAMES)
    sets = list(_iter_marker_sets(mocap))
    if MARKER_SET_NAME:
        for nm, md in sets:
            if nm == MARKER_SET_NAME:
                return md
        return None
    candidates = [(nm, md) for nm, md in sets if nm.lower() != "all"]
    for nm, md in candidates:
        if len(getattr(md, "marker_pos_list", []) or []) == want:
            return md
    return None


def _collect_from_labeled_markers(mocap):
    """
    ラベル付きマーカーを marker_id で MARKER_NAMES に対応付ける。
    id_num は 上位16bit=model_id / 下位16bit=marker_id。marker_id は 1 始まりを想定し、
    marker_id=i のマーカーを MARKER_NAMES[i-1] に割り当てる。
    occluded ビット付き / 原点近傍 / NaN は未認識として除外する。
    MARKER_MODEL_ID が指定されていれば、その model_id 以外は無視する。
    """
    result = {}
    lm = getattr(mocap, "labeled_marker_data", None)
    for mk in getattr(lm, "labeled_marker_list", None) or []:
        if getattr(mk, "param", 0) & 0x01:   # occluded ビット
            continue
        model_id = mk.id_num >> 16
        marker_id = mk.id_num & 0xFFFF
        if MARKER_MODEL_ID is not None and model_id != MARKER_MODEL_ID:
            continue
        idx = marker_id - 1                   # marker_id は 1 始まり
        if not (0 <= idx < len(MARKER_NAMES)):
            continue
        if _is_occluded_pos(mk.pos):
            continue
        result[MARKER_NAMES[idx]] = (float(mk.pos[0]), float(mk.pos[1]), float(mk.pos[2]))
    return result


def _collect_from_marker_set(mocap):
    """マーカーセット内の並び順(index)を MARKER_NAMES に対応付ける。隠れ(0,0,0)は除外。"""
    result = {}
    md = select_marker_set(mocap)
    if md is None:
        return result
    positions = getattr(md, "marker_pos_list", []) or []
    for name, pos in zip(MARKER_NAMES, positions):
        if _is_occluded_pos(pos):
            continue
        result[name] = (float(pos[0]), float(pos[1]), float(pos[2]))
    return result


def collect_named_markers(mocap):
    """
    フレームから {マーカー名: (x, y, z)} を返す(認識できているものだけ)。
    MARKER_SOURCE に従い、ラベル付きマーカー(marker_id)またはマーカーセット(並び順)から集める。
    """
    if MARKER_SOURCE == "marker_set":
        return _collect_from_marker_set(mocap)
    return _collect_from_labeled_markers(mocap)


def log_frame_diagnostic(mocap, named):
    """
    起動直後の数フレーム、受信フレームの構造を GUI ログへ出力する。
    どの形式・並び順で配信されているかを確認し、EXPECTED_MARKER_NAMES /
    MARKER_SET_NAME を合わせるために使う。
    """
    if _diag_remaining[0] <= 0:
        return
    _diag_remaining[0] -= 1
    n = MARKER_DIAGNOSTIC_FRAMES - _diag_remaining[0]

    lines = ["[診断 {}/{}] 受信フレームの構造:".format(n, MARKER_DIAGNOSTIC_FRAMES)]

    sets = list(_iter_marker_sets(mocap))
    lines.append("  MarkerSet 数: {}".format(len(sets)))
    for nm, md in sets:
        pl = getattr(md, "marker_pos_list", []) or []
        lines.append("   - set '{}' : {} 個".format(nm, len(pl)))
        for i, p in enumerate(pl):
            tag = " <隠れ>" if _is_occluded_pos(p) else ""
            lines.append("       [{}] ({:.3f}, {:.3f}, {:.3f}){}".format(
                i, p[0], p[1], p[2], tag))

    lm = getattr(mocap, "labeled_marker_data", None)
    lm_list = getattr(lm, "labeled_marker_list", None) or []
    lines.append("  LabeledMarker 数: {}".format(len(lm_list)))
    for mk in lm_list:
        occ = bool(getattr(mk, "param", 0) & 0x01)
        lines.append("   - id={} (model={}, marker={}) occ={} pos=({:.3f},{:.3f},{:.3f})".format(
            mk.id_num, mk.id_num >> 16, mk.id_num & 0xFFFF, occ,
            mk.pos[0], mk.pos[1], mk.pos[2]))

    rb = getattr(mocap, "rigid_body_data", None)
    rb_list = getattr(rb, "rigid_body_list", None) or []
    lines.append("  RigidBody 数: {}".format(len(rb_list)))
    for r in rb_list:
        lines.append("   - id={} valid={} pos=({:.3f},{:.3f},{:.3f})".format(
            r.id_num, getattr(r, "tracking_valid", None),
            r.pos[0], r.pos[1], r.pos[2]))

    lines.append("  → 名前解決できたマーカー: {}".format(sorted(named.keys())))
    missing = [nm for nm in MARKER_NAMES if nm not in named]
    if missing:
        lines.append("  → 未解決/未認識: {}".format(missing))
    _flog("\n".join(lines))


# =============================================================================
# スレッド間共有: 各マーカーの最新 OptiTrack 座標
# =============================================================================

# Lock で保護される共有状態。NatNet コールバック(書き込み)と
# VNA ループ(読み出し)の双方からアクセスされる。各マーカーごとに {x,y,z,valid} を保持。
_pos_lock = threading.Lock()
_latest_positions = {
    name: {"x": None, "y": None, "z": None, "valid": False}
    for name in MARKER_NAMES
}


def marker_group_label(key=None):
    """部位キー("upper"/"lower")の表示名を返す。key 省略で現在の選択。"""
    g = MARKER_GROUPS.get(key if key is not None else MARKER_GROUP)
    return g["label"] if g else str(key)


def set_marker_group(key):
    """
    計測部位(上半身/下半身)を切り替える。

    MARKER_NAMES(=CSV 位置列・欠損判定・スナップショットの対象)と、共有状態の
    座標スロットをその部位の 7 点で作り直す。計測スレッドが動いていない状態
    (計測開始の直前)から呼ぶこと。
    """
    global MARKER_GROUP, MARKER_NAMES, EXPECTED_MARKER_NAMES
    if key not in MARKER_GROUPS:
        raise ValueError("未知の計測部位: {}".format(key))
    MARKER_GROUP = key
    EXPECTED_MARKER_NAMES = list(MARKER_GROUPS[key]["names"])
    MARKER_NAMES = tuple(EXPECTED_MARKER_NAMES)
    with _pos_lock:
        _latest_positions.clear()
        for name in MARKER_NAMES:
            _latest_positions[name] = {"x": None, "y": None, "z": None,
                                       "valid": False}
    return MARKER_NAMES

# 最後にフレームを受信した時刻(ストリーミング途絶検知用)。Lock 下で更新。
_last_frame_time = [0.0]

# 全スレッド共通の停止フラグ
_stop_event = threading.Event()

# --- FPS 計測用の累計カウンタ ---
# OptiTrack: NatNet コールバック(受信スレッド)で 1 フレームごとに +1
# NanoVNA  : 各チャンネルのリーダースレッドで 1 掃引ごとに +1(チャンネルごとに独立)
# どちらも reset_runtime_state() で 0 にリセットする。
_fps_lock = threading.Lock()
_opti_frame_count = [0]                              # OptiTrack 受信フレーム累計
_vna_sample_counts = [0] * len(VNA_CHANNEL_NAMES)    # 各チャンネルの取得掃引累計


def incr_opti_frame():
    with _fps_lock:
        _opti_frame_count[0] += 1


def incr_vna_sample(ch_index):
    """指定チャンネルの掃引取得カウンタを +1 する。"""
    with _fps_lock:
        _vna_sample_counts[ch_index] += 1


def read_fps_counters():
    """(OptiTrack 累計, [各チャンネルの掃引累計]) を返す。"""
    with _fps_lock:
        return _opti_frame_count[0], list(_vna_sample_counts)


def update_latest_position(name, x, y, z):
    """NatNet コールバックから呼ばれ、指定マーカーの最新座標をスレッド安全に更新する。"""
    with _pos_lock:
        slot = _latest_positions[name]
        slot["x"] = x
        slot["y"] = y
        slot["z"] = z
        slot["valid"] = True


def mark_position_invalid(name):
    """
    NatNet コールバックから呼ばれ、指定マーカーを「このフレームでは未検出(消失/オクルージョン中)」
    として無効化する。古い座標をそのまま出力し続けないよう、valid を False に戻す。
    """
    with _pos_lock:
        _latest_positions[name]["valid"] = False


def mark_frame_received():
    """フレーム受信時刻を更新(ストリーミング生存確認用)。"""
    with _pos_lock:
        _last_frame_time[0] = time.time()


def read_latest_positions():
    """
    VNA ループから呼ばれ、全マーカーの最新座標のスナップショットを
    同一ロック下で一括取得する。
    戻り値: {マーカー名: (x, y, z, valid), ...}(MARKER_NAMES 全件)
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
    (停止フラグ・最新座標・受信時刻・診断ログ回数をクリアする)
    GUI で計測をやり直す場合に、前回の状態が残らないようにする。
    """
    _stop_event.clear()
    with _pos_lock:
        for slot in _latest_positions.values():
            slot["x"] = None
            slot["y"] = None
            slot["z"] = None
            slot["valid"] = False
        _last_frame_time[0] = 0.0
    _diag_remaining[0] = MARKER_DIAGNOSTIC_FRAMES
    with _fps_lock:
        _opti_frame_count[0] = 0
        for i in range(len(_vna_sample_counts)):
            _vna_sample_counts[i] = 0


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


# --- VNA 通信の堅牢化パラメータ(無応答/切断時にフリーズしないため) ---
# 書き込みタイムアウト[秒]: これが無いと、デバイスがハングしたとき write()/flush() が
# 無限にブロックし、close() 時に固まって保存できなくなる。
SERIAL_WRITE_TIMEOUT = 3.0
# 接続(初期化ハンドシェイク)を何回まで試みるか。1 回目が Write timeout 等で失敗しても、
# ポートを開き直して再試行する(前回セッションの残りや列挙直後の不安定状態から回復するため)。
SERIAL_CONNECT_ATTEMPTS = 3
# 掃引読み取り中、これ以上「新しいデータが来ない」状態が続いたら無応答とみなして打ち切る[秒]。
SERIAL_STALL_SEC = 2.5
# 連続で掃引取得に失敗した回数がこれを超えたら GUI に警告を出す。
VNA_STALL_WARN_FAILS = 2
# 連続失敗がこの回数に達したら「VNA 無応答」として計測を止め、保存フローへ移る。
VNA_STALL_LOST_FAILS = 8


class NanoVNA:
    """
    NanoVNA-H4 / JNCRadio VNA 3G など NanoVNA 系のコンソールコマンドを扱う薄いラッパ。

    本機のシリアルは「コマンド\\r を送ると、エコー → 結果行 → プロンプト 'ch> '」
    の順で応答する。scan コマンドで毎回フレッシュな掃引を実行する。

    使用するコマンド(NanoVNA-D ファームウェア):
        sweep {start(Hz)} {stop(Hz)} [points]   掃引条件の設定
        sweep cw {freq(Hz)}                     CW(ゼロスパン)= 単一周波数モードの設定
        scan {start(Hz)} {stop(Hz)} [points] [outmask]
            outmask は出力内容のビット指定: bit0(=1) 周波数, bit1(=2) S11, bit2(=4) S21
            本クラスは「周波数 + S11」を出力(outmask=3)する。校正は適用される。
        freq {freq(Hz)}                         測定せずに発振器だけを設定(pause 状態)
        bandwidth {n}                           DSP 平均化窓(IF 帯域 ≒ 1000/(n+1) Hz)

    掃引条件(開始/終了周波数・点数)は GUI から渡され、初期化時に sweep コマンドで
    デバイスへ設定する。単一周波数モードでは、測定のたびに park() で発振器を
    測定周波数から離し、複数台が同じ CW 周波数を送信し続けないようにする。
    """

    PROMPT = b"ch> "

    def __init__(self, port, baud=115200, timeout=1.0,
                 start_hz=SWEEP_START_HZ, stop_hz=SWEEP_STOP_HZ,
                 points=SCAN_POINTS, park_hz=None):
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
        # 待機中に発振器を逃がす周波数[Hz]。None なら退避しない(1 台だけのときなど)。
        # 複数台が同じ CW 周波数に居座ると相互干渉でΓが回るため、その対策(定数の説明参照)。
        self.park_hz = int(park_hz) if park_hz else None
        # freq コマンドにファームが対応しているか(None=未判定)。park() が最初に判定する。
        # 計測中の退避は scan の終端で行うので、非対応でも計測結果には影響しない。
        self.park_supported = None
        # 直近の scan 応答(失敗時の原因調査用。GUI の警告に一部を載せる)
        self.last_response = ""
        # 接続情報(再オープン時に使う)。write_timeout を必ず設定する。これが無いと
        # デバイスがハングしたとき write()/flush() が無限にブロックし、停止・保存時にフリーズする。
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        # ポートを開いて初期化ハンドシェイクを行う(Write timeout 等は開き直して再試行する)。
        self._open_and_init()

    def _open_serial(self):
        """シリアルポートを開き、DTR/RTS を明示的に有効化する。"""
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                 write_timeout=SERIAL_WRITE_TIMEOUT)
        # 一部の USB シリアルは DTR/RTS が有効でないと送受信できない/目覚めないことがある。
        try:
            self.ser.dtr = True
            self.ser.rts = True
        except Exception:
            pass

    def _open_and_init(self):
        """
        ポートを開いて pause→sweep の初期化を行う。Write timeout(デバイスがデータを
        受け取らない)や SerialException が出た場合は、ポートを閉じて開き直し再試行する。
        全リトライに失敗したら最後の例外を送出する(呼び出し側が接続失敗として扱う)。
        """
        last_err = None
        for attempt in range(SERIAL_CONNECT_ATTEMPTS):
            try:
                if self.ser is None or not self.ser.is_open:
                    self._open_serial()
                # 起動直後は少し待つ(USB 列挙/デバイス側の準備待ち)。回を追うごとに長めに。
                time.sleep(0.3 + 0.4 * attempt)
                self.ser.reset_input_buffer()
                self.ser.reset_output_buffer()
                # 連続出力を止めてプロンプト状態を確定させる
                self._send_raw("pause")
                self._read_until_prompt()
                # 掃引範囲・点数をデバイスへ設定(初期化コマンド)
                self.setup_sweep()
                # 初回計測の前に発振器を退避しておく(他機の 1 回目から干渉させない)
                self.park()
                return  # 成功
            except (serial.SerialTimeoutException, serial.SerialException) as e:
                last_err = e
                # ポートを閉じて開き直す(スタック状態の解消を試みる)
                try:
                    if self.ser is not None:
                        self.ser.close()
                except Exception:
                    pass
                self.ser = None
                time.sleep(0.5)
        # 全リトライ失敗
        if last_err is not None:
            raise last_err
        raise serial.SerialException("VNA の初期化に失敗しました(不明なエラー)。")

    def setup_sweep(self):
        """
        デバイスの掃引条件を設定する。

        単一周波数モードでは、NanoVNA-D ファームウェアが持つ正規の CW(ゼロスパン)
        コマンド `sweep cw {freq}` を使う(内部で start=stop=freq に設定される)。
        掃引モードでは従来どおり `sweep {start} {stop} {points}` を発行する。
        いずれもファームが当該書式に対応していなくても、毎回の scan が範囲と点数を
        指定するため計測自体は継続できる。

        VNA_IF_BANDWIDTH が指定されていれば `bandwidth {n}` も送る(DSP の平均化窓。
        小さい帯域ほど低ノイズだが 1 点あたりの所要時間が伸びる)。
        """
        self.ser.reset_input_buffer()
        if VNA_IF_BANDWIDTH is not None:
            self._send_raw("bandwidth {}".format(int(VNA_IF_BANDWIDTH)))
            self._read_until_prompt()
        if self.single:
            cmd = "sweep cw {freq}".format(freq=self.start_hz)
        else:
            cmd = "sweep {start} {stop} {points}".format(
                start=self.start_hz, stop=self.stop_hz, points=self.points)
        self._send_raw(cmd)
        self._read_until_prompt()

    def park(self):
        """
        発振器を測定周波数から離す(複数台の相互干渉対策)。接続直後と復帰時に使う。

        NanoVNA-D の `freq {Hz}` は pause_sweep() + set_frequency() を行うだけで
        測定はしないため、キャリアだけを退避できる。
        ※ 計測中(ホットパス)ではこのコマンドは使わない。1 サンプルあたりのシリアル
          往復が増えると応答の取りこぼしでコマンドとプロンプトがずれ、「VNA 無応答」で
          計測が止まる事故につながるため、measure_sweep は scan の終端で退避する。
        park_hz が None のとき(1 台運用など)は何もしない。
        失敗しても計測は止めない(次の scan で周波数は上書きされる)。
        """
        if not self.park_hz:
            return
        try:
            self._send_raw("freq {}".format(self.park_hz))
            resp = self._read_until_prompt(max_wait=1.0)
        except (serial.SerialTimeoutException, serial.SerialException) as e:
            _dbg("[WARN] park に失敗(無視): {}".format(e))
            return
        # 未対応ファームは "freq?" のように返す。接続時の 1 回目だけ判定して記録する
        # (呼び出し側が park_supported を見て GUI に警告を出せるようにする)。
        if self.park_supported is None:
            self.park_supported = ("?" not in resp) and ("usage" not in resp.lower())

    def _send_raw(self, cmd):
        """
        コマンドを CR 終端で送信する(本機の終端は <CR>)。
        flush() は使わない(デバイスがハングすると無限にブロックしうるため)。
        write() は write_timeout により有限時間で戻る(超過時は SerialTimeoutException)。
        """
        self.ser.write((cmd + "\r").encode("ascii"))

    def _read_until_prompt(self, max_wait=3.0, stall_sec=SERIAL_STALL_SEC):
        """
        プロンプト 'ch> ' が現れるまで読み、受信テキスト全体を返す。
        - 停止フラグ(_stop_event)が立ったら即座に打ち切る(停止・保存を速くする)。
        - stall_sec の間まったくデータが来なければ、デバイス無応答とみなして打ち切る
          (無応答のまま max_wait いっぱいブロックし続けるのを防ぐ)。
        """
        buf = bytearray()
        now = time.time()
        deadline = now + max_wait
        last_data = now
        while time.time() < deadline:
            if _stop_event.is_set():
                break
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                last_data = time.time()
                if self.PROMPT in buf:
                    break
            else:
                if self.PROMPT in buf:
                    break
                if time.time() - last_data > stall_sec:
                    break  # 無応答: 打ち切る
        return buf.decode("ascii", errors="ignore")

    def recover(self):
        """
        無応答からのリカバリを試みる: 入出力バッファをクリアし pause / sweep を再送する。
        失敗しても例外は投げない(呼び出し側は継続してリトライする)。
        """
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except Exception:
            pass
        try:
            self._send_raw("pause")
            self._read_until_prompt(max_wait=1.0)
            self.setup_sweep()
            self.park()
        except Exception:
            pass

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

        単一周波数モードの scan は 2 通り:
          - 退避あり(park_hz あり): `scan {測定周波数} {退避周波数} 2` の 2 点掃引。
            周波数列を見て測定周波数の点だけを採用し、掃引の終端で発振器が退避周波数に
            残ることを利用して、追加コマンドなしで他機への干渉を防ぐ。
          - 退避なし: `scan {f} {f} {SINGLE_DEVICE_POINTS}` の極小掃引。整定していない
            先頭点を捨てた残りを平均する。
        いずれも論理的には 1 点へ集約して返す(npts=1 を保証)。
        """
        if self.single:
            if self.park_hz:
                dev_stop, dev_points = self.park_hz, 2
            else:
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
        # 失敗時の原因調査用に、直近の応答(先頭のみ)を残す
        self.last_response = text[:200]
        if not triples:
            return None

        if self.single:
            if self.park_hz:
                # 測定周波数に最も近い点を採用する(残りは退避用の捨て点)。
                target = float(self.start_hz)
                best = min(triples, key=lambda t: abs(t[0] - target))
                if abs(best[0] - target) > max(1000.0, target * 0.01):
                    return None   # 測定周波数の点が応答に含まれていない
                usable = [best]
            else:
                # 先頭 SINGLE_DISCARD_POINTS 点は整定していないことがあるので捨てる。
                usable = triples[SINGLE_DISCARD_POINTS:] or triples
            re = float(np.mean([t[1] for t in usable]))
            im = float(np.mean([t[2] for t in usable]))
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

    マーカーセット内の並び順を MARKER_NAMES に対応付けて各マーカーの座標を更新する。
    このフレームで認識できなかったマーカーは無効化し、古い座標を保持し続けないようにする。
    """
    mark_frame_received()
    incr_opti_frame()  # OptiTrack FPS 計測: 受信フレームをカウント

    mocap = data_dict.get("mocap_data")
    if mocap is None:
        return

    # マーカーセットの並び順からラベル名 -> 座標 を得る(認識できているものだけ)。
    named = collect_named_markers(mocap)

    # 起動直後の数フレームは受信構造を診断ログに出す(並び順/形式の確認用)。
    log_frame_diagnostic(mocap, named)

    # 認識できたマーカーは更新、できなかったマーカーは無効化する。
    for name in MARKER_NAMES:
        pos = named.get(name)
        if pos is None:
            mark_position_invalid(name)
        else:
            update_latest_position(name, pos[0], pos[1], pos[2])


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

    # 最初のフレーム受信待ちは呼び出し側(ワーカー)が停止フラグを見ながら行う。
    # ここではブロックしない。
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

def _format_wallclock(epoch_sec):
    """
    エポック秒(time.time() 由来)をローカル時刻の文字列
    "YYYY-MM-DD HH:MM:SS.fff"(ミリ秒精度)へ整形する。
    CSV の WallClock 列・meta.json・GUI 時計表示で共通に使う。動画同期の基準となる。
    """
    dt = datetime.datetime.fromtimestamp(epoch_sec)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + "{:03d}".format(dt.microsecond // 1000)


def _fmt_xyz(snap, name):
    """マーカーの (x,y,z) を CSV セル 3 個へ整形。未受信なら空欄。"""
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

class VnaChannel:
    """
    1 台の nanoVNA の状態を保持する。各チャンネルは専用のリーダースレッドで並行して
    掃引し、最新の掃引結果(seq, S11, Z_R, Z_X)をロック下で publish する。
    コンバイナ(ワーカー)は各チャンネルの最新結果を read_latest で読み、位置と結合する。
    """

    def __init__(self, index, name, com_port):
        self.index = index          # 0,1,... (FPS カウンタ等のインデックス)
        self.name = name            # 列名プレフィックス("VNA1" 等)
        self.com_port = com_port    # 接続先("COM3" 等 / TEST_MODE)
        self.test_mode = (com_port == TEST_MODE)
        self.vna = None
        self.reader = None
        self._lock = threading.Lock()
        self._seq = 0
        self._latest = None         # (seq, s11_arr, z_r, z_x)

    def publish(self, s11_arr, z_r, z_x):
        """リーダースレッドから: 最新の掃引結果を差し替える(seq をインクリメント)。"""
        with self._lock:
            self._seq += 1
            self._latest = (self._seq, s11_arr, z_r, z_x)

    def read_latest(self):
        """コンバイナから: 最新の (seq, s11_arr, z_r, z_x)。未取得なら None。"""
        with self._lock:
            return self._latest


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
        self.channels = []          # VnaChannel のリスト(GUI で選択したチャンネル数ぶん)
        self.client = None
        self.worker = None
        self._reader_threads = []   # VNA リーダースレッド(交互:1本 / 並列:台数ぶん)
        self.rows = []
        self.rows_lock = threading.Lock()
        self.sample_count = 0
        # 動画同期用: 計測(結合ループ)の開始/終了の絶対時刻(エポック秒)。
        # save_csv でサイドカー meta.json に書き出す。
        self.meas_start_wall = None
        self.meas_stop_wall = None
        # webカメラ録画の情報(App が計測後にセット)。meta.json に動画ファイル名・録画開始時刻を残す。
        self.video_info = None
        self._cleaned = False
        self._cleanup_lock = threading.Lock()
        # [計測開始]時に GUI から渡される計測条件(既定値で初期化)
        self.com_ports = [NANOVNA_PORT]
        self.start_hz = SWEEP_START_HZ
        self.stop_hz = SWEEP_STOP_HZ
        self.points = SCAN_POINTS
        self.freq_grid_hz = FREQ_GRID_HZ
        self.csv_header = CSV_HEADER
        # 計測部位(上半身/下半身)と、そのマーカー名(配信順)。start() で確定し meta.json に残す。
        self.marker_group = MARKER_GROUP
        self.marker_names = list(MARKER_NAMES)
        self.use_optitrack = True   # False で OptiTrack を使わない(VNA のみ計測)
        self.use_camera_sync = True  # False でカメラ動画同期を使わない(WallClock 列/meta.json を出さない)

    # ---- GUI へ通知 ----
    def _post(self, kind, value=None):
        try:
            self.out_queue.put_nowait((kind, value))
        except Exception:
            pass

    # ---- 開始 ----
    def start(self, com_ports, start_hz, stop_hz, points, use_optitrack=True,
              use_camera_sync=True):
        """
        共有状態を初期化し、計測ワーカースレッドを起動する。
        com_ports は GUI で選択された各チャンネルの接続先 COM ポートのリスト
        (例 ["COM3", "COM4"])。要素数ぶんの nanoVNA を並行して計測する。
        start_hz/stop_hz/points は GUI で指定された掃引条件(全チャンネル共通)。
        use_optitrack=False のときは OptiTrack(NatNet)を使わず VNA のみ計測する。
        use_camera_sync=False のときは動画同期用の絶対時刻(WallClock 列・meta.json)を出さない。
        この条件から周波数グリッドと CSV ヘッダーを動的に生成する。
        """
        self.com_ports = list(com_ports)
        self.use_optitrack = bool(use_optitrack)
        self.use_camera_sync = bool(use_camera_sync)
        self.start_hz = int(start_hz)
        self.stop_hz = int(stop_hz)
        self.points = int(points)
        # 単一周波数(ピンポイント)モードの正規化: 点数 1 または 開始==終了 -> 1 点に揃える
        if self.points <= 1 or self.start_hz == self.stop_hz:
            self.points = 1
            self.stop_hz = self.start_hz
        # 計測部位は GUI が start() の直前に set_marker_group() で確定させている。
        # ここで控えておき、CSV ヘッダー生成と meta.json の記録に使う。
        self.marker_group = MARKER_GROUP
        self.marker_names = list(MARKER_NAMES)
        self.freq_grid_hz = make_freq_grid(self.start_hz, self.stop_hz, self.points)
        self.csv_header = build_csv_header(
            self.freq_grid_hz, VNA_CHANNEL_NAMES,
            include_wallclock=self.use_camera_sync)

        # チャンネルを構築(名前は VNA_CHANNEL_NAMES、ポートは GUI 選択)
        self.channels = [
            VnaChannel(i, VNA_CHANNEL_NAMES[i], self.com_ports[i])
            for i in range(len(self.com_ports))
        ]

        reset_runtime_state()
        with self.rows_lock:
            self.rows = []
        self.sample_count = 0
        self._cleaned = False
        self.worker = threading.Thread(
            target=self._worker_loop, name="MeasurementWorker", daemon=True)
        self.worker.start()

    # ---- ワーカースレッド本体(接続 → リーダー起動 → コンバイナ) ----
    def _worker_loop(self):
        # 0) 単一周波数(CW)モードで実機が 2 台以上あるときは、待機中の発振器を
        #    測定周波数から離す(相互干渉で Γ が回るのを防ぐ。VNA_PARK_* の説明参照)。
        n_real = sum(1 for ch in self.channels if not ch.test_mode)
        park_hz = None
        if VNA_PARK_ENABLE and self.points <= 1 and n_real >= 2:
            park_hz = park_freq_for(self.start_hz)
            if park_hz is None:
                self._post("status",
                           "[警告] 測定周波数が機体の上限に近く、発振器の退避先を確保"
                           "できません。2 台が同じ周波数を送信し続けるため、Γ が"
                           "ゆっくり回転する可能性があります。")
            else:
                self._post("status",
                           "単一周波数モードの相互干渉対策: 各 scan を {:.4f} MHz(測定)→"
                           "{:.4f} MHz(退避)の 2 点掃引にして、待機中は測定周波数を"
                           "送信しないようにします。".format(
                               self.start_hz / 1e6, park_hz / 1e6))
            if park_hz is not None and not VNA_ALTERNATE_SWEEP:
                self._post("status",
                           "[警告] 並列掃引モード(VNA_ALTERNATE_SWEEP=False)では 2 台が"
                           "同時に同じ周波数を送信するため、退避しても干渉は防げません。"
                           "単一周波数で 2 台使う場合は交互計測を推奨します。")

        # 1) 全チャンネルの VNA を接続。1 台でも開けなければ中止する。
        for ch in self.channels:
            if ch.test_mode:
                self._post("status",
                           "[{}] 【VNAなしテストモード】接続をスキップ(列は 0)。".format(ch.name))
                continue
            # SerialException / FileNotFoundError はいずれも OSError のサブクラス。
            try:
                ch.vna = NanoVNA(ch.com_port, NANOVNA_BAUD,
                                 start_hz=self.start_hz, stop_hz=self.stop_hz,
                                 points=self.points, park_hz=park_hz)
            except OSError as e:
                hint = ""
                if "timeout" in str(e).lower():
                    # Write timeout = デバイスがデータを受け取らない(ハング/前回の残り/USB不調)。
                    hint = ("\n\n【Write timeout の対処】VNA がコマンドを受け付けていません。\n"
                            "・VNA を一度 USB から抜き差し(再接続)してから[ポート再検索]→再選択してください。\n"
                            "・NanoVNA-App など他ソフトが同じ VNA を開いていないか確認してください。\n"
                            "・USB ハブ経由なら直挿しに、別ポートに変えると改善することがあります。\n"
                            "・カメラ等と同じ USB バスで帯域が逼迫している場合は、別系統の USB 端子へ。")
                self._post("error",
                           "[{}] VNAの接続に失敗しました(COM: {})。\n"
                           "COM番号が正しいか、他のソフトが占有していないか確認してください。\n"
                           "(2 台の VNA が同じポートを指していないかも確認してください){}\n"
                           "\n詳細: {}".format(ch.name, ch.com_port, hint, e))
                self._close_all_vnas()
                self._post("finished")
                return
            except Exception as e:
                self._post("error", "[{}] VNA 初期化中に予期せぬ例外: {}".format(ch.name, e))
                self._close_all_vnas()
                self._post("finished")
                return
            if self.points <= 1:
                if park_hz:
                    cond = ("CW(単一周波数) {:.4f} MHz / 掃引終端 {:.4f} MHz で"
                            "発振器を退避".format(self.start_hz / 1e6, park_hz / 1e6))
                else:
                    cond = "CW(単一周波数) {:.4f} MHz / 実機 {} 点平均".format(
                        self.start_hz / 1e6,
                        SINGLE_DEVICE_POINTS - SINGLE_DISCARD_POINTS)
            else:
                cond = "掃引 {:.3f}-{:.3f} MHz {}点".format(
                    self.start_hz / 1e6, self.stop_hz / 1e6, self.points)
            self._post("status",
                       "[{}] VNA 接続 OK: {} @ {}bps / {} ({})".format(
                           ch.name, ch.com_port, NANOVNA_BAUD, cond, VNA_SPARAM))
            if park_hz and ch.vna.park_supported is False:
                # 計測中の退避は scan の終端で行うので、freq 非対応でも実害は無い
                # (影響するのは 1 回目の測定前だけ)。念のためログに残す。
                _dbg("[INFO] [{}] freq コマンド未対応(初回測定前の退避のみ省略)。".format(
                    ch.name))

        # 2) NatNet 開始(ブロックしない)。OptiTrack を使わないモードでは丸ごとスキップ。
        if not self.use_optitrack:
            self.client = None
            self._post("status",
                       "【OptiTrack なしモード】NatNet を使用しません(VNA のみ計測)。座標列は空欄になります。")
        else:
            # フレーム受信スレッドからの診断ログを GUI ログへ流すシンクを登録する。
            set_frame_log_sink(lambda m: self._post("log", m))
            self.client = start_natnet()
            if self.client is None:
                self._post("status", "OptiTrack なしで継続(座標列は空欄になります)。")
            else:
                self._post("status",
                           "OptiTrack 受信開始。全 {} マーカーが揃ったサンプルのみ記録します"
                           "(未認識のフレームは自動破棄)。".format(len(MARKER_NAMES)))

        # 3) 最初のフレーム受信を「停止フラグを見ながら」少し待つ(状態表示のため)
        if self.client is not None:
            deadline = time.time() + STREAM_STALE_SEC
            while not _stop_event.is_set() and seconds_since_last_frame() is None:
                if time.time() > deadline:
                    self._post("status",
                               "[警告] OptiTrack フレームがまだ届きません。Motive の Data Streaming"
                               "(Markers / 127.0.0.1 / Unicast)を確認してください。")
                    break
                time.sleep(0.05)

        # 4) リーダースレッドを起動して各 VNA を掃引する。
        #    交互モード(既定): 1 本のスレッドが「実機だけ」を 1 台ずつ順番に掃引する。
        #      → 2 台が同時に送信しないため RF 干渉によるノイズを防げる。テストモードの
        #        チャンネルは掃引せずダミー 0 を埋めるだけなので、実機 1 台だけのときは
        #        その 1 台を全速で掃引でき、交互待ちで FPS を損なわない。
        #    並列モード: チャンネルごとに専用スレッドで同時掃引する(高速だが干渉しうる)。
        n_real = sum(1 for ch in self.channels if not ch.test_mode)
        self._reader_threads = []
        if not _stop_event.is_set():
            if VNA_ALTERNATE_SWEEP:
                t = threading.Thread(
                    target=self._alternating_reader_loop,
                    name="VnaReader-alt", daemon=True)
                self._reader_threads.append(t)
                t.start()
                if n_real >= 2:
                    self._post("status",
                               "交互計測モード: 実機 {} 台を 1 台ずつ順番に掃引します"
                               "(RF 干渉対策。各台の FPS は同時掃引の約 1/{} になります)。".format(
                                   n_real, n_real))
                elif n_real == 1:
                    self._post("status",
                               "接続された実機 1 台のみを全速で掃引します"
                               "(他チャンネルはテストモード=ダミー 0。交互待ちなしで FPS を損ないません)。")
            else:
                for ch in self.channels:
                    t = threading.Thread(
                        target=self._reader_loop, args=(ch,),
                        name="VnaReader-{}".format(ch.name), daemon=True)
                    ch.reader = t
                    self._reader_threads.append(t)
                    t.start()

        # 5) コンバイナ: 全チャンネルの最新掃引 + 位置を 1 行に結合して蓄積する
        self._post("status", "計測中...")
        self._combine_loop()
        # 動画同期用に計測終了の絶対時刻を記録する
        self.meas_stop_wall = time.time()

        self._post("finished")

    # ---- 1 台を 1 回だけ掃引して publish する(交互/並列で共用) ----
    def _sweep_once(self, ch, npts, state):
        """
        指定チャンネルを 1 回掃引し、成功したら ch へ publish する。
        戻り値: 継続してよければ True、停止/VNA 無応答なら False。

        state: {ch.index: {"warn": 最後に警告した時刻, "fails": 連続失敗回数}} を保持する dict。
        VNA が応答しない(データ取得失敗が続く)場合でも "error" は投げない。
        代わりに警告→リカバリ試行→(連続失敗が続けば)"vna_lost" を送って計測を止め、
        それまでに集めたデータを保存できるようにする。
        """
        st = state.setdefault(ch.index, {"warn": 0.0, "fails": 0})

        if ch.test_mode:
            # VNA は使わない。全点ダミー 0。間隔をあけて publish。
            s11 = np.zeros(npts, dtype=complex)
            z_r = np.zeros(npts)
            z_x = np.zeros(npts)
            if _stop_event.wait(TEST_MODE_INTERVAL_SEC):
                return False
        else:
            try:
                result = ch.vna.measure_sweep()
            except serial.SerialException as e:
                # ポート切断など: 復帰不能とみなし、保存フローへ移る(データは保持)
                self._post("vna_lost",
                           "[{}] VNA との通信が切断されました: {}".format(ch.name, e))
                _stop_event.set()
                return False
            except Exception as e:
                self._post("vna_lost",
                           "[{}] VNA 取得中に予期せぬ例外: {}".format(ch.name, e))
                _stop_event.set()
                return False

            # データ取得の成否を判定(None=取得失敗 / 点数不一致=不正)
            fail_reason = None
            if result is None:
                fail_reason = "掃引データを取得できません"
            elif len(result[1]) != npts:
                fail_reason = "掃引点数が {} 点でした(期待 {} 点)".format(
                    len(result[1]), npts)

            if fail_reason is not None:
                st["fails"] += 1
                now = time.time()
                # 何度か失敗したらリカバリ(pause/sweep 再送)を試みる
                if st["fails"] % 3 == 0 and ch.vna is not None:
                    ch.vna.recover()
                # 連続失敗が続けば「無応答」として停止・保存へ
                if st["fails"] >= VNA_STALL_LOST_FAILS:
                    # 何を受信していたのかを残す(通信ずれ/エラー応答の切り分け用)
                    raw = getattr(ch.vna, "last_response", "") or ""
                    raw = raw.replace("\r", " ").replace("\n", " ").strip()
                    self._post("vna_lost",
                               "[{}] VNA が応答しません({}。{} 回連続で失敗)。"
                               "計測を停止して保存します。\n直近の応答: {}".format(
                                   ch.name, fail_reason, st["fails"],
                                   repr(raw[:120]) if raw else "(受信なし)"))
                    _stop_event.set()
                    return False
                # 警告は間引いて出す
                if st["fails"] >= VNA_STALL_WARN_FAILS and now - st["warn"] > 2.0:
                    st["warn"] = now
                    self._post("status",
                               "[{}][警告] {}(リトライ中 {} 回)。".format(
                                   ch.name, fail_reason, st["fails"]))
                return True  # スキップして次のリトライへ

            # 成功: 連続失敗カウンタをリセットして変換・publish
            st["fails"] = 0
            freqs, s11 = result
            # scikit-rf で全点をまとめて Z11 に変換(この計測の周波数グリッドで)
            z_r, z_x = s11_sweep_to_z(s11, self.freq_grid_hz, Z0)
        ch.publish(s11, z_r, z_x)
        incr_vna_sample(ch.index)  # チャンネル別 FPS 計測
        return True

    # ---- 並列モード: 1 チャンネル専用リーダースレッド ----
    def _reader_loop(self, ch):
        """1 台の nanoVNA を連続して掃引し publish する(並列モード用)。"""
        warn_state = {}
        while not _stop_event.is_set():
            if not self._sweep_once(ch, self.points, warn_state):
                break

    def _publish_test_dummy(self, ch):
        """テストモードのチャンネルにダミー 0 の掃引を publish する(待ち時間なし)。"""
        npts = self.points
        ch.publish(np.zeros(npts, dtype=complex), np.zeros(npts), np.zeros(npts))
        incr_vna_sample(ch.index)

    # ---- 交互モード: 1 本のスレッドで「実機だけ」を 1 台ずつ順番に掃引 ----
    def _alternating_reader_loop(self):
        """
        実機のチャンネルだけをラウンドロビンで 1 台ずつ掃引する。同時に 2 台が送信しないため
        RF 干渉によるノイズを防ぐ。掃引間に待ち時間は入れないので FPS 低下を最小化する。
        テストモードのチャンネルは掃引せず、実機の掃引ごとにダミー 0 を埋める
        (実機 1 台だけのときは、その 1 台を全速で掃引できる)。
        全チャンネルがテストモードのときは、全チャンネルをダミーで順に埋める。
        """
        warn_state = {}
        real = [ch for ch in self.channels if not ch.test_mode]
        test = [ch for ch in self.channels if ch.test_mode]
        # 掃引対象: 実機があれば実機のみ。全てテストなら全チャンネル(ダミー生成)。
        sweep_list = real if real else self.channels
        while not _stop_event.is_set():
            for ch in sweep_list:
                if _stop_event.is_set():
                    break
                if not self._sweep_once(ch, self.points, warn_state):
                    return
                # 実機掃引のたびにテストチャンネルを埋め、コンバイナが行を作れるようにする
                if real:
                    for tch in test:
                        self._publish_test_dummy(tch)

    # ---- コンバイナ: 全チャンネルの最新掃引が揃ったら位置と結合して 1 行にする ----
    def _combine_loop(self):
        """
        各チャンネルが「前回消費より新しい掃引」を持つのを待ち、その瞬間の
        マーカー座標と結合して 1 行にする。ペースは最も遅い VNA に律速される。
        マーカーが 1 個でも未認識のサンプルは破棄する。
        """
        npts = self.points
        nch = len(self.channels)
        last_stale_warn = 0.0
        last_missing_warn = 0.0
        last_seqs = [0] * nch
        # 経過時間の基準(この時点=最初の結合直前を 0 秒とする)。
        # 単調増加時計 perf_counter を使い、システム時刻補正の影響を受けないようにする。
        t0 = time.perf_counter()
        # 動画同期用の絶対時刻(壁時計)の基準。t0 とほぼ同時刻に取得し、
        # 各サンプルの WallClock = wall0 + (perf_counter - t0) として算出する。
        # → 相対 Timestamp と絶対 WallClock がドリフトせず整合する。
        wall0 = time.time()
        self.meas_start_wall = wall0   # meta.json / ログ用(計測開始の絶対時刻)
        while not _stop_event.is_set():
            # 全チャンネルが「前回消費より新しい掃引」を持つまで待つ
            latests = [ch.read_latest() for ch in self.channels]
            if any(l is None for l in latests) or \
                    not all(latests[i][0] > last_seqs[i] for i in range(nch)):
                if _stop_event.wait(0.003):
                    break
                continue
            for i in range(nch):
                last_seqs[i] = latests[i][0]

            # OptiTrack ストリーミング途絶の警告(座標が古い可能性)
            if self.client is not None:
                elapsed = seconds_since_last_frame()
                now = time.time()
                if (elapsed is not None and elapsed > STREAM_STALE_SEC
                        and (now - last_stale_warn) > STREAM_STALE_SEC):
                    last_stale_warn = now
                    self._post("status",
                               "[警告] OptiTrack フレームが {:.1f}s 途絶(座標が古い可能性)".format(elapsed))

            snap = read_latest_positions()

            # OptiTrack 使用時、マーカーのいずれかが未認識ならこのサンプルは行ごと破棄する。
            if self.client is not None and not all(snap[m][3] for m in MARKER_NAMES):
                now = time.time()
                if now - last_missing_warn > 2.0:
                    last_missing_warn = now
                    missing = [m for m in MARKER_NAMES if not snap[m][3]]
                    self._post("status",
                               "[警告] マーカー未認識のためサンプルを破棄: {}".format(
                                   ", ".join(missing)))
                continue

            # タイムスタンプ = 計測開始からの経過時間[秒](ミリ秒精度)
            elapsed = time.perf_counter() - t0
            ts = "{:.3f}".format(elapsed)
            # 先頭列: Timestamp [+ WallClock(カメラ同期 ON 時のみ)]
            head_cols = [ts]
            if self.use_camera_sync:
                # 絶対時刻(壁時計)。動画(Windows カメラで別撮り)との突き合わせ基準。
                head_cols.append(_format_wallclock(wall0 + elapsed))
            pos_cols = []
            for name in MARKER_NAMES:
                pos_cols += _fmt_xyz(snap, name)
            # 各チャンネルの掃引を順に展開(列順は VNA_CHANNEL_NAMES と一致)
            vna_cols = []
            for i in range(nch):
                _, s11, z_r, z_x = latests[i]
                for k in range(npts):
                    vna_cols += ["{:.6f}".format(s11[k].real),
                                 "{:.6f}".format(s11[k].imag),
                                 "{:.4f}".format(z_r[k]),
                                 "{:.4f}".format(z_x[k])]
            row = head_cols + pos_cols + vna_cols
            with self.rows_lock:
                self.rows.append(row)
                self.sample_count += 1
                count = self.sample_count
            self._post("sample", count)
            # リアルタイムグラフ用に全チャンネルの (S11, Z_R, Z_X) を送る(GUI 側で間引いて描画)
            plot_payload = [
                (np.asarray(latests[i][1]), np.asarray(latests[i][2]),
                 np.asarray(latests[i][3]))
                for i in range(nch)]
            self._post("plot", plot_payload)

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

            # 1) ワーカー(コンバイナ)スレッドの終了を待つ
            if self.worker is not None and self.worker.is_alive():
                if self.worker is not threading.current_thread():
                    self.worker.join(timeout=8.0)

            # 2) リーダースレッド(交互:1本 / 並列:台数ぶん)の終了を待つ
            #    (measure_sweep が最長 ~8s ブロックしうるため余裕をもって join する)
            for r in self._reader_threads:
                if r is not None and r.is_alive() and r is not threading.current_thread():
                    r.join(timeout=10.0)

            # 3) 全チャンネルの VNA(シリアル)を閉じる -> COM ポート解放
            self._close_all_vnas()

            # 4) NatNet を停止 -> UDP ソケット/受信スレッド解放
            if self.client is not None:
                shutdown_natnet(self.client)
                self.client = None

            # フレーム受信スレッドからのログ出力先を解除する。
            set_frame_log_sink(None)

            self._cleaned = True

    # ---- 全チャンネルの VNA を閉じる(冪等) ----
    def _close_all_vnas(self):
        for ch in self.channels:
            if ch.vna is not None:
                try:
                    ch.vna.close()
                except Exception as e:
                    _dbg("[WARN] [{}] VNA close 中に例外(無視): {}".format(ch.name, e))
                ch.vna = None

    # ---- CSV 保存 ----
    def save_csv(self, path):
        """蓄積済みデータを CSV に書き出す。書き出した行数を返す。"""
        with self.rows_lock:
            rows = list(self.rows)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self.csv_header)  # 計測条件に応じて動的生成したヘッダー
            writer.writerows(rows)
        # 動画同期用のサイドカー(<csv>.meta.json)を書き出す(カメラ同期 ON 時のみ)。
        # 突き合わせ自体は CSV の WallClock 列で行えるが、計測全体の絶対時刻の
        # 目安として start/end を残しておくと、動画ファイルの録画時刻との対応確認に役立つ。
        if self.use_camera_sync:
            self._save_meta(path, len(rows))
        return len(rows)

    def _save_meta(self, csv_path, n_rows):
        """CSV の隣に <csv>.meta.json を書き出す(動画同期の補助情報)。失敗しても無視する。"""
        # 先頭行/末尾行の WallClock も入れておく(動画突き合わせの実データ基準)。
        with self.rows_lock:
            first_wall = self.rows[0][1] if self.rows else None
            last_wall = self.rows[-1][1] if self.rows else None
        meta = {
            "csv_file": os.path.basename(csv_path),
            "n_samples": n_rows,
            "marker_group": self.marker_group,             # "upper"(上半身) / "lower"(下半身)
            "marker_group_label": marker_group_label(self.marker_group),
            "marker_names": list(self.marker_names),       # CSV の位置列の並び順
            "timestamp_column": "Timestamp",       # 計測開始からの経過秒
            "wallclock_column": "WallClock",        # 絶対時刻(ローカル・ミリ秒)
            "wallclock_format": "%Y-%m-%d %H:%M:%S.%f (ローカル時刻・ミリ秒)",
            "meas_start_wall": (_format_wallclock(self.meas_start_wall)
                                if self.meas_start_wall else None),
            "meas_stop_wall": (_format_wallclock(self.meas_stop_wall)
                               if self.meas_stop_wall else None),
            "first_sample_wall": first_wall,
            "last_sample_wall": last_wall,
            "video_sync_note": (
                "CSV の WallClock と動画の録画開始時刻を sync_video_with_dataset.py で"
                "突き合わせて同期する。"),
        }
        # webカメラを本アプリで録画した場合は、その動画情報も残す(同期の基準に使える)。
        if self.video_info:
            vi = self.video_info
            meta["video_file"] = vi.get("file")
            meta["video_start_wall"] = (_format_wallclock(vi["start_wall"])
                                        if vi.get("start_wall") else None)
            meta["video_fps"] = vi.get("fps")
            meta["video_frames"] = vi.get("frames")
            meta["video_size"] = ([vi.get("width"), vi.get("height")]
                                  if vi.get("width") else None)
        meta_path = os.path.splitext(csv_path)[0] + ".meta.json"
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _dbg("[WARN] meta.json の保存に失敗(無視): {}".format(e))

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
        # スミスチャート(目盛ラベル付き)が初期表示から切れずに収まるよう広めにとる。
        self.geometry("940x860")
        self.minsize(800, 640)

        self.msg_queue = queue.Queue()
        self.controller = None
        self.measuring = False
        # webカメラ録画 / ライブ表示ダッシュボード(計測開始で起動、停止でクローズ)
        self.camera = None            # CameraRecorder(計測中のみ)
        self.dashboard = None         # LiveDashboard(計測中のみ)
        self.video_tmp_path = None    # 録画中の一時 mp4 パス(保存時に CSV の隣へ移動)
        self.video_start_wall = None  # 録画開始のエポック秒(ファイル名/meta 用)
        self.camera_info = None       # camera.stop() の戻り(保存時に使用)
        self._finalizing = False  # 停止→保存→(終了 or 待機復帰)の処理中フラグ
        self._exit_after_save = False  # True: 保存後にアプリ終了 / False: 待機状態へ戻る
        self._latest_plot = None  # 直近の各チャンネル [(s11, z_r, z_x), ...]。poll ごとに1回描画
        self._counter_active = False  # コンソールの \r カウンタ行が出ているか

        # --- FPS 計測の状態(GUI=メインスレッドで 1 秒ごとに算出) ---
        n_ch = len(VNA_CHANNEL_NAMES)
        self._latest_count = 0          # 直近のサンプル数(結合行数)
        self._opti_fps = 0.0            # 直近 1 秒の OptiTrack FPS
        self._vna_fps = [0.0] * n_ch    # 直近 1 秒の 各 VNA 掃引 FPS
        self._fps_win_start = None      # FPS 計算ウィンドウの起点時刻
        self._fps_win_opti = 0          # ウィンドウ起点での OptiTrack 累計
        self._fps_win_vna = [0] * n_ch  # ウィンドウ起点での 各 VNA 累計
        self._meas_start_time = None    # 計測全体の開始時刻(平均 FPS 用)
        self._meas_stop_time = None     # 計測全体の停止時刻(平均 FPS 用)

        # スミスチャート用の周波数グリッド(Hz)。計測条件に合わせて更新され、
        # 「読み取り周波数」に最も近い掃引点を探すのに使う。
        self._plot_freq_hz = np.asarray(FREQ_GRID_HZ, dtype=float)
        # 単一周波数(1点)モードのスミスチャート用: 直近サンプルの Γ 軌跡(チャンネル別)。
        # 1 点だけだと「点が 1 個ちらつく」だけで動きが読めないため、直近
        # TIME_PLOT_WINDOW サンプルを線でつないで時間方向の軌跡として描く。
        self._smith_trail = [collections.deque(maxlen=TIME_PLOT_WINDOW)
                             for _ in VNA_CHANNEL_NAMES]

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
            text=("計測: {}（固定）  /  VNA: {} 台  /  マーカー: {} 個（ラベル認識）\n"
                  "各 VNA の COM ポートと掃引条件、計測部位（上半身/下半身）を設定して[計測開始]。"
                  "全マーカーが揃ったサンプルのみ記録します。"
                  ).format(VNA_SPARAM, len(VNA_CHANNEL_NAMES),
                           len(MARKER_GROUPS[DEFAULT_MARKER_GROUP]["names"])),
            justify="left")
        info.pack(anchor="w", **pad)

        # --- VNA 接続先 COM ポート選択(チャンネルぶん) + 再検索 ---
        # nanoVNA を複数台つなぐ場合、チャンネルごとに別々の COM ポートを選ぶ。
        self.port_vars = []
        self.port_combos = []
        for i, name in enumerate(VNA_CHANNEL_NAMES):
            row = ttk.Frame(self)
            row.pack(fill="x", **pad)
            ttk.Label(row, text="{} COM:".format(name), width=10).pack(side="left")
            var = tk.StringVar(value="")
            combo = ttk.Combobox(row, textvariable=var, values=[],
                                 state="readonly", width=34)
            combo.pack(side="left", padx=6)
            self.port_vars.append(var)
            self.port_combos.append(combo)
            # 再検索ボタンは最初の行にだけ置く(押すと全チャンネルの一覧を更新する)
            if i == 0:
                self.rescan_btn = ttk.Button(
                    row, text="ポート再検索", command=self.on_rescan_ports)
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

        # --- 読み取り周波数(スミスチャート上の数値表示に使う) ---
        # ここで指定した周波数に最も近い掃引点のインピーダンスを数値で表示する。
        # 計測中でも変更でき、次の更新から反映される。
        readout_frame = ttk.Frame(self)
        readout_frame.pack(fill="x", **pad)
        ttk.Label(readout_frame, text="読み取り周波数[MHz]:").pack(side="left")
        self.readout_var = tk.StringVar(value="{:g}".format(TARGET_FREQ_HZ / 1e6))
        self.readout_entry = ttk.Entry(
            readout_frame, textvariable=self.readout_var, width=10)
        self.readout_entry.pack(side="left", padx=(2, 8))
        ttk.Label(readout_frame,
                  text="(この周波数のインピーダンスを各 body について数値表示)"
                  ).pack(side="left")

        # --- 計測部位(上半身 / 下半身)の選択 ---
        # 計測開始前にどちらを計測するか選ぶ。選んだ側の 7 マーカーだけを記録し、
        # CSV の位置列・ライブ表示・マーカー欠損判定もその 7 点で行う。
        body_frame = ttk.Frame(self)
        body_frame.pack(fill="x", **pad)
        ttk.Label(body_frame, text="計測部位:").pack(side="left")
        self.body_var = tk.StringVar(value=DEFAULT_MARKER_GROUP)
        self.body_radios = []
        for key in MARKER_GROUPS:
            rb = ttk.Radiobutton(
                body_frame, text="{}（{}点）".format(
                    MARKER_GROUPS[key]["label"], len(MARKER_GROUPS[key]["names"])),
                value=key, variable=self.body_var, command=self._on_body_change)
            rb.pack(side="left", padx=4)
            self.body_radios.append(rb)
        self.body_markers_var = tk.StringVar(value="")
        ttk.Label(body_frame, textvariable=self.body_markers_var,
                  foreground="#555").pack(side="left", padx=6)
        self._on_body_change()   # マーカー一覧ラベルを初期化

        # --- 計測モード(OptiTrack を使うか) ---
        opt_frame = ttk.Frame(self)
        opt_frame.pack(fill="x", **pad)
        self.optitrack_var = tk.BooleanVar(value=True)
        self.optitrack_chk = ttk.Checkbutton(
            opt_frame,
            text="OptiTrack を使う（OFF にすると VNA のみ計測：Motive 未接続でも可）",
            variable=self.optitrack_var)
        self.optitrack_chk.pack(side="left")

        # --- カメラ撮影(ウェブカメラ動画)と同期するか ---
        # ON のとき、計測開始と連動して webカメラ(OpenCV)を録画開始し、絶対時刻列 WallClock と
        # 保存時のサイドカー meta.json を記録する。動画は計測終了後に CSV の隣へ保存する。OFF なら従来形式。
        cam_frame = ttk.Frame(self)
        cam_frame.pack(fill="x", **pad)
        self.camera_var = tk.BooleanVar(value=True)
        self.camera_chk = ttk.Checkbutton(
            cam_frame,
            text="カメラ撮影と同期する（計測開始でwebカメラを録画。WallClock列/meta.jsonも記録）",
            variable=self.camera_var)
        self.camera_chk.pack(side="left")
        ttk.Label(cam_frame, text="  カメラ番号:").pack(side="left")
        self.camera_index_var = tk.StringVar(value="1")
        self.camera_index_spin = ttk.Spinbox(
            cam_frame, textvariable=self.camera_index_var, from_=0, to=8, width=4)
        self.camera_index_spin.pack(side="left", padx=2)

        # --- 計測中にライブ表示(3D/肘角度/スミス/カメラ)を別ウィンドウで開くか ---
        # 表示は nanoVNA の掃引レートに律速されず、専用の高速タイマーで最新値を直接読んで更新する。
        live_frame = ttk.Frame(self)
        live_frame.pack(fill="x", **pad)
        self.live_var = tk.BooleanVar(value=True)
        self.live_chk = ttk.Checkbutton(
            live_frame,
            text="計測中にライブ表示を開く（別ウィンドウ：3Dマーカー/関節角度（肘 or 膝）/スミス/カメラ映像。掃引レート非依存で更新）",
            variable=self.live_var)
        self.live_chk.pack(side="left")

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

        # --- サンプリングレート(FPS)表示 + 現在時刻(動画同期の基準時計)---
        fps_frame = ttk.Frame(self)
        fps_frame.pack(fill="x", **pad)
        ttk.Label(fps_frame, text="取得レート:").pack(side="left")
        self.fps_var = tk.StringVar(value=self._fps_default_text())
        ttk.Label(fps_frame, textvariable=self.fps_var,
                  foreground="#06c").pack(side="left", padx=6)
        # 現在時刻(壁時計)。動画は Windows 標準カメラで別撮りし、この時計(=CSV の WallClock)を
        # 共有の時刻軸として後処理で突き合わせる。PC の時計が動画側と同一であることの確認にも使う。
        self.clock_var = tk.StringVar(value="現在時刻: --:--:--")
        ttk.Label(fps_frame, textvariable=self.clock_var,
                  foreground="#a60").pack(side="right")

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

    # ---- 計測部位の選択が変わったとき(マーカー一覧ラベルの更新) ----
    def _on_body_change(self):
        """選択中の部位のマーカー名(配信順)をラベルに表示する。"""
        key = self.body_var.get()
        names = MARKER_GROUPS.get(key, {}).get("names", [])
        self.body_markers_var.set("マーカー（配信順）: " + ", ".join(names))

    # ---- FPS 表示テキストの生成 ----
    def _fps_default_text(self):
        parts = ["OptiTrack: --"] + ["{}: --".format(n) for n in VNA_CHANNEL_NAMES]
        return " / ".join(parts) + " FPS"

    def _fps_text(self, opti, vna_list):
        parts = ["OptiTrack: {:.1f}".format(opti)]
        for name, f in zip(VNA_CHANNEL_NAMES, vna_list):
            parts.append("{}: {:.1f}".format(name, f))
        return " / ".join(parts) + " FPS"

    # ---- リアルタイムグラフ(スミスチャート)の構築 ----
    def _build_plot(self, pad):
        plot_frame = ttk.LabelFrame(
            self, text="スミスチャート (S11 / インピーダンス Z11)")
        plot_frame.pack(fill="both", expand=True, **pad)

        # チャンネル数ぶんのスミスチャートを横に並べる
        n = len(VNA_CHANNEL_NAMES)
        colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e"]
        # 各チャートの抵抗/リアクタンス目盛ラベルを表示するため、1 枚あたりの領域を広めに取る。
        self.fig = Figure(figsize=(4.3 * n, 4.2), dpi=100)
        self._smith_axes = []
        self._trace_lines = []    # 掃引全点の Γ(=S11) トレース
        self._marker_lines = []   # 読み取り周波数の点(★)

        for i, name in enumerate(VNA_CHANNEL_NAMES):
            ax = self.fig.add_subplot(1, n, i + 1)
            # scikit-rf のスミスチャート格子を描く(インピーダンス基準)。
            # draw_labels=True で抵抗円/リアクタンス円の目盛ラベルを表示する
            # (初期表示でラベルが出ず見づらかったのを改善)。
            rf.plotting.smith(ax=ax, chart_type="z", draw_labels=True)
            ax.set_title(name, fontsize=10)
            ax.set_aspect("equal")
            # 直交座標の目盛(-1..1)は不要なので消す。スミス円の目盛ラベルは残る。
            ax.set_xticks([])
            ax.set_yticks([])
            color = colors[i % len(colors)]
            (trace,) = ax.plot([], [], color=color, marker=".", markersize=3,
                               linewidth=1.0, zorder=3)
            (marker,) = ax.plot([], [], marker="*", markersize=14, color="k",
                                linestyle="none", zorder=4)
            self._smith_axes.append(ax)
            self._trace_lines.append(trace)
            self._marker_lines.append(marker)

        # ラベルが図の縁で切れないよう、少し余白を持たせて配置する。
        self.fig.tight_layout(pad=1.2)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.draw()

        # 各チャンネルの「読み取り周波数でのインピーダンス」数値表示
        readout_box = ttk.Frame(plot_frame)
        readout_box.pack(fill="x", padx=6, pady=(0, 4))
        self.readout_vars = []
        for i, name in enumerate(VNA_CHANNEL_NAMES):
            color = colors[i % len(colors)]
            var = tk.StringVar(value="{}:  Z = --".format(name))
            ttk.Label(readout_box, textvariable=var, foreground=color,
                      font=("", 10, "bold")).pack(anchor="w")
            self.readout_vars.append(var)

    def _reconfigure_plot(self, start_hz, stop_hz, points):
        """掃引条件の変更にあわせてスミスチャートの周波数グリッドとトレースをリセットする。"""
        self._plot_freq_hz = np.asarray(
            make_freq_grid(start_hz, stop_hz, points), dtype=float)
        single = (len(self._plot_freq_hz) <= 1)
        for i in range(len(self._trace_lines)):
            self._trace_lines[i].set_data([], [])
            self._marker_lines[i].set_data([], [])
            self._smith_trail[i].clear()   # 前回計測の軌跡を持ち越さない
            self.readout_vars[i].set("{}:  Z = --".format(VNA_CHANNEL_NAMES[i]))
        # 単一周波数モードは「掃引の形」ではなく「1 点が時間とともに動く軌跡」を見る表示になる。
        for ax, name in zip(self._smith_axes, VNA_CHANNEL_NAMES):
            if single:
                # 図中の文字は英語(matplotlib の既定フォントに日本語が無いため)
                ax.set_title("{}  @ {:.4f} MHz (trail)".format(
                    name, float(self._plot_freq_hz[0]) / 1e6), fontsize=10)
            else:
                ax.set_title(name, fontsize=10)
        self.canvas.draw_idle()

    def _readout_index(self, npts):
        """読み取り周波数[MHz]に最も近い掃引点のインデックスを返す。不正入力なら None。"""
        try:
            f_hz = float(self.readout_var.get()) * 1e6
        except (ValueError, TypeError):
            return None
        if len(self._plot_freq_hz) != npts or npts == 0:
            return None
        return int(np.argmin(np.abs(self._plot_freq_hz - f_hz)))

    def _redraw_plot(self, payload):
        """
        最新の各チャンネル (s11, z_r, z_x) でスミスチャートと数値表示を更新する。
        payload: [(s11_complex, z_r, z_x), ...]（チャンネル順）。メインスレッドから呼ぶこと。
        """
        n = min(len(payload), len(self._trace_lines))
        for i in range(n):
            s11, z_r, z_x = payload[i]
            s11 = np.asarray(s11)
            if s11.size == 0:
                continue
            # スミスチャート上の点 = 反射係数 Γ = S11(Z0=50Ω 基準)
            if s11.size == 1:
                # 単一周波数(1点)モード: 掃引トレースが引けないので、直近サンプルの
                # 軌跡(時間方向)を線でつなぐ。現在値は下の ★ マーカーで示す。
                self._smith_trail[i].append(complex(s11[0]))
                trail = np.fromiter(self._smith_trail[i], dtype=complex,
                                    count=len(self._smith_trail[i]))
                self._trace_lines[i].set_data(trail.real, trail.imag)
            else:
                self._trace_lines[i].set_data(s11.real, s11.imag)

            idx = self._readout_index(len(s11))
            name = VNA_CHANNEL_NAMES[i]
            if idx is None or idx >= len(s11):
                self._marker_lines[i].set_data([], [])
                self.readout_vars[i].set("{}:  Z = --  (読み取り周波数を確認)".format(name))
            else:
                g = complex(s11[idx])
                self._marker_lines[i].set_data([g.real], [g.imag])
                r = float(z_r[idx])
                x = float(z_x[idx])
                f_mhz = float(self._plot_freq_hz[idx]) / 1e6
                sign = "+" if x >= 0 else "-"
                self.readout_vars[i].set(
                    "{}:  Z = {:.2f} {} j{:.2f} Ω   |S11|={:.3f}   @ {:.4f} MHz".format(
                        name, r, sign, abs(x), abs(g), f_mhz))
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
        test_index = displays.index(NO_VNA_DISPLAY)

        # 各チャンネルの Combobox に一覧を反映する。
        # 既定選択: チャンネル i には i 番目の実ポートを割り当て(実ポートが足りなければ
        # テストモード)。2 台つないでいれば VNA1=1台目, VNA2=2台目 が自動で入る。
        for i, combo in enumerate(self.port_combos):
            combo.configure(values=displays)
            if i < len(ports):
                combo.current(i)
            else:
                combo.current(test_index)

        return len(ports)

    def get_selected_ports(self):
        """各チャンネルの Combobox 選択から、実デバイス名("COM3"/TEST_MODE)のリストを返す。"""
        out = []
        for var in self.port_vars:
            disp = var.get()
            out.append(self._port_display_to_device.get(disp, disp))
        return out

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
        vna_str = ", ".join(
            "{}: {:.1f}".format(n, f)
            for n, f in zip(VNA_CHANNEL_NAMES, self._vna_fps))
        print("\r[計測中] 取得数: {} 件 (OptiTrack: {:.1f} Hz, {})   ".format(
            self._latest_count, self._opti_fps, vna_str),
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
        # 開始時点で選択されている各チャンネルの COM ポートを読み込む
        com_ports = self.get_selected_ports()
        if any(not p for p in com_ports):
            messagebox.showerror(
                "COM ポート未選択",
                "VNA の COM ポートが選択されていないチャンネルがあります。\n"
                "VNA を接続し、[ポート再検索] で一覧を更新してから選択してください。")
            return
        # 実ポートの重複禁止(同じ COM を 2 台で同時に開くことはできない)
        real = [p for p in com_ports if p != TEST_MODE]
        if len(set(real)) != len(real):
            messagebox.showerror(
                "COM ポート重複",
                "同じ COM ポートを複数のチャンネルに割り当てています。\n"
                "各 VNA には別々の COM ポートを選択してください。")
            return

        # 掃引条件(開始/終了周波数・点数)を読み取り検証
        cfg = self._read_sweep_config()
        if cfg is None:
            return
        start_hz, stop_hz, points = cfg
        use_optitrack = bool(self.optitrack_var.get())
        use_camera_sync = bool(self.camera_var.get())

        # 計測部位(上半身/下半身)を確定する。以降の MARKER_NAMES・共有座標スロット・
        # CSV 位置列・ライブ表示は、ここで選んだ 7 点だけを対象にする。
        body_key = self.body_var.get()
        try:
            set_marker_group(body_key)
        except ValueError:
            messagebox.showerror("計測部位エラー",
                                 "計測部位が選択されていません。上半身か下半身を選んでください。")
            return

        # 新しい掃引条件にあわせてグラフ横軸を作り直す
        self._reconfigure_plot(start_hz, stop_hz, points)

        self.measuring = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        for combo in self.port_combos:                 # 計測中は変更不可
            combo.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")
        self.start_entry.configure(state="disabled")
        self.stop_entry.configure(state="disabled")
        self.points_spin.configure(state="disabled")
        self.optitrack_chk.configure(state="disabled")
        self.camera_chk.configure(state="disabled")
        self.camera_index_spin.configure(state="disabled")
        self.live_chk.configure(state="disabled")
        for rb in self.body_radios:                    # 計測中は部位を変更不可
            rb.configure(state="disabled")
        self.status_var.set("接続中...")
        self.count_var.set("サンプル数: 0")
        # FPS 計測の初期化
        self._latest_count = 0
        self._opti_fps = 0.0
        self._vna_fps = [0.0] * len(VNA_CHANNEL_NAMES)
        now = time.time()
        self._meas_start_time = now
        self._meas_stop_time = None
        self._fps_win_start = now
        self._fps_win_opti = 0
        self._fps_win_vna = [0] * len(VNA_CHANNEL_NAMES)
        self.fps_var.set(self._fps_default_text())
        if points <= 1:
            sweep_desc = "単一周波数 {:.4f} MHz (1点)".format(start_hz / 1e6)
        else:
            sweep_desc = "掃引 {:.3f}-{:.3f} MHz {}点".format(
                start_hz / 1e6, stop_hz / 1e6, points)
        opt_desc = "OptiTrack ON" if use_optitrack else "OptiTrack OFF(VNAのみ)"
        ports_desc = " / ".join(
            "{}={}".format(VNA_CHANNEL_NAMES[i],
                           "テストモード" if com_ports[i] == TEST_MODE else com_ports[i])
            for i in range(len(com_ports)))
        cam_desc = "カメラ同期 ON" if use_camera_sync else "カメラ同期 OFF"
        body_desc = "計測部位: {}".format(marker_group_label(body_key))
        msg = "計測を開始しました。{}（{}, {}, {}, {}, {}）".format(
            ports_desc, VNA_SPARAM, sweep_desc, opt_desc, cam_desc, body_desc)
        self._log(msg)
        # どのマーカーをどの順で拾うかはログに残す(診断ログの並びと突き合わせるため)
        self._log("{}のマーカー（配信順）: {}".format(
            marker_group_label(body_key), ", ".join(MARKER_NAMES)))
        # 動画同期 ON のとき: 開始の絶対時刻を残す(webカメラ録画も計測開始と連動して始まる)。
        if use_camera_sync:
            self._log("開始時刻(壁時計): {}".format(_format_wallclock(time.time())))
        self._console_event("[計測開始] " + msg)  # 重要イベントはコンソールにも残す
        self.controller = MeasurementController(self.msg_queue)
        self.controller.start(com_ports, start_hz, stop_hz, points,
                              use_optitrack, use_camera_sync)
        # 計測開始と連動して webカメラ録画を開始し、ライブ表示ウィンドウを開く
        self._start_camera(use_camera_sync)
        self._open_dashboard()

    # ---- webカメラ録画の開始(計測開始と連動) ----
    def _start_camera(self, use_camera_sync):
        """
        カメラ同期 ON かつ OpenCV 利用可のとき、計測開始と連動して webカメラ録画を始める。
        録画は一時 mp4 に書き、計測終了・CSV 保存時に CSV の隣へ移動する。
        失敗しても計測は止めず、ログに理由を出して録画なしで続行する。
        """
        self.camera = None
        self.video_tmp_path = None
        self.video_start_wall = None
        self.camera_info = None
        if not use_camera_sync:
            return
        if camera_recorder is None or getattr(camera_recorder, "cv2", None) is None:
            self._log("[カメラ] OpenCV(cv2)が無いため録画をスキップします "
                      "(pip install opencv-python で有効化)。")
            return
        try:
            idx = int(float(self.camera_index_var.get()))
        except (ValueError, TypeError):
            idx = 0
        tmp = os.path.join(tempfile.gettempdir(),
                           "nanovna_cam_{}.mp4".format(time.strftime("%Y%m%d_%H%M%S")))
        try:
            rec = camera_recorder.CameraRecorder(index=idx, out_path=tmp)
            rec.start()
        except Exception as e:
            self._log("[カメラ] 録画を開始できませんでした(index={}): {}".format(idx, e))
            return
        self.camera = rec
        self.video_tmp_path = tmp
        self.video_start_wall = rec.start_wall
        self._log("[カメラ] 録画開始 index={} {}x{} @~{:.0f}fps (明るさ mean={:.1f} std={:.1f})".format(
            idx, rec.width, rec.height, rec.fps or 0,
            rec.first_mean or 0.0, rec.first_std or 0.0))
        # 真っ黒(=レンズが塞がれている/光が入っていない)らしいときは即警告する。
        # ThinkPad は画面上部に物理カメラシャッター(ThinkShutter)があり、閉じていると真っ黒になる。
        if rec.is_probably_black():
            warn = ("カメラの映像が真っ黒です（レンズに光が入っていません）。このまま録画すると"
                    "真っ黒な動画になります。\n\n次を確認してください:\n"
                    "・ThinkPad の物理カメラシャッター（画面上部のスライダー）が開いているか\n"
                    "・レンズカバー/テープでふさがれていないか\n"
                    "・「カメラ番号」が意図したカメラか（外部webカメラなら番号を変える）\n"
                    "・Windows の設定 > プライバシー > カメラ でアプリのアクセスが許可されているか\n\n"
                    "計測は続行します（動画が不要なら「カメラ撮影と同期する」を OFF に）。")
            self._log("[カメラ][警告] 映像が真っ黒です。カメラシャッター/カバー/カメラ番号を確認してください。")
            self._console_event("[カメラ][警告] 映像が真っ黒（レンズ塞がり）の可能性。")
            try:
                messagebox.showwarning("カメラが真っ黒です", warn)
            except Exception:
                pass

    # ---- ライブ表示ダッシュボード(別ウィンドウ)を開く ----
    def _open_dashboard(self):
        """
        ライブ表示 ON かつ live_dashboard 利用可のとき、別ウィンドウの
        ライブ・ダッシュボードを開く。表示は掃引レートに律速されず、専用タイマーで
        最新のマーカー座標・カメラフレーム・各 VNA の最新掃引を直接読んで更新する。
        """
        self.dashboard = None
        if not bool(self.live_var.get()):
            return
        if live_dashboard is None:
            self._log("[ライブ表示] モジュールが無いため開けません。")
            return
        ctrl = self.controller

        def get_sweep(i):
            try:
                chans = ctrl.channels
                if 0 <= i < len(chans):
                    latest = chans[i].read_latest()
                    if latest is not None:
                        return latest[1]  # s11(複素配列)
            except Exception:
                pass
            return None

        get_frame = self.camera.get_latest_frame if self.camera is not None else None
        try:
            self.dashboard = live_dashboard.LiveDashboard(
                self, VNA_CHANNEL_NAMES, ctrl.freq_grid_hz,
                get_positions=read_latest_positions,
                get_sweep=get_sweep,
                get_frame=get_frame,
                has_video=(self.camera is not None),
                interval_ms=50,
                body_part=MARKER_GROUP)
        except Exception as e:
            self._log("[ライブ表示] 開けませんでした: {}".format(e))
            self.dashboard = None

    # ---- ライブ表示を閉じる(メインスレッドから) ----
    def _close_dashboard(self):
        if self.dashboard is not None:
            try:
                self.dashboard.close()
            except Exception:
                pass
            self.dashboard = None

    # ---- webカメラ録画を停止して mp4 を確定する ----
    def _stop_camera(self):
        if self.camera is not None:
            try:
                self.camera_info = self.camera.stop()
            except Exception as e:
                self._console_event("[カメラ] 停止中に例外: {}".format(e))
                self.camera_info = None
            self.camera = None

    # ---- 録画した動画を CSV の隣へ移動し、controller.video_info をセットする ----
    def _finalize_video_next_to_csv(self, csv_path):
        """
        一時 mp4 を CSV と同じフォルダへ WIN_YYYYMMDD_HH_MM_SS_Pro.mp4 の名前で移動する。
        この名前は sync_video_with_dataset.py がファイル名から録画開始時刻を読めるため、
        そのまま同期に使える。移動情報は controller.video_info に入れて meta.json へ残す。
        """
        if not self.video_tmp_path or not os.path.isfile(self.video_tmp_path):
            return
        start = self.video_start_wall or time.time()
        fname = "WIN_{}_Pro.mp4".format(
            datetime.datetime.fromtimestamp(start).strftime("%Y%m%d_%H_%M_%S"))
        dest = os.path.join(os.path.dirname(os.path.abspath(csv_path)), fname)
        base, ext = os.path.splitext(dest)
        k = 1
        while os.path.exists(dest):
            dest = "{}_{}{}".format(base, k, ext)
            k += 1
        try:
            shutil.move(self.video_tmp_path, dest)
        except Exception as e:
            self._log("[カメラ] 動画の移動に失敗(一時ファイルのまま): {}".format(e))
            dest = self.video_tmp_path
        info = dict(self.camera_info or {})
        info["file"] = os.path.basename(dest)
        info["path"] = dest
        info["start_wall"] = start
        if self.controller is not None:
            self.controller.video_info = info
        self.video_tmp_path = None
        self._log("[カメラ] 動画を保存しました: {}".format(dest))

    # ---- 計測終了 -> 保存 -> (待機復帰) ----
    def on_stop(self):
        self._finalize_and_save("計測を停止しています...")

    # ---- 停止 → クリーンアップ → 保存フローへ(on_stop / VNA 無応答から共用) ----
    def _finalize_and_save(self, reason=""):
        """
        計測を止めてクリーンアップし、保存ダイアログへ進む。GUI を固めないよう
        クリーンアップは別スレッドで行い、完了後に ready_to_save を通知する。
        """
        if not self.measuring or self._finalizing:
            return
        self._finalizing = True
        self._exit_after_save = False        # 保存後はウィンドウを閉じず待機状態へ戻す
        self._meas_stop_time = time.time()  # 平均 FPS 算出用に停止時刻を記録
        self.stop_btn.configure(state="disabled")
        self.status_var.set("停止処理中...")
        if reason:
            self._log(reason)

        # ライブ表示を閉じる(Toplevel 破棄はメインスレッドで)
        self._close_dashboard()

        # 停止要求(即時に戻る)。クリーンアップは GUI を固めないよう別スレッドで。
        if self.controller:
            self.controller.request_stop()

        def _finalize():
            try:
                if self.controller:
                    self.controller.cleanup()
                self._stop_camera()   # webカメラ録画を停止して mp4 を確定
            finally:
                # メインスレッドで保存ダイアログを出すため通知
                self.msg_queue.put(("ready_to_save", None))

        threading.Thread(target=_finalize, name="Finalizer",
                          daemon=True).start()

    # ---- 保存ダイアログ -> 保存 -> (待機復帰 or 終了) ----
    def _do_save_then_finish(self):
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
                # 録画した動画を CSV の隣へ移動し、meta.json に動画情報を含める(保存前に実施)
                self._finalize_video_next_to_csv(path)
                written = self.controller.save_csv(path)
                if getattr(self.controller, "use_camera_sync", False):
                    meta_path = os.path.splitext(path)[0] + ".meta.json"
                    self._log("保存しました: {} ({} サンプル) / meta: {}".format(
                        path, written, os.path.basename(meta_path)))
                else:
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
            tail = "終了しますか?" if self._exit_after_save else "破棄しますか?(いいえ=保存ダイアログに戻る)"
            if n > 0 and not messagebox.askyesno(
                    "確認", "保存をキャンセルしました。{} サンプルを{}".format(n, tail)):
                # 取りやめ -> 保存ダイアログを再表示
                self._do_save_then_finish()
                return
            # 破棄を選択: 録画した動画は一時ファイルとして残るので場所を知らせる
            if self.video_tmp_path and os.path.isfile(self.video_tmp_path):
                self._log("[カメラ] 録画した動画は一時ファイルに残っています: {}".format(
                    self.video_tmp_path))

        # 保存/破棄が完了 -> ウィンドウ × からの場合は終了、計測終了ボタンなら待機状態へ戻す
        if self._exit_after_save:
            self.destroy()
        else:
            self._log("待機状態に戻りました。続けて[計測開始]できます。")
            self._reset_to_idle()

    # ---- 計測 1 回ぶんを終えて待機状態(初期状態)に戻す ----
    def _reset_to_idle(self):
        self.measuring = False
        self._finalizing = False
        self.controller = None
        self._counter_active = False
        self._meas_start_time = None
        self._meas_stop_time = None
        self._fps_win_start = None
        self._opti_fps = 0.0
        self._vna_fps = [0.0] * len(VNA_CHANNEL_NAMES)
        # カメラ/ライブ表示/動画一時ファイルの状態をクリア(念のため)
        self._close_dashboard()
        self.camera = None
        self.camera_info = None
        self.video_tmp_path = None
        self.video_start_wall = None
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        for combo in self.port_combos:
            combo.configure(state="readonly")
        self.rescan_btn.configure(state="normal")
        self.start_entry.configure(state="normal")
        self.stop_entry.configure(state="normal")
        self.points_spin.configure(state="normal")
        self.optitrack_chk.configure(state="normal")
        self.camera_chk.configure(state="normal")
        self.camera_index_spin.configure(state="normal")
        self.live_chk.configure(state="normal")
        for rb in self.body_radios:
            rb.configure(state="normal")
        self.status_var.set("待機中")
        self.fps_var.set(self._fps_default_text())

    # ---- ウィンドウ × ----
    def on_window_close(self):
        if self._finalizing:
            return
        if self.measuring:
            if not messagebox.askokcancel(
                    "終了確認", "計測中です。停止して終了しますか?"):
                return
            self._finalizing = True
            self._exit_after_save = True         # ウィンドウ × は保存後にアプリを終了する
            self._meas_stop_time = time.time()  # 平均 FPS 算出用に停止時刻を記録
            self.status_var.set("停止処理中...")
            self._log("ウィンドウを閉じています。クリーンアップ中...")
            self._close_dashboard()             # ライブ表示を閉じる(メインスレッド)

            def _finalize_close():
                try:
                    if self.controller:
                        self.controller.request_stop()
                        self.controller.cleanup()
                    self._stop_camera()          # webカメラ録画を停止して mp4 を確定
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
                elif kind == "log":
                    # フレーム受信スレッドからの診断ログ等(状態表示は更新しない)
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
                elif kind == "vna_lost":
                    # VNA 無応答/切断: これまでのデータは保持し、停止 → 保存フローへ進む。
                    self._finalize_counter_line()
                    self._log("[VNA] " + str(value))
                    self._console_event("[VNA] " + str(value))
                    messagebox.showwarning(
                        "VNA 応答なし",
                        str(value) + "\n\nこれまでに取得したデータは保存できます。"
                        "続いて保存先の選択ダイアログを表示します。")
                    self._finalize_and_save("VNA 応答なしのため計測を停止し、保存します。")
                elif kind == "finished":
                    self._finalize_counter_line()
                    self._log("計測ループ終了。")
                elif kind == "ready_to_save":
                    self._finalize_counter_line()
                    self.status_var.set("停止しました")
                    self.count_var.set("サンプル数: {}".format(
                        self.controller.get_sample_count() if self.controller else 0))
                    self._do_save_then_finish()
                    if not self.winfo_exists():
                        return  # 終了(ウィンドウ破棄)した場合は再ポーリングしない
                    # 待機状態へ戻した場合はそのままポーリングを継続する
        except queue.Empty:
            pass

        # 現在時刻(動画同期の基準時計)を毎サイクル更新する
        self.clock_var.set("現在時刻: " + _format_wallclock(time.time()))

        # --- FPS を約 1 秒ごとに算出して表示(GUI ラベル + コンソール) ---
        fps_updated = self._update_fps_if_due()

        # コンソールの \r 行は、FPS 更新時 か 新サンプル到着時に上書き更新する
        if self.measuring and (fps_updated or got_sample):
            self._console_counter()

        # 今サイクルに届いた最新スイープでスミスチャートを 1 回だけ更新(描画負荷を抑制)
        if self._latest_plot is not None:
            self._redraw_plot(self._latest_plot)
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
        opti_total, vna_totals = read_fps_counters()
        self._opti_fps = (opti_total - self._fps_win_opti) / dt
        self._vna_fps = [
            (vna_totals[i] - self._fps_win_vna[i]) / dt
            for i in range(len(vna_totals))]
        # 次ウィンドウへ
        self._fps_win_start = now
        self._fps_win_opti = opti_total
        self._fps_win_vna = list(vna_totals)
        self.fps_var.set(self._fps_text(self._opti_fps, self._vna_fps))
        return True

    def _log_fps_summary(self):
        """計測全体の平均 FPS を GUI ログとコンソールへ出力する。"""
        opti_total, vna_totals = read_fps_counters()
        start = self._meas_start_time
        stop = self._meas_stop_time or time.time()
        dur = (stop - start) if start else 0.0
        avg_opti = (opti_total / dur) if dur > 0 else 0.0
        vna_parts = []
        for name, tot in zip(VNA_CHANNEL_NAMES, vna_totals):
            avg = (tot / dur) if dur > 0 else 0.0
            vna_parts.append("{} 平均 {:.1f} FPS ({} 掃引)".format(name, avg, tot))
        summary = ("計測サマリー: 計測時間 {:.1f} 秒 / "
                   "OptiTrack 平均 {:.1f} FPS ({} フレーム) / {}").format(
            dur, avg_opti, opti_total, " / ".join(vna_parts))
        self._log(summary)
        self._console_event("[サマリー] " + summary)

    def _recover_after_error(self):
        """致命的エラー後、計測状態を解除して再度開始できるようにする。"""
        if self._finalizing:
            return

        # ライブ表示を閉じる(メインスレッド)。カメラ停止はクリーンアップ側で行う。
        self._close_dashboard()

        def _cl():
            if self.controller:
                self.controller.cleanup()
            self._stop_camera()
        threading.Thread(target=_cl, daemon=True).start()
        self.measuring = False
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        for combo in self.port_combos:                # ポート再選択を許可
            combo.configure(state="readonly")
        self.rescan_btn.configure(state="normal")
        self.start_entry.configure(state="normal")   # 掃引条件の再編集を許可
        self.stop_entry.configure(state="normal")
        self.points_spin.configure(state="normal")
        self.optitrack_chk.configure(state="normal")
        self.camera_chk.configure(state="normal")
        self.camera_index_spin.configure(state="normal")
        self.live_chk.configure(state="normal")
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
