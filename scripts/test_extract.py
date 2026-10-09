"""
Verifica que el filtro de prosa funciona en un PDF real.

Uso:
    python scripts/test_extract.py
    python scripts/test_extract.py https://otra-url.pdf
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import httpx
import fitz
from app.extract.pdf import extract_pdf, chunk_pages, debug_extract


DEFAULT_URL = "https://www.dane.gov.co/files/operaciones/PM/pres-PMultidimensional-2025.pdf"


async def main(url: str) -> None:
    print(f"Descargando: {url}")
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        pdf = r.content
    print(f"Descargado: {len(pdf)} bytes")
    print()

    print("Diagnóstico de descartes:")
    info = debug_extract(pdf)
    print(f"  Tamaño de fuente del cuerpo: {info['body_font_size']}")
    print(f"  Bloques totales:             {info['blocks']}")
    for reason, count in sorted(info["reasons"].items(), key=lambda x: -x[1]):
        print(f"  {reason:<28} {count}")
    print("  Ejemplos de not_text_block:")
    for s in info["samples"]:
        print(f"    - {s}")
    print()

    print("Spans de la página 4 (diagnóstico de palabras pegadas):")
    doc = fitz.open(stream=pdf, filetype="pdf")
    for blk in doc[3].get_text("dict")["blocks"]:
        if blk.get("type") == 0:
            for line in blk["lines"]:
                print([s["text"] for s in line["spans"]])
    doc.close()
    print()

    pages, total = extract_pdf(pdf)
    for p in pages:
        if p["page_number"] == 4:
            i = p["content"].find("cuales")
            print(repr(p["content"][i:i + 40]))
    print()
    print(f"Páginas totales:     {total}")
    print(f"Páginas con prosa:   {len(pages)}")
    print(f"Páginas descartadas: {total - len(pages)}")
    print()

    chunks = chunk_pages(pages)
    print(f"Chunks generados: {len(chunks)}")
    print()

    if not chunks:
        print("⚠️  No se generó ningún chunk. El filtro puede estar muy agresivo.")
        return

    print("=" * 70)
    print("Primeros 3 chunks (deben ser prosa, no tablas):")
    print("=" * 70)
    for i, ch in enumerate(chunks[:3]):
        page = ch["page_number"]
        content = ch["content"]
        preview = content[:400]
        print()
        print(f"--- Chunk {i} (página {page}, {len(content)} chars) ---")
        print(preview)
        if len(content) > 400:
            print("...")
        print()

    # Verificación heurística: ¿cuántos chunks parecen tablas?
    def looks_like_table(text: str) -> bool:
        import re
        # 5+ números seguidos separados por espacios
        return bool(re.search(r"(?:\b\d+[.,]?\d*\b\s+){5,}", text))

    table_chunks = sum(1 for ch in chunks if looks_like_table(ch["content"]))
    print("=" * 70)
    print(f"Chunks que parecen tablas: {table_chunks} / {len(chunks)}")
    if table_chunks == 0:
        print("✅ Filtro OK — ningún chunk parece tabla numérica.")
    else:
        print(f"⚠️  {table_chunks} chunks aún contienen tablas. Sube PDF_MIN_LETTER_RATIO.")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    asyncio.run(main(url))