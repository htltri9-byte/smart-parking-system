import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from .config import settings
from .db import SessionLocal, init_db
from .models import Event, ParkingSession, Slot
from .mqtt_client import bridge, iso, slot_to_dict
from .ws import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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


@app.get("/api/health")
def health():
    return {"status": "ok", "mqtt_connected": bridge.connected}


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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # giữ kết nối; client không cần gửi gì
    except WebSocketDisconnect:
        manager.disconnect(ws)
