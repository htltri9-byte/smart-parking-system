"""So sánh ROI cố định và YOLO trên cùng bộ ảnh mẫu.

Với mỗi ảnh trong `--dir` (có `labels.csv` như tools/bench_alpr.py), script đo:

- **tỉ lệ tìm được biển** (cần `boxes.csv` để đúng nghĩa; xem README mục 6.2),
- **thời gian tạo vùng cắt** (ROI: cắt + phóng to; YOLO: suy luận + cắt + phóng to),
- **tỉ lệ ALPR đọc đúng** khi gửi đúng vùng cắt của mỗi bên sang `POST /recognize`,
- **tổng thời gian** cắt + gọi ALPR.

Chạy:
    python tools/yolo/eval_detector.py --dir anh-mau --weights best.pt --roi 0.10,0.60,0.90,0.95

Cần: `opencv-python-headless`, `requests` (đã có trong tools/requirements.txt) và
`ultralytics` (chỉ để chạy YOLO). Dịch vụ ALPR phải đang chạy và **ROI trong alpr/.env
để trống** - nếu không, vùng cắt của YOLO sẽ bị service cắt thêm lần nữa.

Không import gì từ alpr/app: cách cắt ROI, cách phóng to và cách chuẩn hoá biển được
sao chép có chủ đích để số đo phản ánh đúng hành vi service mà không sửa service.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import cv2
    import numpy as np
    import requests
except ImportError as exc:  # pragma: no cover - phụ thuộc môi trường
    cv2 = None
    np = None
    requests = None
    IMPORT_ERROR: ImportError | None = exc
else:
    IMPORT_ERROR = None

REQUIRED_COLUMNS = ("filename", "plate")
BOX_COLUMNS = ("filename", "x1", "y1", "x2", "y2")
GT_IOU = 0.5
ROI_CONTAINMENT = 0.9


@dataclass
class Sample:
    filename: str
    expected: str | None
    note: str
    box: tuple[float, float, float, float] | None = None


@dataclass
class Row:
    filename: str
    note: str
    expected: str | None
    mode: str
    found: bool
    det_ms: float
    plate: str | None
    valid: bool
    alpr_ms: int | None
    total_ms: float
    match: bool
    error: str = ""


def normalize_plate(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("-", "").replace(".", "").strip().upper()


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100.0)
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def parse_roi(text: str) -> tuple[float, float, float, float] | None:
    """ROI dạng "x1,y1,x2,y2" tỉ lệ 0-1; rỗng -> None (toàn ảnh), giống alpr/app/config.py."""
    value = text.strip()
    if not value:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise SystemExit(f'--roi phải có 4 số "x1,y1,x2,y2", nhận được {text!r}')
    try:
        x1, y1, x2, y2 = (float(part) for part in parts)
    except ValueError as exc:
        raise SystemExit(f"--roi phải là 4 số thực trong khoảng 0-1, nhận được {text!r}") from exc
    if not all(0.0 <= number <= 1.0 for number in (x1, y1, x2, y2)):
        raise SystemExit(f"--roi phải nằm trong khoảng 0-1, nhận được {text!r}")
    if x1 >= x2 or y1 >= y2:
        raise SystemExit(f"--roi phải có x1 < x2 và y1 < y2, nhận được {text!r}")
    return (x1, y1, x2, y2)


def load_samples(directory: Path, boxes_path: Path | None) -> list[Sample]:
    labels_path = directory / "labels.csv"
    if not labels_path.is_file():
        raise SystemExit(
            f"Không thấy {labels_path}. Cần labels.csv (filename,plate,note) như tools/bench_alpr.py."
        )

    boxes: dict[str, tuple[float, float, float, float]] = {}
    candidate_boxes = boxes_path or (directory / "boxes.csv")
    if candidate_boxes.is_file():
        with candidate_boxes.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = [c.strip() for c in (reader.fieldnames or [])]
            missing = [c for c in BOX_COLUMNS if c not in columns]
            if missing:
                raise SystemExit(f"{candidate_boxes} thiếu cột {', '.join(missing)}")
            for raw in reader:
                filename = (raw.get("filename") or "").strip()
                if not filename:
                    continue
                try:
                    box = tuple(float(raw[key]) for key in ("x1", "y1", "x2", "y2"))
                except (TypeError, ValueError) as exc:
                    raise SystemExit(f"{candidate_boxes}: toạ độ không hợp lệ ở dòng {filename}") from exc
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise SystemExit(f"{candidate_boxes}: box rỗng ở dòng {filename}")
                boxes[filename] = box  # type: ignore[assignment]
        print(f"Nhãn kiểm chứng: {candidate_boxes} ({len(boxes)} box)")
    else:
        print("Nhãn kiểm chứng: không có boxes.csv -> 'tìm thấy biển' là proxy (xem README 6.2)")

    with labels_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise SystemExit(f"{labels_path} thiếu cột {', '.join(missing)}")
        samples = [
            Sample(
                filename=(raw.get("filename") or "").strip(),
                expected=normalize_plate(raw.get("plate")) or None,
                note=(raw.get("note") or "").strip(),
                box=boxes.get((raw.get("filename") or "").strip()),
            )
            for raw in reader
            if (raw.get("filename") or "").strip()
        ]

    if not samples:
        raise SystemExit(f"{labels_path} không có dòng dữ liệu nào")
    return samples


def roi_to_pixels(
    roi: tuple[float, float, float, float] | None, width: int, height: int
) -> tuple[int, int, int, int]:
    """Sao chép cách tính trong alpr/app/detector.py::RoiDetector.detect."""
    if roi is None:
        x1, y1, x2, y2 = 0, 0, width, height
    else:
        fx1, fy1, fx2, fy2 = roi
        x1, y1 = int(fx1 * width), int(fy1 * height)
        x2, y2 = int(round(fx2 * width)), int(round(fy2 * height))
    x1 = max(0, min(x1, width - 1))
    y1 = max(0, min(y1, height - 1))
    x2 = max(x1 + 1, min(x2, width))
    y2 = max(y1 + 1, min(y2, height))
    return x1, y1, x2, y2


def fit_width(image, min_width: int):
    """Sao chép alpr/app/main.py::Pipeline._upscale (tối đa 4x, CUBIC)."""
    width = image.shape[1]
    if width >= min_width or width == 0:
        return image
    scale = min(4.0, min_width / width)
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def coverage(outer: tuple[float, float, float, float], inner: tuple[float, float, float, float]) -> float:
    """Phần diện tích của `inner` nằm trong `outer` (0-1).

    Dùng cho ROI: ROI thường lớn hơn biển rất nhiều nên IoU luôn thấp dù biển nằm trọn
    trong ROI; cần đo "biển có nằm trong vùng cắt không" thay vì "hai box giống nhau".
    """
    ix1, iy1 = max(outer[0], inner[0]), max(outer[1], inner[1])
    ix2, iy2 = min(outer[2], inner[2]), min(outer[3], inner[3])
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_inner = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    return intersection / area_inner if area_inner > 0 else 0.0


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    intersection = inter_w * inter_h
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def call_recognize(
    session, url: str, image, filename: str, timeout: float
) -> tuple[str | None, bool, int | None, str]:
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        return None, False, None, "không nén được JPEG"
    try:
        response = session.post(
            f"{url.rstrip('/')}/recognize",
            files={"image": (filename, buffer.tobytes(), "image/jpeg")},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, False, None, f"{type(exc).__name__}: {exc}"
    if response.status_code != 200:
        return None, False, None, f"HTTP {response.status_code}: {response.text[:120]}"
    try:
        body = response.json()
    except ValueError:
        return None, False, None, "JSON không hợp lệ"
    service_ms = body.get("ms")
    return (
        body.get("plate"),
        bool(body.get("valid")),
        int(service_ms) if isinstance(service_ms, (int, float)) else None,
        "",
    )


def evaluate_mode(mode: str, samples: list[Sample], args, model, roi, session) -> list[Row]:
    rows: list[Row] = []
    total = len(samples)
    for index, sample in enumerate(samples, start=1):
        path: Path = args.dir / sample.filename
        image = cv2.imread(str(path)) if path.is_file() else None
        if image is None:
            reason = "không thấy file ảnh" if not path.is_file() else "không đọc được ảnh"
            rows.append(Row(sample.filename, sample.note, sample.expected, mode, False, 0.0, None, False, None, 0.0, sample.expected is None, reason))
            print(f"[{mode} {index}/{total}] {sample.filename}: LỖI ({reason})", flush=True)
            continue

        started = time.perf_counter()
        crop = None
        det_box: tuple[float, float, float, float] | None = None
        found = False

        if mode == "roi":
            x1, y1, x2, y2 = roi_to_pixels(roi, image.shape[1], image.shape[0])
            det_box = (float(x1), float(y1), float(x2), float(y2))
            crop = image[y1:y2, x1:x2].copy()
            # Có box thật thì ROI "tìm thấy" khi biển nằm trong ROI; không có thì luôn có vùng cắt.
            found = True if sample.box is None else coverage(det_box, sample.box) >= ROI_CONTAINMENT
        else:
            predictions = model.predict(
                image,
                conf=args.conf,
                imgsz=args.imgsz,
                device=args.device or None,
                classes=[0],
                verbose=False,
            )
            detections: list[tuple[tuple[float, float, float, float], float]] = []
            for result in predictions:
                for box in result.boxes:
                    x1, y1, x2, y2 = (float(v) for v in box.xyxy[0].tolist())
                    detections.append(((x1, y1, x2, y2), float(box.conf[0])))
            detections.sort(key=lambda item: item[1], reverse=True)
            if detections:
                det_box = detections[0][0]
                found = (
                    any(iou(candidate, sample.box) >= GT_IOU for candidate, _ in detections)
                    if sample.box
                    else True
                )
                x1 = max(0, int(det_box[0]))
                y1 = max(0, int(det_box[1]))
                x2 = min(image.shape[1], int(det_box[2]))
                y2 = min(image.shape[0], int(det_box[3]))
                if x2 - x1 >= 8 and y2 - y1 >= 8:
                    crop = image[y1:y2, x1:x2].copy()
            else:
                found = False

        if crop is not None and crop.size:
            crop = fit_width(crop, args.min_plate_width)
        det_ms = (time.perf_counter() - started) * 1000.0

        if crop is None or crop.size == 0:
            rows.append(Row(sample.filename, sample.note, sample.expected, mode, found, det_ms, None, False, None, det_ms, sample.expected is None))
            print(f"[{mode} {index}/{total}] {sample.filename}: không có vùng cắt ({det_ms:.0f} ms)", flush=True)
            continue

        alpr_started = time.perf_counter()
        plate, valid, alpr_ms, error = call_recognize(
            session, args.url, crop, f"{mode}__{sample.filename}", args.timeout
        )
        total_ms = det_ms + (time.perf_counter() - alpr_started) * 1000.0
        match = error == "" and normalize_plate(plate) == normalize_plate(sample.expected)

        rows.append(
            Row(sample.filename, sample.note, sample.expected, mode, found, det_ms, plate, valid, alpr_ms, total_ms, match, error)
        )
        print(
            f"[{mode} {index}/{total}] {sample.filename}: "
            f"tìm={'có' if found else 'KHÔNG'} | cắt {det_ms:.0f} ms | "
            f"biển {plate or 'null'} | {'đúng' if match else 'SAI'} | {total_ms:.0f} ms"
            + (f" | LỖI: {error}" if error else ""),
            flush=True,
        )
    return rows


@dataclass
class Summary:
    mode: str
    label: str
    n: int
    n_plate: int
    found: int
    alpr_correct: int
    correct_when_found: int
    det_p50: float
    det_p95: float
    total_p50: float
    total_p95: float
    gt: str


def summarize(rows: list[Row], label: str, has_boxes: bool) -> Summary:
    plate_rows = [row for row in rows if row.expected]
    det_times = [row.det_ms for row in rows]
    total_times = [row.total_ms for row in rows]
    return Summary(
        mode=rows[0].mode if rows else "?",
        label=label,
        n=len(rows),
        n_plate=len(plate_rows),
        found=sum(1 for row in plate_rows if row.found),
        alpr_correct=sum(1 for row in rows if row.match),
        correct_when_found=sum(1 for row in plate_rows if row.found and row.match),
        det_p50=percentile(det_times, 50),
        det_p95=percentile(det_times, 95),
        total_p50=percentile(total_times, 50),
        total_p95=percentile(total_times, 95),
        gt="boxes.csv" if has_boxes else "proxy",
    )


def ratio(count: int, total: int) -> str:
    return f"{count}/{total} ({count / total * 100:.1f}%)" if total else "0/0"


def print_table(summaries: list[Summary]) -> None:
    print("\n| Chế độ | Ảnh | Tìm thấy biển | p50 cắt (ms) | p95 cắt (ms) | ALPR đúng | Đúng/ảnh tìm thấy | p50 tổng (ms) | p95 tổng (ms) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for item in summaries:
        print(
            f"| {item.label} | {item.n} | {ratio(item.found, item.n_plate)}"
            f"{' *' if item.gt == 'proxy' else ''} | "
            f"{item.det_p50:.0f} | {item.det_p95:.0f} | {ratio(item.alpr_correct, item.n)} | "
            f"{item.correct_when_found}/{item.found} | {item.total_p50:.0f} | {item.total_p95:.0f} |"
        )
    if any(item.gt == "proxy" for item in summaries):
        print("\n* proxy: không có boxes.csv nên ROI luôn tính là 'tìm thấy' (luôn có vùng cắt).")


EVAL_COLUMNS = (
    "filename", "note", "expected", "mode", "found", "det_ms",
    "plate", "valid", "alpr_ms", "total_ms", "match", "error",
)
SUMMARY_COLUMNS = (
    "mode", "n", "n_plate", "found", "found_pct", "alpr_correct", "alpr_pct",
    "correct_when_found", "det_p50_ms", "det_p95_ms", "total_p50_ms", "total_p95_ms", "gt_source",
)


def write_eval_csv(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(EVAL_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row.filename, row.note, row.expected or "", row.mode,
                    "1" if row.found else "0", f"{row.det_ms:.1f}",
                    row.plate or "", "1" if row.valid else "0",
                    "" if row.alpr_ms is None else row.alpr_ms,
                    f"{row.total_ms:.1f}", "1" if row.match else "0", row.error,
                ]
            )


def write_summary_csv(path: Path, summaries: list[Summary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(SUMMARY_COLUMNS)
        for item in summaries:
            writer.writerow(
                [
                    item.mode, item.n, item.n_plate, item.found,
                    f"{item.found / item.n_plate * 100:.1f}" if item.n_plate else "",
                    item.alpr_correct,
                    f"{item.alpr_correct / item.n * 100:.1f}" if item.n else "",
                    item.correct_when_found,
                    f"{item.det_p50:.0f}", f"{item.det_p95:.0f}",
                    f"{item.total_p50:.0f}", f"{item.total_p95:.0f}", item.gt,
                ]
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="So sánh ROI cố định và YOLO: tìm thấy biển, thời gian, ALPR đọc đúng.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dir", required=True, type=Path, help="thư mục ảnh có labels.csv (và boxes.csv nếu có)")
    parser.add_argument("--weights", required=True, help="best.pt do train_plate.py xuất ra")
    parser.add_argument("--roi", default="", help='ROI đang dùng "x1,y1,x2,y2" tỉ lệ 0-1; rỗng = toàn ảnh')
    parser.add_argument("--conf", type=float, default=0.25, help="ngưỡng tin cậy YOLO")
    parser.add_argument("--imgsz", type=int, default=640, help="kích thước suy luận YOLO")
    parser.add_argument("--device", default="", help='"" = tự chọn, "cpu", "0"')
    parser.add_argument("--min-plate-width", type=int, default=480, help="phóng to vùng cắt tới bề rộng này (px)")
    parser.add_argument("--url", default="http://localhost:8001", help="địa chỉ dịch vụ ALPR")
    parser.add_argument("--timeout", type=float, default=30.0, help="timeout mỗi request ALPR (giây)")
    parser.add_argument("--boxes", type=Path, default=None, help="CSV nhãn box; mặc định <dir>/boxes.csv")
    parser.add_argument("--csv", default="results/yolo_detector_eval.csv", help="CSV từng ảnh (chuỗi rỗng = không ghi)")
    parser.add_argument("--summary", default="results/yolo_detector_summary.csv", help="CSV tổng hợp (chuỗi rỗng = không ghi)")
    parser.add_argument("--max-images", type=int, default=0, help="chỉ chạy N ảnh đầu (0 = tất cả)")
    parser.add_argument("--verbose", action="store_true", help="in thêm danh sách ảnh sai")
    args = parser.parse_args(argv)
    if not 0.0 <= args.conf <= 1.0:
        parser.error("--conf phải trong khoảng 0-1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if IMPORT_ERROR is not None:
        print(
            f"Thiếu gói '{IMPORT_ERROR.name}'. Cài: pip install opencv-python-headless requests ultralytics",
            file=sys.stderr,
        )
        return 2

    if not args.dir.is_dir():
        print(f"Không thấy thư mục: {args.dir}", file=sys.stderr)
        return 2
    weights_path = Path(args.weights)
    if not weights_path.is_file():
        print(
            f"Không thấy trọng số {weights_path}. Train trước bằng tools/yolo/train_plate.py.",
            file=sys.stderr,
        )
        return 2
    try:
        roi = parse_roi(args.roi)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        from ultralytics import YOLO  # noqa: PLC0415 - import nặng, chỉ khi chạy YOLO
    except ImportError:
        print(
            "Chưa cài 'ultralytics'. Cài bằng: pip install ultralytics\n"
            "(chế độ ROI không cần model nhưng script so sánh hai bên nên vẫn cần YOLO)",
            file=sys.stderr,
        )
        return 2

    samples = load_samples(args.dir, args.boxes)
    if args.max_images > 0:
        samples = samples[: args.max_images]
    has_boxes = any(sample.box is not None for sample in samples)
    roi_label = "toàn ảnh" if roi is None else ",".join(f"{value:g}" for value in roi)

    print(f"Ảnh     : {len(samples)} ({args.dir.resolve()})")
    print(f"ALPR    : {args.url}")
    print(f"ROI     : {roi_label}")
    print(f"YOLO    : {weights_path} (conf={args.conf}, imgsz={args.imgsz})\n")

    model = YOLO(str(weights_path))
    names = getattr(model, "names", {}) or {}
    print(f"Lớp model: {names} (cần 0: plate)")
    # Warmup: lần suy luận đầu chậm do nạp CUDA/ONNX - không tính vào số đo.
    model.predict(np.zeros((640, 640, 3), dtype=np.uint8), conf=args.conf, imgsz=args.imgsz,
                  device=args.device or None, classes=[0], verbose=False)

    session = requests.Session()
    roi_rows = evaluate_mode("roi", samples, args, model, roi, session)
    yolo_rows = evaluate_mode("yolo", samples, args, model, roi, session)

    summaries = [
        summarize(roi_rows, f"ROI cố định ({roi_label})", has_boxes),
        summarize(yolo_rows, f"YOLO ({weights_path.name}, conf {args.conf})", has_boxes),
    ]
    print_table(summaries)

    errors = [row for row in roi_rows + yolo_rows if row.error]
    if len(errors) == len(roi_rows) + len(yolo_rows) and errors:
        print("\nMọi request ALPR đều lỗi - kiểm tra dịch vụ ALPR đã chạy chưa (docker compose ps alpr).")

    if args.verbose:
        print("\nẢnh đọc sai:")
        for row in roi_rows + yolo_rows:
            if not row.match:
                print(
                    f"  [{row.mode}] {row.filename}: mong đợi {row.expected or 'null'} | "
                    f"nhận {row.plate or 'null'}" + (f" | {row.error}" if row.error else "")
                )

    if args.csv.strip():
        csv_path = Path(args.csv)
        write_eval_csv(csv_path, roi_rows + yolo_rows)
        print(f"\nĐã ghi từng ảnh: {csv_path}")
    if args.summary.strip():
        summary_path = Path(args.summary)
        write_summary_csv(summary_path, summaries)
        print(f"Đã ghi tổng hợp: {summary_path}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
