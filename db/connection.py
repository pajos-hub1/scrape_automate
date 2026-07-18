import os
import sqlite3
from contextlib import contextmanager

from config import DB_PATH
from db.schema import init_db


@contextmanager
def get_connection(db_path=DB_PATH):
    db_path = str(db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
