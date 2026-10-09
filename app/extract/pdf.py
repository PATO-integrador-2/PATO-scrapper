"""
Extracción de texto de PDFs y chunking, filtrando ruido.

Se descarta:
1. Texto que cae sobre imágenes o gráficos/tablas vectoriales (ejes, etiquetas, valores).
2. Pies de figura y fuentes ("Figura 3", "Gráfico", "Fuente:", "Nota:"...), por regex
   y por cercanía a una imagen/gráfico con fuente menor al cuerpo.
3. Notas al pie (fuente claramente menor que la del cuerpo del documento).
4. Encabezados/pies de página: zona de márgenes + texto repetido entre páginas
   + numeración de página.
5. Bloques que parecen tablas numéricas (heurísticas previas).
"""

import re
from collections import Counter

import fitz  # PyMuPDF
import unicodedata
from app.cleaner.text_utils import fix_mojibake

try:
    from app.core.config import load_settings as _load_settings
    _MIN_LETTER_RATIO = _load_settings("pdf-extractor").pdf_min_letter_ratio
except Exception:  # noqa: BLE001
    _MIN_LETTER_RATIO = 0.55

# ---------------------------------------------------------------- parámetros
MAX_DIGIT_RATIO = 0.15
MAX_NUMERIC_TOKEN_RATIO = 0.30

MARGIN_RATIO = 0.08            # franja superior/inferior = encabezado/pie
MARGIN_MAX_CHARS = 150         # texto corto en margen => ruido
REPEAT_MIN_PAGES = 3           # un texto repetido en >= N páginas...
REPEAT_MIN_FRACTION = 0.30     # ...y >= 30% de las páginas => encabezado/pie
SMALL_FONT_FACTOR = 0.88       # fuente < 88% del cuerpo => nota al pie / caption
OVERLAP_THRESHOLD = 0.40       # % del bloque dentro de imagen/gráfico => descartar
CAPTION_DISTANCE = 60          # pt de distancia a imagen/gráfico para considerar caption
MIN_VISUAL_SIZE = 30           # ignora dibujos pequeños (líneas, viñetas)

_NUMERIC_TOKEN = re.compile(r"^[\-+]?\d{1,3}(?:[.,]\d+)*%?$")

_CAPTION_RE = re.compile(
    r"^\s*(?:"
    r"(?:figura|fig\.|gr[aá]fico|gr[aá]fica|cuadro|tabla|mapa|diagrama|ilustraci[oó]n|"
    r"recuadro|imagen|foto|infograf[ií]a|figure|chart|table|source)\s*[\dIVXivx\.\-:]*"
    r"|fuente\s*:|fuentes\s*:|nota\s*s?\s*:|notas\s*:|elaboraci[oó]n\s+(?:propia|de)|"
    r"elaborado\s+(?:por|con)|a/|\*+\s|\(\*\)|p\s*/|e\s*/|1/\s)",
    re.IGNORECASE,
)
_PAGE_NUM_RE = re.compile(
    r"^\s*(?:p[aá]g(?:ina)?\.?\s*)?\d{1,4}(?:\s*(?:/|de|of)\s*\d{1,4})?\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- utilidades
def _letter_ratio(text: str) -> float:
    return sum(c.isalpha() for c in text) / len(text) if text else 0.0


def _digit_ratio(text: str) -> float:
    return sum(c.isdigit() for c in text) / len(text) if text else 0.0


def _numeric_token_ratio(text: str) -> float:
    tokens = text.split()
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if _NUMERIC_TOKEN.match(t)) / len(tokens)


def _has_numeric_run(text: str, min_run: int = 3) -> bool:
    return bool(re.search(r"(?:\b\d+[.,]?\d*\b\s+){" + str(min_run) + r",}", text))


def _signature(text: str) -> str:
    """Firma para detectar texto repetido entre páginas (números -> #)."""
    t = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", t).strip()[:120]


def _is_prose_block(text: str, min_letter_ratio: float | None = None) -> bool:
    text = text.strip()
    if len(text) < 100 or len(text.split()) < 20:
        return False
    threshold = min_letter_ratio if min_letter_ratio is not None else _MIN_LETTER_RATIO
    if _letter_ratio(text) < threshold:
        return False
    if _digit_ratio(text) > MAX_DIGIT_RATIO:
        return False
    if _numeric_token_ratio(text) > MAX_NUMERIC_TOKEN_RATIO:
        return False
    if _has_numeric_run(text):
        return False
    return True


