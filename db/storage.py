"""Gzip packing for the SQLite files git stores.

git only ever tracks the *.db.gz copies -- the plain *.db files are
gitignored working copies. ensure_unpacked() restores a working copy
whenever its .gz is newer (fresh checkout, or a `git pull` that brought in
a newer one); pack() writes the .gz back after a cycle changes the DB.
"""
import gzip
import os
import shutil
from pathlib import Path

CHUNK = 1024 * 1024


def gz_path_for(db_path):
    return Path(str(db_path) + ".gz") if not str(db_path).endswith(".gz") else Path(db_path)


def ensure_unpacked(db_path):
    """Decompress db_path.gz -> db_path if the .db is missing or older than
    the .gz. No-op when there's no .gz (e.g. a brand new local DB)."""
    db_path = Path(db_path)
    gz = gz_path_for(db_path)
    if not gz.exists():
        return
    if db_path.exists() and db_path.stat().st_mtime >= gz.stat().st_mtime:
        return

    tmp = db_path.with_suffix(".db.tmp")
    with gzip.open(gz, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, CHUNK)
    os.replace(tmp, db_path)
    _sync_mtime(db_path, gz)


def pack(db_path):
    """Compress db_path -> db_path.gz. Deterministic (mtime=0, no embedded
    filename) so identical DB bytes always produce identical .gz bytes --
    an unchanged archive never shows up as a git diff."""
    db_path = Path(db_path)
    gz = gz_path_for(db_path)
    tmp = gz.with_suffix(".gz.tmp")
    with open(db_path, "rb") as src, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
            shutil.copyfileobj(src, dst, CHUNK)
    os.replace(tmp, gz)
    _sync_mtime(db_path, gz)
    return gz


def _sync_mtime(db_path, gz):
    # Same mtime on both, so ensure_unpacked doesn't needlessly re-decompress
    # a .gz we just wrote from this very .db.
    st = gz.stat()
    os.utime(db_path, (st.st_atime, st.st_mtime))
