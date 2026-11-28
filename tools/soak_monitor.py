"""Theo dõi soak test hệ thống bãi xe trong N phút (giai đoạn 7).

Subscribes `parking/#` và theo dõi 3 thiết bị `gate_in`, `gate_out`, `slots`:
- Online/offline từ `parking/device/<id>/status`, đếm số lần rớt (mỗi bản tin
  `offline` là một lần rớt).
- Khoảng cách giữa hai bản tin liên tiếp của mỗi ô đỗ (`parking/slot/<n>/state`);
  cảnh báo khi lớn hơn 15 s (firmware gửi mỗi 5 s).
- Mỗi 30 s gọi `GET /api/health` của backend và ghi nhận `mqtt_connected`.

Kết thúc: in bảng tóm tắt, ghi `results/soak_summary.csv`, thoát mã 1 nếu có thiết bị
rớt quá 2 lần.

Chạy:
    python tools/soak_monitor.py --minutes 30
    python tools/soak_monitor.py --minutes 2 --url http://localhost:8000
"""
from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
    import requests
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'paho-mqtt' hoặc 'requests'. Cài: pip install -r tools/requirements.txt")

DEVICES = ("gate_in", "gate_out", "slots")
# Firmware gửi trạng thái ô đỗ mỗi 5 s -> quá 15 s là bất thường.
SLOT_GAP_LIMIT_S = 15.0
# Chu kỳ gọi /api/health.
HEALTH_INTERVAL_S = 30.0
# Rớt quá số này thì kết thúc với mã 1.
DROP_LIMIT = 2
CSV_COLUMNS = ("metric", "device", "value")


@dataclass
class DeviceStat:
    """Số liệu của một thiết bị trong suốt phiên theo dõi."""

    name: str
    messages: int = 0
    drops: int = 0
    last_data_mono: float | None = None  # bản tin dữ liệu gần nhất (không tính status)
    longest_silence_s: float = 0.0
    slot_alerts: int = 0


def clock() -> str:
    return time.strftime("%H:%M:%S")


def mqtt_down_seconds(samples: list[tuple[float, bool | None]], end_mono: float) -> float:
    """Tổng thời gian backend báo `mqtt_connected=false`.

    Mẫu `None` (gọi /api/health lỗi) đóng khoảng đang tính vì không biết trạng thái;
    lỗi được đếm riêng. Mẫu `False` cuối cùng kéo dài tới lúc kết thúc.
    """
    total = 0.0
    down_at: float | None = None
    for mono, state in samples:
        if state is False:
            if down_at is None:
                down_at = mono
            continue
        if down_at is not None:
            total += max(0.0, mono - down_at)
            down_at = None
    if down_at is not None:
        total += max(0.0, end_mono - down_at)
    return total


