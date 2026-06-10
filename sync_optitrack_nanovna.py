# -*- coding: utf-8 -*-
"""
OptiTrack (Motive / NatNet) + NanoVNA / JNCRadio VNA-3G 同期計測スクリプト
================================================================================

同一PC上で動作する Motive から NatNet 経由でリジッドボディ座標を受信しつつ、
USB シリアル接続された NanoVNA(JNCRadio VNA-3G)から特定単一周波数の S11 を
連続取得し、両者を同期して CSV に保存する。

計測対象は腕に直接貼り付けた 3 つの「単一マーカー」(Labeled Marker):
  1. 上腕   (Upper Arm)
  2. 関節   (Joint / 肘など)
  3. 前腕   (Forearm)
各マーカーの中心 [X, Y, Z] 座標を同時に取得し、S11 と紐付けて 1 行に記録する。

※ リジッドボディではなく、Motive 側で個別にラベル付けした「Labeled Markers」
  として配信される 3 マーカーを、マーカー ID で識別して取得する。
  (Motive 3.1.0 Beta 2 以降のラベル付きマーカー配信を想定)

- OptiTrack: 高速更新（〜100-360Hz）。フレームコールバックで 3 マーカーの
            「最新座標」を更新し続ける。
- NanoVNA : 低速取得（1点 数十〜数百ms）。1点取れるたびにその瞬間の 3 点(計9値)の
            最新座標をまとめて紐付け、CSV へ1行書き込む。
- 速度差を吸収するためマルチスレッド + threading.Lock でスレッド安全に共有する。

CSV ヘッダー:
  [Timestamp,
   UpperArm_X, UpperArm_Y, UpperArm_Z,
   Joint_X,    Joint_Y,    Joint_Z,
   Forearm_X,  Forearm_Y,  Forearm_Z,
   S11_Real, S11_Imag, Z_R, Z_X]
"""

import csv
import sys
import time
import threading
from datetime import datetime

import serial  # pip install pyserial


# =============================================================================
# 設定（ここを環境に合わせて変更してください）
# =============================================================================

# --- NanoVNA / JNCRadio VNA-3G (シリアル) ---
NANOVNA_PORT   = "COM3"        # Windows の デバイスマネージャーで確認した COM 番号
NANOVNA_BAUD   = 115200        # ボーレート(JNCRadio VNA-3G は 115200 を推奨)
TARGET_FREQ_HZ = 13_560_000    # 取得したい単一周波数 [Hz] (例: 13.56 MHz)
SCAN_POINTS    = 11            # 1回の scan の掃引点数(本機の最小は 11)。同一周波数で平均する
Z0             = 50.0          # 特性インピーダンス [Ω]

# --- OptiTrack (NatNet) ---
NATNET_SERVER_IP = "127.0.0.1"  # 同一PCなので localhost
NATNET_LOCAL_IP  = "127.0.0.1"  # 同一PCなので localhost

# 計測する 3 つの単一マーカー(Labeled Marker)の ID
# Motive 側で各マーカーに割り当てたラベル ID に合わせて変更する。
UPPER_ARM_MARKER_ID = 1   # 上腕   (Upper Arm)
JOINT_MARKER_ID     = 2   # 関節   (Joint / 肘など)
FOREARM_MARKER_ID   = 3   # 前腕   (Forearm)

# 内部処理で使う論理名 -> マーカー ID の対応表
MARKER_IDS = {
    "UpperArm": UPPER_ARM_MARKER_ID,
    "Joint":    JOINT_MARKER_ID,
    "Forearm":  FOREARM_MARKER_ID,
}


def match_marker_name(marker_id):
    """
    受信した Labeled Marker の ID を、計測対象 3 マーカーのどれかに突き合わせる。
    NatNet の Labeled Marker ID は、アセットに属する場合 (model_id<<16 | member_id)
    の合成値になることがあるため、生の ID と下位 16bit の両方で照合する。
    一致しなければ None を返す。
    """
    low16 = marker_id & 0xFFFF
    for name, target in MARKER_IDS.items():
        if marker_id == target or low16 == target:
            return name
    return None

