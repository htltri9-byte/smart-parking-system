"""Kiểm tra trước buổi demo (preflight) và in PASS/FAIL từng mục.

Các mục kiểm tra:
    1. containers  - mọi dịch vụ Docker Compose đang healthy
    2. capture-agent - GET /health, `camera_ok` phải true
    3. devices     - 3 thiết bị online theo bản tin retained `parking/device/<id>/status`
    4. database    - GET /api/health của backend với `db_connected` true
    5. simulator   - một chu trình cổng: event -> cmd open -> (ack + passed) -> cmd close

Thoát mã 1 nếu có mục FAIL; 0 khi tất cả PASS.

Chạy:
    python tools/preflight.py
    python tools/preflight.py --port 1883 --url http://localhost:8000
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

try:
    import paho.mqtt.client as mqtt
    import requests
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'paho-mqtt' hoặc 'requests'. Cài: pip install -r tools/requirements.txt")

SERVICES = ("mosquitto", "postgres", "backend", "alpr", "portal")
DEVICES = ("gate_in", "gate_out", "slots")
STATE_TEMPLATE = "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"
READY_STATES = {"healthy", "running"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""


# ---- Docker ----
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
    args: argparse.Namespace, cmd: list[str], timeout: float = 60.0
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
        return subprocess.CompletedProcess(argv, 127, "", f"không chạy được compose: {exc}")


def container_state(args: argparse.Namespace, service: str) -> str:
    result = compose_run(args, ["ps", "-q", service], timeout=30)
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not ids:
        return "missing"
    try:
        inspect = subprocess.run(
            [*args.docker_cmd, "inspect", "-f", STATE_TEMPLATE, ids[0]],
            cwd=args.directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if inspect.returncode != 0:
        return "missing"
    return (inspect.stdout or "").strip() or "unknown"


def check_containers(args: argparse.Namespace) -> Check:
    states = []
    failed = []
    for service in args.services:
        state = container_state(args, service)
        states.append(f"{service}={state}")
        if state not in READY_STATES:
            failed.append(service)
    detail = ", ".join(states)
    if failed:
        return Check(
            "containers",
            False,
            f"{detail} | chưa sẵn sàng: {', '.join(failed)}",
            "docker compose up -d --build rồi đợi healthy",
        )
    return Check("containers", True, detail)


# ---- capture-agent ----
def check_capture(args: argparse.Namespace) -> Check:
    url = args.capture_url.rstrip("/") + "/health"
    try:
        response = requests.get(url, timeout=args.timeout)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return Check(
            "capture-agent",
            False,
            f"GET {url} lỗi: {exc}",
            "chạy `python capture-agent/app.py` trên máy host (cổng 8002)",
        )
    ok = bool(data.get("camera_ok"))
    detail = (
        f"camera_ok={ok} frame_age_s={data.get('frame_age_s')} "
        f"source={data.get('source')} ({data.get('source_kind')})"
    )
    if not ok:
        error = data.get("last_error")
        detail += f" | last_error={error}" if error else ""
        return Check("capture-agent", False, detail, "kiểm tra camera/file video và CAMERA_SOURCE")
    return Check("capture-agent", True, detail)


# ---- thiết bị online (retained status) ----
def check_devices(args: argparse.Namespace) -> Check:
    statuses: dict[str, str] = {}
    connected = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="preflight-devices")

    def on_connect(c, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            c.subscribe(f"{args.prefix}/device/+/status", qos=1)
            connected.set()

    def on_message(c, userdata, msg) -> None:
        parts = msg.topic.split("/")
        if len(parts) >= 4 and parts[1] == "device":
            statuses[parts[2]] = msg.payload.decode("utf-8", errors="replace").strip().lower()

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.host, args.port, keepalive=15)
    except OSError as exc:
        return Check(
            "devices",
            False,
            f"không nối được MQTT {args.host}:{args.port}: {exc}",
            "docker compose up -d mosquitto",
        )
    client.loop_start()
    try:
        if not connected.wait(10.0):
            return Check("devices", False, "không nhận CONNACK sau 10 s", "kiểm tra broker")
        deadline = time.perf_counter() + args.retained_wait
        while time.perf_counter() < deadline and not all(d in statuses for d in DEVICES):
            time.sleep(0.1)
    finally:
        client.loop_stop()
        client.disconnect()

    missing = [device for device in DEVICES if device not in statuses]
    offline = [device for device in DEVICES if statuses.get(device) == "offline"]
    detail = ", ".join(f"{device}={statuses.get(device, '(không thấy)')}" for device in DEVICES)
    if missing or offline:
        parts = []
        if missing:
            parts.append(f"không thấy status: {', '.join(missing)}")
        if offline:
            parts.append(f"offline: {', '.join(offline)}")
        return Check(
            "devices",
            False,
            f"{detail} | {'; '.join(parts)}",
            "bật nguồn board, kiểm tra WiFi/MQTT_HOST; last will phát offline khi mất kết nối",
        )
    return Check("devices", True, detail)


# ---- database qua /api/health ----
def check_database(args: argparse.Namespace) -> Check:
    url = args.url.rstrip("/") + "/api/health"
    try:
        response = requests.get(url, timeout=args.timeout)
    except requests.RequestException as exc:
        return Check(
            "database",
            False,
            f"GET {url} lỗi: {exc}",
            "kiểm tra container backend (docker compose ps backend)",
        )
    try:
        data = response.json()
    except ValueError:
        return Check("database", False, f"GET {url} trả về không phải JSON", "xem log backend")
    db_ok = bool(data.get("db_connected"))
    detail = (
        f"HTTP {response.status_code} status={data.get('status')} "
        f"db_connected={db_ok} mqtt_connected={data.get('mqtt_connected')}"
    )
    if not db_ok or response.status_code != 200:
        return Check("database", False, detail, "kiểm tra postgres và DATABASE_URL trong .env")
    return Check("database", True, detail)


# ---- một chu trình cổng qua MQTT ----
def check_cycle(args: argparse.Namespace) -> Check:
    cmd_topic = f"{args.prefix}/gate/in/cmd"
    events: queue.Queue[tuple[str, float]] = queue.Queue()
    connected = threading.Event()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="preflight-cycle")

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
        barrier = payload.get("barrier") if isinstance(payload, dict) else None
        if barrier in ("open", "close"):
            events.put((barrier, time.perf_counter()))

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(args.host, args.port, keepalive=15)
    except OSError as exc:
        return Check(
            "simulator",
            False,
            f"không nối được MQTT {args.host}:{args.port}: {exc}",
            "docker compose up -d mosquitto",
        )

    client.loop_start()

    def wait_for(kind: str, timeout: float) -> float | None:
        deadline = time.perf_counter() + timeout
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return None
            try:
                got_kind, ts = events.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            if got_kind == kind:
                return ts

    try:
        if not connected.wait(10.0):
            return Check("simulator", False, "không nhận CONNACK sau 10 s", "kiểm tra broker")
        started = time.perf_counter()
        client.publish(
            f"{args.prefix}/gate/in/event",
            json.dumps({"type": "vehicle_detected", "distance_cm": 30.0, "ts": time.time()}),
            qos=1,
        )
        opened = wait_for("open", args.cycle_timeout)
        if opened is None:
            return Check(
                "simulator",
                False,
                f"không nhận gate/in/cmd open sau {args.cycle_timeout:.0f} s",
                "xem log backend; kiểm tra MQTT_PREFIX và luồng cổng",
            )
        # Giả lập ESP32: servo quay xong thì báo ack, rồi báo xe đã qua.
        client.publish(
            f"{args.prefix}/gate/in/ack",
            json.dumps({"type": "barrier_opened", "ts": time.time()}),
            qos=1,
        )
        client.publish(
            f"{args.prefix}/gate/in/passed",
            json.dumps({"type": "vehicle_passed", "ts": time.time()}),
            qos=1,
        )
        closed = wait_for("close", args.close_timeout)
        if closed is None:
            return Check(
                "simulator",
                False,
                f"mở sau {(opened - started) * 1000:.0f} ms nhưng không nhận lệnh close",
                "backend phải đóng barrier sau gate/in/passed",
            )
        return Check(
            "simulator",
            True,
            f"open sau {(opened - started) * 1000:.0f} ms, close sau {(closed - started) * 1000:.0f} ms",
        )
    finally:
        client.loop_stop()
        client.disconnect()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Kiểm tra trước demo: container healthy, camera, 3 thiết bị online, DB, "
            "một chu trình cổng qua MQTT."
        ),
        epilog="Thoát mã 1 nếu có mục FAIL; 0 khi tất cả PASS.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--services",
        default=",".join(SERVICES),
        help="danh sách dịch vụ compose, phân tách bằng dấu phẩy",
    )
    parser.add_argument("--directory", default=".", help="thư mục chứa docker-compose.yml")
    parser.add_argument("--compose-cmd", default=None, help="lệnh compose (mặc định tự dò)")
    parser.add_argument("--docker", default="docker", help="lệnh docker dùng để inspect")
    parser.add_argument("--url", default="http://localhost:8000", help="địa chỉ backend")
    parser.add_argument("--capture-url", default="http://localhost:8002", help="địa chỉ capture-agent")
    parser.add_argument("--host", default="localhost", help="địa chỉ Mosquitto")
    parser.add_argument("--port", type=int, default=1883, help="cổng Mosquitto")
    parser.add_argument("--prefix", default="parking", help="tiền tố topic theo docs/CONTRACT.md")
    parser.add_argument("--timeout", type=float, default=5.0, help="timeout gọi HTTP (giây)")
    parser.add_argument(
        "--retained-wait",
        type=float,
        default=3.0,
        help="thời gian chờ bản tin status retained (giây)",
    )
    parser.add_argument(
        "--cycle-timeout",
        type=float,
        default=30.0,
        help="thời gian chờ lệnh mở barrier của chu trình thử (giây)",
    )
    parser.add_argument(
        "--close-timeout",
        type=float,
        default=15.0,
        help="thời gian chờ lệnh đóng barrier sau khi gửi passed (giây)",
    )
    args = parser.parse_args(argv)
    for name in ("timeout", "retained_wait", "cycle_timeout", "close_timeout"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} phải > 0")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.services = [name.strip() for name in args.services.split(",") if name.strip()]
    args.compose_cmd = detect_compose(args.compose_cmd)
    args.docker_cmd = shlex.split(args.docker)

    print("=== PREFLIGHT DEMO ===")
    print(f"MQTT {args.host}:{args.port} | backend {args.url} | capture {args.capture_url}\n")

    checks = [
        ("containers", check_containers),
        ("capture-agent", check_capture),
        ("devices", check_devices),
        ("database", check_database),
        ("simulator", check_cycle),
    ]
    results: list[Check] = []
    for name, func in checks:
        try:
            result = func(args)
        except Exception as exc:  # một mục lỗi không được làm sập cả preflight
            result = Check(name, False, f"lỗi không mong đợi: {exc}")
        results.append(result)
        print(f"[{'PASS' if result.ok else 'FAIL'}] {result.name}: {result.detail}")
        if not result.ok and result.hint:
            print(f"        gợi ý: {result.hint}")

    failed = [result for result in results if not result.ok]
    status = "PASS" if not failed else "FAIL"
    print(f"\n=== {status}: {len(results) - len(failed)}/{len(results)} mục đạt ===")
    if failed:
        print("Chưa sẵn sàng demo: " + ", ".join(result.name for result in failed))
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
