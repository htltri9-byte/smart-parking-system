"""Thu thập ảnh mẫu biển số để đo chất lượng ALPR (dùng cho tools/bench_alpr.py).

Mỗi lần nhấn Enter: script gọi capture-agent (GET /capture) lấy khung hình mới nhất,
hỏi biển số thật của xe đang đặt trước camera và điều kiện chụp, rồi lưu:
    data/samples/sample_001.jpg
    data/samples/labels.csv        (filename,plate,note)

Phím tắt:
    Enter  chụp và ghi nhãn
    u      xoá ảnh + dòng nhãn vừa ghi (khi nhập sai)
    q      thoát

Chạy:
    python tools/collect_samples.py
    python tools/collect_samples.py --url http://localhost:8002 --dir data/samples
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Thiếu gói 'requests'. Cài: pip install -r tools/requirements.txt")

COLUMNS = ["filename", "plate", "note"]
SAMPLE_RE = re.compile(r"^sample_(\d+)\.(jpg|jpeg|png)$", re.IGNORECASE)


def ensure_csv(path: Path) -> None:
    """Tạo labels.csv kèm BOM để Excel mở đúng tiếng Việt."""
    if not path.exists():
        path.write_text("\ufeff" + ",".join(COLUMNS) + "\n", encoding="utf-8")


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [row for row in csv.DictReader(handle) if row.get("filename")]


def rewrite_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("\ufeff")
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def append_row(path: Path, filename: str, plate: str, note: str) -> None:
    with path.open("a", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow([filename, plate, note])


def next_index(directory: Path) -> int:
    highest = 0
    if directory.is_dir():
        for item in directory.iterdir():
            match = SAMPLE_RE.match(item.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


def capture_frame(url: str, timeout: float) -> bytes:
    response = requests.get(f"{url.rstrip('/')}/capture", timeout=timeout)
    if response.status_code != 200:
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = response.text[:120]
        raise RuntimeError(f"HTTP {response.status_code}: {detail}")
    if not response.content:
        raise RuntimeError("ảnh trả về rỗng")
    return response.content


def ask(prompt: str) -> str:
    """input() có chuẩn hoá 'q' để thoát; trả về chuỗi đã strip."""
    return input(prompt).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Thu ảnh mẫu + nhãn biển số từ capture-agent.")
    parser.add_argument("--url", default="http://localhost:8002", help="địa chỉ capture-agent")
    parser.add_argument("--dir", default="data/samples", type=Path, help="thư mục lưu ảnh và labels.csv")
    parser.add_argument("--timeout", type=float, default=10.0, help="timeout mỗi lần chụp (giây)")
    args = parser.parse_args(argv)

    directory: Path = args.dir
    directory.mkdir(parents=True, exist_ok=True)
    labels_path = directory / "labels.csv"
    ensure_csv(labels_path)

    collected = 0
    print(f"Capture : {args.url}/capture")
    print(f"Lưu vào : {directory.resolve()}")
    print("Enter = chụp | u = xoá ảnh vừa thu | q = thoát")
    print("Biển số gõ không dấu, ví dụ 30A-12345 (để trống nếu ảnh không có biển).\n")

    while True:
        try:
            command = ask("Nhấn Enter để chụp, 'u' xoá ảnh cuối, 'q' thoát: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        lowered = command.lower()
        if lowered == "q":
            break
        if lowered == "u":
            rows = read_rows(labels_path)
            if not rows:
                print("  Chưa có dòng nhãn nào để xoá.\n")
                continue
            removed = rows.pop()
            image_path = directory / removed["filename"]
            if image_path.exists():
                image_path.unlink()
            rewrite_rows(labels_path, rows)
            collected = max(0, collected - 1)
            print(f"  Đã xoá {removed['filename']} ({removed.get('plate') or 'không biển'}).\n")
            continue
        if command:
            print("  Chỉ nhận Enter, 'u' hoặc 'q'.\n")
            continue

        # ---- chụp ----
        try:
            image_bytes = capture_frame(args.url, args.timeout)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"  Không lấy được ảnh: {exc}\n  (capture-agent đã chạy chưa? camera đã cắm chưa?)\n")
            continue

        index = next_index(directory)
        filename = f"sample_{index:03d}.jpg"
        print(f"  Đã chụp ({len(image_bytes) // 1024} KB) -> {filename}")

        # ---- nhãn ----
        try:
            plate = ask("  Biển số thật (Enter = không có biển, 'q' = bỏ ảnh này): ").upper()
            if plate.lower() == "q":
                print("  Bỏ ảnh vừa chụp.\n")
                continue
            note = ask("  Điều kiện (sáng/tối/lệch/...): ")
            if note.lower() == "q":
                print("  Bỏ ảnh vừa chụp.\n")
                continue
        except (EOFError, KeyboardInterrupt):
            print("\n  Bỏ ảnh vừa chụp.")
            break

        (directory / filename).write_bytes(image_bytes)
        append_row(labels_path, filename, plate, note)
        collected += 1
        print(f"  Đã lưu {filename}: biển {plate or '(không có)'} | {note or '(không ghi)'}\n")

    total = len(read_rows(labels_path))
    print(f"\nĐã thu được {collected} ảnh trong phiên này. Tổng trong {directory}: {total} ảnh.")
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