# --- 出力 ---
OUTPUT_CSV = "sync_dataset.csv"
CSV_HEADER = ["Timestamp",
              "UpperArm_X", "UpperArm_Y", "UpperArm_Z",
              "Joint_X",    "Joint_Y",    "Joint_Z",
              "Forearm_X",  "Forearm_Y",  "Forearm_Z",
              "S11_Real", "S11_Imag", "Z_R", "Z_X"]


# =============================================================================
# スレッド間共有: 3 点の最新 OptiTrack 座標
# =============================================================================

# Lock で保護される共有状態。NatNet コールバック(書き込み)と
# NanoVNA ループ(読み出し)の双方からアクセスされる。
# 各部位ごとに {x, y, z, valid} を保持する。
_pos_lock = threading.Lock()
_latest_positions = {
    "UpperArm": {"x": None, "y": None, "z": None, "valid": False},
    "Joint":    {"x": None, "y": None, "z": None, "valid": False},
    "Forearm":  {"x": None, "y": None, "z": None, "valid": False},
}

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


def read_latest_positions():
    """
    NanoVNA ループから呼ばれ、3 点すべての最新座標のスナップショットを
    まとめて(同一ロック下で)取得する。
    戻り値: {"UpperArm": (x,y,z,valid), "Joint": (...), "Forearm": (...)}
    """
    snapshot = {}
    with _pos_lock:
        for name, slot in _latest_positions.items():
            snapshot[name] = (slot["x"], slot["y"], slot["z"], slot["valid"])
    return snapshot


# =============================================================================
# インピーダンス変換
# =============================================================================

def s11_to_impedance(s11: complex, z0: float = Z0):
    """
    反射係数 S11 (複素数) から負荷インピーダンス Z = R + jX を計算する。
        Z = Z0 * (1 + S11) / (1 - S11)
    戻り値: (R, X)  実部・虚部 [Ω]
    """
    denom = (1.0 - s11)
    if denom == 0:
        # 完全反射(開放相当)。発散を避けて inf を返す。
        return float("inf"), float("inf")
    z = z0 * (1.0 + s11) / denom
    return z.real, z.imag


# =============================================================================
# NanoVNA / JNCRadio VNA-3G シリアル制御
# =============================================================================

