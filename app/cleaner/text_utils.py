import re
import unicodedata


ENCODING_FIXES = {
    "�": "",
}


MOJIBAKE_SIGNATURES = (
    "Ã©", "Ã¡", "Ã", "Ã³", "Ãº", "Ã±", "Ã¼",
    "Ã", "Ã‰", "Ã", "Ã“", "Ãš", "Ã‘",
    "â€", "â€™", "â€œ", "â€", "â€¦",
    "Â¡", "Â¿", "Â°",
)


def _mojibake_score(text: str) -> int:
    """Cuenta cuántas secuencias típicas de mojibake aparecen."""
    return sum(text.count(sig) for sig in MOJIBAKE_SIGNATURES)


def fix_mojibake(value: str) -> str:
    """
    Repara texto mal decodificado (mojibake) proveniente de
    archivos que originalmente estaban en UTF-8 y fueron leídos
    como latin-1/cp1252 (o viceversa).

    Elige el candidato con menos "señales" de mojibake, no solo
    contando el carácter de reemplazo (�), porque el mojibake
    típico (Ã©, Ã³, Ã±) no incluye ese carácter.
    """
    if not value:
        return value

    candidates = [value]

    try:
        candidates.append(value.encode("latin-1").decode("utf-8"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    try:
        candidates.append(value.encode("cp1252").decode("utf-8"))
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    return min(candidates, key=_mojibake_score)

def normalize_text(value) -> str:
    """
    Limpia espacios redundantes, repara encoding y normaliza
    unicode (NFC) para que valores como 'A�o ' y 'Año' o
    'Dominio' y 'Dominio ' se traten como iguales.
    """

    if value is None:
        return ""

    text = str(value)

    text = fix_mojibake(text)

    text = unicodedata.normalize("NFC", text)

    text = text.replace("\xa0", " ")

    text = re.sub(r"\s+", " ", text).strip()

    return text


def slugify_column(value) -> str:
    """
    Convierte un encabezado humano ('Año ', 'Privación por Analfabetismo')
    en un identificador snake_case sin tildes, usado como clave interna
    para comparar/mapear columnas entre archivos distintos.
    """

    text = normalize_text(value).lower()

    text = "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )

    text = re.sub(r"[^a-z0-9]+", "_", text)

    return text.strip("_")
