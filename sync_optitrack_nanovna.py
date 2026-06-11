# -*- coding: utf-8 -*-
"""
OptiTrack (Motive 3.1 / NatNet) + JNCRadio VNA 3G 同期計測スクリプト
================================================================================

同一 Windows PC 上で動作する Motive から NatNet 経由でラベル付きマーカーの座標を
受信しつつ、USB シリアル接続された JNCRadio VNA 3G から特定単一周波数の S11 を
連続取得し、両者を同期して CSV に保存する。

計測対象は腕に直接貼り付けた 3 個の「単一マーカー」(Labeled Marker):
  1. 上腕   (Upper Arm)
  2. 関節   (Joint / 肘など)
  3. 前腕   (Forearm)
各マーカーの中心 [X, Y, Z] 座標(Motive 3.x の Z-Up 座標系)を同時に取得し、
S11 / インピーダンスと紐付けて 1 行に記録する。

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

# 計測する 3 個の対象を「Motive 側で付けた名前」で指定する。
# ★ID はハードコードしない★。実行ごとに変わる ID ではなく、この名前をキーに
#   起動時の Data Descriptions(NAT_MODELDEF)から現在の ID/インデックスを自動解決する。
#
# 左 = スクリプト内部の論理名(CSV 列名に対応), 右 = Motive 上の名前(大文字小文字は無視)。
# Motive 上で Rigid Body として作っていても、Marker Set 内の単一マーカーとして
# 名前を付けていても、どちらでも同じ名前で解決できる(自動判別)。
TARGET_NAMES = {
    "UpperArm": "upperarm",   # 上腕   (Upper Arm)
    "Joint":    "joint",      # 関節   (Joint / 肘など)
    "Forearm":  "forearm",    # 前腕   (Forearm)
}

# True にすると、受信した Data Descriptions に含まれる Rigid Body 名 / Marker Set 名と
# マーカー名の一覧をコンソールへ出力する。Motive 側の正確な名前が分からないときの調査に使う。
DEBUG_PRINT_DESCRIPTIONS = True

# 名前解決(Data Descriptions 受信)を待つ最大秒数。
DESC_RESOLVE_TIMEOUT_SEC = 5.0

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
# 動的マッピング: Motive 上の名前 -> 現在の ID / インデックス
# =============================================================================
#
# Data Descriptions を解決した結果をここに保持する。各論理名 (UpperArm 等) に対し、
# 「フレームデータのどこから座標を取り出すか」を表す解決済みエントリを格納する。
#   - Rigid Body の場合 : {"kind": "rigid_body", "id": <現在の RigidBody ID>}
#   - Marker Set の場合 : {"kind": "marker_set", "set": <セット名>, "index": <マーカー番号>}
#
# _resolve_lock 下で書き込み、フレームコールバック側は基本的に起動直後に1回だけ
# 確定したこの辞書を読む(以後不変)。
_resolve_lock = threading.Lock()
_resolved_targets = {}          # name -> エントリ(上記)
_descriptions_ready = threading.Event()  # Data Descriptions を1回でも解析できたら set


def _norm(s):
    """名前比較用に正規化(前後空白除去 + 小文字化)。bytes も str も受ける。"""
    if isinstance(s, bytes):
        s = s.decode("utf-8", errors="ignore")
    return s.strip().lower()


def resolve_targets_from_descriptions(data_descs):
    """
    受信した DataDescriptions から、TARGET_NAMES の各名前に対応する
    現在の ID / インデックスを解決して _resolved_targets を更新する。

    Rigid Body 名で一致すれば rigid_body エントリ、見つからなければ
    Marker Set 内のマーカー名で一致を探して marker_set エントリにする。
    """
    # 解決しやすいように、説明側を正規化名で索引化する。
    rb_by_name = {}                 # 正規化RB名 -> RigidBodyDescription
    for rb in getattr(data_descs, "rigid_body_list", []) or []:
        rb_by_name[_norm(getattr(rb, "sz_name", ""))] = rb

    # Marker Set 内のマーカー名 -> (セット名, そのセット内インデックス)
    marker_by_name = {}
    for ms in getattr(data_descs, "marker_set_list", []) or []:
        set_name = getattr(ms, "marker_set_name", "")
        for idx, mname in enumerate(getattr(ms, "marker_names_list", []) or []):
            marker_by_name[_norm(mname)] = (set_name, idx)

    if DEBUG_PRINT_DESCRIPTIONS:
        print("[DESC] Rigid Bodies: {}".format(
            ", ".join("{}(id={})".format(_norm(rb.sz_name), rb.id_num)
                      for rb in rb_by_name.values()) or "(なし)"))
        print("[DESC] Marker Set マーカー名: {}".format(
            ", ".join("{}[{}#{}]".format(n, _norm(s), i)
                      for n, (s, i) in marker_by_name.items()) or "(なし)"))

    resolved = {}
    for logical, motive_name in TARGET_NAMES.items():
        key = _norm(motive_name)
        if key in rb_by_name:
            rb = rb_by_name[key]
            resolved[logical] = {"kind": "rigid_body", "id": rb.id_num}
        elif key in marker_by_name:
            set_name, idx = marker_by_name[key]
            resolved[logical] = {
                "kind": "marker_set", "set": set_name, "index": idx}
        # 見つからなければ resolved に入れない(未解決として後で警告)

    with _resolve_lock:
        _resolved_targets.clear()
        _resolved_targets.update(resolved)
    _descriptions_ready.set()

    # 解決結果のログ
    for logical in PART_NAMES:
        ent = resolved.get(logical)
        if ent is None:
            print("[WARN] '{}'(Motive名='{}') を Data Descriptions 内に発見できませんでした。"
                  .format(logical, TARGET_NAMES[logical]))
        elif ent["kind"] == "rigid_body":
            print("[INFO] {} -> Rigid Body (現在ID={})".format(logical, ent["id"]))
        else:
            print("[INFO] {} -> Marker Set '{}' の {} 番目のマーカー".format(
                logical, _norm(ent["set"]), ent["index"]))
    return resolved


def get_resolved_targets():
    """フレームコールバックから呼ぶ: 解決済みマッピングのスナップショット。"""
    with _resolve_lock:
        return dict(_resolved_targets)


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


def _extract_rigid_body_positions(mocap, resolved):
    """解決済みの rigid_body エントリについて、現在フレームから id 一致の座標を取り出す。"""
    rb_data = getattr(mocap, "rigid_body_data", None)
    if rb_data is None:
        return
    rb_list = getattr(rb_data, "rigid_body_list", None) or []
    # id -> 論理名 の逆引き(rigid_body のものだけ)
    id_to_name = {ent["id"]: name
                  for name, ent in resolved.items() if ent["kind"] == "rigid_body"}
    if not id_to_name:
        return
    for rb in rb_list:
        name = id_to_name.get(getattr(rb, "id_num", None))
        if name is None:
            continue
        # トラッキング有効フラグ(tracking_valid)が False の間は更新しない
        if SKIP_OCCLUDED and not getattr(rb, "tracking_valid", True):
            continue
        pos = rb.pos
        update_latest_position(name, float(pos[0]), float(pos[1]), float(pos[2]))


def _extract_marker_set_positions(mocap, resolved):
    """解決済みの marker_set エントリについて、セット名+インデックスで座標を取り出す。"""
    ms_data = getattr(mocap, "marker_set_data", None)
    if ms_data is None:
        return
    ms_list = getattr(ms_data, "marker_data_list", None) or []
    # (正規化セット名, index) -> 論理名 の逆引き
    want = {(_norm(ent["set"]), ent["index"]): name
            for name, ent in resolved.items() if ent["kind"] == "marker_set"}
    if not want:
        return
    for md in ms_list:
        set_key = _norm(getattr(md, "model_name", ""))
        pos_list = getattr(md, "marker_pos_list", None) or []
        for idx, pos in enumerate(pos_list):
            name = want.get((set_key, idx))
            if name is None:
                continue
            update_latest_position(name, float(pos[0]), float(pos[1]), float(pos[2]))


def _on_frame_with_data(data_dict):
    """
    NatNetClient の new_frame_with_data_listener コールバック。
    1 フレームごとに呼ばれ、data_dict["mocap_data"] に MoCapData が入る。
    起動時に解決した name->(ID/インデックス) マッピングを使って、対象 3 点の座標を更新する。
    """
    mark_frame_received()

    mocap = data_dict.get("mocap_data")
    if mocap is None:
        return

    resolved = get_resolved_targets()
    if not resolved:
        return  # まだ名前解決が済んでいない

    # Rigid Body 由来と Marker Set 由来の両方を取り出す(混在していてもよい)。
    _extract_rigid_body_positions(mocap, resolved)
    _extract_marker_set_positions(mocap, resolved)


def _install_description_capture(client):
    """
    この同梱 NatNet SDK は Data Descriptions を内部で print するだけで、
    コールバックも保持用属性も提供しない。そこで SDK ファイルには手を入れず、
    Data Descriptions を解析する private メソッドをインスタンス単位でラップし、
    解析結果を横取りして resolve_targets_from_descriptions() に渡す。

    NatNet の private メソッド名はクラス名でマングリングされるため、
    実体は '_NatNetClient__unpack_data_descriptions' でアクセスする。
    インスタンス属性に関数を差し込むと、SDK 内部の self.__unpack_data_descriptions(...)
    呼び出しはこのラッパへ解決される(インスタンス辞書がクラス属性より優先されるため)。
    """
    mangled = "_NatNetClient__unpack_data_descriptions"
    original = getattr(client, mangled, None)
    if original is None:
        print("[WARN] SDK の data descriptions 解析メソッドが見つかりません。"
              "名前の自動解決ができない可能性があります。")
        return

    def wrapper(data, packet_size, major, minor):
        offset, data_descs = original(data, packet_size, major, minor)
        try:
            resolve_targets_from_descriptions(data_descs)
        except Exception as e:  # 解決失敗でも受信自体は壊さない
            print("[WARN] Data Descriptions の名前解決に失敗: {}".format(e))
        return offset, data_descs

    setattr(client, mangled, wrapper)


def _request_model_definitions(client):
    """サーバ(Motive)へ Data Descriptions(NAT_MODELDEF)を明示的に要求する。"""
    try:
        client.send_request(
            client.command_socket,
            client.NAT_REQUEST_MODELDEF,
            "",
            (client.server_ip_address, client.command_port),
        )
    except Exception as e:
        print("[WARN] NAT_REQUEST_MODELDEF の送信に失敗: {}".format(e))


def start_natnet():
    """
    NatNetClient を起動し、Data Descriptions による名前解決とフレーム受信コールバックを
    セットアップする。成功すると streaming_client を返す。失敗時は None。
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

    # Data Descriptions(名前→ID)を横取りするためのフックを先に仕掛ける
    _install_description_capture(client)

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

    # --- Data Descriptions を要求し、名前→ID の解決完了を待つ ---
    print("[INFO] Data Descriptions を要求して名前を解決します...")
    _request_model_definitions(client)
    if not _descriptions_ready.wait(timeout=DESC_RESOLVE_TIMEOUT_SEC):
        # 自動要求に応答が来なくても、Motive が定期配信していれば後から解決されることがある。
        print("[WARN] {:.0f}s 以内に Data Descriptions を取得できませんでした。"
              .format(DESC_RESOLVE_TIMEOUT_SEC))
        print("       Motive の Data Streaming で対象を配信しているか、名前("
              + " / ".join(TARGET_NAMES.values()) + ")が正しいか確認してください。")
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

    # --- NatNet 開始 ---
    natnet_client = start_natnet()
    if natnet_client is None:
        print("[WARN] OptiTrack なしで続行します(座標列は空欄になります)。")
    else:
        resolved = get_resolved_targets()
        print("[INFO] 計測対象(Motive名 -> 解決結果):")
        for logical in PART_NAMES:
            ent = resolved.get(logical)
            if ent is None:
                desc = "未解決"
            elif ent["kind"] == "rigid_body":
                desc = "RigidBody id={}".format(ent["id"])
            else:
                desc = "MarkerSet '{}' #{}".format(_norm(ent["set"]), ent["index"])
            print("       {:8s} ('{}') -> {}".format(
                logical, TARGET_NAMES[logical], desc))

    # OptiTrack の初回データ到着を待つ(3 マーカーすべて)
    if natnet_client is not None:
        for _ in range(50):
            snap = read_latest_positions()
            if all(snap[n][3] for n in PART_NAMES):
                print("[INFO] OptiTrack 初期データ受信 OK(3マーカーすべて)。")
                break
            time.sleep(0.1)
        else:
            snap = read_latest_positions()
            missing = [n for n in PART_NAMES if not snap[n][3]]
            print("[WARN] 初期データ未受信の対象があります: {}".format(missing))
            print("       Motive 側の名前/トラッキング状態/Data Streaming(Rigid Bodies または")
            print("       Markers)の配信設定を確認してください。")
            print("       (DEBUG_PRINT_DESCRIPTIONS=True にすると受信した名前一覧を確認できます)")

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
