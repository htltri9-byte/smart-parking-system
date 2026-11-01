"""Giả lập 3 ESP32 (cổng vào, cổng ra, khu đỗ) để chạy backend khi chưa có phần cứng.

Chạy:  python sim_esp32.py --host localhost --interval 2
Gửi: trạng thái ô đỗ ngẫu nhiên, sự kiện xe qua cổng vào/ra. In ra lệnh nhận từ backend.
"""
import argparse
import json
import random
import threading
import time

import paho.mqtt.client as mqtt

PREFIX = "parking"
NUM_SLOTS = 4


def pub(client, topic, payload, retain=False):
    data = payload if isinstance(payload, str) else json.dumps(payload)
    client.publish(f"{PREFIX}/{topic}", data, qos=1, retain=retain)


def gate_cycle(client, gate: str, hold_s: float = 2.0):
    pub(client, f"gate/{gate}/event",
        {"type": "vehicle_detected", "distance_cm": round(random.uniform(15, 40), 1), "ts": time.time()})
    time.sleep(hold_s)
    pub(client, f"gate/{gate}/passed", {"type": "vehicle_passed", "ts": time.time()})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--interval", type=float, default=2.0, help="giây giữa các lần đổi trạng thái")
    ap.add_argument("--count", type=int, default=0, help="số vòng (0 = chạy mãi)")
    args = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sim-esp32")

    def on_message(c, u, msg):
        print(f"[lệnh nhận] {msg.topic}: {msg.payload.decode()}")

    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=30)
    client.subscribe(f"{PREFIX}/gate/+/cmd", qos=1)
    client.loop_start()

    for dev in ("gate_in", "gate_out", "slots"):
        pub(client, f"device/{dev}/status", "online", retain=True)

    occupied = [False] * NUM_SLOTS
    n = 0
    try:
        while args.count == 0 or n < args.count:
            i = random.randrange(NUM_SLOTS)
            occupied[i] = not occupied[i]
            dist = round(random.uniform(3, 12), 1) if occupied[i] else round(random.uniform(25, 60), 1)
            pub(client, f"slot/{i + 1}/state",
                {"slot": i + 1, "occupied": occupied[i], "distance_cm": dist, "ts": time.time()})
            print(f"ô {i + 1}: {'có xe' if occupied[i] else 'trống'}")
            if n % 3 == 0:
                gate = random.choice(["in", "out"])
                threading.Thread(target=gate_cycle, args=(client, gate), daemon=True).start()
                print(f"cổng {gate}: xe tới")
            n += 1
            time.sleep(args.interval)
        time.sleep(3)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
