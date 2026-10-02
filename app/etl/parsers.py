from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterator


def _decode_csv(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def parse_csv(raw: bytes) -> Iterator[dict[str, Any]]:
    text = _decode_csv(raw)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    yield from reader


def parse_json(raw: bytes) -> Iterator[dict[str, Any]]:
    data = json.loads(_decode_csv(raw))
    if isinstance(data, list):
        for item in data:
            yield item if isinstance(item, dict) else {"value": item}
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list) and all(isinstance(x, dict) for x in value):
                yield from value
            else:
                yield {"key": key, "value": value}


def parse_excel(raw: bytes, suffix: str) -> Iterator[dict[str, Any]]:
    import pandas as pd
    engine = "xlrd" if suffix == ".xls" else "openpyxl"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp.flush()
        os.fsync(tmp.fileno())
        path = tmp.name

    workbook = None
    try:
        workbook = pd.ExcelFile(path, engine=engine)
        for sheet in workbook.sheet_names:
            frame = pd.read_excel(path, sheet_name=sheet, dtype=object, engine=engine)
            frame = frame.dropna(how="all")
            for record in frame.to_dict(orient="records"):
                record["_sheet"] = sheet
                yield record
    finally:
        # Cierra el workbook antes de intentar borrar el archivo.
        if workbook is not None:
            try:
                workbook.close()
            except Exception:  # noqa: BLE001
                pass
        # En Windows, el archivo puede seguir bloqueado unos ms después
        # de cerrar los handles. Reintenta con backoff.
        for _ in range(10):
            try:
                os.unlink(path)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                time.sleep(0.1)


def parse_bytes(raw: bytes, resource_type: str, file_name: str) -> Iterator[dict[str, Any]]:
    suffix = Path(file_name).suffix.lower()
    if resource_type == "csv" or suffix == ".csv":
        yield from parse_csv(raw)
    elif resource_type in {"xlsx", "xls"} or suffix in {".xlsx", ".xls", ".xlsm"}:
        yield from parse_excel(raw, suffix if suffix in {".xlsx", ".xls"} else ".xlsx")
    elif resource_type == "json" or suffix == ".json":
        yield from parse_json(raw)
    elif resource_type == "zip" or suffix == ".zip":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                if info.is_dir() or info.file_size > 250 * 1024 * 1024:
                    continue
                member = Path(info.filename).name
                member_type = {".csv": "csv", ".xlsx": "xlsx", ".xls": "xls", ".json": "json"}.get(Path(member).suffix.lower())
                if member_type:
                    yield from parse_bytes(archive.read(info), member_type, member)
    else:
        return