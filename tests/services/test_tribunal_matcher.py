"""Tests for tribunal name normalization and matching helpers."""

import pytest

from app.services.tribunal_matcher import match_tribunal, normalize_court_name


class TestNormalizeCourtName:
    def test_masculine_ordinal_indicator(self):
        # º (U+00BA) must become "o"
        assert normalize_court_name("1º Juzgado Civil de Santiago") == "1o juzgado civil de santiago"

    def test_degree_symbol(self):
        # ° (U+00B0) must become "o"
        assert normalize_court_name("1° Juzgado Civil de Santiago") == "1o juzgado civil de santiago"

    def test_ordinal_with_preceding_space(self):
        # "1 º" — space before º is consumed by the regex \s*[°º]
        assert normalize_court_name("1 º Juzgado Civil de Santiago") == "1o juzgado civil de santiago"

    def test_already_latin_o(self):
        # "1o" stays "1o"
        assert normalize_court_name("1o Juzgado Civil de Santiago") == "1o juzgado civil de santiago"

    def test_accents_stripped(self):
        assert normalize_court_name("Copiapó") == "copiapo"

    def test_tilde_n_stripped(self):
        # ñ → n
        assert normalize_court_name("Rancagua") == "rancagua"  # sanity
        assert normalize_court_name("España") == "espana"

    def test_uppercase_lowercased(self):
        assert normalize_court_name("1o JUZGADO CIVIL DE SANTIAGO") == "1o juzgado civil de santiago"

    def test_whitespace_collapsed(self):
        assert normalize_court_name("  1o   Juzgado  Civil  ") == "1o juzgado civil"

    def test_trailing_punctuation_stripped(self):
        assert normalize_court_name("Tribunal Civil.") == "tribunal civil"

    def test_empty_string(self):
        assert normalize_court_name("") == ""

    def test_whitespace_only(self):
        assert normalize_court_name("   ") == ""

    def test_high_ordinal_number(self):
        assert normalize_court_name("29º Juzgado Civil de Santiago") == "29o juzgado civil de santiago"


class TestMatchTribunal:
    DIRECTORY = [
        {"corte": 90, "tribunal": 259, "name": "1° Juzgado Civil de Santiago"},
        {"corte": 90, "tribunal": 260, "name": "2° Juzgado Civil de Santiago"},
        {"corte": 90, "tribunal": 287, "name": "29° Juzgado Civil de Santiago"},
        {"corte": 20, "tribunal": 100, "name": "Juzgado de Letras de Copiapó"},
    ]

    def test_matches_masculine_ordinal_against_degree_symbol(self):
        # "1º" in DB vs "1°" from PJUD
        result = match_tribunal("1º Juzgado Civil de Santiago", self.DIRECTORY)
        assert result == {"corte": 90, "tribunal": 259, "name": "1° Juzgado Civil de Santiago"}

    def test_matches_uppercase_input(self):
        result = match_tribunal("1o JUZGADO CIVIL DE SANTIAGO", self.DIRECTORY)
        assert result == {"corte": 90, "tribunal": 259, "name": "1° Juzgado Civil de Santiago"}

    def test_matches_accented_vs_unaccented(self):
        # DB has "Copiapo" (without accent), PJUD has "Copiapó"
        result = match_tribunal("Juzgado de Letras de Copiapo", self.DIRECTORY)
        assert result == {"corte": 20, "tribunal": 100, "name": "Juzgado de Letras de Copiapó"}

    def test_reverse_accented_vs_unaccented(self):
        # DB has "Copiapó" (with accent), PJUD has no accent
        result = match_tribunal("Juzgado de Letras de Copiapó", self.DIRECTORY)
        assert result == {"corte": 20, "tribunal": 100, "name": "Juzgado de Letras de Copiapó"}

    def test_no_match_returns_none(self):
        result = match_tribunal("Tribunal Inexistente", self.DIRECTORY)
        assert result is None

    def test_empty_name_returns_none(self):
        result = match_tribunal("", self.DIRECTORY)
        assert result is None

    def test_whitespace_only_returns_none(self):
        result = match_tribunal("   ", self.DIRECTORY)
        assert result is None

    def test_empty_directory_returns_none(self):
        result = match_tribunal("1º Juzgado Civil de Santiago", [])
        assert result is None

    def test_no_substring_match(self):
        # Partial name must NOT match — substring guessing is explicitly forbidden
        result = match_tribunal("Juzgado Civil de Santiago", self.DIRECTORY)
        assert result is None

    def test_high_ordinal_match(self):
        result = match_tribunal("29º Juzgado Civil de Santiago", self.DIRECTORY)
        assert result == {"corte": 90, "tribunal": 287, "name": "29° Juzgado Civil de Santiago"}
