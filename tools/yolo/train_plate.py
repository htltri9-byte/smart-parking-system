"""Huấn luyện YOLOv8n phát hiện vùng biển số (một lớp `plate`).

Chạy được trên Google Colab (GPU T4) và trên máy cá nhân. Cần `ultralytics`
(không có trong tools/requirements.txt vì rất nặng):

    pip install ultralytics

Colab:
    from google.colab import drive; drive.mount('/content/drive')
    !pip install -q ultralytics
    !python tools/yolo/train_plate.py \\
        --data /content/drive/MyDrive/plate_dataset/dataset.yaml \\
        --epochs 100 --imgsz 640 --batch 16 \\
        --out /content/drive/MyDrive/plate_dataset/best.pt

Máy Windows (không GPU vẫn chạy, chậm):
    python tools/yolo/train_plate.py --data dataset/dataset.yaml --epochs 30 --batch 8 --workers 0

Ra file `best.pt` ở `--out`; log và biểu đồ ở `runs/plate_detect/train/`.
Hướng dẫn gán nhãn và cấu trúc dataset: tools/yolo/README.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def default_workers() -> int:
    """Windows dễ lỗi đa tiến trình khi train -> mặc định 0 (đọc dữ liệu trong tiến trình chính)."""
    return 0 if os.name == "nt" else 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Huấn luyện YOLOv8n một lớp 'plate' và xuất best.pt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--data", required=True, type=Path, help="đường dẫn dataset.yaml")
    parser.add_argument("--model", default="yolov8n.pt", help="trọng số khởi đầu (tự tải nếu chưa có)")
    parser.add_argument("--epochs", type=int, default=100, help="số epoch")
    parser.add_argument("--imgsz", type=int, default=640, help="kích thước ảnh đầu vào")
    parser.add_argument("--batch", type=int, default=16, help="batch size (giảm nếu hết VRAM)")
    parser.add_argument("--device", default="", help='"" = tự chọn, "cpu", "0", "0,1"')
    parser.add_argument("--workers", type=int, default=default_workers(), help="số tiến trình đọc dữ liệu")
    parser.add_argument("--patience", type=int, default=20, help="dừng sớm sau N epoch không cải thiện")
    parser.add_argument("--seed", type=int, default=0, help="seed để tái lập kết quả")
    parser.add_argument("--project", default="runs/plate_detect", help="thư mục chứa lần chạy")
    parser.add_argument("--name", default="train", help="tên lần chạy trong --project")
    parser.add_argument("--out", type=Path, default=Path("best.pt"), help="nơi chép best.pt sau khi train")
    args = parser.parse_args(argv)
    if args.epochs < 1:
        parser.error("--epochs phải >= 1")
    if args.imgsz < 128:
        parser.error("--imgsz phải >= 128")
    if args.batch < 1:
        parser.error("--batch phải >= 1")
    return args


def check_dataset(path: Path) -> None:
    """Kiểm tra sớm để không mất 10 phút train rồi mới báo thiếu file."""
    if not path.is_file():
        raise SystemExit(
            f"Không thấy {path}. Cần file dataset.yaml (xem tools/yolo/README.md mục 3)."
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    if "names" not in text:
        raise SystemExit(f"{path} thiếu khai báo 'names' (phải có 0: plate).")
    if "train" not in text:
        raise SystemExit(f"{path} thiếu khai báo 'train'.")


def import_ultralytics():
    try:
        from ultralytics import YOLO  # noqa: PLC0415 - import nặng, chỉ khi train
    except ImportError:  # pragma: no cover - phụ thuộc môi trường
        print(
            "Chưa cài 'ultralytics'. Cài bằng: pip install ultralytics\n"
            "(trên Colab: !pip install -q ultralytics, nhớ chọn Runtime > GPU)",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    return YOLO


def copy_weights(source: Path, destination: Path) -> Path:
    destination = destination.expanduser()
    if destination.suffix != ".pt":
        destination = destination / "best.pt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.resolve()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    check_dataset(args.data)
    YOLO = import_ultralytics()

    out_path = args.out
    print(f"Dataset : {args.data.resolve()}")
    print(f"Model   : {args.model}")
    print(f"Train   : epochs={args.epochs} imgsz={args.imgsz} batch={args.batch} "
          f"device={args.device or 'auto'} workers={args.workers}")
    print(f"Log     : {Path(args.project) / args.name}\n")

    model = YOLO(args.model)
    train_args: dict[str, object] = {
        "data": str(args.data),
        "epochs": args.epochs,
        "imgsz": args.imgsz,
        "batch": args.batch,
        "patience": args.patience,
        "seed": args.seed,
        "workers": args.workers,
        "project": args.project,
        "name": args.name,
        "exist_ok": True,
    }
    if args.device:
        train_args["device"] = args.device

    results = model.train(**train_args)

    save_dir = Path(getattr(results, "save_dir", Path(args.project) / args.name))
    best = save_dir / "weights" / "best.pt"
    if not best.is_file():
        raise SystemExit(f"Train xong nhưng không thấy {best}. Xem log trong {save_dir}.")

    final_path = copy_weights(best, out_path)

    metrics = getattr(results, "results_dict", {}) or {}
    def metric(*names: str) -> str:
        for key in names:
            value = metrics.get(key)
            if isinstance(value, (int, float)):
                return f"{value:.4f}"
        return "—"

    print("\n=== Kết quả train ===")
    print(f"mAP50      : {metric('metrics/mAP50(B)')}")
    print(f"mAP50-95   : {metric('metrics/mAP50-95(B)')}")
    print(f"precision  : {metric('metrics/precision(B)')}")
    print(f"recall     : {metric('metrics/recall(B)')}")
    print(f"Biểu đồ    : {save_dir / 'results.png'}")
    print(f"best.pt    : {final_path}")

    if metric("metrics/mAP50(B)") == "—":
        print("Không đọc được mAP từ results_dict; xem results.csv trong thư mục log.")

    print("\nBước tiếp theo:")
    print(f"  yolo predict model={final_path} source=dataset/images/val imgsz={args.imgsz} conf=0.25 save=True")
    print(f"  python tools/yolo/eval_detector.py --dir anh-mau --weights {final_path} --roi <ROI đang dùng>")
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
