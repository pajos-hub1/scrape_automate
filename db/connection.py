import os
import sqlite3
from contextlib import contextmanager

from config import DB_PATH
from db.archive import attach_archives
from db.schema import init_db
from db.storage import ensure_unpacked


@contextmanager
def get_connection(db_path=DB_PATH):
    db_path = str(db_path)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    ensure_unpacked(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    attach_archives(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
