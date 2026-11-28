"""API ALPR theo docs/CONTRACT.md - POST /recognize (multipart, trường "image")."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .detector import Detector, Region, build_detector
from .ocr import OcrEngine, build_ocr, merge_lines
from .plate import format_plate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alpr")


@dataclass(frozen=True)
class Result:
    plate: str | None
    confidence: float
    valid: bool
    ms: int
    raw: str


def _clahe(image: np.ndarray) -> np.ndarray:
    """Tăng tương phản cục bộ trên kênh sáng (LAB) - dùng cho ảnh thiếu sáng/ngược sáng."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    equalized = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lightness)
    return cv2.cvtColor(cv2.merge((equalized, a_channel, b_channel)), cv2.COLOR_LAB2BGR)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.detector: Detector = build_detector(settings)
        self.ocr: OcrEngine = build_ocr(settings)

    def warmup(self) -> None:
        """Nạp model + chạy thử ảnh trắng để lần gọi đầu không bị chậm."""
        started = time.perf_counter()
        white = np.full((720, 1280, 3), 255, dtype=np.uint8)
        self.recognize(white)
        log.info("Warmup xong sau %d ms", int((time.perf_counter() - started) * 1000))

    def _upscale(self, region: Region) -> np.ndarray:
        image = region.image
        width = image.shape[1]
        min_width = self.settings.min_plate_width
        if width >= min_width or width == 0:
            return image
        scale = min(4.0, min_width / width)
        return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    def recognize(self, image: np.ndarray) -> Result:
        started = time.perf_counter()
        raw_parts: list[str] = []
        best_confidence = 0.0

        # Bản gốc trước; không ra biển hợp lệ thì thử bản tăng tương phản CLAHE.
        variants = [("goc", image), ("clahe", _clahe(image))]
        for label, variant in variants:
            for region in self.detector.detect(variant):
                crop = self._upscale(region)
                lines = self.ocr.read(crop)
                text, confidence = merge_lines(lines)
                best_confidence = max(best_confidence, confidence)
                if not text:
                    continue
                raw_parts.append(f"{label}:{text}")
                plate, valid = format_plate(text)
                if valid:
                    return Result(
                        plate=plate,
                        confidence=round(confidence, 3),
                        valid=True,
                        ms=int((time.perf_counter() - started) * 1000),
                        raw=" | ".join(raw_parts),
                    )

        return Result(
            plate=None,
            confidence=round(best_confidence, 3),
            valid=False,
            ms=int((time.perf_counter() - started) * 1000),
            raw=" | ".join(raw_parts),
        )


settings = load_settings()
pipeline: Pipeline | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline
    log.info("ALPR khởi động: %s", settings.describe())
    pipeline = Pipeline(settings)
    pipeline.warmup()
    yield
    pipeline = None


app = FastAPI(title="Smart Parking ALPR", lifespan=lifespan)


def get_pipeline() -> Pipeline:
    if pipeline is None:  # chưa startup xong
        raise HTTPException(status_code=503, detail="ALPR đang khởi động")
    return pipeline


@app.get("/health")
def health() -> JSONResponse:
    """200 khi pipeline đã nạp model xong; 503 trong lúc warmup (dùng cho healthcheck)."""
    ready = pipeline is not None
    return JSONResponse(
        {"status": "ok" if ready else "error", "ready": ready},
        status_code=200 if ready else 503,
    )


@app.post("/recognize")
async def recognize(image: UploadFile = File(...), debug: bool = Query(False)) -> dict[str, object]:
    data = await image.read()
    if not data:
        raise HTTPException(status_code=400, detail="file ảnh rỗng")

    frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise HTTPException(status_code=400, detail="không giải mã được ảnh (cần JPEG/PNG)")

    result = get_pipeline().recognize(frame)
    body: dict[str, object] = {
        "plate": result.plate,
        "confidence": result.confidence,
        "valid": result.valid,
        "ms": result.ms,
    }
    if debug:
        body["raw"] = result.raw
    return body
