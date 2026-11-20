"""One snapshot (or a loop of them): fetch every bookmaker, match games, record arbs.

    python -m fb_tracker.collect                 one snapshot
    python -m fb_tracker.collect --loop 330 \\
        --every 10 --after scripts             snapshots every 10 min for 330 min,
                                               running `--after` (saving) after each

An arb seen in consecutive snapshots is one "episode": first_seen..last_seen
tells how long it stayed open (to within one snapshot interval).
"""
import argparse
import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from fb_tracker.arbs import closest, find_arbs
from fb_tracker.matching import match_books, normalise
from fb_tracker.sources import SOURCES

DB_PATH = Path(__file__).with_name("data") / "fb.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (ts TEXT PRIMARY KEY, counts TEXT, matched INTEGER, arbs INTEGER, errors TEXT);
CREATE TABLE IF NOT EXISTS games (key TEXT PRIMARY KEY, country TEXT, league TEXT, kickoff TEXT, names TEXT,
                                  books TEXT, first_seen TEXT, last_seen TEXT, closest REAL);
CREATE TABLE IF NOT EXISTS arbs (key TEXT, cover TEXT, first_seen TEXT, last_seen TEXT, snapshots INTEGER,
                                 best_margin REAL, last_margin REAL, legs TEXT, suspicious INTEGER,
                                 PRIMARY KEY (key, cover, first_seen));
"""


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def game_key(match):
    first = next(iter(match.values()))
    return f"{first.kickoff[:16]}|{normalise(first.home)}|{normalise(first.away)}"


def snapshot(conn, sources=SOURCES, ts=None):
    ts = ts or now_iso()
    events, errors = {}, {}
    for book, fetch in sources.items():
        try:
            events[book] = fetch()
        except Exception as e:  # noqa: BLE001 -- a bookmaker being down mustn't stop the others
            errors[book] = f"{type(e).__name__}: {e}"[:200]
            events[book] = []
    matches = match_books({b: es for b, es in events.items() if es})
    prev = conn.execute("SELECT MAX(ts) FROM runs").fetchone()[0]
    n_arbs = 0
    for m in matches:
        key = game_key(m)
        first = next(iter(m.values()))
        names = json.dumps({b: f"{e.home} - {e.away}" for b, e in m.items()})
        near = closest(m)
        conn.execute("""INSERT INTO games (key, country, league, kickoff, names, books, first_seen, last_seen, closest)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET last_seen = excluded.last_seen, names = excluded.names,
                            books = excluded.books, closest = MIN(COALESCE(closest, 9), COALESCE(excluded.closest, 9))""",
                     (key, first.country, first.league, first.kickoff, names, ",".join(sorted(m)), ts, ts, near))
        for a in find_arbs(m):
            n_arbs += 1
            legs = json.dumps(a["legs"])
            open_ = conn.execute("SELECT first_seen FROM arbs WHERE key = ? AND cover = ? AND last_seen = ?",
                                 (key, a["cover"], prev)).fetchone() if prev else None
            if open_:                                    # still open since the last snapshot: extend the episode
                conn.execute("""UPDATE arbs SET last_seen = ?, snapshots = snapshots + 1, last_margin = ?, legs = ?,
                                best_margin = MAX(best_margin, ?) WHERE key = ? AND cover = ? AND first_seen = ?""",
                             (ts, a["margin"], legs, a["margin"], key, a["cover"], open_[0]))
            else:
                conn.execute("INSERT INTO arbs VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                             (key, a["cover"], ts, ts, a["margin"], a["margin"], legs, int(a["suspicious"])))
    counts = {b: len(es) for b, es in events.items()}
    conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)",
                 (ts, json.dumps(counts), len(matches), n_arbs, json.dumps(errors) if errors else None))
    conn.commit()
    print(f"{ts}: events {counts}, matched {len(matches)}, arbs {n_arbs}" + (f", errors {errors}" if errors else ""))
    return {"counts": counts, "matched": len(matches), "arbs": n_arbs, "errors": errors}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--loop", type=float, default=0, help="keep snapshotting for this many minutes")
    ap.add_argument("--every", type=float, default=10, help="minutes between snapshots")
    ap.add_argument("--after", default=None, help="shell command to run after each snapshot (e.g. save)")
    args = ap.parse_args()
    end = time.time() + args.loop * 60
    while True:
        started = time.time()
        conn = connect()
        snapshot(conn)
        conn.close()
        if args.after:
            subprocess.run(args.after, shell=True, check=False)
        if time.time() + args.every * 60 > end:
            break
        time.sleep(max(0.0, started + args.every * 60 - time.time()))


if __name__ == "__main__":
    main()
