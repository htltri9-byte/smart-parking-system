"""Chuẩn hoá biển số Việt Nam.

Quy tắc (theo yêu cầu đồ án):
- Chỉ giữ A-Z và 0-9.
- Ô tô:  2 số + 1-2 chữ + 4-5 số      ví dụ 30A-12345, 51F-1234, 30AB-12345
- Xe máy: 2 số + 1 chữ + 1 số + 5 số  ví dụ 59X2-12345
- Sửa nhầm theo vị trí:
    vị trí số:  O->0, I->1, B->8, S->5, Z->2
    vị trí chữ: 8->B, 0->D, 5->S
- Chữ series hợp lệ: A B C D E F G H K L M N P R S T U V X Y Z
"""
from __future__ import annotations

import re

SERIES_LETTERS = frozenset("ABCDEFGHKLMNPRSTUVXYZ")

# Sửa ký tự ở vị trí phải là SỐ
DIGIT_FIX = {"O": "0", "I": "1", "B": "8", "S": "5", "Z": "2", "D": "0", "Q": "0"}
# Sửa ký tự ở vị trí phải là CHỮ
LETTER_FIX = {"8": "B", "0": "D", "5": "S", "6": "G", "1": "T", "2": "Z"}

# (số chữ series, số chữ số sau series) - ô tô
CAR_PATTERNS: tuple[tuple[int, int], ...] = ((1, 5), (2, 5), (1, 4), (2, 4))
# xe máy: 2 số + 1 chữ + 1 số + 5 số
MOTO_EXTRA_DIGIT = 5

_KEEP = re.compile(r"[^A-Z0-9]")


def normalize(raw: str) -> str:
    """Bỏ mọi ký tự ngoài A-Z, 0-9 (khoảng trắng, dấu chấm, gạch nối...)."""
    return _KEEP.sub("", (raw or "").upper())


def _coerce(text: str, letters: int, digits_after: int, kind: str) -> tuple[str, int] | None:
    """Sửa ký tự nhầm theo vị trí. Trả (biển dạng "30A-12345", số lần phải sửa) hoặc None."""
    if kind == "car":
        expected = 2 + letters + digits_after
    else:  # moto: 2 số + letters chữ + 1 số + digits_after
        expected = 2 + letters + 1 + digits_after
    if len(text) != expected:
        return None

    fixes = 0
    out: list[str] = []
    for index, char in enumerate(text):
        if kind == "car":
            must_be_letter = 2 <= index < 2 + letters
        else:
            must_be_letter = 2 <= index < 2 + letters

        if must_be_letter:
            fixed = char if char in SERIES_LETTERS else LETTER_FIX.get(char, char)
            if fixed not in SERIES_LETTERS:
                return None
        else:
            fixed = char if char.isdigit() else DIGIT_FIX.get(char, char)
            if not fixed.isdigit():
                return None

        if fixed != char:
            fixes += 1
        out.append(fixed)

    text_fixed = "".join(out)
    prefix_end = 2 + letters + (1 if kind == "moto" else 0)
    return f"{text_fixed[:prefix_end]}-{text_fixed[prefix_end:]}", fixes


def format_plate(raw: str) -> tuple[str | None, bool]:
    """Chuẩn hoá chữ OCR thô thành biển số.

    Trả (biển số, hợp_lệ). Không nhận ra mẫu nào -> (None, False).
    """
    text = normalize(raw)
    if not text:
        return None, False

    # (không phủ hết chuỗi, số lần phải sửa, -độ dài, vị trí bắt đầu, biển số)
    candidates: list[tuple[int, int, int, int, str]] = []
    lengths = {2 + letters + digits for letters, digits in CAR_PATTERNS}
    lengths.add(2 + 1 + 1 + MOTO_EXTRA_DIGIT)

    # Thử mọi cửa sổ có độ dài hợp lệ để bỏ qua chữ thừa OCR đọc được (tên tỉnh, logo...).
    for length in sorted(lengths):
        if length > len(text):
            continue
        for start in range(0, len(text) - length + 1):
            window = text[start : start + length]
            results = [
                _coerce(window, letters, digits_after, "car")
                for letters, digits_after in CAR_PATTERNS
            ]
            results.append(_coerce(window, 1, MOTO_EXTRA_DIGIT, "moto"))
            for result in results:
                if result is not None:
                    covers_all = start == 0 and length == len(text)
                    candidates.append(
                        (0 if covers_all else 1, result[1], -length, start, result[0])
                    )

    if not candidates:
        return None, False

    # Ưu tiên: cửa sổ phủ trọn chuỗi OCR (cả biển) -> ít phải sửa nhất -> dài nhất.
    # Nhờ vậy "30A-1234S" thành "30A-12345" chứ không bị cắt thành "30A-1234";
    # chữ thừa quanh biển ("BIEN SO 30A 12345") vẫn được xử lý bằng cửa sổ con.
    candidates.sort()
    return candidates[0][4], True


def is_valid(plate: str | None) -> bool:
    """Kiểm tra một biển đã chuẩn hoá (dạng "30A-12345")."""
    if not plate:
        return False
    body = normalize(plate)
    return format_plate(body)[0] == plate