# ---------------------------------------------------- regiones visuales (ruido)
def _visual_rects(page: "fitz.Page") -> tuple[list["fitz.Rect"], list["fitz.Rect"]]:
    """Devuelve (imágenes, dibujos vectoriales) de la página."""
    page_area = page.rect.get_area()
    images: list[fitz.Rect] = []
    drawings: list[fitz.Rect] = []

    try:
        for info in page.get_image_info():
            r = fitz.Rect(info["bbox"])
            if r.width >= MIN_VISUAL_SIZE and r.height >= MIN_VISUAL_SIZE:
                images.append(r)
    except Exception:  # noqa: BLE001
        pass

    try:
        for r in page.cluster_drawings():
            r = fitz.Rect(r)
            if r.width < MIN_VISUAL_SIZE or r.height < MIN_VISUAL_SIZE:
                continue
            if r.get_area() > 0.5 * page_area:  # fondos / marcos de diapositiva
                continue
            drawings.append(r)
    except Exception:  # noqa: BLE001
        pass

    return images, drawings


def _looks_like_chart_text(text: str) -> bool:
    """Texto típico de gráfico: corto o con mucho contenido numérico."""
    words = text.split()
    return (
        len(words) < 8
        or _digit_ratio(text) > 0.10
        or _numeric_token_ratio(text) > 0.20
    )
def _is_text_block(text: str) -> bool:
    """Filtro por bloque, permisivo: acepta viñetas cortas, rechaza números y fórmulas."""
    text = text.strip()
    words = text.split()
    if len(words) < 3:
        return False
    if _letter_ratio(text) < _MIN_LETTER_RATIO:
        return False
    if _is_name_list(text):
        return False
    if _digit_ratio(text) > 0.20:
        return False
    if _numeric_token_ratio(text) > MAX_NUMERIC_TOKEN_RATIO:
        return False
    if _has_numeric_run(text):
        return False
    # Fórmulas / letras matemáticas: muchos tokens de 1 carácter o símbolos unicode matemáticos
    if sum(len(w) == 1 for w in words) / len(words) > 0.25:
        return False
    if any(0x1D400 <= ord(c) <= 0x1D7FF for c in text):
        return False
    return True

_MATH_CHARS = set("=∑∏√∫≥≤≠≈÷×∗±")
_NUM_PREFIX_RE = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[IVX]+\.)\s+")
_BULLET_RE = re.compile(r"^[•▪●◦\-–]\s*")
_CONTACT_RE = re.compile(r"(@|https?://|www\.)", re.IGNORECASE)
_DATE_LINE_RE = re.compile(
    r"^[A-Za-zÁÉÍÓÚáéíóúñ .]+\s*,\s*\d{1,2} de [a-záéíóú]+ (?:de )?\d{4}$"
)


def _heading_core(text: str) -> str:
    """Título sin viñeta ni numeración, para comparar índice vs. títulos reales."""
    return _NUM_PREFIX_RE.sub("", _clean_heading(text)).lower().strip(" .:")

def _is_formula(text: str) -> bool:
    """Bloque corto con símbolos matemáticos y letras (no una tabla de números)."""
    if len(text) > 400 or not any(c in _MATH_CHARS for c in text):
        return False
    return _digit_ratio(text) < 0.30 and sum(c.isalpha() for c in text) >= 2


def _clean_heading(text: str) -> str:
    return _BULLET_RE.sub("", text).strip()


def _is_heading(b: dict, body_size: float) -> bool:
    text = _clean_heading(b["text"])
    words = text.split()
    if not (1 <= len(words) <= 12) or len(text) > 120:
        return False
    if text.endswith((".", ":", ";", ",")):
        return False
    core = _NUM_PREFIX_RE.sub("", text)
    numbered = core != text
    if _letter_ratio(core) < 0.6 or _digit_ratio(core) > 0.10:
        return False
    larger = body_size > 0 and b["size"] >= body_size * 1.15
    return numbered or larger or b.get("bold", False)

def _overlap_fraction(block: "fitz.Rect", other: "fitz.Rect") -> float:
    inter = block & other
    if inter.is_empty or block.get_area() == 0:
        return 0.0
    return inter.get_area() / block.get_area()


def _near_visual(block: "fitz.Rect", rects: list["fitz.Rect"]) -> bool:
    """Bloque justo encima/debajo de una imagen o gráfico, alineado horizontalmente."""
    for r in rects:
        h_overlap = min(block.x1, r.x1) - max(block.x0, r.x0)
        if h_overlap <= 0:
            continue
        gap = max(block.y0 - r.y1, r.y0 - block.y1)  # >0 si están separados
        if gap <= CAPTION_DISTANCE:
            return True
    return False


