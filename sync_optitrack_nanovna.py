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

スレッド構成:
  - OptiTrack: 高速更新。new_frame_with_data_listener が 3 マーカーの最新座標を更新。
  - VNA      : 低速取得。1 点取れるたびに 3 マーカー(計9値)の最新座標を一括取得して書込み。
  - 共有変数は threading.Lock() で保護してスレッド安全に読み書きする。

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
import threading
from datetime import datetime

import serial  # pip install pyserial


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

    # --- 最初の有効フレームでの自動判別(キャリブレーション)完了を待つ ---
    axis_name = {0: "X", 1: "Y", 2: "Z"}.get(HEIGHT_AXIS_INDEX, "?")
    print("[INFO] 開始時の高さ({} 軸)で 3 対象を自動判別します。"
          "3 個すべてをトラッキング可能な状態にしてください...".format(axis_name))
    if not _calibrated.wait(timeout=CALIBRATION_TIMEOUT_SEC):
        print("[WARN] {:.0f}s 以内に自動判別が完了しませんでした。".format(
            CALIBRATION_TIMEOUT_SEC))
        print("       3 対象({}個)がすべて同時にトラッキングされているか、".format(
            EXPECTED_OBJECT_COUNT))
        print("       OBJECT_SOURCE('{}')や Data Streaming 設定を確認してください。".format(
            OBJECT_SOURCE))
        print("       (判別が完了すると、以後フレーム受信時に自動でマッピングされます)")
    return client


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


def main():
    # --- NanoVNA 接続 ---
    try:
        vna = NanoVNA(NANOVNA_PORT, NANOVNA_BAUD)
    except serial.SerialException as e:
        print("[ERROR] VNA シリアル接続に失敗: {}".format(e))
        print("        NANOVNA_PORT('{}') が正しいか、他ソフトが占有していないか確認してください。".format(NANOVNA_PORT))
        sys.exit(1)
    except Exception as e:
        print("[ERROR] VNA 初期化中に予期せぬ例外: {}".format(e))
        sys.exit(1)
    print("[INFO] VNA 接続 OK: {} @ {}bps, {:.3f} MHz".format(
        NANOVNA_PORT, NANOVNA_BAUD, TARGET_FREQ_HZ / 1e6))

    # --- NatNet 開始(内部で開始時の高さによる自動判別まで実施) ---
    natnet_client = start_natnet()
    if natnet_client is None:
        print("[WARN] OptiTrack なしで続行します(座標列は空欄になります)。")
    else:
        mapping = get_id_to_part()
        if mapping:
            part_to_id = {part: oid for oid, part in mapping.items()}
            print("[INFO] 確定マッピング(部位 -> 固定ID):")
            for part in PART_NAMES:
                print("       {:8s} -> id={}".format(
                    part, part_to_id.get(part, "?")))
        else:
            print("[WARN] 自動判別が未完了です。座標列が空欄になる可能性があります。")

    # OptiTrack の初回データ到着を待つ(3 対象すべて)
    if natnet_client is not None:
        for _ in range(50):
            snap = read_latest_positions()
            if all(snap[n][3] for n in PART_NAMES):
                print("[INFO] OptiTrack 初期データ受信 OK(3対象すべて)。")
                break
            time.sleep(0.1)
        else:
            snap = read_latest_positions()
            missing = [n for n in PART_NAMES if not snap[n][3]]
            print("[WARN] 初期データ未受信の対象があります: {}".format(missing))
            print("       3 対象がすべて同時にトラッキングされているか、")
            print("       OBJECT_SOURCE / Data Streaming(Rigid Bodies または Markers)の")
            print("       配信設定を確認してください。")

    # --- CSV 準備 & 計測ループ ---
    sample_count = 0
    last_stale_warn = 0.0
    try:
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            f.flush()

            print("[INFO] 計測開始。Ctrl+C で安全に停止します。")
            while not _stop_event.is_set():
                # 1) VNA から S11 を1点取得(ここがレート律速)
                try:
                    s11 = vna.measure_s11()
                except serial.SerialException as e:
                    print("[ERROR] VNA シリアル通信が切断されました: {}".format(e))
                    break
                if s11 is None:
                    print("[WARN] S11 のパースに失敗。スキップします。")
                    continue

                # 2) ストリーミング途絶チェック(警告のみ。計測は継続)
                if natnet_client is not None:
                    elapsed = seconds_since_last_frame()
                    now = time.time()
                    if elapsed is not None and elapsed > STREAM_STALE_SEC and (now - last_stale_warn) > STREAM_STALE_SEC:
                        last_stale_warn = now
                        print("[WARN] OptiTrack フレームが {:.1f}s 途絶しています(座標が古い可能性)。".format(elapsed))

                # 3) その瞬間の 3 マーカー(計9値)の最新座標を一括スナップショット
                snap = read_latest_positions()

                # 4) インピーダンス計算
                z_r, z_x = s11_to_impedance(s11, Z0)

                # 5) CSV へ1行書き込み(3マーカー座標 + S11 + Z)
                ts = datetime.now().isoformat(timespec="milliseconds")
                writer.writerow(
                    [ts]
                    + _fmt_xyz(snap, "UpperArm")
                    + _fmt_xyz(snap, "Joint")
                    + _fmt_xyz(snap, "Forearm")
                    + [
                        "{:.6f}".format(s11.real),
                        "{:.6f}".format(s11.imag),
                        "{:.4f}".format(z_r),
                        "{:.4f}".format(z_x),
                    ]
                )
                f.flush()  # 途中でクラッシュしてもデータを残す

                sample_count += 1
                if sample_count % 10 == 0:
                    print("[{:5d}] UA={} J={} FA={} | S11=({:.4f},{:.4f}) Z=({:.2f}{:+.2f}j)Ω".format(
                        sample_count,
                        _short_xyz(snap, "UpperArm"),
                        _short_xyz(snap, "Joint"),
                        _short_xyz(snap, "Forearm"),
                        s11.real, s11.imag, z_r, z_x))

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C を検出。停止処理中...")
    except Exception as e:
        print("\n[ERROR] 計測ループで予期せぬ例外: {}".format(e))
    finally:
        _stop_event.set()
        try:
            vna.close()
        except Exception:
            pass
        if natnet_client is not None:
            try:
                natnet_client.shutdown()
            except Exception:
                pass
        print("[INFO] 終了。{} サンプルを '{}' に保存しました。".format(sample_count, OUTPUT_CSV))


if __name__ == "__main__":
    main()
