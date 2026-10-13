"""
Pruebas unitarias (sin base de datos) del mapeo de dashboard_02:
cada hogar del microdato debe sobrevivir a la deduplicación aunque
comparta perfil con otro, y debe conservar su factor de expansión.
"""

from app.cleaner.household_mapper import row_to_household
from app.cleaner.schema import DASHBOARD_02_HOGARES, PRIVACION_COLUMNS
from app.cleaner.table_mapper import _dedupe_rows


def _household_row(
    directorio: int,
    secuencia_p: int = 1,
    secuencia_encuesta: int = 1,
    fex_c: float = 300.0,
) -> dict:

    row = {
        "anio": 2025,
        "directorio": directorio,
        "secuencia_p": secuencia_p,
        "secuencia_encuesta": secuencia_encuesta,
        "region": 6,
        "departamento": 5,
        "personas_hogar": 3,
        "ipm": 0.0,
        "pobre": False,
        "fex_c": fex_c,
    }

    row.update({column: False for column in PRIVACION_COLUMNS})

    return row


def test_dedupe_keeps_distinct_households_with_identical_profile():

    rows = [
        _household_row(1),
        _household_row(2),
        _household_row(2, secuencia_p=2),
        _household_row(2, secuencia_p=2, secuencia_encuesta=3),
    ]

    assert len(_dedupe_rows(rows, DASHBOARD_02_HOGARES)) == 4


def test_dedupe_drops_only_repeated_household_id():

    rows = [_household_row(1), _household_row(1)]

    assert len(_dedupe_rows(rows, DASHBOARD_02_HOGARES)) == 1


def test_row_to_household_carries_expansion_factor_and_survey_id():

    household = row_to_household(
        _household_row(8369550, secuencia_p=2, secuencia_encuesta=4, fex_c=472.13)
    )

    assert household["expansion_factor"] == 472.13
    assert household["survey_household_id"] == "8369550-2-4"


def test_csv_reader_handles_semicolon_and_bom(tmp_path):

    from app.cleaner.block_extractor import extract_blocks_from_csv

    path = tmp_path / "HOGARES (DEPARTAMENTAL) 2018.csv"
    path.write_bytes("﻿DIRECTORIO;FEX_C\n1;2.5\n".encode("utf-8"))

    block = extract_blocks_from_csv(str(path))[0]

    assert block.headers == ["DIRECTORIO", "FEX_C"]
