import logging
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("ws")


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
            except Exception:  # client đã đóng kết nối
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
