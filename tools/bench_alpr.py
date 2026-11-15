"""Đo chất lượng dịch vụ ALPR (giai đoạn 3) trên một thư mục ảnh có nhãn.

Cấu trúc thư mục:
    anh-mau/
    ├── labels.csv        # cột: filename,plate,note
    ├── 001.jpg
    └── 002.jpg

Chạy:
    python tools/bench_alpr.py --dir anh-mau
    python tools/bench_alpr.py --dir anh-mau --csv ket-qua.csv

labels.csv: `plate` là biển đúng (dạng 30A-12345 hoặc 30A12345); để trống nếu ảnh
không có biển số (khi đó chỉ tính đúng nếu server trả plate = null).
So sánh sau khi bỏ '-' và '.' và chuyển sang chữ hoa.

Chỉ dùng `requests` và thư viện chuẩn của Python.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import mimetypes
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'requests'. Cài: pip install -r tools/requirements.txt")

TARGET_MS = 500.0
REQUIRED_COLUMNS = ("filename", "plate")


@dataclass
class Row:
    filename: str
    expected: str | None
    note: str


@dataclass
class Outcome:
    filename: str
    expected: str | None
    got: str | None
    valid: bool
    confidence: float
    ms: int | None
    raw: str
    match: bool
    error: str = ""


def normalize_plate(value: str | None) -> str:
    """Bỏ '-' và '.', chuyển chữ hoa - dùng để so sánh biển số."""
    if not value:
        return ""
    return value.replace("-", "").replace(".", "").strip().upper()


def load_labels(directory: Path) -> list[Row]:
    labels_path = directory / "labels.csv"
    if not labels_path.is_file():
        raise SystemExit(f"Không thấy file nhãn: {labels_path}")

    with labels_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [c.strip() for c in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in columns]
        if missing:
            raise SystemExit(
                f"labels.csv thiếu cột {', '.join(missing)}; cần tối thiểu: filename,plate,note"
            )

        rows: list[Row] = []
        for index, raw in enumerate(reader, start=2):
            filename = (raw.get("filename") or "").strip()
            if not filename:
                continue
            plate = normalize_plate(raw.get("plate"))
            rows.append(
                Row(
                    filename=filename,
                    expected=plate or None,
                    note=(raw.get("note") or "").strip(),
                )
            )

    if not rows:
        raise SystemExit(f"{labels_path} không có dòng dữ liệu nào")
    return rows


def percentile(values: list[float], percent: float) -> float:
    """Nội suy tuyến tính - đủ chính xác cho báo cáo, không cần numpy."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * (percent / 100.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def call_recognize(session: requests.Session, url: str, image_path: Path, timeout: float) -> Outcome:
    mime = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    started = time.perf_counter()
    try:
        with image_path.open("rb") as handle:
            response = session.post(
                f"{url.rstrip('/')}/recognize",
                params={"debug": "true"},
                files={"image": (image_path.name, handle, mime)},
                timeout=timeout,
            )
    except requests.RequestException as exc:
        return Outcome(image_path.name, None, None, False, 0.0, None, "", False, error=f"{type(exc).__name__}: {exc}")

    elapsed_ms = int(round((time.perf_counter() - started) * 1000))
    if response.status_code != 200:
        return Outcome(
            image_path.name, None, None, False, 0.0, elapsed_ms, "", False,
            error=f"HTTP {response.status_code}: {response.text[:120]}",
        )

    try:
        body = response.json()
    except json.JSONDecodeError:
        return Outcome(image_path.name, None, None, False, 0.0, elapsed_ms, "", False, error="JSON không hợp lệ")

    return Outcome(
        filename=image_path.name,
        expected=None,
        got=body.get("plate"),
        valid=bool(body.get("valid")),
        confidence=float(body.get("confidence") or 0.0),
        ms=int(body["ms"]) if isinstance(body.get("ms"), (int, float)) else elapsed_ms,
        raw=str(body.get("raw") or ""),
        match=False,
    )


def is_match(expected: str | None, got: str | None) -> bool:
    return normalize_plate(expected) == normalize_plate(got)