class NanoVNA:
    """
    JNCRadio VNA-3G (NanoVNA 互換) のコンソールコマンドを扱う薄いラッパ。

    本機のシリアルは「コマンド\\r を送ると、エコー → 結果行 → プロンプト 'ch> '」
    の順で応答する。scan コマンドで毎回フレッシュな掃引を実行し S11 を取得する。

        scan {start(Hz)} {stop(Hz)} [points] [outmask]
        outmask=2 -> 各掃引点の S11 データ(real imag)のみを出力
    """

    PROMPT = b"ch> "

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        # 起動直後のゴミ/残プロンプトを読み捨てる
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        # 連続出力を止めてプロンプト状態を確定させる
        self._send_raw("pause")
        self._read_until_prompt()

    def _send_raw(self, cmd: str):
        """コマンドを CR 終端で送信する(本機の終端は <CR>)。"""
        self.ser.write((cmd + "\r").encode("ascii"))
        self.ser.flush()

    def _read_until_prompt(self, max_wait: float = 3.0) -> str:
        """
        プロンプト 'ch> ' が現れるまで読み、受信テキスト全体を返す。
        タイムアウト時はそれまでに受信した分を返す。
        """
        buf = bytearray()
        deadline = time.time() + max_wait
        while time.time() < deadline:
            chunk = self.ser.read(self.ser.in_waiting or 1)
            if chunk:
                buf += chunk
                if buf.endswith(self.PROMPT) or self.PROMPT in buf:
                    break
            else:
                # データが来ない瞬間が続いてもプロンプトが既にあれば抜ける
                if self.PROMPT in buf:
                    break
        return buf.decode("ascii", errors="ignore")

    @staticmethod
    def _parse_s11_lines(text: str):
        """
        受信テキストから S11 の (real, imag) ペア群を抽出する。
        outmask=2 の各データ行は "real imag" の2列。コマンドエコーや
        プロンプト等、2 float に解釈できない行はスキップする。
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
                # コマンドエコー・プロンプト・その他テキスト行
                continue
            pairs.append((real, imag))
        return pairs

    def measure_s11(self):
        """
        単一周波数 TARGET_FREQ_HZ で 1 回 scan を実行し、S11 (複素数) を返す。
        SCAN_POINTS 点すべて同一周波数(start==stop)なので平均してノイズを抑える。
        取得失敗時は None を返す。
        """
        cmd = "scan {start} {stop} {pts} 2".format(
            start=int(TARGET_FREQ_HZ),
            stop=int(TARGET_FREQ_HZ),
            pts=int(SCAN_POINTS),
        )
        # 送信前に入力バッファを空にして、直前応答の取りこぼし/混線を防ぐ
        self.ser.reset_input_buffer()
        self._send_raw(cmd)
        text = self._read_until_prompt()
        pairs = self._parse_s11_lines(text)
        if not pairs:
            return None
        # 同一周波数の複数点を平均
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
# OptiTrack / NatNet スレッド
# =============================================================================

def _extract_marker_id_pos(marker):
    """
    1 個の Labeled Marker オブジェクト/タプルから (marker_id, x, y, z) を取り出す。
    NatNet SDK のバージョン差(LabeledMarker クラス属性 / 生タプル)を吸収する。
    取り出せない場合は None を返す。
    """
    # --- オブジェクト形式 (MoCapData.LabeledMarker など) ---
    pos = getattr(marker, "pos", None)
    mid = None
    for attr in ("id_num", "marker_id", "id"):
        if hasattr(marker, attr):
            mid = getattr(marker, attr)
            break
    if pos is not None and mid is not None and len(pos) >= 3:
        return int(mid), float(pos[0]), float(pos[1]), float(pos[2])

    # --- タプル/リスト形式: (id, x, y, z, ...) を想定 ---
    try:
        if len(marker) >= 4:
            return int(marker[0]), float(marker[1]), float(marker[2]), float(marker[3])
    except (TypeError, ValueError):
        pass
    return None


def process_labeled_markers(markers):
    """
    Labeled Marker のリストを走査し、計測対象 3 マーカー(上腕/関節/前腕)の
    最新座標をスレッド安全に更新する。
    markers: LabeledMarker オブジェクト、または (id, x, y, z) タプルの反復可能
    """
    if not markers:
        return
    for m in markers:
        parsed = _extract_marker_id_pos(m)
        if parsed is None:
            continue
        marker_id, x, y, z = parsed
        name = match_marker_name(marker_id)
        if name is None:
            continue  # 計測対象外のマーカーは無視
        update_latest_position(name, x, y, z)


def _markers_from_mocap(mocap_data):
    """MoCapData オブジェクトから Labeled Marker のリストを取り出す(SDK差吸収)。"""
    lm = getattr(mocap_data, "labeled_marker_data", None)
    if lm is None:
        return None
    for attr in ("labeled_marker_list", "marker_list", "markers"):
        lst = getattr(lm, attr, None)
        if lst is not None:
            return lst
    return None


def start_natnet():
    """
    NatNetClient を起動し、Labeled Marker(ラベル付きマーカー)受信コールバックを
    登録する。成功すると streaming_client を返す。NatNet SDK の Python サンプル
    (NatNetClient.py / MoCapData.py 等) が import パス上にある必要がある。
    """
    try:
        from NatNetClient import NatNetClient
    except ImportError:
        print("[ERROR] NatNetClient が import できません。")
        print("        OptiTrack NatNet SDK 同梱の Python サンプル(NatNetClient.py 等)を")
        print("        このスクリプトと同じフォルダ、または PYTHONPATH に置いてください。")
        return None

    client = NatNetClient()

    # --- サーバ/クライアント IP の設定(SDK バージョン差を吸収) ---
    try:
        client.set_client_address(NATNET_LOCAL_IP)
        client.set_server_address(NATNET_SERVER_IP)
        client.set_use_multicast(False)   # 同一PCループバックは Unicast 推奨
    except AttributeError:
        # 旧 API: 属性で直接指定
        client.local_ip_address = NATNET_LOCAL_IP
        client.server_ip_address = NATNET_SERVER_IP

    # --- フレーム受信コールバック(MoCapData 付き) ---
    def on_frame_with_data(*args):
        """
        1 フレーム届くたびに呼ばれる。引数に含まれる MoCapData から
        Labeled Marker のリストを取り出して処理する。
        SDK により署名が (data_dict, mocap_data) などと異なるため、
        引数群から MoCapData らしきものを探して使う。
        """
        mocap = None
        for a in args:
            if hasattr(a, "labeled_marker_data"):
                mocap = a
                break
        if mocap is None:
            return
        markers = _markers_from_mocap(mocap)
        process_labeled_markers(markers)

    # 一部の SDK/フォークは Labeled Marker 専用 listener を持つ
    def on_labeled_markers(markers, *_):
        process_labeled_markers(markers)

    # --- コールバック登録(SDK バージョン差を吸収) ---
    registered = False
    # 1) MoCapData を渡してくれる listener を最優先
    for attr in ("new_frame_with_data_listener",
                 "labeled_marker_listener",
                 "marker_set_listener"):
        if hasattr(client, attr):
            if attr == "labeled_marker_listener":
                setattr(client, attr, on_labeled_markers)
            else:
                setattr(client, attr, on_frame_with_data)
            registered = True
            print("[INFO] Labeled Marker コールバックを '{}' に登録しました。".format(attr))
            break

    if not registered:
        # 2) フォールバック: 通常の new_frame_listener。
        #    ※ 標準 SDK の new_frame_listener は集計値のみで座標を含まない場合があります。
        #    その場合は NatNetClient 側の改造、または MoCapData を渡す listener が必要です。
        if hasattr(client, "new_frame_listener"):
            client.new_frame_listener = on_frame_with_data
            print("[WARN] new_frame_with_data_listener が見つからないため new_frame_listener を使用します。")
            print("       マーカー座標が取得できない場合は、お使いの NatNetClient が")
            print("       MoCapData をコールバックに渡す実装か確認してください。")
        else:
            print("[WARN] Labeled Marker 用コールバックを登録できませんでした。SDK 版を確認してください。")

    # --- 受信開始 ---
    started = False
    try:
        # 新しめの API は run() に通信モードを渡す
        started = client.run("d")  # "d" = data + command threads
    except TypeError:
        started = client.run()

    if started is False:
        print("[ERROR] NatNetClient の起動に失敗しました。Motive のストリーミング設定を確認してください。")
        return None

    print("[INFO] NatNet クライアント開始。Motive からのデータ受信待ち...")
    return client


# =============================================================================
# メイン: NanoVNA サンプリングループ + CSV 書き込み
# =============================================================================

def main():
    # --- NanoVNA 接続 ---
    try:
        vna = NanoVNA(NANOVNA_PORT, NANOVNA_BAUD)
    except serial.SerialException as e:
        print("[ERROR] NanoVNA シリアル接続に失敗: {}".format(e))
        print("        NANOVNA_PORT('{}') が正しいか確認してください。".format(NANOVNA_PORT))
        sys.exit(1)
    print("[INFO] NanoVNA 接続 OK: {} @ {}bps, {:.3f} MHz".format(
        NANOVNA_PORT, NANOVNA_BAUD, TARGET_FREQ_HZ / 1e6))

    # --- NatNet 開始 ---
    natnet_client = start_natnet()
    if natnet_client is None:
        print("[WARN] OptiTrack なしで続行します(座標列は空欄になります)。")

    print("[INFO] 計測対象マーカー ID -> UpperArm={}, Joint={}, Forearm={}".format(
        UPPER_ARM_MARKER_ID, JOINT_MARKER_ID, FOREARM_MARKER_ID))

    # OptiTrack の初回データ到着を少し待つ(3 マーカーすべての受信を確認)
    for _ in range(50):
        snap = read_latest_positions()
        if all(snap[n][3] for n in ("UpperArm", "Joint", "Forearm")):
            print("[INFO] OptiTrack 初期データ受信 OK(3マーカーすべて)。")
            break
        time.sleep(0.1)
    else:
        if natnet_client is not None:
            snap = read_latest_positions()
            missing = [n for n in ("UpperArm", "Joint", "Forearm") if not snap[n][3]]
            print("[WARN] OptiTrack の初期データが未受信のマーカーがあります: {}".format(missing))
            print("       マーカー ID/ラベル付け/トラッキング状態を確認してください。")

    # --- CSV 準備 & 計測ループ ---
    sample_count = 0
    try:
        with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            f.flush()

            print("[INFO] 計測開始。Ctrl+C で停止します。")
            while not _stop_event.is_set():
                # 1) NanoVNA から S11 を1点取得(ここがレート律速)
                s11 = vna.measure_s11()
                if s11 is None:
                    print("[WARN] S11 のパースに失敗。スキップします。")
                    continue

                # 2) 取得が完了した「その瞬間」の 3 点(計9値)の最新座標を
                #    まとめてスナップショット取得(同一ロック下で一括取得)
                snap = read_latest_positions()

                # 3) インピーダンス計算
                z_r, z_x = s11_to_impedance(s11, Z0)

                # 4) CSV へ1行書き込み(3点の座標 + S11 + Z をガッチャンコ)
                ts = datetime.now().isoformat(timespec="milliseconds")

                def fmt(name):
                    """部位の (x,y,z) を CSV セル 3 個へ整形。未受信なら空欄。"""
                    x, y, z, valid = snap[name]
                    if not valid:
                        return ["", "", ""]
                    return ["{:.6f}".format(x), "{:.6f}".format(y), "{:.6f}".format(z)]

                writer.writerow(
                    [ts]
                    + fmt("UpperArm")
                    + fmt("Joint")
                    + fmt("Forearm")
                    + [
                        "{:.6f}".format(s11.real),
                        "{:.6f}".format(s11.imag),
                        "{:.4f}".format(z_r),
                        "{:.4f}".format(z_x),
                    ]
                )
                f.flush()  # 計測中に随時保存(途中でクラッシュしてもデータを残す)

                sample_count += 1
                if sample_count % 10 == 0:
                    def short(name):
                        x, y, z, valid = snap[name]
                        if not valid:
                            return "(----,----,----)"
                        return "({:.2f},{:.2f},{:.2f})".format(x, y, z)
                    print("[{:5d}] UA={} J={} FA={} | S11=({:.4f},{:.4f}) Z=({:.2f}{:+.2f}j)Ω".format(
                        sample_count,
                        short("UpperArm"), short("Joint"), short("Forearm"),
                        s11.real, s11.imag, z_r, z_x))

    except KeyboardInterrupt:
        print("\n[INFO] Ctrl+C を検出。停止処理中...")
    finally:
        _stop_event.set()
        vna.close()
        if natnet_client is not None:
            try:
                natnet_client.shutdown()
            except Exception:
                pass
        print("[INFO] 終了。{} サンプルを '{}' に保存しました。".format(sample_count, OUTPUT_CSV))


if __name__ == "__main__":
    main()
