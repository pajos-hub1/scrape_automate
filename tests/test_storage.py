from db.storage import ensure_unpacked, pack


def test_pack_unpack_roundtrip(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"sqlite-bytes" * 1000)
    gz = pack(db)
    db.unlink()
    ensure_unpacked(db)
    assert db.read_bytes() == b"sqlite-bytes" * 1000
    assert gz.exists()


def test_pack_is_deterministic(tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"abc" * 500)
    first = pack(db).read_bytes()
    second = pack(db).read_bytes()
    assert first == second   # unchanged DB must never show up as a git diff


def test_newer_gz_replaces_stale_db(tmp_path):
    import os
    db = tmp_path / "x.db"
    db.write_bytes(b"new")
    gz = pack(db)
    db.write_bytes(b"stale-local-copy")
    os.utime(db, (gz.stat().st_mtime - 100,) * 2)   # .gz newer, e.g. after git pull
    ensure_unpacked(db)
    assert db.read_bytes() == b"new"
