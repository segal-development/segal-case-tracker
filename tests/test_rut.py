"""Tests for RUT normalization utilities.

S2-T1: normalize_rut produces consistent canonical output for all input formats.
The canonical form is '{digits}-{verif}' (no dots, uppercase verification digit,
hyphen separator, verification digit always preserved).
"""

import pytest

from app.utils.rut import normalize_rut


class TestNormalizeRut:
    """Tests for normalize_rut canonical normalization."""

    def test_already_canonical(self):
        assert normalize_rut("12345678-9") == "12345678-9"

    def test_with_dots(self):
        assert normalize_rut("12.345.678-9") == "12345678-9"

    def test_with_leading_trailing_spaces(self):
        assert normalize_rut(" 12345678-9 ") == "12345678-9"

    def test_lowercase_k_becomes_uppercase(self):
        assert normalize_rut("12345678-k") == "12345678-K"

    def test_dots_and_lowercase_k(self):
        assert normalize_rut("12.345.678-k") == "12345678-K"

    def test_clave_unica_format_preserved(self):
        # Clave Única input — verification digit must NOT be stripped
        assert normalize_rut("16021492-9") == "16021492-9"

    def test_captcha_user_input_with_dots(self):
        # Captcha users often type with dots
        assert normalize_rut("16.021.492-9") == "16021492-9"

    def test_idempotent(self):
        # normalize_rut(normalize_rut(x)) == normalize_rut(x)
        once = normalize_rut("12.345.678-9")
        twice = normalize_rut(once)
        assert once == twice == "12345678-9"

    def test_no_hyphen_input_gets_hyphen(self):
        # Input without hyphen: last char is verification digit
        assert normalize_rut("123456789") == "12345678-9"
