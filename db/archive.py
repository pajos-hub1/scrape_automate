"""Moves old, settled rows out of the live DB into frozen archive files so
zoom.db stays well under GitHub's 100 MiB per-file limit.

Only the three big append-only tables are archived -- odds, predictions,
prediction_results (together ~90% of the DB). seasons/matches/fixtures/
features stay live: they're small, and features/build.py needs every past
match for its cross-season priors.

What's safe to move:
  - predictions + their prediction_results, once reconciled more than
    ARCHIVE_KEEP_DAYS ago. reconcile() only ever looks at UNreconciled
    predictions, so it never needs these again.
  - odds for fixtures last priced more than ARCHIVE_KEEP_DAYS ago and with
    no unreconciled prediction left. Odds are only ever read for the
    current/pending fixtures (features, orphan predictions, reconcile's
    odds-implied label) -- training deliberately excludes them (see
    predict/training_data.py).

Each archive run writes a NEW data/archive/zoom_archive_NNN.db and never
touches an older one again, so git stores each archive exactly once.
Readers that need all-time history (track/report.py, dashboard/data.py)
query the all_predictions / all_prediction_results TEMP views, which
UNION the live tables with every attached archive.
"""
import os
from datetime import datetime, timedelta, timezone

from config import ARCHIVE_DIR, ARCHIVE_KEEP_DAYS, ARCHIVE_TRIGGER_MB
from db.storage import ensure_unpacked, pack

ARCHIVED_TABLES = ["odds", "predictions", "prediction_results",
                   # other-league analysis data (2026-10-23): finished seasons only
                   "lg_matches", "lg_events", "lg_odds"]

# SQLite's default compile-time cap on attached databases (Python's bundled
# build keeps the default). Hitting it means it's time to merge archives.
MAX_ATTACHED = 10


def archive_files():
    """Sorted archive .db paths, unpacking any whose .gz is newer."""
    if not ARCHIVE_DIR.exists():
        return []
    paths = sorted(ARCHIVE_DIR.glob("zoom_archive_*.db.gz"))
    dbs = []
    for gz in paths:
        db = gz.with_suffix("")
        ensure_unpacked(db)
        dbs.append(db)
    return dbs


def attach_archives(conn):
    """Attach every archive and (re)create the all_* TEMP views. Called by
    db/connection.get_connection on every connect."""
    dbs = archive_files()
    if len(dbs) >= MAX_ATTACHED:
        raise RuntimeError(
            f"{len(dbs)} archive files in {ARCHIVE_DIR} -- SQLite can attach at most "
            f"{MAX_ATTACHED - 1} alongside the live DB plus one for a new archive. "
            f"Merge older archives into one file."
        )

    aliases = []
    for i, db in enumerate(dbs):
        alias = f"arc{i}"
        conn.execute("ATTACH DATABASE ? AS " + alias, (str(db),))
        aliases.append(alias)

    for table in ARCHIVED_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info({table})")]
        selects = [f"SELECT {', '.join(cols)} FROM main.{table}"]
        for alias in aliases:
            have = {r[1] for r in conn.execute(f"PRAGMA {alias}.table_info({table})")}
            if not have:
                continue
            # Tolerate columns added to the live schema after this archive
            # was frozen -- they read as NULL for archived rows.
            exprs = [c if c in have else f"NULL AS {c}" for c in cols]
            selects.append(f"SELECT {', '.join(exprs)} FROM {alias}.{table}")
        conn.execute(f"DROP VIEW IF EXISTS temp.all_{table}")
        conn.execute(f"CREATE TEMP VIEW all_{table} AS " + " UNION ALL ".join(selects))


def db_size_mb(conn):
    page_size = conn.execute("PRAGMA main.page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA main.page_count").fetchone()[0]
    return page_size * page_count / (1024 * 1024)


def _next_archive_path():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(ARCHIVE_DIR.glob("zoom_archive_*.db.gz"))
    n = int(existing[-1].name.split("_")[-1].split(".")[0]) + 1 if existing else 1
    return ARCHIVE_DIR / f"zoom_archive_{n:03d}.db"