# ---------------------------------------------------------------- extracción
def _collect_blocks(doc: "fitz.Document") -> list[dict]:
    """Primera pasada: bloques de texto con posición, tamaño de fuente y regiones visuales."""
    collected: list[dict] = []
    for n, page in enumerate(doc, 1):
        images, drawings = _visual_rects(page)
        visuals = images + drawings
        height = page.rect.height
        try:
            data = page.get_text("dict")
        except Exception:  # noqa: BLE001
            continue

        for blk in data.get("blocks", []):
            if blk.get("type") != 0:  # 1 = imagen
                continue
            parts: list[str] = []
            weighted = chars = bold_chars = 0
            for line in blk.get("lines", []):
                for span in line.get("spans", []):
                    s = span.get("text", "")
                    if not s.strip():
                        continue
                    parts.append(s)
                    weighted += span.get("size", 0) * len(s)
                    chars += len(s)
                    if (span.get("flags", 0) & 16) or "bold" in span.get("font", "").lower():
                        bold_chars += len(s)
            if not chars:
                continue
            text = unicodedata.normalize(
                "NFKC", fix_mojibake(" ".join(" ".join(parts).split()))
            )
            if not text:
                continue
            collected.append({
                "page": n,
                "rect": fitz.Rect(blk["bbox"]),
                "text": text,
                "size": weighted / chars,
                "bold": bold_chars / chars >= 0.6,
                "page_height": height,
                "images": images,
                "drawings": drawings,
                "visuals": visuals,
            })
    return collected


def _body_font_size(blocks: list[dict]) -> float:
    """Tamaño de fuente dominante (por cantidad de caracteres) = cuerpo del texto."""
    counter: Counter = Counter()
    for b in blocks:
        counter[round(b["size"] * 2) / 2] += len(b["text"])
    return counter.most_common(1)[0][0] if counter else 0.0


def _repeated_signatures(blocks: list[dict], total_pages: int) -> set[str]:
    """Firmas de texto corto que se repiten en muchas páginas (encabezados/pies)."""
    pages_by_sig: dict[str, set[int]] = {}
    for b in blocks:
        if len(b["text"]) > 200:
            continue
        pages_by_sig.setdefault(_signature(b["text"]), set()).add(b["page"])
    min_pages = max(REPEAT_MIN_PAGES, int(total_pages * REPEAT_MIN_FRACTION))
    return {sig for sig, pgs in pages_by_sig.items() if len(pgs) >= min_pages}


def _noise_reason(b: dict, body_size: float, repeated: set[str]) -> str | None:
    text, rect, h = b["text"], b["rect"], b["page_height"]
    if _PAGE_NUM_RE.match(text):
        return "page_number"
    if len(text) < 120 and _CONTACT_RE.search(text):
        return "contact"
    if _DATE_LINE_RE.match(text):
        return "date_line"
    if _signature(text) in repeated:
        return "repeated_header_footer"
    in_margin = rect.y1 <= h * MARGIN_RATIO or rect.y0 >= h * (1 - MARGIN_RATIO)
    if in_margin and len(text) <= MARGIN_MAX_CHARS:
        return "margin"
    for r in b["images"]:
        if _overlap_fraction(rect, r) >= OVERLAP_THRESHOLD:
            return "over_image"
    if _looks_like_chart_text(text) and not _is_formula(text):
        for r in b["drawings"]:
            if _overlap_fraction(rect, r) >= OVERLAP_THRESHOLD:
                return "over_chart"
    is_small = body_size > 0 and b["size"] < body_size * SMALL_FONT_FACTOR
    if _CAPTION_RE.match(text):
        return "caption_pattern"
    if is_small and _near_visual(rect, b["visuals"]):
        return "caption_small_near_visual"
    if is_small:
        return "small_font"
    return None

def _body_size_for(blocks):
    texty = [b for b in blocks if _is_text_block(b["text"])]
    return _body_font_size(texty or blocks)
def _is_noise(b, body_size, repeated) -> bool:
    return _noise_reason(b, body_size, repeated) is not None


