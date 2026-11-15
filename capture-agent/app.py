"""capture-agent - đọc camera trên MÁY HOST và trả ảnh JPEG cho backend.

Chạy ngoài Docker (xem README.md). Backend trong container gọi
http://host.docker.internal:8002/capture.

Endpoints:
    GET /capture  -> ảnh JPEG của khung hình mới nhất (503 nếu chưa có hoặc cũ hơn 5s)
    GET /health   -> {"camera_ok": bool, "frame_age_s": ...}

Biến môi trường:
    CAMERA_SOURCE    số thứ tự camera (0), URL luồng (http://IP:8080/video), hoặc file video
    CAMERA_WIDTH     mặc định 1280
    CAMERA_HEIGHT    mặc định 720
    CAPTURE_PORT     mặc định 8002
    JPEG_QUALITY     mặc định 85
    STALE_AFTER_S    tuổi tối đa của khung hình, mặc định 5 (giây)
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import cv2
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

log = logging.getLogger("capture")

# Số giây chờ trước khi thử mở lại nguồn ảnh sau khi lỗi (rút camera, mất mạng...).
RETRY_DELAY_S = 2.0
# Số khung hình bỏ đi ngay sau khi mở camera (nhiều webcam trả khung đen/thiếu sáng).
WARMUP_FRAMES = 3
# FPS giả định cho file video không khai báo FPS.
DEFAULT_FILE_FPS = 25.0


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        log.warning("Giá trị %s không hợp lệ, dùng mặc định %s", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        log.warning("Giá trị %s không hợp lệ, dùng mặc định %s", name, default)
        return default


@dataclass(frozen=True)
class Settings:
    source: str
    width: int
    height: int
    port: int
    jpeg_quality: int
    stale_after_s: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            source=os.getenv("CAMERA_SOURCE", "0").strip(),
            width=_env_int("CAMERA_WIDTH", 1280),
            height=_env_int("CAMERA_HEIGHT", 720),
            port=_env_int("CAPTURE_PORT", 8002),
            jpeg_quality=_env_int("JPEG_QUALITY", 85),
            stale_after_s=_env_float("STALE_AFTER_S", 5.0),
        )


settings = Settings.from_env()


def source_kind(source: str) -> str:
    """Phân loại CAMERA_SOURCE: camera (số thứ tự), stream (URL), file (đường dẫn)."""
    if source.isdigit():
        return "camera"
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "rtsp://", "rtmp://")):
        return "stream"
    return "file"


class FrameReader:
    """Luồng nền đọc camera liên tục, chỉ giữ khung hình mới nhất trong RAM."""

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._kind = source_kind(config.source)
        self._lock = threading.Lock()
        self._frame = None  # numpy.ndarray BGR của khung hình mới nhất
        self._frame_ts: float | None = None  # time.monotonic() khi nhận khung
        self._frame_size: tuple[int, int] | None = None
        self._last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cap: cv2.VideoCapture | None = None
        self._file_fps = DEFAULT_FILE_FPS

    # ---- vòng đời ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="frame-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._release()

    # ---- đọc ----
    def latest(self) -> tuple[object | None, float | None]:
        """Trả (khung hình, tuổi tính bằng giây) - khung hình là None nếu chưa có."""
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
            ts = self._frame_ts
        age = None if ts is None else max(0.0, time.monotonic() - ts)
        return frame, age

    def status(self) -> dict[str, object]:
        frame, age = self.latest()
        with self._lock:
            return {
                "camera_ok": frame is not None and age is not None and age <= self._config.stale_after_s,
                "frame_age_s": None if age is None else round(age, 3),
                "source": self._config.source,
                "source_kind": self._kind,
                "frame_size": None if self._frame_size is None else list(self._frame_size),
                "stale_after_s": self._config.stale_after_s,
                "last_error": self._last_error,
            }

    # ---- luồng nền ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._cap is None and not self._open():
                self._stop.wait(RETRY_DELAY_S)
                continue

            cap = self._cap
            if cap is None:  # bị release ở vòng khác
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                self._set_error("không đọc được khung hình")
                if self._kind == "file" and self._rewind():
                    continue  # file video: quay lại đầu để lặp
                self._release()
                self._stop.wait(RETRY_DELAY_S)
                continue

            with self._lock:
                self._frame = frame
                self._frame_ts = time.monotonic()
                height, width = frame.shape[:2]
                self._frame_size = (width, height)
                self._last_error = None

            if self._kind == "file":
                # Giữ nhịp như video thật, tránh đọc hết file trong 1 giây.
                self._stop.wait(1.0 / self._file_fps)

        self._release()

    def _open(self) -> bool:
        source: object
        if self._kind == "camera":
            source = int(self._config.source)
        else:
            source = self._config.source

        if self._kind == "file" and not os.path.isfile(str(source)):
            self._set_error(f"không thấy file video: {source}")
            log.error("Không thấy file video %s", source)
            return False

        cap = self._open_capture(source)
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            self._set_error(f"không mở được nguồn ảnh: {self._config.source}")
            log.warning("Không mở được nguồn ảnh %s, thử lại sau %.0fs", self._config.source, RETRY_DELAY_S)
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height)
        # Chỉ giữ 1 khung trong buffer của driver -> khung đọc ra luôn là mới nhất.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if self._kind == "file":
            fps = cap.get(cv2.CAP_PROP_FPS)
            self._file_fps = fps if fps and 1.0 <= fps <= 120.0 else DEFAULT_FILE_FPS

        for _ in range(WARMUP_FRAMES if self._kind == "camera" else 0):
            cap.read()
            self._stop.wait(0.05)

        self._cap = cap
        log.info("Đã mở nguồn ảnh %s (%s)", self._config.source, self._kind)
        return True

    def _open_capture(self, source: object) -> cv2.VideoCapture | None:
        if self._kind == "camera" and sys.platform.startswith("win"):
            # Trên Windows phải dùng DirectShow, backend MSMF mở webcam rất chậm/treo.
            cap = cv2.VideoCapture(int(source), cv2.CAP_DSHOW)
            if cap.isOpened():
                return cap
            cap.release()
            log.warning("CAP_DSHOW không mở được camera %s, thử backend mặc định", source)
        return cv2.VideoCapture(source)

    def _rewind(self) -> bool:
        if self._cap is None:
            return False
        return bool(self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0))

    def _release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # pragma: no cover - driver có thể đã chết
                log.debug("Lỗi khi release camera", exc_info=True)
            self._cap = None

    def _set_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message
        log.warning("%s", message)


reader = FrameReader(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "capture-agent: nguồn=%s (%s), %dx%d, cổng %d",
        settings.source,
        source_kind(settings.source),
        settings.width,
        settings.height,
        settings.port,
    )
    reader.start()
    yield
    reader.stop()


app = FastAPI(title="Smart Parking Capture Agent", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, object]:
    status = reader.status()
    status["camera_ok"] = bool(status["camera_ok"])
    return status


@app.get("/capture")
def capture() -> Response:
    frame, age = reader.latest()
    if frame is None or age is None:
        raise HTTPException(status_code=503, detail="chưa có khung hình nào từ camera")
    if age > settings.stale_after_s:
        raise HTTPException(
            status_code=503,
            detail=f"khung hình cũ {age:.1f}s (quá {settings.stale_after_s:.0f}s)",
        )

    ok, buffer = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), settings.jpeg_quality])
    if not ok:
        raise HTTPException(status_code=500, detail="không nén được ảnh JPEG")

    return Response(
        content=buffer.tobytes(),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


if __name__ == "__main__":
    import uvicorn

    # Console Windows mặc định cp1252 -> log tiếng Việt bị lỗi ký tự, ép UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=settings.port)
