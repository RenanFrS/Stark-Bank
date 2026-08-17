import random

import pytest

from app.utils import cpf


def test_known_valid_cpf_passes():
    assert cpf.is_valid("529.982.247-25")


def test_wrong_check_digit_fails():
    assert not cpf.is_valid("529.982.247-24")


def test_repeated_digits_are_rejected():
    # Arithmetically consistent but not a real CPF.
    assert not cpf.is_valid("111.111.111-11")


def test_short_value_is_rejected():
    assert not cpf.is_valid("123")


def test_generated_cpfs_are_always_valid():
    rng = random.Random(1234)
    for _ in range(500):
        assert cpf.is_valid(cpf.generate(rng))


def test_generate_returns_formatted_value():
    value = cpf.generate(random.Random(7))
    assert len(value) == 14
    assert value[3] == "." and value[7] == "." and value[11] == "-"


def test_format_rejects_wrong_length():
    with pytest.raises(ValueError):
        cpf.format_cpf("123")
