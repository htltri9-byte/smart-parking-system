"""Đo độ trễ từ sự kiện cảm biến tới lệnh mở barrier (giai đoạn 7).

Hai chế độ:
    --mode sim   script tự publish parking/gate/in/event giả, N lượt (mặc định 30)
    --mode live  chỉ lắng nghe và ghi từng lượt; nhóm tự đưa vật vào cảm biến thật

Đồng hồ: chỉ dùng time.perf_counter() của chính script.
KHÔNG dùng trường `ts` của ESP32: đồng hồ board và laptop lệch nhau, không so được.

Mỗi lượt đo: parking/gate/in/event -> parking/gate/in/cmd có "barrier":"open".
Bỏ 2 lượt đầu (làm nóng). Xuất results/latency.csv và in p50/p95/trung bình/lớn nhất.

Chạy:
    python tools/measure_latency.py --mode sim
    python tools/measure_latency.py --mode live
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'paho-mqtt'. Cài: pip install -r tools/requirements.txt")

# Số lượt đầu bỏ đi (làm nóng: model ALPR, kết nối DB, driver camera còn nguội).
WARMUP_TURNS = 2

# Loại bản tin trong hàng đợi.
EVENT = "event"
OPEN = "open"
ACK = "ack"

CSV_COLUMNS = ("turn", "mode", "started_at", "ms_to_open", "ms_to_ack", "status", "note")


@dataclass
class Turn:
    """Một lượt đo."""

    number: int
    mode: str
    started_at: str
    ms_to_open: float | None = None
    ms_to_ack: float | None = None
    status: str = "ok"
    note: str = ""


def topic(args: argparse.Namespace, suffix: str) -> str:
    """Topic cổng vào theo docs/CONTRACT.md: <prefix>/gate/in/<suffix>."""
    return f"{args.prefix}/gate/in/{suffix}"


class Inbox:
    """Hàng đợi bản tin MQTT; bản tin chưa dùng tới được giữ lại cho lượt sau."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, float, dict[str, Any]]] = queue.Queue()
        self._backlog: list[tuple[str, float, dict[str, Any]]] = []

    def put(self, kind: str, ts: float, payload: dict[str, Any]) -> None:
        self._queue.put((kind, ts, payload))

    def wait(
        self, kind: str, timeout: float | None
    ) -> tuple[str, float, dict[str, Any]] | None:
        """Chờ bản tin đúng loại; bản tin khác được giữ lại, không bị mất.

        timeout=None: chờ tới khi có (vẫn phản hồi Ctrl+C vì chỉ block 0.2 s mỗi lần).
        """
        for index, item in enumerate(self._backlog):
            if item[0] == kind:
                return self._backlog.pop(index)

        deadline = None if timeout is None else time.perf_counter() + timeout
        while True:
            if deadline is None:
                wait_s = 0.2
            else:
                wait_s = deadline - time.perf_counter()
                if wait_s <= 0:
                    return None
                wait_s = min(wait_s, 0.2)
            try:
                item = self._queue.get(timeout=wait_s)
            except queue.Empty:
                continue
            if item[0] == kind:
                return item
            self._backlog.append(item)

    def clear(self) -> None:
        """Bỏ mọi bản tin còn sót để không ghép nhầm giữa hai lượt."""
        self._backlog.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


