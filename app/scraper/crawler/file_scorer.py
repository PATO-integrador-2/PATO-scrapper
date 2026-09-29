import re
import unicodedata

from pathlib import Path
from urllib.parse import unquote, urlparse




# Países de América Latina y el Caribe, en español, inglés y
# portugués, ya normalizados (minúsculas y sin tildes). Sitios como
# OPHI nombran sus reportes por país ("paraguay_2024_report.pdf",
# "Mexico MPI report 2024.pdf"), nunca con "latam" o
# "latin america", así que una consulta regional se expande a esta
# lista.
LATAM_COUNTRIES = (
    "mexico",
    "guatemala",
    "honduras",
    "el salvador",
    "nicaragua",
    "costa rica",
    "panama",
    "colombia",
    "venezuela",
    "ecuador",
    "peru",
    "bolivia",
    "chile",
    "argentina",
    "uruguay",
    "paraguay",
    "brasil",
    "brazil",
    "cuba",
    "haiti",
    "dominican",
    "dominicana",
    "jamaica",
    "trinidad",
    "barbados",
    "bahamas",
    "belize",
    "guyana",
    "suriname",
    "saint lucia",
    "santa lucia",
    "grenada",
    "antigua",
    "latin america",
    "america latina",
    "latinoamerica",
    "latam",
    "caribbean",
    "caribe",
    "centroamerica",
    "central america",
    "sudamerica",
    "south america",
)



# Alias de región que puede escribir el usuario -> términos con los
# que se reconoce esa región en nombres de archivo, textos y URLs.
REGION_ALIASES = {
    alias: LATAM_COUNTRIES
    for alias in (
        "latam",
        "lac",
        "latinoamerica",
        "latino america",
        "latin america",
        "latin america and the caribbean",
        "america latina",
        "america latina y el caribe",
        "iberoamerica",
        "caribe",
        "caribbean",
        "centroamerica",
        "central america",
        "sudamerica",
        "suramerica",
        "south america",
    )
}




def normalize_text(
    value: str,
) -> str:

    # Las URLs llegan con escapes ("Mexico%20MPI%20report") que
    # pegan palabras y rompen las coincidencias.
    value = unquote(
        value
    )

    # Quitar tildes para que "índice" y "indice", "América" y
    # "America" coincidan.
    value = "".join(
        char
        for char in unicodedata.normalize(
            "NFKD",
            value,
        )
        if not unicodedata.combining(char)
    )

    value = value.lower()

    value = re.sub(
        r"[_\-/\\.,;:()\[\]%+]",
        " ",
        value,
    )

    # Eliminar espacios duplicados

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()




def term_in(
    term: str,
    text: str,
) -> bool:

    # Los términos cortos ("ipm", "mpi", "lac", "peru") se buscan
    # como palabra completa: como substring aparecen dentro de
    # palabras sin relación ("co-mpi-led", "lac-k"). Los largos se
    # siguen buscando como substring porque el DANE pega prefijos
    # a los nombres ("anex-PMultidimensional-2025").
    if len(term) <= 4:

        return re.search(
            r"\b" + re.escape(term) + r"\b",
            text,
        ) is not None

    return term in text




def parse_query(
    query: str,
) -> tuple[list[str], list[str]]:

    """
    Separa el query (palabras clave unidas por comas) en:

    - topic_keywords: palabras de tema ("ipm", "desempleo").
    - region_terms: términos de región expandidos a países
      ("latam" -> "mexico", "colombia", ...).
    """

    topic_keywords = []

    region_terms = []

    for keyword in query.split(","):

        keyword = normalize_text(
            keyword
        )

        if not keyword:
            continue

        if keyword in REGION_ALIASES:

            region_terms.extend(
                REGION_ALIASES[keyword]
            )

        elif keyword in LATAM_COUNTRIES:

            region_terms.append(
                keyword
            )

        else:

            topic_keywords.append(
                keyword
            )

    return (
        list(dict.fromkeys(topic_keywords)),
        list(dict.fromkeys(region_terms)),
    )






# Términos ya normalizados (minúsculas, sin tildes, separadores
# convertidos a espacio). Incluyen inglés, portugués y francés
# porque fuentes como OPHI / PNUD publican el IPM como "MPI"
# ("India_MPI_2021.pdf", "Mauritius_nMPI_2022.pdf").
STRONG_TERMS = {
    "ipm",
    "mpi",
    "gmpi",
    "nmpi",
    # Abreviaturas de OPHI: "Dominican_2020_MDP_Covid-19.pdf",
    # "National_MD_Poverty_in_Maldives_2020.pdf".
    "mdp",
    "md poverty",
    "indice de pobreza multidimensional",
    "pobreza multidimensional",
    "multidimensional poverty",
    "pauvrete multidimensionnelle",
}



