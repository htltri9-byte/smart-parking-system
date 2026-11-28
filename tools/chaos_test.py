"""Chaos test Docker Compose: stop rồi start lại từng dịch vụ (giai đoạn 8).

Với mỗi dịch vụ:
    1. `docker compose stop <service>` (đo thời gian stop)
    2. `docker compose start <service>`, chờ tới khi healthcheck báo `healthy`
    3. kiểm tra luồng cổng còn chạy: publish `parking/gate/in/event` và chờ
       `parking/gate/in/cmd` với `barrier=open`

Kết thúc in bảng markdown. Mã thoát 1 nếu có bước nào thất bại, 2 nếu chưa có
môi trường healthy để bắt đầu.

Chạy:
    python tools/chaos_test.py
    python tools/chaos_test.py --services backend,portal --health-timeout 120
"""
from __future__ import annotations

import argparse
import json
import queue
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'paho-mqtt'. Cài: pip install -r tools/requirements.txt")

DEFAULT_SERVICES = "mosquitto,postgres,backend,alpr,portal"
# Lấy health nếu container có healthcheck, không thì lấy trạng thái thường.
STATE_TEMPLATE = "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"
# Các trạng thái coi là đã sẵn sàng.
READY_STATES = {"healthy", "running"}


@dataclass
class Row:
    service: str
    stop_s: float | None = None
    healthy_s: float | None = None
    gate_ok: bool | None = None
    gate_ms: float | None = None
    note: str = ""


