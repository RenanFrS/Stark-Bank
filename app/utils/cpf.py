"""CPF generation and validation using the modulus 11 check digit rule."""

import random
import re

_DIGITS_ONLY = re.compile(r"\D")


def _check_digit(digits: list[int]) -> int:
    weight = len(digits) + 1
    total = sum(digit * (weight - index) for index, digit in enumerate(digits))
    remainder = total % 11
    return 0 if remainder < 2 else 11 - remainder


def strip(value: str) -> str:
    return _DIGITS_ONLY.sub("", value)


def is_valid(value: str) -> bool:
    digits_text = strip(value)
    if len(digits_text) != 11:
        return False
    if digits_text == digits_text[0] * 11:
        # Sequences such as 11111111111 pass the arithmetic but are invalid.
        return False

    digits = [int(character) for character in digits_text]
    if _check_digit(digits[:9]) != digits[9]:
        return False
    return _check_digit(digits[:10]) == digits[10]


def format_cpf(value: str) -> str:
    digits = strip(value)
    if len(digits) != 11:
        raise ValueError(f"Expected 11 digits, got {len(digits)}")
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def generate(rng: random.Random | None = None) -> str:
    """Return a syntactically valid, formatted CPF for sandbox use."""
    rng = rng or random
    while True:
        base = [rng.randint(0, 9) for _ in range(9)]
        if len(set(base)) == 1:
            continue
        base.append(_check_digit(base))
        base.append(_check_digit(base))
        candidate = "".join(str(digit) for digit in base)
        if is_valid(candidate):
            return format_cpf(candidate)
