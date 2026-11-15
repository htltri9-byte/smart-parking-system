"""Lớp OCR đổi engine được: rapidocr (mặc định) hoặc easyocr."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .config import ConfigError, Settings

log = logging.getLogger("alpr.ocr")


@dataclass(frozen=True)
class TextLine:
    """Một đoạn chữ OCR đọc được, kèm hộp bao để ghép dòng."""

    text: str
    confidence: float
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center_y(self) -> float:
        return (self.y1 + self.y2) / 2.0

    @property
    def center_x(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def height(self) -> int:
        return max(1, self.y2 - self.y1)


class OcrEngine(Protocol):
    name: str

    def read(self, image: np.ndarray) -> list[TextLine]: ...


class RapidOcrEngine:
    """rapidocr-onnxruntime: PP-OCR (PaddleOCR) chạy bằng ONNX, không cần torch."""

    name = "rapidocr"

    def __init__(self, use_cls: bool = False, threads: int = 4) -> None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415 - import nặng
        except ImportError as exc:
            raise ConfigError(
                "Thiếu gói 'rapidocr-onnxruntime'. Cài: pip install rapidocr-onnxruntime"
            ) from exc
        # Nạp model một lần, dùng lại cho mọi request.
        # use_cls=False: bỏ model phân loại hướng xoay (biển số luôn thẳng) -> nhanh hơn ~25%.
        self._engine = RapidOCR(use_cls=use_cls, intra_op_num_threads=max(1, threads))

    def read(self, image: np.ndarray) -> list[TextLine]:
        result, _elapse = self._engine(image)
        if not result:
            return []
        lines: list[TextLine] = []
        for item in result:
            box, text, score = item[0], item[1], item[2]
            xs = [int(point[0]) for point in box]
            ys = [int(point[1]) for point in box]
            lines.append(
                TextLine(
                    text=str(text).strip(),
                    confidence=float(score),
                    x1=min(xs),
                    y1=min(ys),
                    x2=max(xs),
                    y2=max(ys),
                )
            )
        return lines


class EasyOcrEngine:
    """easyocr (thay thế, nặng hơn vì cần torch)."""

    name = "easyocr"

    def __init__(self) -> None:
        try:
            import easyocr  # noqa: PLC0415 - import nặng
        except ImportError as exc:
            raise ConfigError("Thiếu gói 'easyocr'. Cài: pip install easyocr") from exc
        self._reader = easyocr.Reader(["en"], gpu=False, verbose=False)

    def read(self, image: np.ndarray) -> list[TextLine]:
        lines: list[TextLine] = []
        for box, text, score in self._reader.readtext(image):
            xs = [int(point[0]) for point in box]
            ys = [int(point[1]) for point in box]
            lines.append(
                TextLine(
                    text=str(text).strip(),
                    confidence=float(score),
                    x1=min(xs),
                    y1=min(ys),
                    x2=max(xs),
                    y2=max(ys),
                )
            )
        return lines


def build_ocr(settings: Settings) -> OcrEngine:
    if settings.ocr_engine == "rapidocr":
        return RapidOcrEngine(use_cls=settings.ocr_use_cls, threads=settings.ocr_threads)
    if settings.ocr_engine == "easyocr":
        return EasyOcrEngine()
    raise ConfigError(f"OCR engine không hỗ trợ: {settings.ocr_engine!r}")


def merge_lines(lines: list[TextLine]) -> tuple[str, float]:
    """Ghép các đoạn chữ thành 1 chuỗi.

    Biển 1 dòng: nối theo trục x. Biển 2 dòng (xe máy): dòng trên trước, dòng dưới sau
    theo toạ độ y của đoạn chữ.
    """
    if not lines:
        return "", 0.0

    average_height = sum(line.height for line in lines) / len(lines)
    rows: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda item: item.center_y):
        if rows and abs(line.center_y - rows[-1][-1].center_y) <= 0.6 * average_height:
            rows[-1].append(line)
        else:
            rows.append([line])

    ordered: list[TextLine] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda item: item.center_x))

    text = "".join(line.text for line in ordered)
    confidence = sum(line.confidence for line in ordered) / len(ordered)
    return text, confidence
