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


def _predictions_by_fixture(conn, fixture_ids):
    """{team pairing + markets} shape shared by every unreconciled-batch
    view (upcoming, pending, superseded) -- one row per fixture, markets
    nested by model."""
    if not fixture_ids:
        return []
    placeholders = ",".join("?" for _ in fixture_ids)
    rows = conn.execute(
        f"""SELECT p.fixture_ref, f.team_a, f.team_b, f.round_number, f.kickoff_time,
                   p.model_version, p.market, p.label, p.confidence
            FROM predictions p
            JOIN fixtures f ON f.fixture_id = p.fixture_ref
            WHERE p.fixture_ref IN ({placeholders})
            ORDER BY f.match_number, p.market, p.model_version""",
        fixture_ids,
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


def get_pending_batches(conn):
    """Every distinct batch of predictions with no result yet, oldest to
    newest -- NOT just the single current one. A batch ends up here for
    two different reasons that look the same from our side: its round is
    still being played and hasn't posted results, or its specific preview
    pairing got superseded by a fresher poll before it ever locked in and
    played (see db/queries.get_current_fixture_batch) -- either way, the
    honest state is "predicted, outcome not known yet," not invisible.
    Without this, a round superseded by a newer batch before its own
    reconciliation would silently disappear from the dashboard entirely --
    visible in neither the "upcoming" slot (no longer current) nor
    "reconciled" (no result exists yet).

    One batch = every fixture sharing the same fixtures.scraped_at, which
    is set once per poll and stops changing for a fixture the moment it's
    no longer the live batch being re-polled every cycle -- a stable
    grouping key for "everything scraped together in one pass."
    """
    rows = conn.execute(
        """SELECT DISTINCT f.fixture_id, f.scraped_at
           FROM predictions p
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           LEFT JOIN prediction_results pr ON pr.prediction_id = p.prediction_id
           WHERE pr.result_id IS NULL"""
    ).fetchall()

    by_ts = {}
    for r in rows:
        by_ts.setdefault(r["scraped_at"], []).append(r["fixture_id"])

    batches = []
    for ts in sorted(by_ts):
        fixtures = _predictions_by_fixture(conn, by_ts[ts])
        if fixtures:
            batches.append({"scraped_at": ts, "round_number": fixtures[0]["round_number"], "fixtures": fixtures})
    return batches


def get_upcoming_predictions(conn):
    """The single most recent pending batch. Thin wrapper around
    get_pending_batches() for callers that only want the live one."""
    batches = get_pending_batches(conn)
    return batches[-1]["fixtures"] if batches else []


def get_reconciled_round(conn, season_id, round_number):
    """Full predicted-vs-actual detail for one specific (season_id,
    round_number). Each market cell carries every live model's prediction
    separately. Scoped by season_id, not just round_number -- round numbers
    reset to 1 every season, so round_number alone can't distinguish
    "season 3's round 4" from "season 1's round 4."
    """
    rows = conn.execute(
        """SELECT f.team_a, f.team_b, m.ft_a, m.ft_b, m.ht_a, m.ht_b, m.scraped_at,
                  p.model_version, pr.market, pr.predicted_label, pr.actual_label, pr.correct
           FROM prediction_results pr
           JOIN predictions p ON p.prediction_id = pr.prediction_id
           JOIN fixtures f ON f.fixture_id = p.fixture_ref
           JOIN matches m ON m.match_id = pr.match_ref
           WHERE m.season_id = ? AND m.round_number = ?
           ORDER BY f.team_a, pr.market, p.model_version""",
        (season_id, round_number),
    ).fetchall()
    if not rows:
        return None

    matches = {}
    sort_key = min(r["scraped_at"] for r in rows)
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
    return {"season_id": season_id, "round_number": round_number, "status": "reconciled",
            "sort_key": sort_key, "matches": list(matches.values())}


def get_round_history(conn, max_reconciled=20):
    """Chronological browsing sequence for the dashboard's </> round
    navigator: up to `max_reconciled` most recently reconciled rounds, plus
    every currently pending batch (including the live "upcoming" one --
    always last, the default view), all interleaved in true time order.

    Sorted by each entry's own scraped_at (matches.scraped_at for
    reconciled rounds, fixtures.scraped_at for pending/upcoming batches) --
    both are set once when first recorded and never touched again, so
    they're stable, genuinely chronological timestamps. NOT round_number
    (resets to 1 every season) and NOT reconciled_at/predicted_at (can get
    re-touched later, e.g. backfilling a second model's predictions for an
    already-played round, which would jumble the order).
    """
    groups = conn.execute(
        """SELECT DISTINCT m.season_id, m.round_number
           FROM prediction_results pr JOIN matches m ON m.match_id = pr.match_ref
           ORDER BY m.season_id DESC, m.round_number DESC
           LIMIT ?""",
        (max_reconciled,),
    ).fetchall()

    entries = []
    for g in groups:
        rr = get_reconciled_round(conn, g["season_id"], g["round_number"])
        if rr:
            entries.append(rr)

    pending = get_pending_batches(conn)
    for i, batch in enumerate(pending):
        status = "upcoming" if i == len(pending) - 1 else "pending"
        entries.append({
            "round_number": batch["round_number"], "status": status,
            "sort_key": batch["scraped_at"], "fixtures": batch["fixtures"],
        })

    entries.sort(key=lambda e: e["sort_key"])
    return entries


def get_accuracy_trend(conn, model_version=None):
    """Overall (all-markets-blended) accuracy per actual round, chronological.
    model_version=None blends every live model together.

    Grouped by (season_id, round_number), NOT round_number alone -- round
    numbers reset to 1 every season, so grouping by round_number alone
    would merge season 1's round 5 and season 3's round 5 into one point.
    The x-position returned is a synthetic sequential index (0, 1, 2, ...),
    not the real round_number, so points from different seasons can never
    collide on the chart; "label" carries the real season/round identity
    for the tooltip.
    """
    query = """SELECT m.season_id, m.round_number, COUNT(*) AS n, SUM(pr.correct) AS correct
               FROM prediction_results pr
               JOIN matches m ON m.match_id = pr.match_ref"""
    params = ()
    if model_version is not None:
        query += " JOIN predictions p ON p.prediction_id = pr.prediction_id WHERE p.model_version = ?"
        params = (model_version,)
    query += " GROUP BY m.season_id, m.round_number ORDER BY m.season_id, m.round_number"
    rows = conn.execute(query, params).fetchall()
    return [
        {"round_number": i, "label": f'S{r["season_id"]} R{r["round_number"]}',
         "n": r["n"], "accuracy": r["correct"] / r["n"]}
        for i, r in enumerate(rows)
    ]


def get_models_and_stats(conn):
    """{model_version: accuracy_stats(...)} for every model with live
    predictions -- the per-model comparison the dashboard's accuracy
    section renders one chart per model from."""
    return {m: accuracy_stats(conn, model_version=m) for m in list_model_versions(conn)}