def format_ratio(count: int, total: int) -> str:
    return f"{count}/{total} ({count / total * 100:.1f}%)" if total else "0/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Đo chất lượng ALPR: độ đúng, độ hợp lệ, p50/p95 và tỉ lệ ảnh dưới 500 ms."
    )
    parser.add_argument("--dir", required=True, type=Path, help="thư mục ảnh có labels.csv (filename,plate,note)")
    parser.add_argument("--url", default="http://localhost:8001", help="địa chỉ dịch vụ ALPR")
    parser.add_argument("--csv", type=Path, default=None, help="ghi kết quả từng ảnh ra file CSV")
    parser.add_argument("--timeout", type=float, default=30.0, help="timeout mỗi request (giây)")
    args = parser.parse_args(argv)

    directory: Path = args.dir
    if not directory.is_dir():
        raise SystemExit(f"Không thấy thư mục: {directory}")

    rows = load_labels(directory)
    session = requests.Session()
    outcomes: list[Outcome] = []

    print(f"Thư mục : {directory.resolve()}")
    print(f"Server  : {args.url}")
    print(f"Ảnh     : {len(rows)}\n")

    for index, row in enumerate(rows, start=1):
        image_path = directory / row.filename
        if not image_path.is_file():
            outcomes.append(
                Outcome(row.filename, row.expected, None, False, 0.0, None, "", False, error="không thấy file ảnh")
            )
            print(f"[{index}/{len(rows)}] {row.filename}: THIẾU FILE", flush=True)
            continue

        outcome = call_recognize(session, args.url, image_path, args.timeout)
        outcome.expected = row.expected
        outcome.match = outcome.error == "" and is_match(row.expected, outcome.got)
        outcomes.append(outcome)
        status = "đúng" if outcome.match else "SAI"
        print(
            f"[{index}/{len(rows)}] {row.filename}: {status} "
            f"(mong đợi {outcome.expected or 'null'} | nhận {outcome.got or 'null'} | {outcome.ms} ms)",
            flush=True,
        )

    total = len(outcomes)
    correct = sum(1 for item in outcomes if item.match)
    valid_count = sum(1 for item in outcomes if item.valid)
    failed_requests = [item for item in outcomes if item.error]
    timings = [float(item.ms) for item in outcomes if item.ms is not None]
    under_target = sum(1 for value in timings if value < TARGET_MS)

    print("\n" + "=" * 62)
    print(f"Số ảnh                : {total}")
    print(f"Đọc đúng              : {format_ratio(correct, total)}")
    print(f"Biển hợp lệ (valid)   : {format_ratio(valid_count, total)}")
    if timings:
        average = sum(timings) / len(timings)
        print(
            f"Thời gian server (ms) : p50 {percentile(timings, 50):.0f} | "
            f"p95 {percentile(timings, 95):.0f} | trung bình {average:.0f}"
        )
        print(f"Ảnh dưới {TARGET_MS:.0f} ms      : {format_ratio(under_target, len(timings))}")
    else:
        print("Thời gian server (ms) : không có dữ liệu (mọi request đều lỗi)")
    if failed_requests:
        print(f"Request lỗi           : {len(failed_requests)}")

    wrong = [item for item in outcomes if not item.match]
    if wrong:
        print(f"\nẢnh đọc sai ({len(wrong)}):")
        for item in wrong:
            if item.error:
                print(f"  {item.filename:<28} LỖI: {item.error}  (mong đợi {item.expected or 'null'})")
                continue
            print(
                f"  {item.filename:<28} mong đợi {item.expected or 'null':<12} "
                f"nhận {item.got or 'null'} (valid={item.valid}, conf={item.confidence:.3f})"
            )
            print(f"  {'':<28} raw: {item.raw or '(rỗng)'}")
    else:
        print("\nKhông có ảnh đọc sai.")
    print("=" * 62)

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["filename", "expected", "got", "match", "valid", "confidence", "ms", "raw", "error"])
            for item in outcomes:
                writer.writerow(
                    [
                        item.filename,
                        item.expected or "",
                        item.got or "",
                        "1" if item.match else "0",
                        "1" if item.valid else "0",
                        f"{item.confidence:.3f}",
                        "" if item.ms is None else item.ms,
                        item.raw,
                        item.error,
                    ]
                )
        print(f"Đã ghi kết quả từng ảnh: {args.csv}")

    return 0


if __name__ == "__main__":
    # Console Windows mặc định cp1252 -> in tiếng Việt bị lỗi ký tự khi ghi ra file/pipe.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
