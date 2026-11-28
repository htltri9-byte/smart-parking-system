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

Báo cáo thêm (giai đoạn 7):
- Bảng theo nhóm `note` (sang/toi/lech/2dong/...): số ảnh, tỉ lệ đúng, tỉ lệ hợp lệ,
  p50/p95 thời gian.
- Danh sách nhầm lẫn ký tự hay gặp (cặp "ký tự đúng -> ký tự đọc sai").
- Ghi results/alpr_bench.csv (từng ảnh) và results/alpr_summary.csv (theo nhóm),
  đồng thời in bảng markdown ra màn hình.

Chỉ dùng `requests` và thư viện chuẩn của Python.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import json
import math
import mimetypes
import sys
import time
from collections import Counter
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
    note: str = ""


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


NO_NOTE = "(không ghi)"


def note_key(note: str) -> str:
    """Tên nhóm theo cột `note`; note trống gom vào một nhóm riêng."""
    return note.strip().lower() or NO_NOTE


def confusion_pairs(expected: str | None, got: str | None) -> list[tuple[str, str]]:
    """Cặp (ký tự đúng, ký tự đọc sai) giữa hai biển đã bỏ '-' và '.'.

    Căn hai chuỗi bằng difflib; chỉ lấy các đoạn thay thế (replace), bỏ qua ký tự
    thừa/thiếu để không báo nhầm thành nhầm lẫn.
    """
    if not expected or not got:
        return []
    left, right = normalize_plate(expected), normalize_plate(got)
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    pairs: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        for offset in range(min(i2 - i1, j2 - j1)):
            pairs.append((left[i1 + offset], right[j1 + offset]))
    return pairs


@dataclass
class GroupStat:
    note: str
    total: int
    correct: int
    valid: int
    p50: float | None
    p95: float | None


def group_stats(outcomes: list[Outcome]) -> list[GroupStat]:
    """Gom kết quả theo `note`, nhóm trống xếp cuối."""
    groups: dict[str, list[Outcome]] = {}
    for item in outcomes:
        groups.setdefault(note_key(item.note), []).append(item)

    stats: list[GroupStat] = []
    for name in sorted(groups, key=lambda key: (key == NO_NOTE, key)):
        items = groups[name]
        timings = [float(item.ms) for item in items if item.ms is not None]
        stats.append(
            GroupStat(
                note=name,
                total=len(items),
                correct=sum(1 for item in items if item.match),
                valid=sum(1 for item in items if item.valid),
                p50=percentile(timings, 50) if timings else None,
                p95=percentile(timings, 95) if timings else None,
            )
        )
    return stats


def confusion_stats(outcomes: list[Outcome]) -> list[tuple[str, str, int]]:
    """Đếm các cặp nhầm lẫn ký tự, nhiều nhất trước."""
    counter: Counter[tuple[str, str]] = Counter()
    for item in outcomes:
        if item.error or not item.expected or not item.got:
            continue
        for pair in confusion_pairs(item.expected, item.got):
            counter[pair] += 1
    return [(left, right, count) for (left, right), count in sorted(
        counter.items(), key=lambda entry: (-entry[1], entry[0])
    )]


def print_group_markdown(stats: list[GroupStat], outcomes: list[Outcome]) -> None:
    print("| Note | Ảnh | Đọc đúng | Biển hợp lệ | p50 (ms) | p95 (ms) |")
    print("|---|---:|---:|---:|---:|---:|")
    for stat in stats:
        p50 = "—" if stat.p50 is None else f"{stat.p50:.0f}"
        p95 = "—" if stat.p95 is None else f"{stat.p95:.0f}"
        print(
            f"| {stat.note} | {stat.total} | {format_ratio(stat.correct, stat.total)} | "
            f"{format_ratio(stat.valid, stat.total)} | {p50} | {p95} |"
        )

    timings = [float(item.ms) for item in outcomes if item.ms is not None]
    total = len(outcomes)
    correct = sum(1 for item in outcomes if item.match)
    valid = sum(1 for item in outcomes if item.valid)
    p50 = "—" if not timings else f"{percentile(timings, 50):.0f}"
    p95 = "—" if not timings else f"{percentile(timings, 95):.0f}"
    print(
        f"| **Tổng** | {total} | {format_ratio(correct, total)} | "
        f"{format_ratio(valid, total)} | {p50} | {p95} |"
    )


