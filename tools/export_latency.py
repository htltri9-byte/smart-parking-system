"""Tổng hợp độ trễ chia chặng từ /api/traces.csv và in bảng markdown.

- Gọi API CSV của backend (giai đoạn 7), bỏ N lượt đầu (làm nóng, mặc định 2).
- Tính p50, p95, trung bình, lớn nhất cho capture_ms, alpr_ms, decide_ms,
  barrier_ms, total_ms.
- Tính tỉ lệ lượt chặng ALPR dưới 500 ms.
- In bảng markdown ra màn hình và ghi results/latency_summary.csv.

Lưu ý: API trả các dòng **mới nhất trước**, nên script sắp xếp theo `id` tăng dần
rồi mới bỏ N lượt đầu (đúng nghĩa "lượt đầu tiên" theo thời gian).

Chạy:
    python tools/export_latency.py
    python tools/export_latency.py --url http://localhost:8000 --limit 200
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import sys
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover - thông báo dễ hiểu thay vì traceback
    sys.exit("Thiếu gói 'requests'. Cài: pip install -r tools/requirements.txt")

# Ngưỡng mục tiêu của chặng ALPR (ms).
ALPR_TARGET_MS = 500.0
# Các chặng cần thống kê, đúng thứ tự cột tính sẵn trong /api/traces.csv.
METRICS: tuple[tuple[str, str], ...] = (
    ("capture_ms", "capture"),
    ("alpr_ms", "alpr"),
    ("decide_ms", "decide"),
    ("barrier_ms", "barrier"),
    ("total_ms", "total"),
)
SUMMARY_COLUMNS = ("metric", "n", "p50_ms", "p95_ms", "mean_ms", "max_ms", "under_500ms_pct")


def fetch_rows(base_url: str, limit: int, timeout: float) -> list[dict[str, str]]:
    """Tải /api/traces.csv và trả danh sách dòng (đã bỏ BOM)."""
    url = f"{base_url.rstrip('/')}/api/traces.csv"
    response = requests.get(url, params={"limit": limit}, timeout=timeout)
    response.raise_for_status()
    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [row for row in reader if any((value or "").strip() for value in row.values())]


def row_number(row: dict[str, str], fallback: int) -> int:
    try:
        return int((row.get("id") or "").strip())
    except ValueError:
        return fallback


def chronological(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """API trả mới nhất trước; sắp theo id tăng dần để bỏ đúng các lượt đầu."""
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda pair: (row_number(pair[1], pair[0]), pair[0]))
    return [row for _index, row in indexed]


def metric_values(rows: list[dict[str, str]], metric: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = (row.get(metric) or "").strip()
        if not raw:
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


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


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "mean": None, "max": None}
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "mean": sum(values) / len(values),
        "max": max(values),
    }


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def write_summary(path: Path, stats: list[tuple[str, dict[str, Any]]], alpr_ratio: float | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(SUMMARY_COLUMNS)
        for metric, data in stats:
            ratio = fmt(alpr_ratio * 100.0) if metric == "alpr_ms" and alpr_ratio is not None else ""
            writer.writerow(
                [
                    metric,
                    data["n"],
                    fmt(data["p50"]),
                    fmt(data["p95"]),
                    fmt(data["mean"]),
                    fmt(data["max"]),
                    ratio,
                ]
            )


def print_markdown(stats: list[tuple[str, dict[str, Any]]], used: int, dropped: int) -> None:
    print("| Chặng | n | p50 (ms) | p95 (ms) | TB (ms) | Lớn nhất (ms) |")
    print("|---|---:|---:|---:|---:|---:|")
    for metric, data in stats:
        print(
            f"| {metric} | {data['n']} | {fmt(data['p50']) or '—'} | {fmt(data['p95']) or '—'} | "
            f"{fmt(data['mean']) or '—'} | {fmt(data['max']) or '—'} |"
        )
    print(f"\nSố lượt dùng: {used} (bỏ {dropped} lượt làm nóng)")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        rows = fetch_rows(args.url, args.limit, args.timeout)
    except requests.RequestException as exc:
        print(f"Không tải được {args.url.rstrip('/')}/api/traces.csv: {exc}")
        print("  (backend đã chạy chưa? thử: docker compose up -d backend)")
        return 1

    ordered = chronological(rows)
    dropped = min(args.warmup, len(ordered))
    used_rows = ordered[dropped:]

    print(f"Nguồn: {args.url.rstrip('/')}/api/traces.csv?limit={args.limit}")
    print(f"Tải về: {len(rows)} dòng | bỏ {dropped} lượt làm nóng | dùng {len(used_rows)}\n")

    if not used_rows:
        print("Không có lượt nào để thống kê (ít hơn số lượt làm nóng).")
        return 1

    stats: list[tuple[str, dict[str, Any]]] = [
        (metric, summarize(metric_values(used_rows, metric))) for metric, _label in METRICS
    ]
    print_markdown(stats, len(used_rows), dropped)

    alpr_values = metric_values(used_rows, "alpr_ms")
    alpr_ratio = (
        sum(1 for value in alpr_values if value < ALPR_TARGET_MS) / len(alpr_values)
        if alpr_values
        else None
    )
    if alpr_ratio is None:
        print(f"Tỉ lệ ALPR dưới {ALPR_TARGET_MS:.0f} ms: không có dữ liệu")
    else:
        under = sum(1 for value in alpr_values if value < ALPR_TARGET_MS)
        print(
            f"Tỉ lệ ALPR dưới {ALPR_TARGET_MS:.0f} ms: {alpr_ratio * 100:.1f} % "
            f"({under}/{len(alpr_values)})"
        )

    write_summary(args.csv, stats, alpr_ratio)
    print(f"\nĐã ghi bảng tổng hợp: {args.csv}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gọi /api/traces.csv, bỏ N lượt làm nóng, in bảng markdown p50/p95/TB/max "
            "cho từng chặng và ghi results/latency_summary.csv."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--url", default="http://localhost:8000", help="địa chỉ backend")
    parser.add_argument("--limit", type=int, default=100, help="số dòng lấy từ API")
    parser.add_argument(
        "--warmup",
        type=int,
        default=2,
        help="số lượt đầu bỏ đi (làm nóng; tính theo thứ tự thời gian)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("results/latency_summary.csv"),
        help="file CSV tổng hợp (đường dẫn tính từ thư mục đang chạy)",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="timeout gọi API (giây)")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit phải > 0")
    if args.warmup < 0:
        parser.error("--warmup phải >= 0")
    if args.timeout <= 0:
        parser.error("--timeout phải > 0")
    return args


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nĐã dừng.")
        raise SystemExit(130)
