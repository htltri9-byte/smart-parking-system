"""Ghi vết độ trễ chia chặng cho mỗi lượt xe qua cổng (giai đoạn 7).

Nguyên tắc đo:
- Dùng ĐỒNG HỒ CỦA BACKEND. Khoảng thời gian giữa các chặng tính bằng
  `time.perf_counter()` (đồng hồ đơn điệu, không bị chỉnh giờ giữa lượt), rồi
  quy ra timestamp tường minh neo tại `t_event_recv` để lưu CSDL. Nhờ vậy các
  cột `*_ms` tính từ timestamp luôn khớp với số đo bằng `perf_counter`.
- KHÔNG dùng `ts` của ESP32 để tính độ trễ (đồng hồ NTP của board lệch đồng hồ
  laptop); giá trị đó chỉ ghi vào `note` để tham khảo.
- Ghi CSDL chạy trong luồng riêng: luồng cổng chỉ đẩy bản ghi vào hàng đợi nên
  không làm chậm việc publish lệnh mở barrier.
"""
from __future__ import annotations

import csv
import io
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import SessionLocal
from .models import GateTrace, utcnow

log = logging.getLogger("trace")

# Thứ tự cột CSV. `alpr_service_ms` là thời gian ALPR tự báo (tham khảo);
# `alpr_ms` là chặng ALPR đo bằng đồng hồ backend.
CSV_COLUMNS = (
    "id",
    "gate",
    "session_id",
    "t_event_recv",
    "t_capture_done",
    "t_alpr_done",
    "t_cmd_published",
    "t_ack_recv",
    "ok",
    "note",
    "alpr_service_ms",
    "capture_ms",
    "alpr_ms",
    "decide_ms",
    "barrier_ms",
    "total_ms",
)