def debug_extract(content: bytes) -> dict:
    """Cuenta cuántos bloques se descartan por cada motivo (para calibrar)."""
    doc = fitz.open(stream=content, filetype="pdf")
    total = len(doc)
    try:
        blocks = _collect_blocks(doc)
    finally:
        doc.close()
    body = _body_size_for(blocks)
    rep = _repeated_signatures(blocks, total)
    counts: Counter = Counter()
    samples: list[str] = []
    hsamples: list[str] = []
    fsamples: list[str] = []
    for b in blocks:
        reason = _noise_reason(b, body, rep)
        if reason is None:
            if _is_formula(b["text"]):
                reason = "formula"
                if len(fsamples) < 15:
                    fsamples.append(b["text"][:100])
            elif _is_heading(b, body):
                reason = "heading"
                if len(hsamples) < 30:
                    hsamples.append(_clean_heading(b["text"]))
            elif _is_text_block(b["text"]):
                reason = "kept"
            else:
                reason = "not_text_block"
                if len(samples) < 40:
                    samples.append(b["text"][:80])
        counts[reason] += 1
    return {
        "body_font_size": body,
        "blocks": len(blocks),
        "reasons": dict(counts),
        "samples": samples,
        "headings": hsamples,
        "formulas": fsamples,
    }

def _is_name_list(text: str) -> bool:
    """Listas de nombres/cargos/títulos: casi todas las palabras capitalizadas."""
    words = [w for w in text.split() if w.isalpha()]
    if len(words) < 8:
        return False
    caps = sum(w[0].isupper() for w in words)
    return caps / len(words) > 0.70

def extract_pdf(content: bytes):
    """
    Devuelve (páginas, total_páginas). Cada página:
    {'page_number', 'content', 'segments': [{'section', 'text'}]}
    """
    doc = fitz.open(stream=content, filetype="pdf")
    total_pages = len(doc)
    try:
        blocks = _collect_blocks(doc)
    finally:
        doc.close()

    if not blocks:
        return [], total_pages

    body_size = _body_size_for(blocks)
    repeated = _repeated_signatures(blocks, total_pages)

    real_headings = {
        _heading_core(b["text"])
        for b in blocks
        if not _is_noise(b, body_size, repeated) and _is_heading(b, body_size)
    }

    by_page: dict[int, list[dict]] = {}
    section: str | None = None
    for b in blocks:
        if _is_noise(b, body_size, repeated):
            continue
        text = b["text"]
                # Entrada de índice: viñeta cuyo texto coincide con un título real del documento
        if _BULLET_RE.match(text) and _heading_core(text) in real_headings:
            continue
        if _is_formula(text):
            by_page.setdefault(b["page"], []).append({"section": section, "text": text})
        elif _is_heading(b, body_size):
            section = _clean_heading(text)   # el título pasa a ser la sección vigente
        elif _is_text_block(text):
            by_page.setdefault(b["page"], []).append({"section": section, "text": text})

    out = [
        {
            "page_number": n,
            "content": " ".join(s["text"] for s in segs),
            "segments": segs,
        }
        for n, segs in sorted(by_page.items())
    ]
    return out, total_pages


MIN_SECTION_WORDS = 40   # secciones más cortas se fusionan con la siguiente


def _group_segments(pages):
    groups = []
    for p in pages:
        for seg in p.get("segments", []):
            if groups and groups[-1]["section"] == seg["section"]:
                groups[-1]["parts"].append(seg["text"])
            else:
                groups.append({"section": seg["section"], "page": p["page_number"],
                               "parts": [seg["text"]]})
    return groups


def _merge_small(groups):
    """Una sección corta no se pierde: se antepone (con su título) a la siguiente."""
    merged, carry = [], ""
    for g in groups:
        text = " ".join(g["parts"])
        if len(text.split()) < MIN_SECTION_WORDS:
            label = f"{g['section']}. " if g["section"] else ""
            carry = f"{carry} {label}{text}".strip()
            continue
        if carry:
            text, carry = f"{carry} {text}", ""
        merged.append({"section": g["section"], "page": g["page"], "text": text})
    if carry:
        if merged:
            merged[-1]["text"] += " " + carry
        elif groups:
            merged.append({"section": None, "page": groups[0]["page"], "text": carry})
    return merged


def chunk_pages(pages, max_words: int = 450, overlap: int = 60):
    out = []
    for g in _merge_small(_group_segments(pages)):
        words = g["text"].split()
        start = 0
        while start < len(words):
            end = min(len(words), start + max_words)
            chunk_text = " ".join(words[start:end])
            if _is_prose_block(chunk_text):
                content = f"{g['section']}. {chunk_text}" if g["section"] else chunk_text
                out.append({
                    "page_number": g["page"],
                    "section": g["section"],
                    "content": content,
                })
            if end == len(words):
                break
            start = end - overlap
    return out