RELATED_TERMS = {
    "pobreza",
    "poverty",
    "pauvrete",
    "multidimensional",
    "privaciones",
    "privacion",
    "deprivation",
    "carencias",
    "dimensiones",
    "dimension",
}



POVERTY_TERMS = (
    "pobreza",
    "poverty",
    "pauvrete",
)

MULTIDIMENSIONAL_TERMS = (
    "multidimensional",
    "multidimensionnelle",
)

DEPRIVATION_TERMS = (
    "privaciones",
    "privacion",
    "deprivation",
    "carencias",
)



OTHER_STATISTICS = {
    "iccv",
    "ices",
    "ictc",
    "ictip",
    "ipc",
    "ipp",
    "ica",
    "icac",
    "ens",
    "eas",
    "etup",
}



# Peso de una coincidencia con la palabra clave del usuario según
# dónde aparece.
QUERY_WEIGHTS = {
    "filename": 100,
    "text": 80,
    "url": 70,
    "source": 50,
}

# Bono cuando el archivo pertenece a la región pedida.
REGION_BONUS = 60




def calculate_file_score(
    url: str,
    link_text: str,
    query: str,
    source_page: str = "",
) -> int:

    """
    Puntúa qué tan probable es que el archivo sea un dato de IPM
    relevante para el query. `query` puede traer varias palabras
    clave separadas por comas ("ipm,latam").

    Devuelve 0 si el archivo se descarta.
    """

    topic_keywords, region_terms = parse_query(
        query
    )

    filename = Path(
        urlparse(url).path
    ).name

    contexts = {
        "filename": normalize_text(filename),
        "text": normalize_text(link_text),
        "url": normalize_text(url),
        "source": normalize_text(source_page),
    }

    full_context = " ".join(
        contexts.values()
    )

    score = 0



    # Coincidencia con las palabras clave del usuario: se suma la
    # mejor palabra, no todas, para que un query largo no infle el
    # score.
    query_score = max(
        (
            sum(
                weight
                for place, weight in QUERY_WEIGHTS.items()
                if term_in(keyword, contexts[place])
            )
            for keyword in topic_keywords
        ),
        default=0,
    )

    score += query_score



    strong_matches = 0

    for term in STRONG_TERMS:

        if term_in(term, full_context):

            strong_matches += 1

            # Una coincidencia fuerte
            # ya es una evidencia importante.

            score += 60



    related_matches = 0

    for term in RELATED_TERMS:

        if term_in(term, full_context):

            related_matches += 1

            score += 15


    has_poverty = any(
        term_in(term, full_context)
        for term in POVERTY_TERMS
    )

    has_multidimensional = any(
        term_in(term, full_context)
        for term in MULTIDIMENSIONAL_TERMS
    )

    if (
        has_poverty
        and has_multidimensional
    ):
        score += 80


    if (
        has_poverty
        and any(
            term_in(term, full_context)
            for term in DEPRIVATION_TERMS
        )
    ):
        score += 50


    if (
        has_poverty
        and term_in("dimension", full_context)
    ):
        score += 40



    for statistic in OTHER_STATISTICS:

        # Límites de palabra (\b): sin esto, códigos cortos de otras
        # encuestas del DANE aparecen como substring dentro de
        # palabras legítimas del IPM (p. ej. "ens" dentro de
        # "pmultidimENSional", "ica" dentro de "estadISTICAs"),
        # penalizando -300 y descartando archivos correctos.
        pattern = r"\b" + re.escape(statistic) + r"\b"

        for place in ("filename", "url", "source"):

            if re.search(pattern, contexts[place]):
                score -= 100



    # Si el usuario pidió una región, es un filtro: el archivo (su
    # nombre, texto del enlace, URL o página de origen) tiene que
    # mencionar algún país de esa región.
    if region_terms:

        in_region = any(
            term_in(term, full_context)
            for term in region_terms
        )

        if not in_region:
            return 0

        score += REGION_BONUS



    # Se acepta el archivo si hay evidencia de que es IPM, o si
    # coincide con una palabra clave de tema que pidió el usuario.
    # Una región sola no basta: "colombia" aparece en casi todos
    # los archivos del DANE.
    has_topic_evidence = (
        strong_matches > 0
        or (
            has_poverty
            and has_multidimensional
        )
        or related_matches >= 2
    )

    if not (
        has_topic_evidence
        or query_score > 0
    ):
        return 0

    return max(
        score,
        0,
    )
