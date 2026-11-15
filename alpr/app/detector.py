"""Tìm vùng biển số trong ảnh.

- PLATE_DETECTOR=roi (mặc định): cắt cố định vùng ROI theo tỉ lệ 0-1.
- PLATE_DETECTOR=yolo: YOLOv8 qua ultralytics, trọng số ở YOLO_WEIGHTS (để sẵn, chưa dùng).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .config import ConfigError, Settings


@dataclass(frozen=True)
class Region:
    """Một vùng nghi là biển số, toạ độ tính trên ảnh gốc."""

    image: np.ndarray
    box: tuple[int, int, int, int]  # x1, y1, x2, y2
    source: str  # "roi" | "yolo"


class Detector(Protocol):
    name: str

    def detect(self, image: np.ndarray) -> list[Region]: ...


class RoiDetector:
    """Cắt vùng ROI cố định; không khai báo ROI thì lấy toàn bộ ảnh."""

    name = "roi"

    def __init__(self, roi: tuple[float, float, float, float] | None = None) -> None:
        self._roi = roi

    def detect(self, image: np.ndarray) -> list[Region]:
        height, width = image.shape[:2]
        if self._roi is None:
            x1, y1, x2, y2 = 0, 0, width, height
        else:
            fx1, fy1, fx2, fy2 = self._roi
            x1, y1 = int(fx1 * width), int(fy1 * height)
            x2, y2 = int(round(fx2 * width)), int(round(fy2 * height))

        x1 = max(0, min(x1, width - 1))
        y1 = max(0, min(y1, height - 1))
        x2 = max(x1 + 1, min(x2, width))
        y2 = max(y1 + 1, min(y2, height))

        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            return []
        return [Region(image=crop.copy(), box=(x1, y1, x2, y2), source=self.name)]


class YoloDetector:
    """YOLOv8 - chỉ để sẵn cho giai đoạn sau.

    Cần: pip install ultralytics và một file trọng số đã huấn luyện vùng biển số
    (YOLO_WEIGHTS). Chưa có trọng số thì lớp này báo lỗi rõ ràng ngay khi khởi động,
    không âm thầm trả kết quả rỗng.
    """

    name = "yolo"

    def __init__(self, weights: str, conf: float) -> None:
        if not weights:
            raise ConfigError("PLATE_DETECTOR=yolo nhưng chưa khai báo YOLO_WEIGHTS")
        try:
            from ultralytics import YOLO  # noqa: PLC0415 - import nặng, chỉ khi cần
        except ImportError as exc:
            raise ConfigError(
                "PLATE_DETECTOR=yolo cần gói 'ultralytics' (chưa cài). "
                "Đổi lại PLATE_DETECTOR=roi nếu chưa có trọng số."
            ) from exc
        self._model = YOLO(weights)
        self._conf = conf

    def detect(self, image: np.ndarray) -> list[Region]:
        height, width = image.shape[:2]
        results = self._model.predict(image, conf=self._conf, verbose=False)
        regions: list[Region] = []
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
                x1, x2 = max(0, x1), min(width, x2)
                y1, y2 = max(0, y1), min(height, y2)
                if x2 - x1 < 8 or y2 - y1 < 8:
                    continue
                regions.append(
                    Region(image=image[y1:y2, x1:x2].copy(), box=(x1, y1, x2, y2), source=self.name)
                )
        return regions


def build_detector(settings: Settings) -> Detector:
    if settings.detector == "roi":
        return RoiDetector(settings.roi)
    return YoloDetector(settings.yolo_weights, settings.yolo_conf)
