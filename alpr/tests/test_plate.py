"""Kiểm thử app/plate.py - chuẩn hoá biển số Việt Nam.

Chạy:  cd alpr && python -m pytest tests -q
(không cần PYTHONPATH: `python -m pytest` tự thêm thư mục hiện tại vào sys.path)
"""
from __future__ import annotations

import pytest

from app.ocr import TextLine, merge_lines
from app.plate import format_plate, is_valid, normalize


def plate_of(raw: str) -> str | None:
    """Chỉ lấy biển số, bỏ qua cờ valid (dùng cho test ngắn gọn)."""
    return format_plate(raw)[0]


# ---- 1. Ô tô: 2 số + 1-2 chữ + 4-5 số ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30A-12345", "30A-12345"),      # ô tô 1 chữ, 5 số
        ("30AB-12345", "30AB-12345"),    # ô tô 2 chữ
        ("51F-1234", "51F-1234"),        # ô tô 1 chữ, 4 số (biển cũ)
        ("30A12345", "30A-12345"),       # không có gạch nối
        ("30a-12345", "30A-12345"),      # chữ thường
    ],
)
def test_car_plates(raw: str, expected: str) -> None:
    assert plate_of(raw) == expected


# ---- 2. Xe máy: 2 số + 1 chữ + 1 số + 5 số, đọc thành 2 dòng ----
def test_moto_two_lines_merged_top_then_bottom() -> None:
    """Biển xe máy 2 dòng: dòng trên (59X2) ghép trước dòng dưới (12345) theo y."""
    lines = [
        TextLine("12345", 0.95, x1=10, y1=60, x2=200, y2=100),
        TextLine("59X2", 0.93, x1=10, y1=10, x2=160, y2=50),
    ]
    text, _confidence = merge_lines(lines)
    assert text == "59X212345"
    assert plate_of(text) == "59X2-12345"


def test_moto_joined_string() -> None:
    assert plate_of("59X212345") == "59X2-12345"


def test_moto_joins_left_to_right_on_same_line() -> None:
    """Biển 1 dòng bị OCR tách 2 đoạn: nối theo trục x, không theo thứ tự trả về."""
    lines = [
        TextLine("12345", 0.9, x1=200, y1=10, x2=400, y2=60),
        TextLine("30A", 0.9, x1=40, y1=10, x2=150, y2=60),
    ]
    text, _confidence = merge_lines(lines)
    assert text == "30A12345"
    assert plate_of(text) == "30A-12345"


# ---- 3. Sửa nhầm lẫn theo vị trí ----
@pytest.mark.parametrize(
    ("raw", "expected", "ghi_chu"),
    [
        ("3OA-12345", "30A-12345", "O -> 0 ở vị trí số"),
        ("30A-1234O", "30A-12340", "O -> 0 ở số cuối"),
        ("30A-1234B", "30A-12348", "B -> 8 ở vị trí số"),
        ("308-12345", "30B-12345", "8 -> B ở vị trí chữ"),
        ("30A-1234S", "30A-12345", "S -> 5 ở vị trí số"),
        ("59X2-1234S", "59X2-12345", "S -> 5, biển xe máy"),
        ("30A-I2345", "30A-12345", "I -> 1 ở vị trí số"),
        ("3OA-I234Z", "30A-12342", "nhiều lỗi cùng lúc: O, I, Z"),
    ],
)
def test_position_based_corrections(raw: str, expected: str, ghi_chu: str) -> None:
    assert plate_of(raw) == expected, ghi_chu


# ---- 4. Dấu chấm, khoảng trắng, gạch ngang ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30A.123.45", "30A-12345"),
        ("30-A 123.45", "30A-12345"),
        ("  30A - 12345  ", "30A-12345"),
        ("59X2.12345", "59X2-12345"),
    ],
)
def test_separators_and_spaces(raw: str, expected: str) -> None:
    assert plate_of(raw) == expected


# ---- 5. Chuỗi rác / quá ngắn -> None ----
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "12",
        "30A",
        "30A-123",
        "ABCDEF",
        "HELLO WORLD",
        "30J-12345",      # J không có trong danh sách chữ series hợp lệ
        "---",
        "abcxyz",         # không có chữ số nào
    ],
)
def test_junk_and_too_short_return_none(raw: str) -> None:
    plate, valid = format_plate(raw)
    assert plate is None
    assert valid is False


# ---- 6. Bỏ qua chữ thừa quanh biển (tên tỉnh, logo...) ----
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BIEN SO 30A 12345", "30A-12345"),
        ("HCM 51F-67890", "51F-67890"),
    ],
)
def test_extra_text_around_plate(raw: str, expected: str) -> None:
    assert plate_of(raw) == expected


# ---- 7. Hàm phụ: normalize / is_valid ----
def test_normalize_strips_everything_but_alnum() -> None:
    assert normalize(" 30a - 12.345 ") == "30A12345"
    assert normalize("59X2 / 12345") == "59X212345"


@pytest.mark.parametrize("raw", ["30A-12345", "51F-1234", "59X2-12345", "30AB-12345"])
def test_valid_flag_true_for_real_plates(raw: str) -> None:
    plate, valid = format_plate(raw)
    assert valid is True
    assert plate == raw
    assert is_valid(plate) is True


@pytest.mark.parametrize("raw", ["", "30A-123", "ABC", "30J-12345"])
def test_is_valid_false_for_garbage(raw: str) -> None:
    assert is_valid(raw) is False
