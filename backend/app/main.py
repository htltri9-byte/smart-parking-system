import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from sqlalchemy import select, text

from .config import settings
from .db import SessionLocal, init_db
from .models import Event, GateTrace, ParkingSession, Slot
from .mqtt_client import bridge, iso, slot_to_dict
from .traces import trace_to_dict, traces_to_csv
from .ws import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backend")


def seed_slots() -> None:
    with SessionLocal() as db:
        for i in range(1, settings.num_slots + 1):
            if db.get(Slot, i) is None:
                db.add(Slot(id=i, occupied=False))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_slots()
    bridge.start(asyncio.get_running_loop())
    yield
    bridge.stop()


app = FastAPI(title="Smart Parking Backend", lifespan=lifespan)


def _db_ok() -> bool:
    """Kiểm tra kết nối CSDL - dùng cho /api/health và healthcheck của Docker."""
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # DB tắt, sai mật khẩu, chưa sẵn sàng...
        log.warning("Kiểm tra kết nối DB lỗi: %s", exc)
        return False


@app.get("/api/health")
def health(response: Response):
    """Trả 200 khi MQTT (không bắt buộc) và DB đều ổn; 503 khi DB mất kết nối."""
    db_ok = _db_ok()
    if not db_ok:
        response.status_code = 503
    return {
        "status": "ok" if db_ok else "error",
        "mqtt_connected": bridge.connected,
        "db_connected": db_ok,
    }


@app.get("/api/slots")
def get_slots():
    with SessionLocal() as db:
        slots = db.scalars(select(Slot).order_by(Slot.id)).all()
        return [slot_to_dict(s) for s in slots]


@app.get("/api/events")
def get_events(limit: int = Query(50, ge=1, le=500)):
    with SessionLocal() as db:
        rows = db.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()
        return [
            {"id": e.id, "ts": iso(e.ts), "topic": e.topic, "payload": e.payload}
            for e in rows
        ]


@app.get("/api/sessions")
def get_sessions(limit: int = Query(50, ge=1, le=500)):
    with SessionLocal() as db:
        rows = db.scalars(select(ParkingSession).order_by(ParkingSession.id.desc()).limit(limit)).all()
        return [
            {
                "id": s.id,
                "plate": s.plate,
                "entry_time": iso(s.entry_time),
                "exit_time": iso(s.exit_time),
                "status": s.status,
                "confidence": s.confidence,
            }
            for s in rows
        ]


@app.get("/api/traces")
def get_traces(limit: int = Query(100, ge=1, le=1000)):
    """Độ trễ chia chặng của các lượt xe gần nhất (giai đoạn 7)."""
    with SessionLocal() as db:
        rows = db.scalars(select(GateTrace).order_by(GateTrace.id.desc()).limit(limit)).all()
        return [trace_to_dict(row) for row in rows]


@app.get("/api/traces.csv")
def download_traces(limit: int = Query(100, ge=1, le=1000)):
    """Tải CSV các lượt gần nhất, kèm cột chặng tính sẵn."""
    with SessionLocal() as db:
        rows = db.scalars(select(GateTrace).order_by(GateTrace.id.desc()).limit(limit)).all()
        body = traces_to_csv(list(rows))
    return Response(
        content="\ufeff" + body,  # BOM để Excel mở đúng tiếng Việt
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="gate_traces.csv"'},
    )


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # giữ kết nối; client không cần gửi gì
    except WebSocketDisconnect:
        manager.disconnect(ws)
