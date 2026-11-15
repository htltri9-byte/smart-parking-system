"""Cấu hình ALPR - đọc từ biến môi trường (AGENTS.md: không hardcode)."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(ValueError):
    """Cấu hình sai (biến môi trường không hợp lệ) - làm app dừng ngay khi khởi động."""


def _env_str(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} phải là số nguyên, nhận được {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} phải là số thực, nhận được {raw!r}") from exc


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def parse_roi(text: str) -> tuple[float, float, float, float] | None:
    """Đọc ROI="x1,y1,x2,y2" theo tỉ lệ 0-1. Rỗng -> None (dùng toàn bộ ảnh)."""
    value = text.strip()
    if not value:
        return None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 4:
        raise ConfigError(f'ROI phải có 4 số "x1,y1,x2,y2", nhận được {text!r}')
    try:
        x1, y1, x2, y2 = (float(p) for p in parts)
    except ValueError as exc:
        raise ConfigError(f"ROI phải là 4 số thực trong khoảng 0-1, nhận được {text!r}") from exc
    if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
        raise ConfigError(f"ROI phải nằm trong khoảng 0-1, nhận được {text!r}")
    if x1 >= x2 or y1 >= y2:
        raise ConfigError(f"ROI phải có x1 < x2 và y1 < y2, nhận được {text!r}")
    return (x1, y1, x2, y2)


@dataclass(frozen=True)
class Settings:
    ocr_engine: str = "rapidocr"
    ocr_use_cls: bool = False  # classifier xoay ảnh của RapidOCR: biển số luôn thẳng nên tắt cho nhanh
    ocr_threads: int = 4
    detector: str = "roi"
    roi: tuple[float, float, float, float] | None = None
    yolo_weights: str = ""
    yolo_conf: float = 0.25
    min_plate_width: int = 480
    port: int = 8001

    def describe(self) -> str:
        roi = "-" if self.roi is None else ",".join(f"{v:g}" for v in self.roi)
        return (
            f"ocr={self.ocr_engine} cls={self.ocr_use_cls} threads={self.ocr_threads} "
            f"detector={self.detector} roi={roi}"
        )


def load_settings() -> Settings:
    ocr_engine = _env_str("OCR_ENGINE", "rapidocr").lower()
    if ocr_engine not in {"rapidocr", "easyocr"}:
        raise ConfigError(f"OCR_ENGINE chỉ nhận 'rapidocr' hoặc 'easyocr', nhận được {ocr_engine!r}")

    detector = _env_str("PLATE_DETECTOR", "roi").lower()
    if detector not in {"roi", "yolo"}:
        raise ConfigError(f"PLATE_DETECTOR chỉ nhận 'roi' hoặc 'yolo', nhận được {detector!r}")

    return Settings(
        ocr_engine=ocr_engine,
        # Mặc định 4 luồng: đo thực tế nhanh hơn cả 1, 2 và 8 luồng khi chạy trong Docker Desktop.
        ocr_threads=max(1, _env_int("OCR_THREADS", min(4, os.cpu_count() or 4))),
        ocr_use_cls=_env_flag("OCR_USE_CLS", False),
        detector=detector,
        roi=parse_roi(_env_str("ROI", "")),
        yolo_weights=_env_str("YOLO_WEIGHTS", ""),
        yolo_conf=_env_float("YOLO_CONF", 0.25),
        min_plate_width=_env_int("MIN_PLATE_WIDTH", 480),
        port=_env_int("ALPR_PORT", 8001),
    )