def archive_if_needed(conn, force=False):
    """Archive old rows when the live DB is over ARCHIVE_TRIGGER_MB (or
    always, with force=True). Returns a summary dict."""
    size_before = db_size_mb(conn)
    summary = {"size_before_mb": round(size_before, 1), "archived": False}
    if not force and size_before < ARCHIVE_TRIGGER_MB:
        return summary

    cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_KEEP_DAYS)).isoformat(timespec="seconds")

    conn.execute("DROP TABLE IF EXISTS temp.arc_pred_ids")
    conn.execute(
        """CREATE TEMP TABLE arc_pred_ids AS
           SELECT pr.prediction_id FROM main.prediction_results pr
           WHERE pr.reconciled_at < ?""",
        (cutoff,),
    )
    conn.execute("DROP TABLE IF EXISTS temp.arc_fixture_ids")
    conn.execute(
        """CREATE TEMP TABLE arc_fixture_ids AS
           SELECT o.fixture_id FROM main.odds o
           GROUP BY o.fixture_id
           HAVING MAX(o.captured_at) < ?
              AND o.fixture_id NOT IN (
                  SELECT p.fixture_ref FROM main.predictions p
                  LEFT JOIN main.prediction_results pr ON pr.prediction_id = p.prediction_id
                  WHERE pr.result_id IS NULL
              )""",
        (cutoff,),
    )

    # Other leagues: games from a league's finished seasons (any season
    # before its newest one), scraped before the cutoff.
    conn.execute("DROP TABLE IF EXISTS temp.arc_lg_ids")
    conn.execute(
        """CREATE TEMP TABLE arc_lg_ids AS
           SELECT m.ext_match_id FROM main.lg_matches m
           WHERE m.scraped_at < ?
             AND m.season_first_id < (SELECT MAX(season_first_id) FROM main.lg_matches x
                                      WHERE x.competition_id = m.competition_id)""",
        (cutoff,),
    )

    n_pred = conn.execute("SELECT COUNT(*) FROM temp.arc_pred_ids").fetchone()[0]
    n_fix = conn.execute("SELECT COUNT(*) FROM temp.arc_fixture_ids").fetchone()[0]
    n_lg = conn.execute("SELECT COUNT(*) FROM temp.arc_lg_ids").fetchone()[0]
    if n_pred == 0 and n_fix == 0 and n_lg == 0:
        summary["note"] = "nothing old enough to archive"
        return summary

    path = _next_archive_path()
    if path.exists():
        os.remove(path)
    conn.commit()
    conn.execute("ATTACH DATABASE ? AS arc_new", (str(path),))
    try:
        # CREATE TABLE AS (no constraints) on purpose: the archive has no
        # fixtures/matches tables for the live schema's FOREIGN KEYs to point at.
        conn.execute(
            "CREATE TABLE arc_new.predictions AS SELECT * FROM main.predictions "
            "WHERE prediction_id IN (SELECT prediction_id FROM temp.arc_pred_ids)"
        )
        conn.execute(
            "CREATE TABLE arc_new.prediction_results AS SELECT * FROM main.prediction_results "
            "WHERE prediction_id IN (SELECT prediction_id FROM temp.arc_pred_ids)"
        )
        conn.execute(
            "CREATE TABLE arc_new.odds AS SELECT * FROM main.odds "
            "WHERE fixture_id IN (SELECT fixture_id FROM temp.arc_fixture_ids)"
        )
        conn.execute("CREATE INDEX arc_new.idx_arc_pred ON predictions(prediction_id)")
        conn.execute("CREATE INDEX arc_new.idx_arc_pred_fixture ON predictions(fixture_ref)")
        conn.execute("CREATE INDEX arc_new.idx_arc_pr ON prediction_results(prediction_id)")
        conn.execute("CREATE INDEX arc_new.idx_arc_pr_match ON prediction_results(match_ref)")
        conn.execute("CREATE INDEX arc_new.idx_arc_odds ON odds(fixture_id, market, selection)")
        for table in ("lg_matches", "lg_events", "lg_odds"):
            conn.execute(f"CREATE TABLE arc_new.{table} AS SELECT * FROM main.{table} "
                         "WHERE ext_match_id IN (SELECT ext_match_id FROM temp.arc_lg_ids)")
        conn.execute("CREATE INDEX arc_new.idx_arc_lg_matches ON lg_matches(ext_match_id)")
        conn.execute("CREATE INDEX arc_new.idx_arc_lg_events ON lg_events(ext_match_id)")
        conn.execute("CREATE INDEX arc_new.idx_arc_lg_odds ON lg_odds(ext_match_id)")

        # Children before parents (prediction_results -> predictions FK).
        conn.execute(
            "DELETE FROM main.prediction_results "
            "WHERE prediction_id IN (SELECT prediction_id FROM temp.arc_pred_ids)"
        )
        conn.execute(
            "DELETE FROM main.predictions "
            "WHERE prediction_id IN (SELECT prediction_id FROM temp.arc_pred_ids)"
        )
        conn.execute(
            "DELETE FROM main.odds WHERE fixture_id IN (SELECT fixture_id FROM temp.arc_fixture_ids)"
        )
        for table in ("lg_odds", "lg_events", "lg_matches"):
            conn.execute(f"DELETE FROM main.{table} WHERE ext_match_id IN (SELECT ext_match_id FROM temp.arc_lg_ids)")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute("DETACH DATABASE arc_new")
        if path.exists():
            os.remove(path)
        raise

    conn.execute("DETACH DATABASE arc_new")
    conn.execute("VACUUM main")
    pack(path)

    summary.update({
        "archived": True,
        "archive_file": path.name,
        "predictions_moved": n_pred,
        "odds_fixtures_moved": n_fix,
        "league_games_moved": n_lg,
        "size_after_mb": round(db_size_mb(conn), 1),
    })
    return summary


