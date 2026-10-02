from __future__ import annotations

import hashlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

MISSING = {"", "na", "n/a", "n.d.", "nd", "n/d", "null", "none", "nan", "-", "--", "…", "..."}


def normalize_column_name(value: Any) -> str:
    text = str(value).strip().lower()
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "column"


def normalize_text(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in MISSING:
        return None
    return re.sub(r"\s+", " ", text)


def normalize_number(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return value
    text = str(value).strip().replace("%", "")
    if not text or text.lower() in MISSING:
        return None
    # Colombian/Spanish decimal convention: 1.234,56 -> 1234.56
    if "," in text and "." in text and text.rfind(",") > text.rfind("."):
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = Decimal(text)
        return float(number) if number.as_tuple().exponent else int(number)
    except InvalidOperation:
        return value


def clean_row(row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    cleaned: dict[str, Any] = {}
    errors: list[str] = []
    for key, value in row.items():
        normalized_key = normalize_column_name(key)
        value = normalize_text(value)
        if normalized_key in {"year", "ano", "anio", "periodo", "period"} and value is not None:
            match = re.search(r"(?:19|20)\d{2}", str(value))
            if match:
                value = int(match.group(0))
        elif isinstance(value, str) and re.fullmatch(r"-?\d+(?:[.,]\d+)?%?", value):
            value = normalize_number(value)
        cleaned[normalized_key] = value

    year = cleaned.get("year") or cleaned.get("anio") or cleaned.get("ano")
    if year is not None and not (1900 <= int(year) <= 2200):
        errors.append("INVALID_YEAR")
    if not any(value is not None for value in cleaned.values()):
        errors.append("EMPTY_ROW")
    for key, value in cleaned.items():
        if value is not None and any(token in key for token in ("ipm", "porcentaje", "incidencia", "privacion")):
            try:
                number = float(value)
                if number < 0 or number > 100:
                    errors.append(f"OUT_OF_RANGE:{key}")
            except (TypeError, ValueError):
                pass
    return cleaned, errors


def row_hash(row: dict[str, Any]) -> str:
    canonical = "|".join(f"{key}={row[key]!r}" for key in sorted(row))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