class SoakMonitor:
    """Thu thập số liệu từ MQTT và /api/health trong một phiên soak."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.start_mono = time.perf_counter()
        self.devices: dict[str, DeviceStat] = {name: DeviceStat(name) for name in DEVICES}
        self.slot_last: dict[int, float] = {}
        self.total_messages = 0
        self.slot_alerts = 0
        self.health_samples: list[tuple[float, bool | None]] = []
        self.health_polls = 0
        self.health_errors = 0
        self._stop = threading.Event()
        self._last_health_state: bool | None = None
        self._health_thread: threading.Thread | None = None

    # ---- MQTT ----
    def handle(self, topic: str, raw: bytes) -> None:
        mono = time.perf_counter()
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self.args.prefix:
            return

        kind = parts[1]
        if kind == "device":
            self._on_status(parts[2], raw)
            return

        if kind == "gate" and parts[2] in ("in", "out"):
            stat = self.devices.get(f"gate_{parts[2]}")
            if stat is not None:
                self._record_data(stat, mono, self.start_mono)
            return

        if kind == "slot" and len(parts) == 4 and parts[3] == "state":
            self._on_slot(parts[2], mono)

    def _on_status(self, device_id: str, raw: bytes) -> None:
        stat = self.devices.get(device_id)
        if stat is None:
            return
        # Bản tin status không tính vào khoảng im lặng (nó không định kỳ).
        stat.messages += 1
        self.total_messages += 1

        status = raw.decode("utf-8", errors="replace").strip().strip('"').lower()
        if status == "offline":
            stat.drops += 1
            print(f"[{clock()}] {device_id}: OFFLINE (rớt lần {stat.drops})")
        elif status == "online":
            print(f"[{clock()}] {device_id}: online")

    def _on_slot(self, slot_text: str, mono: float) -> None:
        stat = self.devices["slots"]
        stat.messages += 1
        self.total_messages += 1

        try:
            slot_id = int(slot_text)
        except ValueError:
            return

        previous = self.slot_last.get(slot_id, self.start_mono)
        gap = mono - previous
        stat.longest_silence_s = max(stat.longest_silence_s, gap)
        self.slot_last[slot_id] = mono
        stat.last_data_mono = mono

        if gap > SLOT_GAP_LIMIT_S:
            stat.slot_alerts += 1
            self.slot_alerts += 1
            print(
                f"[{clock()}] ô {slot_id}: im lặng {gap:.1f} s "
                f"(> {SLOT_GAP_LIMIT_S:.0f} s, firmware gửi mỗi 5 s)"
            )

    @staticmethod
    def _record_data(stat: DeviceStat, mono: float, start_mono: float) -> None:
        stat.messages += 1
        previous = stat.last_data_mono if stat.last_data_mono is not None else start_mono
        stat.longest_silence_s = max(stat.longest_silence_s, mono - previous)
        stat.last_data_mono = mono

    # ---- health ----
    def start_health(self) -> None:
        self._health_thread = threading.Thread(
            target=self._health_loop, name="soak-health", daemon=True
        )
        self._health_thread.start()

    def stop_health(self) -> None:
        self._stop.set()
        if self._health_thread is not None:
            self._health_thread.join(timeout=10.0)
            self._health_thread = None

    def _health_loop(self) -> None:
        url = self.args.url.rstrip("/") + "/api/health"
        while not self._stop.is_set():
            mono = time.perf_counter()
            self.health_polls += 1
            try:
                response = requests.get(url, timeout=self.args.timeout)
                response.raise_for_status()
                connected = bool(response.json().get("mqtt_connected"))
                self.health_samples.append((mono, connected))
                if connected != self._last_health_state:
                    self._last_health_state = connected
                    print(f"[{clock()}] backend mqtt_connected={connected}")
            except (requests.RequestException, ValueError) as exc:
                self.health_errors += 1
                self.health_samples.append((mono, None))
                print(f"[{clock()}] /api/health lỗi: {exc}")
            if self._stop.wait(HEALTH_INTERVAL_S):
                return

    # ---- tổng kết ----
    def finish(self) -> int:
        end_mono = time.perf_counter()
        duration = end_mono - self.start_mono

        # Thiết bị không gửi bản tin dữ liệu nào coi như im lặng suốt phiên.
        for stat in self.devices.values():
            if stat.last_data_mono is None:
                stat.longest_silence_s = duration

        down_seconds = mqtt_down_seconds(self.health_samples, end_mono)
        self._print_summary(duration, down_seconds)
        self._write_csv(duration, down_seconds)

        failed = [stat for stat in self.devices.values() if stat.drops > DROP_LIMIT]
        if failed:
            names = ", ".join(f"{stat.name} ({stat.drops} lần)" for stat in failed)
            print(f"\nTHẤT BẠI: thiết bị rớt quá {DROP_LIMIT} lần -> {names}")
            return 1
        return 0

    def _print_summary(self, duration: float, down_seconds: float) -> None:
        print("\n" + "=" * 64)
        print(f"SOAK kết thúc: {duration / 60:.1f} phút ({duration:.0f} s)")
        print(f"Tổng bản tin MQTT: {self.total_messages} | cảnh báo ô đỗ im lặng: {self.slot_alerts}")
        print()
        print("| Thiết bị | Bản tin | Số lần rớt | Im lặng dài nhất (s) |")
        print("|---|---:|---:|---:|")
        for stat in self.devices.values():
            print(
                f"| {stat.name} | {stat.messages} | {stat.drops} | {stat.longest_silence_s:.1f} |"
            )
        print()
        print(
            f"Backend: {self.health_polls} lần gọi /api/health, {self.health_errors} lỗi | "
            f"thời gian không nối MQTT: {down_seconds:.1f} s"
        )
        print("=" * 64)

    def _write_csv(self, duration: float, down_seconds: float) -> None:
        rows: list[tuple[str, str, object]] = [
            ("duration_s", "", f"{duration:.1f}"),
            ("total_messages", "", self.total_messages),
        ]
        for stat in self.devices.values():
            rows.append(("messages", stat.name, stat.messages))
            rows.append(("drops", stat.name, stat.drops))
            rows.append(("longest_silence_s", stat.name, f"{stat.longest_silence_s:.1f}"))
        rows.extend(
            [
                ("slot_gap_alerts", "slots", self.slot_alerts),
                ("health_polls", "backend", self.health_polls),
                ("health_errors", "backend", self.health_errors),
                ("mqtt_down_s", "backend", f"{down_seconds:.1f}"),
            ]
        )

        path: Path = self.args.csv
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            writer.writerow(CSV_COLUMNS)
            for row in rows:
                writer.writerow(row)
        print(f"Đã ghi tóm tắt: {path}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Theo dõi soak test: online/offline từng thiết bị, khoảng im lặng của ô đỗ "
            "và tình trạng MQTT của backend."
        ),
        epilog=(
            "Thoát mã 1 nếu có thiết bị rớt quá 2 lần. Kết quả ghi vào "
            "results/soak_summary.csv (cột metric,device,value)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--minutes", type=float, default=30.0, help="thời gian chạy (phút)")
    parser.add_argument("--host", default="localhost", help="địa chỉ Mosquitto")
    parser.add_argument("--port", type=int, default=1883, help="cổng Mosquitto")
    parser.add_argument("--prefix", default="parking", help="tiền tố topic theo docs/CONTRACT.md")
    parser.add_argument("--url", default="http://localhost:8000", help="địa chỉ backend")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("results/soak_summary.csv"),
        help="file CSV tóm tắt (đường dẫn tính từ thư mục đang chạy)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="timeout gọi /api/health (giây)")
    args = parser.parse_args(argv)
    if args.minutes <= 0:
        parser.error("--minutes phải > 0")
    if args.timeout <= 0:
        parser.error("--timeout phải > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    monitor = SoakMonitor(args)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="soak-monitor")
    connected = threading.Event()

    def on_connect(c, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            print(f"[mqtt] kết nối thất bại: {reason_code}")
            return
        c.subscribe(f"{args.prefix}/#", qos=1)
        print(f"[{clock()}] đã subscribe {args.prefix}/#")
        connected.set()

    def on_message(c, userdata, msg) -> None:
        monitor.handle(msg.topic, msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Theo dõi {args.minutes:.1f} phút | MQTT {args.host}:{args.port}/{args.prefix}#")
    print(f"Backend {args.url} (poll mỗi {HEALTH_INTERVAL_S:.0f} s)\n")

    try:
        client.connect(args.host, args.port, keepalive=30)
    except OSError as exc:
        print(f"[mqtt] không kết nối được {args.host}:{args.port}: {exc}")
        print("  (broker đã chạy chưa? thử: docker compose up -d mosquitto)")
        return 2

    client.loop_start()
    if not connected.wait(10.0):
        print("[mqtt] không nhận được phản hồi kết nối (CONNACK) sau 10 s")
        client.loop_stop()
        client.disconnect()
        return 2

    monitor.start_health()
    deadline = monitor.start_mono + args.minutes * 60.0
    try:
        while time.perf_counter() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[info] dừng sớm bằng Ctrl+C, vẫn in tóm tắt.")
    finally:
        monitor.stop_health()
        client.loop_stop()
        client.disconnect()

    return monitor.finish()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
