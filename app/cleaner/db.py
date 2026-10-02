from contextlib import contextmanager
import psycopg2
from app.config import DATA_DATABASE_URL


def get_connection():
    if not DATA_DATABASE_URL:
        raise RuntimeError("DATA_DATABASE_URL no está configurada.")
    return psycopg2.connect(DATA_DATABASE_URL)


@contextmanager
def transaction(conn):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
