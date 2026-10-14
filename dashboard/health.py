"""Pipeline health for the dashboard's top panel -- so a broken pipeline is
visible at a glance instead of discovered weeks later (as happened when
every push silently failed from 2026-09-14 to 2026-10-10).

Two sources:
  - the DB itself: rounds whose results came in over the last 24h with no
    prediction ever made against them (a missed fixture-preview window).
  - the GitHub Actions API: recent cycle runs and their outcomes. Only
    available inside the workflow (GITHUB_REPOSITORY + GH_TOKEN env); a
    local `run.py dashboard` just leaves those tiles out.
"""
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fetch_recent_runs(hours=24):
    """[{created_at, conclusion}] for cycle.yml runs started in the last
    `hours`, or None when not running inside GitHub Actions / on any API
    error -- health info must never be able to fail the dashboard build."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN")
    if not repo or not token:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/cycle.yml/runs?per_page=100",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            runs = json.load(resp)["workflow_runs"]
    except Exception as e:  # noqa: BLE001 -- best-effort, see docstring
        print(f"   (health: couldn't fetch workflow runs: {e})")
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    return [
        {"created_at": _parse_ts(r["created_at"]), "conclusion": r["conclusion"]}
        for r in runs
        if _parse_ts(r["created_at"]) >= cutoff and r["conclusion"] != "cancelled"
    ]


def missed_rounds_last_24h(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT m.season_id, m.round_number, MIN(m.scraped_at) AS first_seen
           FROM matches m
           WHERE m.ft_a IS NOT NULL
           GROUP BY m.season_id, m.round_number
           HAVING first_seen >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM all_prediction_results pr
                  JOIN matches m2 ON m2.match_id = pr.match_ref
                  WHERE m2.season_id = m.season_id AND m2.round_number = m.round_number)""",
        (cutoff,),
    ).fetchall()
    return [f"S{r['season_id']} R{r['round_number']}" for r in rows]


def get_health(conn):
    runs = fetch_recent_runs()
    health = {"missed_rounds": missed_rounds_last_24h(conn), "runs": None, "telegram": None}
    row = conn.execute("SELECT at, summary FROM notify_status WHERE id = 1").fetchone()
    if row:
        health["telegram"] = {"at": row["at"], **json.loads(row["summary"])}
    if runs is not None:
        done = [r for r in runs if r["conclusion"] is not None]
        failed = [r for r in done if r["conclusion"] != "success"]
        ok = [r for r in done if r["conclusion"] == "success"]
        health["runs"] = {
            "total": len(done),
            "failed": len(failed),
            "last_success": max((r["created_at"] for r in ok), default=None),
            "last_failure": max((r["created_at"] for r in failed), default=None),
        }
    return health
