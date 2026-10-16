from pathlib import Path

import pytest

from app.cleaner.departmental_annex import norm, parse_annex, slug


ANNEX_2025 = Path("downloads/143c62b9-182c-4784-94a4-a1fb86f6b822/anex-PMultidimensional-Departamental-2025.xlsx")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Bogotá D.C.", "bogota d c"),
        ("San Andrés, Providencia y Santa Catalina", "san andres providencia y santa catalina"),
        ("  Antioquia  ", "antioquia"),
    ],
)
def test_norm_removes_accents_case_and_punctuation(value, expected):
    assert norm(value) == expected


def test_slug_produces_indicator_code_within_limit():
    code = slug("Sin acceso a fuente de agua mejorada")

    assert code == "SIN_ACCESO_A_FUENTE_DE_AGUA_MEJORADA"
    assert len(slug("x" * 200)) <= 50


@pytest.mark.skipif(not ANNEX_2025.exists(), reason="anexo 2025 no disponible en downloads/")
def test_parse_annex_2025_row_counts():
    rows, sex_mismatches = parse_annex(ANNEX_2025)

    assert len(rows) == 14656
    assert len({r[0] for r in rows}) == 33
    assert len(sex_mismatches) == 16
    assert {m[0] for m in sex_mismatches} == {2025}