BENCH_COLUMNS = (
    "filename",
    "note",
    "expected",
    "got",
    "match",
    "valid",
    "confidence",
    "ms",
    "confusions",
    "raw",
    "error",
)
SUMMARY_COLUMNS = ("note", "n", "correct", "correct_pct", "valid", "valid_pct", "p50_ms", "p95_ms")


def write_bench_csv(path: Path, outcomes: list[Outcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(BENCH_COLUMNS)
        for item in outcomes:
            pairs = [] if item.error else confusion_pairs(item.expected, item.got)
            writer.writerow(
                [
                    item.filename,
                    item.note,
                    item.expected or "",
                    item.got or "",
                    "1" if item.match else "0",
                    "1" if item.valid else "0",
                    f"{item.confidence:.3f}",
                    "" if item.ms is None else item.ms,
                    "; ".join(f"{left}->{right}" for left, right in pairs),
                    item.raw,
                    item.error,
                ]
            )


def write_summary_csv(path: Path, stats: list[GroupStat]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(SUMMARY_COLUMNS)
        for stat in stats:
            correct_pct = f"{stat.correct / stat.total * 100:.1f}" if stat.total else ""
            valid_pct = f"{stat.valid / stat.total * 100:.1f}" if stat.total else ""
            writer.writerow(
                [
                    stat.note,
                    stat.total,
                    stat.correct,
                    correct_pct,
                    stat.valid,
                    valid_pct,
                    "" if stat.p50 is None else f"{stat.p50:.0f}",
                    "" if stat.p95 is None else f"{stat.p95:.0f}",
                ]
            )


def format_ratio(count: int, total: int) -> str:
    return f"{count}/{total} ({count / total * 100:.1f}%)" if total else "0/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Đo chất lượng ALPR: độ đúng, độ hợp lệ, p50/p95 và tỉ lệ ảnh dưới 500 ms."
    )
    parser.add_argument("--dir", required=True, type=Path, help="thư mục ảnh có labels.csv (filename,plate,note)")
    parser.add_argument("--url", default="http://localhost:8001", help="địa chỉ dịch vụ ALPR")
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("results/alpr_bench.csv"),
        help="ghi kết quả từng ảnh ra file CSV (đường dẫn tính từ thư mục đang chạy)",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("results/alpr_summary.csv"),
        help="ghi bảng tổng hợp theo nhóm note ra file CSV",
    )
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
                Outcome(
                    row.filename,
                    row.expected,
                    None,
                    False,
                    0.0,
                    None,
                    "",
                    False,
                    error="không thấy file ảnh",
                    note=row.note,
                )
            )
            print(f"[{index}/{len(rows)}] {row.filename}: THIẾU FILE", flush=True)
            continue

        outcome = call_recognize(session, args.url, image_path, args.timeout)
        outcome.expected = row.expected
        outcome.note = row.note
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

    stats = group_stats(outcomes)
    print("\nKết quả theo nhóm note")
    print_group_markdown(stats, outcomes)

    print("\nNhầm lẫn ký tự hay gặp (ký tự đúng -> ký tự đọc sai)")
    confusions = confusion_stats(outcomes)
    if confusions:
        print("| Đúng | Đọc sai | Số lần |")
        print("|---|---:|---:|")
        for expected_char, got_char, count in confusions:
            print(f"| {expected_char} | {got_char} | {count} |")
    else:
        print("Không có cặp nhầm lẫn nào (không có ảnh đọc sai kèm biển đọc được).")

    if args.csv:
        write_bench_csv(args.csv, outcomes)
        print(f"\nĐã ghi kết quả từng ảnh: {args.csv}")
    if args.summary:
        write_summary_csv(args.summary, stats)
        print(f"Đã ghi tổng hợp theo nhóm: {args.summary}")

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