def build_client(
    args: argparse.Namespace, inbox: Inbox, connected: threading.Event
) -> mqtt.Client:
    """Client MQTT chỉ lắng nghe (trừ chế độ sim tự publish event)."""
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="measure-latency")
    event_topic = topic(args, "event")
    cmd_topic = topic(args, "cmd")

    def on_connect(c, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            print(f"[mqtt] kết nối thất bại: {reason_code}")
            return
        c.subscribe(event_topic, qos=1)
        c.subscribe(cmd_topic, qos=1)
        if args.ack_topic:
            c.subscribe(args.ack_topic, qos=1)
        connected.set()

    def on_message(c, userdata, msg) -> None:
        ts = time.perf_counter()
        if msg.topic == cmd_topic:
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            # Chỉ lệnh mở barrier mới là mốc kết thúc; {"barrier":"close"} bị bỏ qua.
            if isinstance(payload, dict) and payload.get("barrier") == "open":
                inbox.put(OPEN, ts, payload)
            return
        if args.ack_topic and msg.topic == args.ack_topic:
            inbox.put(ACK, ts, {})
        elif msg.topic == event_topic:
            inbox.put(EVENT, ts, {})

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    return client


def percentile(values: list[float], percent: float) -> float:
    """Nội suy tuyến tính - đủ chính xác cho báo cáo, không cần numpy."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def format_ms(value: float | None) -> str:
    return "timeout" if value is None else f"{value:.1f} ms"


def print_turn(turn: Turn) -> None:
    if turn.number <= WARMUP_TURNS:
        print(f"[{turn.number}] làm nóng, bỏ qua ({format_ms(turn.ms_to_open)})", flush=True)
        return
    ack = "" if turn.ms_to_ack is None else f" | ack {turn.ms_to_ack:.1f} ms"
    note = f"  {turn.note}" if turn.note else ""
    print(f"[{turn.number}] event -> open: {format_ms(turn.ms_to_open)}{ack}{note}", flush=True)


def sleep_until(deadline: float) -> None:
    """Ngủ tới mốc perf_counter, vẫn phản hồi Ctrl+C."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.05))


def measure(args: argparse.Namespace, client: mqtt.Client, inbox: Inbox, turns: int) -> int:
    """Vòng đo chính. Trả mã thoát của script."""
    all_turns: list[Turn] = []

    if args.mode == "live":
        print(f"Đang chờ {topic(args, 'event')} - đưa xe/vật vào cảm biến gờ giảm tốc (Ctrl+C để dừng)...")
    else:
        print(f"Tự gửi {topic(args, 'event')} mỗi {args.gap:.0f}s, chờ lệnh mở barrier...")

    try:
        while turns <= 0 or len(all_turns) < turns:
            inbox.clear()  # tránh ghép open/ack của lượt trước vào lượt này

            if args.mode == "sim":
                started_at = datetime.now().isoformat(timespec="milliseconds")
                # t0 = lúc script publish: đo trọn đường broker -> backend -> barrier.
                # (Không dùng ts của ESP32 vì đồng hồ board lệch với laptop.)
                t0 = time.perf_counter()
                client.publish(
                    topic(args, "event"),
                    json.dumps(
                        {"type": "vehicle_detected", "distance_cm": 30.0, "ts": time.time()}
                    ),
                    qos=1,
                )
            else:
                offered = inbox.wait(EVENT, None)
                if offered is None:  # không xảy ra (wait(None) chỉ trả khi có bản tin)
                    continue
                t0 = offered[1]
                started_at = datetime.now().isoformat(timespec="milliseconds")

            turn = Turn(number=len(all_turns) + 1, mode=args.mode, started_at=started_at)

            opened = inbox.wait(OPEN, args.timeout)
            if opened is None:
                turn.status = "timeout"
                turn.note = "không nhận gate/in/cmd open (backend/broker/prefix?)"
            else:
                turn.ms_to_open = round((opened[1] - t0) * 1000.0, 1)
                if args.ack_topic:
                    remaining = args.timeout - (time.perf_counter() - t0)
                    ack = inbox.wait(ACK, max(0.0, remaining))
                    if ack is None:
                        turn.note = "không nhận ack"
                    else:
                        turn.ms_to_ack = round((ack[1] - t0) * 1000.0, 1)

            all_turns.append(turn)
            print_turn(turn)

            if args.mode == "sim":
                # Xác nhận xe đã qua để backend đóng barrier ngay (giống cảm biến sau barrier).
                if turn.status == "ok" and args.send_passed:
                    client.publish(
                        topic(args, "passed"),
                        json.dumps({"type": "vehicle_passed", "ts": time.time()}),
                        qos=1,
                    )
                # Chờ hết khoảng cách: backend bỏ qua event trong GATE_COOLDOWN_SECONDS (mặc định 5 s).
                sleep_until(t0 + args.gap)
    except KeyboardInterrupt:
        print("\n[info] đã dừng bằng Ctrl+C.")

    return report(args, all_turns)


def write_csv(path: Path, turns: list[Turn]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for turn in turns:
            writer.writerow(
                [
                    turn.number,
                    turn.mode,
                    turn.started_at,
                    "" if turn.ms_to_open is None else f"{turn.ms_to_open:.1f}",
                    "" if turn.ms_to_ack is None else f"{turn.ms_to_ack:.1f}",
                    turn.status,
                    turn.note,
                ]
            )


def report(args: argparse.Namespace, all_turns: list[Turn]) -> int:
    measured = [turn for turn in all_turns if turn.number > WARMUP_TURNS]
    open_ms = [turn.ms_to_open for turn in measured if turn.ms_to_open is not None]
    ack_ms = [turn.ms_to_ack for turn in measured if turn.ms_to_ack is not None]
    failures = [turn for turn in measured if turn.status != "ok"]

    print("\n" + "=" * 62)
    print(f"Chế độ             : {args.mode}")
    print(f"Tổng lượt          : {len(all_turns)} (bỏ {min(WARMUP_TURNS, len(all_turns))} lượt làm nóng)")
    print(f"Số lượt tính điểm  : {len(measured)}")
    print(f"Đo được (open)     : {len(open_ms)}/{len(measured)}")
    if open_ms:
        print(
            f"ms event -> open   : p50 {percentile(open_ms, 50):.0f} | "
            f"p95 {percentile(open_ms, 95):.0f} | "
            f"trung bình {sum(open_ms) / len(open_ms):.0f} | "
            f"lớn nhất {max(open_ms):.0f}"
        )
    else:
        print("ms event -> open   : không có số đo (xem ghi chú timeout bên dưới)")
    if args.ack_topic:
        if ack_ms:
            print(
                f"ms event -> ack    : p50 {percentile(ack_ms, 50):.0f} | "
                f"p95 {percentile(ack_ms, 95):.0f} | lớn nhất {max(ack_ms):.0f}"
            )
        else:
            print("ms event -> ack    : không lượt nào nhận ack")
    else:
        print("ms event -> ack    : bỏ qua (chưa cấu hình --ack-topic; CONTRACT chưa có topic ack)")
    print(f"Lượt lỗi/timeout   : {len(failures)}")
    if failures and not open_ms:
        print("Gợi ý               : docker compose ps; kiểm tra MQTT_HOST/MQTT_PREFIX của backend")
    print(f"CSV                : {args.csv}")
    print("=" * 62)

    if measured:
        write_csv(args.csv, measured)
        print(f"Đã ghi {len(measured)} lượt vào {args.csv}")

    return 0 if open_ms or not measured else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Đo độ trễ parking/gate/in/event -> parking/gate/in/cmd (barrier=open) "
            "bằng đồng hồ time.perf_counter của script."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("sim", "live"),
        default="sim",
        help="sim = script tự publish event giả; live = chỉ lắng nghe cảm biến thật",
    )
    parser.add_argument("--host", default="localhost", help="địa chỉ Mosquitto")
    parser.add_argument("--port", type=int, default=1883, help="cổng Mosquitto")
    parser.add_argument("--prefix", default="parking", help="tiền tố topic theo docs/CONTRACT.md")
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        help="số lượt cần đo; 0 = chạy tới khi Ctrl+C (mặc định 30 cho sim, 0 cho live)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=6.0,
        help="giây nghỉ giữa hai lượt ở chế độ sim (phải lớn hơn GATE_COOLDOWN_SECONDS=5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="thời gian tối đa chờ lệnh mở barrier của mỗi lượt (giây)",
    )
    parser.add_argument(
        "--ack-topic",
        default="",
        help="topic xác nhận tuỳ chọn để đo thêm; hợp đồng hiện chưa có nên để trống",
    )
    parser.add_argument(
        "--no-passed",
        dest="send_passed",
        action="store_false",
        help="chế độ sim: không gửi gate/in/passed sau khi barrier mở",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("results/latency.csv"),
        help="file CSV kết quả từng lượt (đường dẫn tính từ thư mục đang chạy)",
    )
    args = parser.parse_args(argv)
    if args.gap < 0:
        parser.error("--gap phải >= 0")
    if args.timeout <= 0:
        parser.error("--timeout phải > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    turns = args.turns if args.turns is not None else (30 if args.mode == "sim" else 0)

    inbox = Inbox()
    connected = threading.Event()
    client = build_client(args, inbox, connected)

    print(f"MQTT   : {args.host}:{args.port}, prefix '{args.prefix}'")
    print(f"Đo     : {topic(args, 'event')} -> {topic(args, 'cmd')} (barrier=open)")
    print(f"Ack    : {args.ack_topic or '(không cấu hình)'}")
    print(f"Lượt   : {turns if turns > 0 else 'không giới hạn'}\n")

    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as exc:
        print(f"[mqtt] không kết nối được {args.host}:{args.port}: {exc}")
        print("  (broker đã chạy chưa? thử: docker compose up -d mosquitto)")
        return 1

    client.loop_start()
    try:
        if not connected.wait(10.0):
            print("[mqtt] không nhận được phản hồi kết nối (CONNACK) sau 10 s")
            return 1
        return measure(args, client, inbox, turns)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    # Console Windows mặc định cp1252 -> in tiếng Việt bị lỗi ký tự, ép UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