def _iso(dt: datetime | None) -> str | None:
    """ISO 8601 luôn kèm múi giờ (SQLite trả datetime không có tzinfo, coi là UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    """Khoảng thời gian (ms) giữa hai mốc; thiếu mốc nào thì trả None."""
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0, 1)


def _payload_ts(payload: dict[str, Any]) -> float | None:
    ts = payload.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else None


@dataclass
class Trace:
    """Một lượt xe qua cổng, cập nhật dần theo từng chặng."""

    gate: str  # "in" | "out"
    start_mono: float
    start_wall: datetime
    esp32_ts: float | None = None
    session_id: int | None = None
    t_capture_done: datetime | None = None
    t_alpr_done: datetime | None = None
    t_cmd_published: datetime | None = None
    t_ack_recv: datetime | None = None
    alpr_ms: int | None = None
    notes: list[str] = field(default_factory=list)
    id: int | None = None

    def at(self, mono: float | None = None) -> datetime:
        """Quy một mốc `perf_counter` thành timestamp, neo tại lúc bắt đầu lượt."""
        elapsed = (time.perf_counter() if mono is None else mono) - self.start_mono
        return self.start_wall + timedelta(seconds=max(0.0, elapsed))

    # ---- các mốc ----
    def mark_capture(self) -> None:
        self.t_capture_done = self.at()

    def mark_alpr(self, result: dict[str, Any] | None) -> None:
        self.t_alpr_done = self.at()
        if result is None:
            self.notes.append("gọi ALPR lỗi")
            return
        reported = result.get("ms")
        if isinstance(reported, (int, float)):
            self.alpr_ms = int(reported)
        if not result.get("valid") or not result.get("plate"):
            self.notes.append("không đọc được biển hợp lệ")

    def mark_cmd(self) -> None:
        self.t_cmd_published = self.at()

    def mark_ack(self, mono: float | None = None) -> None:
        self.t_ack_recv = self.at(mono)

    def is_ok(self) -> bool:
        """Lượt đo đủ mốc: đã publish lệnh mở VÀ đã nhận ack của barrier."""
        return self.t_cmd_published is not None and self.t_ack_recv is not None


class TraceRecorder:
    """Nhận mốc từ luồng cổng/MQTT, ghi CSDL trong luồng nền riêng."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, Trace | None]] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, Trace] = {}  # cổng -> lượt gần nhất đang chờ ack

    # ---- vòng đời ----
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._worker, name="gate-trace", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._queue.put(("stop", None))

    # ---- API cho luồng cổng (chỉ thao tác RAM, không chạm DB) ----
    def begin(self, gate: str, payload: dict[str, Any]) -> Trace:
        trace = Trace(
            gate=gate,
            start_mono=time.perf_counter(),
            start_wall=utcnow(),
            esp32_ts=_payload_ts(payload),
        )
        with self._lock:
            self._latest[gate] = trace
        return trace

    def finish(self, trace: Trace) -> None:
        """Đẩy bản ghi đã đủ mốc vào hàng đợi; luồng cổng không chờ DB."""
        self._queue.put(("insert", trace))

    def on_ack(self, gate: str, mono: float | None = None) -> bool:
        """Ghép ack với lượt gần nhất của cùng cổng. Trả True nếu có ghép được."""
        with self._lock:
            trace = self._latest.get(gate)
        if trace is None:
            log.warning("Nhận ack cổng %s nhưng chưa có lượt nào để ghép", gate)
            return False
        if trace.t_ack_recv is not None:
            log.debug("Bỏ qua ack trùng cho cổng %s (lượt đã có ack)", gate)
            return False
        trace.mark_ack(mono)
        self._queue.put(("ack", trace))
        return True

    # ---- luồng nền ----
    def _worker(self) -> None:
        while self._running:
            kind, trace = self._queue.get()
            if kind == "stop":
                break
            if trace is None:
                continue
            try:
                if kind == "insert":
                    self._insert(trace)
                elif kind == "ack":
                    self._update_ack(trace)
            except Exception:  # luồng ghi vết không được chết vì một dòng lỗi
                log.exception("Ghi vết cổng %s lỗi ở bước %s", trace.gate, kind)

    def _insert(self, trace: Trace) -> None:
        note = "; ".join(trace.notes) or None
        if trace.esp32_ts is not None:
            reference = f"esp32_ts={trace.esp32_ts:.3f} (tham khảo, không dùng để tính)"
            note = f"{note}; {reference}" if note else reference

        row = GateTrace(
            gate=trace.gate,
            session_id=trace.session_id,
            t_event_recv=trace.start_wall,
            t_capture_done=trace.t_capture_done,
            t_alpr_done=trace.t_alpr_done,
            t_cmd_published=trace.t_cmd_published,
            t_ack_recv=trace.t_ack_recv,
            alpr_ms=trace.alpr_ms,
            ok=trace.is_ok(),
            note=note,
        )
        with SessionLocal() as db:
            db.add(row)
            db.commit()
            trace.id = row.id
        log.info(
            "Ghi vết cổng %s: id=%s session=%s ok=%s",
            trace.gate,
            trace.id,
            trace.session_id,
            trace.is_ok(),
        )

    def _update_ack(self, trace: Trace) -> None:
        if trace.id is None:  # dòng chưa kịp ghi (DB lỗi) - bỏ qua
            log.warning("Không có id vết cho cổng %s để cập nhật ack", trace.gate)
            return
        with SessionLocal() as db:
            row = db.get(GateTrace, trace.id)
            if row is None:
                log.warning("Không thấy dòng vết id=%s để cập nhật ack", trace.id)
                return
            row.t_ack_recv = trace.t_ack_recv
            row.ok = trace.is_ok()
            db.commit()
        log.info("Đã ghép ack cho vết id=%s (cổng %s)", trace.id, trace.gate)


recorder = TraceRecorder()


# ---- chuyển dòng CSDL -> JSON/CSV ----
def trace_to_dict(row: GateTrace) -> dict[str, Any]:
    """Một dòng `gate_traces` kèm các chặng tính sẵn bằng timestamp."""
    return {
        "id": row.id,
        "gate": row.gate,
        "session_id": row.session_id,
        "t_event_recv": _iso(row.t_event_recv),
        "t_capture_done": _iso(row.t_capture_done),
        "t_alpr_done": _iso(row.t_alpr_done),
        "t_cmd_published": _iso(row.t_cmd_published),
        "t_ack_recv": _iso(row.t_ack_recv),
        "ok": bool(row.ok),
        "note": row.note,
        "alpr_service_ms": row.alpr_ms,
        "capture_ms": _ms(row.t_event_recv, row.t_capture_done),
        "alpr_ms": _ms(row.t_capture_done, row.t_alpr_done),
        "decide_ms": _ms(row.t_alpr_done, row.t_cmd_published),
        "barrier_ms": _ms(row.t_cmd_published, row.t_ack_recv),
        # total_ms: tới mốc xa nhất đã có (ack nếu đã nhận, không thì tới lệnh mở).
        "total_ms": _ms(row.t_event_recv, row.t_ack_recv or row.t_cmd_published),
    }


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return value


def traces_to_csv(rows: list[GateTrace]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        data = trace_to_dict(row)
        writer.writerow([_csv_value(data.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()
