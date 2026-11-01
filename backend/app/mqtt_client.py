"""Cầu nối MQTT: nhận bản tin theo docs/CONTRACT.md, lưu CSDL và đẩy qua WebSocket."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .config import settings
from .db import SessionLocal
from .models import Event, Slot, utcnow
from .ws import manager

log = logging.getLogger("mqtt")


def iso(dt: datetime | None) -> str | None:
    """ISO 8601 luôn kèm múi giờ (SQLite trả datetime không có tzinfo, coi là UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def slot_to_dict(slot: Slot) -> dict[str, Any]:
    return {
        "id": slot.id,
        "occupied": slot.occupied,
        "distance_cm": slot.distance_cm,
        "updated_at": iso(slot.updated_at),
    }


def _ts_from_payload(payload: dict[str, Any]) -> datetime:
    ts = payload.get("ts")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return utcnow()


class MqttBridge:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="parking-backend")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        # connect_async để backend vẫn khởi động được khi broker chưa sẵn sàng
        self.client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def publish(self, topic: str, payload: dict[str, Any] | str, retain: bool = False) -> None:
        data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.client.publish(f"{settings.mqtt_prefix}/{topic}", data, qos=1, retain=retain)

    # ---- callbacks (chạy trong luồng của paho) ----
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            self.connected = True
            client.subscribe(f"{settings.mqtt_prefix}/#", qos=1)
            log.info("Đã kết nối MQTT %s:%s", settings.mqtt_host, settings.mqtt_port)
        else:
            log.error("Kết nối MQTT thất bại: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = False
        log.warning("Mất kết nối MQTT (%s), sẽ tự kết nối lại", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self._handle(msg.topic, msg.payload)
        except Exception:
            log.exception("Lỗi xử lý bản tin %s", msg.topic)

    # ---- xử lý ----
    def _handle(self, topic: str, raw: bytes) -> None:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except json.JSONDecodeError:
            payload = {"value": text}  # ví dụ device/<id>/status: online/offline

        parts = topic.split("/")  # [prefix, kind, ...]
        slot_data: dict[str, Any] | None = None

        with SessionLocal() as db:
            db.add(Event(ts=_ts_from_payload(payload), topic=topic, payload=payload))
            if len(parts) == 4 and parts[1] == "slot" and parts[3] == "state":
                slot_data = self._upsert_slot(db, parts[2], payload)
            db.commit()

        if slot_data is not None:
            self._broadcast({"type": "slot_update", "slot": slot_data})
        self._broadcast({"type": "event", "topic": topic, "payload": payload})

    def _upsert_slot(self, db, slot_id_text: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            slot_id = int(slot_id_text)
        except ValueError:
            log.warning("Số ô đỗ không hợp lệ: %s", slot_id_text)
            return None
        if not 1 <= slot_id <= settings.num_slots or not isinstance(payload.get("occupied"), bool):
            log.warning("Bản tin ô đỗ không đúng hợp đồng: ô %s, %s", slot_id, payload)
            return None
        slot = db.get(Slot, slot_id) or Slot(id=slot_id)
        slot.occupied = payload["occupied"]
        distance = payload.get("distance_cm")
        slot.distance_cm = float(distance) if isinstance(distance, (int, float)) else None
        slot.updated_at = utcnow()
        db.add(slot)
        db.flush()
        return slot_to_dict(slot)

    def _broadcast(self, message: dict[str, Any]) -> None:
        if self.loop is not None and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(manager.broadcast(message), self.loop)


bridge = MqttBridge()
