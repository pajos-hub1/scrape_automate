"""Pulls plain dicts/lists out of SQLite for the dashboard to render --
kept separate from HTML generation so the query logic stays testable
without needing to touch markup.
"""
from datetime import datetime, timezone

from track.report import accuracy_stats, list_model_versions


def get_meta(conn):
    seasons = conn.execute(
        "SELECT season_id, status, round_count, last_seen FROM seasons ORDER BY season_id"
    ).fetchall()
    current = next((s for s in seasons if s["status"] == "current"), None)
    previous = next((s for s in seasons if s["status"] == "previous"), None)
    archived_count = sum(1 for s in seasons if s["status"] == "archived")
    last_scraped = max((s["last_seen"] for s in seasons), default=None)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current_round": current["round_count"] if current else None,
        "previous_complete": previous is not None,
        "seasons_tracked": len(seasons),
        "seasons_archived": archived_count,
        "last_scraped": last_scraped,
    }


def get_upcoming_predictions(conn):
    """Predictions with no prediction_results row yet, restricted to the
    MOST RECENT predict() batch (max round_number among pending fixtures),
    across every model currently making live predictions.

    Reconciliation matches a prediction to its result by team pairing, not
    by our guessed round_number (see track/reconcile.py) -- a pairing only
    clears once it actually gets played, which can take more than one real
    round if the guess was off. That means older batches can still be
    sitting here unreconciled by the time a newer predict() run adds a
    fresh batch on top. Showing all of them at once looks like duplicate/
    doubled fixtures (the same team appearing twice against different
    opponents); the dashboard should only ever show the latest one.
    """
    rows = conn.execute(
        """SELECT p.fixture_ref, f.team_a, f.team_b, f.round_number, f.kickoff_time,
                  p.model_version, p.market, p.label, p.confidence
           FROM predictions p
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           LEFT JOIN prediction_results pr ON pr.prediction_id = p.prediction_id
           WHERE pr.result_id IS NULL
             AND f.round_number = (
                 SELECT MAX(f2.round_number)
                 FROM predictions p2
                 JOIN fixtures f2 ON f2.fixture_id = p2.fixture_ref
                 LEFT JOIN prediction_results pr2 ON pr2.prediction_id = p2.prediction_id
                 WHERE pr2.result_id IS NULL
             )
           ORDER BY f.match_number, p.market, p.model_version"""
    ).fetchall()

    fixtures = {}
    for r in rows:
        fx = fixtures.setdefault(r["fixture_ref"], {
            "team_a": r["team_a"], "team_b": r["team_b"],
            "round_number": r["round_number"], "kickoff_time": r["kickoff_time"],
            "markets": {},
        })
        fx["markets"].setdefault(r["market"], {})[r["model_version"]] = {
            "label": r["label"], "confidence": r["confidence"],
        }
    return list(fixtures.values())


def get_latest_reconciled_round(conn):
    """Full predicted-vs-actual detail for the most recently reconciled
    round, keyed by the REAL round_number (matches.round_number via
    match_ref) -- not the fixture's guessed round_number, which can be off
    by one (see track/reconcile.py). Each market cell carries every live
    model's prediction separately."""
    latest = conn.execute(
        """SELECT m.round_number FROM prediction_results pr
           JOIN matches m ON m.match_id = pr.match_ref
           ORDER BY m.round_number DESC LIMIT 1"""
    ).fetchone()
    if latest is None:
        return None
    round_number = latest["round_number"]

    rows = conn.execute(
        """SELECT f.team_a, f.team_b, m.ft_a, m.ft_b, m.ht_a, m.ht_b,
                  p.model_version, pr.market, pr.predicted_label, pr.actual_label, pr.correct
           FROM prediction_results pr
           JOIN predictions p ON p.prediction_id = pr.prediction_id
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           JOIN matches m ON m.match_id = pr.match_ref
           WHERE m.round_number = ?
           ORDER BY f.team_a, pr.market, p.model_version""",
        (round_number,),
    ).fetchall()

    matches = {}
    for r in rows:
        key = (r["team_a"], r["team_b"])
        m = matches.setdefault(key, {
            "team_a": r["team_a"], "team_b": r["team_b"],
            "ft_a": r["ft_a"], "ft_b": r["ft_b"], "ht_a": r["ht_a"], "ht_b": r["ht_b"],
            "markets": {},
        })
        m["markets"].setdefault(r["market"], {})[r["model_version"]] = {
            "predicted": r["predicted_label"], "actual": r["actual_label"],
            "correct": bool(r["correct"]),
        }
    return {"round_number": round_number, "matches": list(matches.values())}


def get_accuracy_trend(conn, model_version=None):
    """Overall (all-markets-blended) accuracy per actual round,
    chronological. model_version=None blends every live model together."""
    query = """SELECT m.round_number, COUNT(*) AS n, SUM(pr.correct) AS correct
               FROM prediction_results pr
               JOIN matches m ON m.match_id = pr.match_ref"""
    params = ()
    if model_version is not None:
        query += " JOIN predictions p ON p.prediction_id = pr.prediction_id WHERE p.model_version = ?"
        params = (model_version,)
    query += " GROUP BY m.round_number ORDER BY m.round_number"
    rows = conn.execute(query, params).fetchall()
    return [{"round_number": r["round_number"], "n": r["n"], "accuracy": r["correct"] / r["n"]} for r in rows]


def get_models_and_stats(conn):
    """{model_version: accuracy_stats(...)} for every model with live
    predictions -- the per-model comparison the dashboard's accuracy
    section renders one chart per model from."""
    return {m: accuracy_stats(conn, model_version=m) for m in list_model_versions(conn)}
