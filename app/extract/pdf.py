"""
Extracción de texto de PDFs y chunking.

Los PDFs del DANE (y de muchas fuentes oficiales) mezclan prosa con tablas
estadísticas. Al extraer el texto completo, las tablas salen como una sopa
de números que contamina los embeddings y arruina la búsqueda semántica.

Solución: filtrar chunks que sean mayoritariamente numéricos/simbólicos
antes de mandarlos a embeber. Solo se conservan chunks con suficiente
proporción de letras (prosa real).
"""

import fitz  # PyMuPDF

from app.cleaner.text_utils import fix_mojibake


# Cargamos el umbral desde settings. Si falla por cualquier razón
# (import circular, .env no cargado, etc.), usamos 0.40 por defecto.
try:
    from app.core.config import load_settings as _load_settings
    _MIN_LETTER_RATIO = _load_settings("pdf-extractor").pdf_min_letter_ratio
except Exception:  # noqa: BLE001
    _MIN_LETTER_RATIO = 0.40


def _letter_ratio(text: str) -> float:
    """Proporción de caracteres alfabéticos sobre el total."""
    if not text:
        return 0.0
    letters = sum(1 for c in text if c.isalpha())
    return letters / len(text)


def _is_prose(text: str, min_ratio: float | None = None) -> bool:
    """True si el texto parece prosa (suficiente proporción de letras)."""
    threshold = min_ratio if min_ratio is not None else _MIN_LETTER_RATIO
    return _letter_ratio(text) >= threshold


def extract_pdf(content: bytes):
    """
    Extrae texto de un PDF. Devuelve (páginas, total_páginas).

    Cada página es un dict {'page_number': int, 'content': str}.
    Aplica fix_mojibake para reparar tildes rotas (Ã, Â, etc.).
    """
    doc = fitz.open(stream=content, filetype='pdf')
    out = []
    for n, page in enumerate(doc, 1):
        text = ' '.join(page.get_text('text').split())
        text = fix_mojibake(text)
        if text:
            out.append({'page_number': n, 'content': text})
    total = len(doc)
    doc.close()
    return out, total


def chunk_pages(pages, max_words: int = 450, overlap: int = 60):
    """
    Divide cada página en chunks de `max_words` palabras con `overlap` de solape.

    Filtra chunks que parezcan tablas o ruido numérico (ratio de letras
    por debajo de `_MIN_LETTER_RATIO`). Esto reduce drásticamente los chunks
    basura y mejora la calidad de la búsqueda semántica.
    """
    out = []
    for p in pages:
        words = p['content'].split()
        if not words:
            continue

        start = 0
        while start < len(words):
            end = min(len(words), start + max_words)
            chunk_text = ' '.join(words[start:end])

            # Filtro: solo conservar chunks que parezcan prosa.
            if _is_prose(chunk_text):
                out.append({
                    'page_number': p['page_number'],
                    'content': chunk_text,
                })

            if end == len(words):
                break
            start = end - overlap

    return out