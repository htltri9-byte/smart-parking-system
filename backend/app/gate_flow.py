"""Luồng cổng (giai đoạn 4).

Nhận sự kiện cảm biến qua MQTT (đã được bridge chuyển vào hàng đợi), gọi
capture-agent để lấy ảnh, gửi ALPR nhận diện biển, mở/đóng barrier và ghi
phiên gửi xe (`sessions`, `plate_reads`) theo docs/CONTRACT.md.

Chạy trong một luồng nền riêng để không chặn callback MQTT (mỗi lần
capture + ALPR có thể mất vài trăm ms).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from .config import settings
from .db import SessionLocal
from .models import ParkingSession, PlateRead, Slot

log = logging.getLogger("gate")


def _ts_from_payload(payload: dict[str, Any]) -> datetime:
    """Lấy ts (Unix epoch) từ payload; sai/thiếu thì dùng thời gian hiện tại."""
    ts = payload.get("ts")
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _http_get(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (URL nội bộ)
        return resp.read()


def _post_image(url: str, image: bytes, timeout: float) -> dict[str, Any]:
    """POST multipart/form-data trường `image` (giống hợp đồng ALPR)."""
    boundary = "----parking" + uuid.uuid4().hex
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="image"; filename="capture.jpg"\r\n',
            b"Content-Type: image/jpeg\r\n\r\n",
            image,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (URL nội bộ)
        return json.loads(resp.read().decode("utf-8"))


class GateFlow:
    """Xử lý sự kiện cổng vào/ra trong luồng nền."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self._queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_event: dict[str, float] = {}
        self._close_timers: dict[str, threading.Timer] = {}

    # ---- vòng đời ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, name="gate-flow", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._queue.put(("", {}))
        for timer in self._close_timers.values():
            timer.cancel()

    def submit(self, topic: str, payload: dict[str, Any]) -> None:
        """Chỉ nhận topic sự kiện của cổng: parking/gate/<in|out>/{event,passed}."""
        parts = topic.split("/")
        if len(parts) == 4 and parts[1] == "gate" and parts[3] in ("event", "passed"):
            self._queue.put((topic, payload))

    def _worker(self) -> None:
        while self._running:
            topic, payload = self._queue.get()
            if not self._running:
                break
            try:
                self._dispatch(topic, payload)
            except Exception:  # luồng nền không được chết vì một bản tin lỗi
                log.exception("Lỗi xử lý luồng cổng cho %s", topic)

    def _dispatch(self, topic: str, payload: dict[str, Any]) -> None:
        parts = topic.split("/")
        direction = parts[2]  # in | out
        kind = parts[3]  # event | passed
        if kind == "event":
            self._on_vehicle(direction, payload)
        elif kind == "passed":
            self._on_passed(direction)

    # ---- sự kiện ----
    def _on_vehicle(self, direction: str, payload: dict[str, Any]) -> None:
        now = time.time()
        if now - self._last_event.get(direction, 0.0) < settings.gate_cooldown_seconds:
            log.info("Bỏ qua gate/%s/event (trong thời gian chống trùng)", direction)
            return
        self._last_event[direction] = now

        ts = _ts_from_payload(payload)
        image = self._capture()
        plate: str | None = None
        confidence: float | None = None
        valid = False
        ms: int | None = None

        if image is not None:
            result = self._recognize(image)
            if result is not None:
                plate = result.get("plate")
                confidence = result.get("confidence")
                valid = bool(result.get("valid"))
                ms = result.get("ms")
                log.info(
                    "ALPR cổng %s: plate=%s valid=%s conf=%s ms=%s",
                    direction,
                    plate,
                    valid,
                    confidence,
                    ms,
                )
        else:
            log.warning("Không lấy được ảnh từ capture-agent (%s)", settings.capture_url)

        if direction == "in":
            self._entry(plate, confidence, valid, ms, ts, image)
        else:
            self._exit(plate, confidence, valid, ms, ts, image)

    def _on_passed(self, direction: str) -> None:
        self._cancel_close(direction)
        self.bridge.publish(f"gate/{direction}/cmd", {"barrier": "close"})
        log.info("Cổng %s: xe đã qua, đóng barrier", direction)

    # ---- vào ----
    def _entry(
        self,
        plate: str | None,
        confidence: float | None,
        valid: bool,
        ms: int | None,
        ts: datetime,
        image: bytes | None,
    ) -> None:
        readable = bool(plate) and valid
        lcd: list[str]
        if readable:
            image_path = self._save_image(image, plate, ts, "entry")
            with SessionLocal() as db:
                free_before = self._free_slots(db)
                session = ParkingSession(
                    plate=plate,
                    entry_time=ts,
                    status="inside",
                    confidence=confidence,
                    entry_image=image_path,
                )
                db.add(session)
                db.flush()
                db.add(
                    PlateRead(
                        session_id=session.id,
                        plate_text=plate or "",
                        confidence=confidence,
                        ms=ms,
                        image_path=image_path,
                    )
                )
                db.commit()
            lcd = [plate or "", f"Con trong: {free_before}"]
            log.info("Tạo phiên vào cho %s, còn %d ô trống", plate, free_before)
        else:
            lcd = ["Khong ro bien", "Lien he bao ve"]
            log.warning("Không đọc được biển hợp lệ ở cổng vào; mở barrier thủ công")

        self._open(direction="in", lcd=lcd)

    # ---- ra ----
    def _exit(
        self,
        plate: str | None,
        confidence: float | None,
        valid: bool,
        ms: int | None,
        ts: datetime,
        image: bytes | None,
    ) -> None:
        readable = bool(plate) and valid
        image_path = self._save_image(image, plate, ts, "exit") if readable else None

        if readable:
            with SessionLocal() as db:
                session = db.scalars(
                    select(ParkingSession)
                    .where(ParkingSession.plate == plate, ParkingSession.status == "inside")
                    .order_by(ParkingSession.id.desc())
                    .limit(1)
                ).first()
                if session is not None:
                    session.exit_time = ts
                    session.status = "exited"
                    db.add(session)
                db.add(
                    PlateRead(
                        session_id=session.id if session is not None else None,
                        plate_text=plate or "",
                        confidence=confidence,
                        ms=ms,
                        image_path=image_path,
                    )
                )
                db.commit()
            if session is not None:
                lcd = [plate or "", "Tam biet"]
                log.info("Đóng phiên %s của %s", session.id, plate)
            else:
                lcd = [plate or "", "Khong co phien"]
                log.warning("Không tìm thấy phiên đang gửi cho biển %s", plate)
        else:
            lcd = ["Khong ro bien", "Lien he bao ve"]
            log.warning("Không đọc được biển hợp lệ ở cổng ra; mở barrier thủ công")

        self._open(direction="out", lcd=lcd)

    # ---- barrier ----
    def _open(self, direction: str, lcd: list[str]) -> None:
        self.bridge.publish(f"gate/{direction}/cmd", {"barrier": "open", "lcd": lcd})
        self._cancel_close(direction)
        timer = threading.Timer(
            settings.gate_open_seconds,
            self._auto_close,
            args=(direction,),
        )
        timer.daemon = True
        timer.start()
        self._close_timers[direction] = timer

    def _auto_close(self, direction: str) -> None:
        # Dự phòng khi cảm biến "passed" không báo: tự đóng sau GATE_OPEN_SECONDS.
        self._close_timers.pop(direction, None)
        self.bridge.publish(f"gate/{direction}/cmd", {"barrier": "close"})
        log.info("Cổng %s: tự đóng barrier sau %.0fs", direction, settings.gate_open_seconds)

    def _cancel_close(self, direction: str) -> None:
        timer = self._close_timers.pop(direction, None)
        if timer is not None:
            timer.cancel()

    # ---- tiện ích ----
    @staticmethod
    def _free_slots(db) -> int:
        occupied = db.scalar(
            select(func.count()).select_from(Slot).where(Slot.occupied.is_(True))
        )
        return max(settings.num_slots - int(occupied or 0), 0)

    def _capture(self) -> bytes | None:
        try:
            return _http_get(settings.capture_url, settings.http_timeout)
        except Exception as exc:  # capture-agent tắt / chưa có khung hình
            log.warning("Gọi capture-agent lỗi: %s", exc)
            return None

    def _recognize(self, image: bytes) -> dict[str, Any] | None:
        try:
            return _post_image(settings.alpr_url, image, settings.http_timeout)
        except Exception as exc:
            log.warning("Gọi ALPR lỗi: %s", exc)
            return None

    @staticmethod
    def _save_image(image: bytes | None, plate: str | None, ts: datetime, kind: str) -> str | None:
        if image is None:
            return None
        try:
            os.makedirs(settings.image_dir, exist_ok=True)
            safe_plate = (plate or "unknown").replace("/", "_")
            name = f"{kind}_{safe_plate}_{ts.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.jpg"
            path = os.path.join(settings.image_dir, name)
            with open(path, "wb") as fh:
                fh.write(image)
            return path
        except OSError:
            log.exception("Không lưu được ảnh %s", kind)
            return None