# Merge every archive into one once there are this many -- comfortably under
# MAX_ATTACHED, so the attach limit is never actually hit.
MERGE_AT = 6


def merge_archives_if_needed(force=False, db_path=None):
    """Combine all archive files into a single new one (next number), then
    delete the old ones. Runs on its own connection, after the cycle's main
    connection is closed, since that one has the archives attached.

    Columns follow the live schema (like the all_* views): any column an
    older archive lacks comes through as NULL.
    """
    import sqlite3
    from config import DB_PATH

    db_path = db_path or DB_PATH
    dbs = archive_files()
    if len(dbs) < 2 or (not force and len(dbs) < MERGE_AT):
        return {"merged": False, "archive_files": len(dbs)}

    target = _next_archive_path()
    live = sqlite3.connect(str(db_path))
    try:
        schema = {t: [r[1] for r in live.execute(f"PRAGMA table_info({t})")] for t in ARCHIVED_TABLES}
    finally:
        live.close()

    conn = sqlite3.connect(str(target))
    try:
        for i, db in enumerate(dbs):
            conn.execute(f"ATTACH DATABASE ? AS src{i}", (str(db),))
        for table, cols in schema.items():
            selects = []
            for i in range(len(dbs)):
                have = {r[1] for r in conn.execute(f"PRAGMA src{i}.table_info({table})")}
                if have:
                    exprs = [c if c in have else f"NULL AS {c}" for c in cols]
                    selects.append(f"SELECT {', '.join(exprs)} FROM src{i}.{table}")
            if not selects:
                continue          # no archive has this table yet (e.g. lg_* in pre-2026-10-23 archives)
            conn.execute(f"CREATE TABLE main.{table} AS " + " UNION ALL ".join(selects))
        merged_tables = {r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'")}
        conn.execute("CREATE INDEX idx_arc_pred ON predictions(prediction_id)")
        conn.execute("CREATE INDEX idx_arc_pred_fixture ON predictions(fixture_ref)")
        conn.execute("CREATE INDEX idx_arc_pr ON prediction_results(prediction_id)")
        conn.execute("CREATE INDEX idx_arc_pr_match ON prediction_results(match_ref)")
        conn.execute("CREATE INDEX idx_arc_odds ON odds(fixture_id, market, selection)")
        for table in ("lg_matches", "lg_events", "lg_odds"):
            if table in merged_tables:
                conn.execute(f"CREATE INDEX idx_arc_{table} ON {table}(ext_match_id)")
        conn.commit()

        expected = {t: sum(conn.execute(f"SELECT COUNT(*) FROM src{i}.{t}").fetchone()[0]
                           for i in range(len(dbs))
                           if conn.execute(f"PRAGMA src{i}.table_info({t})").fetchone())
                    for t in ARCHIVED_TABLES if t in merged_tables}
        got = {t: conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0] for t in ARCHIVED_TABLES if t in merged_tables}
        if got != expected:
            raise RuntimeError(f"archive merge row counts don't match: {got} vs {expected}")
        for i in range(len(dbs)):
            conn.execute(f"DETACH DATABASE src{i}")
    except Exception:
        conn.close()
        os.remove(target)
        raise
    conn.close()

    pack(target)
    for db in dbs:
        os.remove(db)
        os.remove(str(db) + ".gz")
    return {"merged": True, "from_files": [d.name for d in dbs], "into": target.name, "rows": got}
