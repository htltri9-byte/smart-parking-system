"""Giả lập N lượt xe vào cổng để đo độ trễ chia chặng (giai đoạn 7).

Mỗi lượt:
    1. publish parking/gate/in/event   {"type":"vehicle_detected",...}
    2. chờ parking/gate/in/cmd         {"barrier":"open",...}
    3. đợi --servo-ms (giả lập servo quay) rồi publish
       parking/gate/in/ack            {"type":"barrier_opened",...}
    4. publish parking/gate/in/passed  {"type":"vehicle_passed",...}
    5. nghỉ --gap giây rồi sang lượt kế tiếp

Capture-agent dùng video làm nguồn nên ALPR chạy thật: mỗi lượt tốn đúng thời
gian capture + ALPR của backend, và backend ghi một dòng `gate_traces`.

Xem kết quả:  curl 'http://localhost:8000/api/traces?limit=5'
Xuất báo cáo: python tools/export_latency.py

Chạy:
    python tools/sim_gate_cycles.py --turns 30
    python tools/sim_gate_cycles.py --turns 5 --gap 8 --servo-ms 300
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'paho-mqtt'. Cài: pip install -r tools/requirements.txt")

# Khoảng cách mặc định giữa hai lượt: backend bỏ qua event trùng trong
# GATE_COOLDOWN_SECONDS (mặc định 5 s), nên gap phải lớn hơn ngưỡng đó.
DEFAULT_GAP_S = 6.0
# Thời gian servo quay mặc định (khớp ~90 độ của SG90 và SERVO_DEG_PER_SEC trong firmware).
DEFAULT_SERVO_MS = 300.0
# Khoảng cách tới gờ giảm tốc trong payload giả lập.
DISTANCE_CM = 30.0


def topic(args: argparse.Namespace, suffix: str) -> str:
    """Topic cổng vào theo docs/CONTRACT.md: <prefix>/gate/in/<suffix>."""
    return f"{args.prefix}/gate/in/{suffix}"


class Inbox:
    """Hàng đợi lệnh mở barrier; lệnh close và bản tin khác bị bỏ qua."""

    def __init__(self) -> None:
        self._queue: queue.Queue[float] = queue.Queue()

    def put(self, ts: float) -> None:
        self._queue.put(ts)

    def wait(self, timeout: float) -> float | None:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                return self._queue.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue

    def clear(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


def build_client(
    args: argparse.Namespace, inbox: Inbox, connected
) -> mqtt.Client:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-gate-cycles")
    cmd_topic = topic(args, "cmd")

    def on_connect(c, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            print(f"[mqtt] kết nối thất bại: {reason_code}")
            return
        c.subscribe(cmd_topic, qos=1)
        connected.set()

    def on_message(c, userdata, msg) -> None:
        if msg.topic != cmd_topic:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        # Chỉ lệnh mở barrier mới mở lượt mới; {"barrier":"close"} bị bỏ qua.
        if isinstance(payload, dict) and payload.get("barrier") == "open":
            inbox.put(time.perf_counter())

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=10)
    return client


def publish(client: mqtt.Client, topic_name: str, payload: dict) -> None:
    client.publish(topic_name, json.dumps(payload), qos=1)


def run_cycles(args: argparse.Namespace, client: mqtt.Client, inbox: Inbox) -> int:
    if args.turns <= 0:
        print("Số lượt phải > 0")
        return 2

    opened_count = 0
    for turn in range(1, args.turns + 1):
        inbox.clear()  # không lấy nhầm lệnh open của lượt trước
        started = time.perf_counter()
        publish(
            client,
            topic(args, "event"),
            {"type": "vehicle_detected", "distance_cm": DISTANCE_CM, "ts": time.time()},
        )

        opened = inbox.wait(args.timeout)
        if opened is None:
            print(f"[{turn}/{args.turns}] KHÔNG nhận được lệnh mở barrier sau {args.timeout:.0f}s")
            if turn == 1:
                print("  (backend chưa chạy? kiểm tra MQTT_HOST/MQTT_PREFIX của backend)")
                return 1
            time.sleep(args.gap)
            continue

        opened_count += 1
        open_ms = (opened - started) * 1000.0

        # Giả lập servo quay: ack chỉ gửi SAU khi servo tới góc mở.
        time.sleep(args.servo_ms / 1000.0)
        publish(client, topic(args, "ack"), {"type": "barrier_opened", "ts": time.time()})
        publish(client, topic(args, "passed"), {"type": "vehicle_passed", "ts": time.time()})

        print(
            f"[{turn}/{args.turns}] mở sau {open_ms:.0f} ms | ack sau servo {args.servo_ms:.0f} ms"
        )
        if turn < args.turns:
            time.sleep(args.gap)

    print(f"\nXong: {opened_count}/{args.turns} lượt nhận được lệnh mở barrier.")
    print("Xem vết:   curl 'http://localhost:8000/api/traces?limit=5'")
    print("Xuất báo cáo: python tools/export_latency.py")
    return 0 if opened_count else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Giả lập N lượt xe vào cổng: event -> chờ lệnh mở -> ack sau --servo-ms -> passed."
        ),
        epilog=(
            "Capture-agent phải đang chạy với nguồn video để ALPR chạy thật. "
            "Mỗi lượt backend ghi một dòng gate_traces."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="localhost", help="địa chỉ Mosquitto")
    parser.add_argument("--port", type=int, default=1883, help="cổng Mosquitto")
    parser.add_argument("--prefix", default="parking", help="tiền tố topic theo docs/CONTRACT.md")
    parser.add_argument("--turns", type=int, default=30, help="số lượt xe cần giả lập")
    parser.add_argument(
        "--gap",
        type=float,
        default=DEFAULT_GAP_S,
        help="số giây nghỉ giữa hai lượt (phải lớn hơn GATE_COOLDOWN_SECONDS=5)",
    )
    parser.add_argument(
        "--servo-ms",
        type=float,
        default=DEFAULT_SERVO_MS,
        help="thời gian giả lập servo quay từ lúc nhận lệnh mở tới lúc gửi ack",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="thời gian tối đa chờ lệnh mở barrier của mỗi lượt (giây)",
    )
    args = parser.parse_args(argv)
    if args.turns <= 0:
        parser.error("--turns phải > 0")
    if args.gap < 0:
        parser.error("--gap phải >= 0")
    if args.servo_ms < 0:
        parser.error("--servo-ms phải >= 0")
    if args.timeout <= 0:
        parser.error("--timeout phải > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    import threading

    args = parse_args(argv)
    inbox = Inbox()
    connected = threading.Event()
    client = build_client(args, inbox, connected)

    print(f"MQTT   : {args.host}:{args.port}, prefix '{args.prefix}'")
    print(f"Kịch bản: {args.turns} lượt, gap {args.gap:.0f}s, servo {args.servo_ms:.0f} ms\n")

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
        return run_cycles(args, client, inbox)
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