def detect_compose(override: str | None) -> list[str]:
    if override:
        return shlex.split(override)
    for candidate in (["docker", "compose"], ["docker-compose"]):
        try:
            result = subprocess.run(
                [*candidate, "version"], capture_output=True, timeout=20, text=True
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    raise SystemExit("Không tìm thấy docker compose. Cài Docker Desktop hoặc dùng --compose-cmd.")


def compose_run(
    args: argparse.Namespace, cmd: list[str], timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    argv = [*args.compose_cmd, *cmd]
    try:
        return subprocess.run(
            argv,
            cwd=args.directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # --compose-cmd sai hoặc compose bị treo: trả về lỗi thay vì traceback.
        return subprocess.CompletedProcess(argv, 127, "", f"không chạy được compose: {exc}")


def container_id(args: argparse.Namespace, service: str) -> str | None:
    result = compose_run(args, ["ps", "-q", service], timeout=30)
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return ids[0] if ids else None


def container_state(args: argparse.Namespace, service: str) -> str:
    cid = container_id(args, service)
    if not cid:
        return "missing"
    try:
        result = subprocess.run(
            [*args.docker_cmd, "inspect", "-f", STATE_TEMPLATE, cid],
            cwd=args.directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "missing"
    return (result.stdout or "").strip() or "unknown"


def wait_healthy(args: argparse.Namespace, service: str) -> tuple[float | None, str]:
    """Chờ tới khi container healthy; trả (số giây, trạng thái cuối)."""
    started = time.perf_counter()
    state = "unknown"
    while time.perf_counter() - started < args.health_timeout:
        state = container_state(args, service)
        if state in READY_STATES:
            return time.perf_counter() - started, state
        time.sleep(0.5)
    return None, state


def check_gate(args: argparse.Namespace) -> tuple[bool, float | None, str]:
    """Publish một sự kiện cổng vào và chờ lệnh mở barrier."""
    cmd_topic = f"{args.prefix}/gate/in/cmd"
    event_topic = f"{args.prefix}/gate/in/event"
    inbox: queue.Queue[float] = queue.Queue()
    connected = threading.Event()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="chaos-gate")
    client.reconnect_delay_set(min_delay=1, max_delay=5)

    def on_connect(c, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            c.subscribe(cmd_topic, qos=1)
            connected.set()

    def on_message(c, userdata, msg) -> None:
        if msg.topic != cmd_topic:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if isinstance(payload, dict) and payload.get("barrier") == "open":
            inbox.put(time.perf_counter())

    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, keepalive=15)
    except OSError as exc:
        return False, None, f"không nối được MQTT: {exc}"

    client.loop_start()
    try:
        if not connected.wait(10.0):
            return False, None, "không nhận CONNACK sau 10 s"
        started = time.perf_counter()
        client.publish(
            event_topic,
            json.dumps({"type": "vehicle_detected", "distance_cm": 30.0, "ts": time.time()}),
            qos=1,
        )
        try:
            opened = inbox.get(timeout=args.gate_timeout)
        except queue.Empty:
            return False, None, f"không nhận lệnh mở sau {args.gate_timeout:.0f} s"
        return True, (opened - started) * 1000.0, ""
    finally:
        client.loop_stop()
        client.disconnect()


def fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def print_table(rows: list[Row]) -> None:
    print("\n| Dịch vụ | Stop (s) | Healthy sau start (s) | Luồng cổng | Ghi chú |")
    print("|---|---:|---:|---|---|")
    for row in rows:
        if row.gate_ok is None:
            gate = "—"
        elif row.gate_ok:
            gate = f"OK ({row.gate_ms:.0f} ms)" if row.gate_ms is not None else "OK"
        else:
            gate = "FAIL"
        print(
            f"| {row.service} | {fmt(row.stop_s)} | {fmt(row.healthy_s)} | {gate} | {row.note} |"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not Path(args.directory).is_dir():
        print(f"Không thấy thư mục compose: {args.directory}")
        return 2

    args.compose_cmd = detect_compose(args.compose_cmd)
    args.docker_cmd = shlex.split(args.docker)
    services = [name.strip() for name in args.services.split(",") if name.strip()]
    if not services:
        print("Danh sách dịch vụ rỗng (--services)")
        return 2

    print(f"Compose: {' '.join(args.compose_cmd)} (cwd {Path(args.directory).resolve()})")
    print(f"Dịch vụ: {', '.join(services)}\n")

    # Cần môi trường healthy trước khi bắt đầu, nếu không số đo vô nghĩa.
    for service in services:
        state = container_state(args, service)
        if state not in READY_STATES:
            print(f"{service}: trạng thái '{state}' - chưa sẵn sàng để chaos test.")
            print("  chạy trước: docker compose up -d && đợi mọi dịch vụ healthy")
            return 2

    rows: list[Row] = []
    failures = 0
    try:
        for service in services:
            print(f"=== {service} ===")
            row = Row(service=service)

            started = time.perf_counter()
            result = compose_run(args, ["stop", service], timeout=args.command_timeout)
            row.stop_s = time.perf_counter() - started
            if result.returncode != 0:
                row.note = f"stop lỗi: {(result.stderr or result.stdout).strip()[:120]}"
                failures += 1
                print(f"  stop lỗi: {row.note}")
                rows.append(row)
                continue
            print(f"  đã stop sau {row.stop_s:.1f} s")

            result = compose_run(args, ["start", service], timeout=args.command_timeout)
            if result.returncode != 0:
                row.note = f"start lỗi: {(result.stderr or result.stdout).strip()[:120]}"
                failures += 1
                print(f"  start lỗi: {row.note}")
                rows.append(row)
                continue
            print("  đã start, chờ healthy...")

            row.healthy_s, state = wait_healthy(args, service)
            if row.healthy_s is None:
                row.note = f"không healthy sau {args.health_timeout:.0f} s (trạng thái: {state})"
                failures += 1
                print(f"  FAIL: {row.note}")
                rows.append(row)
                continue
            print(f"  healthy sau {row.healthy_s:.1f} s, kiểm tra luồng cổng...")

            time.sleep(args.settle)
            row.gate_ok, row.gate_ms, note = check_gate(args)
            if not row.gate_ok:
                row.note = note
                failures += 1
                print(f"  luồng cổng FAIL: {note}")
            else:
                print(f"  luồng cổng OK ({row.gate_ms:.0f} ms)")
            rows.append(row)

            if service != services[-1]:
                time.sleep(args.gate_gap)
    except KeyboardInterrupt:
        print("\n[info] dừng sớm bằng Ctrl+C.")
    finally:
        print_table(rows)

    print(f"\n{len(rows)}/{len(services)} dịch vụ đã thử, {failures} lỗi.")
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Chaos test: lần lượt stop/start từng dịch vụ compose, đo thời gian healthy "
            "và kiểm tra luồng cổng sau khi phục hồi."
        ),
        epilog=(
            "Cần môi trường đang healthy trước khi chạy. Luồng cổng được kiểm tra bằng cách "
            "publish parking/gate/in/event và chờ parking/gate/in/cmd (barrier=open)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--services",
        default=DEFAULT_SERVICES,
        help="danh sách dịch vụ, phân tách bằng dấu phẩy",
    )
    parser.add_argument(
        "--directory",
        default=".",
        help="thư mục chứa docker-compose.yml",
    )
    parser.add_argument(
        "--compose-cmd",
        default=None,
        help="lệnh compose (mặc định tự dò 'docker compose' hoặc 'docker-compose')",
    )
    parser.add_argument("--docker", default="docker", help="lệnh docker dùng để inspect")
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=180.0,
        help="thời gian tối đa chờ một dịch vụ healthy (giây)",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=120.0,
        help="timeout cho một lệnh compose stop/start (giây)",
    )
    parser.add_argument(
        "--gate-timeout",
        type=float,
        default=20.0,
        help="thời gian chờ lệnh mở barrier sau khi phục hồi (giây)",
    )
    parser.add_argument(
        "--gate-gap",
        type=float,
        default=6.0,
        help="nghỉ giữa hai lần kiểm tra cổng (backend chống trùng 5 s)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="chờ thêm sau khi healthy để backend kịp nối lại MQTT",
    )
    parser.add_argument("--host", default="localhost", help="địa chỉ Mosquitto")
    parser.add_argument("--port", type=int, default=1883, help="cổng Mosquitto")
    parser.add_argument("--prefix", default="parking", help="tiền tố topic theo docs/CONTRACT.md")
    args = parser.parse_args(argv)
    for name in ("health_timeout", "command_timeout", "gate_timeout"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} phải > 0")
    for name in ("gate_gap", "settle"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} phải >= 0")
    return args


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
