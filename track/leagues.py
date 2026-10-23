"""Collect every Zoom league's results and upcoming odds into the lg_*
tables (db/schema.py) -- analysis data only. Nothing here feeds Premier's
predictions, betslips, dashboard money or Telegram.

Each cycle: both visible seasons' results per league (results never change
once posted, so INSERT OR IGNORE), plus odds for any upcoming game not
captured yet (Zoom odds never move after posting, so one capture per game).
The odds-vs-results analysis joins lg_odds to lg_matches by bet9ja's
matchId.
"""
from config import LEAGUE_MARKETS
from db.connection import get_connection
from db.upsert import now_iso
from scraper import zoom_api


def collect_leagues():
    comps = zoom_api.competitions()
    groups = zoom_api.league_groups()
    ts = now_iso()
    summary = {"leagues": len(comps), "new_results": 0, "new_events": 0, "odds_rows": 0, "skipped": []}

    with get_connection() as conn:
        for cid, comp in sorted(comps.items()):
            name = comp["name"]
            for prev in (True, False):
                season = zoom_api.season_results(cid, comp["teams"], previous=prev)
                if season["first_id"] is None:
                    continue
                for round_number, matches in season["rounds"].items():
                    for m in matches:
                        cur = conn.execute(
                            """INSERT OR IGNORE INTO lg_matches
                               (ext_match_id, competition_id, league, season_first_id, round_number, match_number,
                                team_a, team_b, ft_a, ft_b, ht_a, ht_b, round_time, scraped_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (m["ext_match_id"], cid, name, season["first_id"], round_number, m["match_number"],
                             m["team_a"], m["team_b"], m["ft_a"], m["ft_b"], m["ht_a"], m["ht_b"],
                             season["round_times"].get(round_number), ts),
                        )
                        summary["new_results"] += cur.rowcount

            group = groups.get(name)
            if group is None:
                summary["skipped"].append(f"{name}: no sportsbook group right now")
                continue
            for e in zoom_api.upcoming_events(group):
                cur = conn.execute(
                    """INSERT OR IGNORE INTO lg_events
                       (ext_match_id, competition_id, league, event_id, team_a, team_b, kickoff_time, first_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (e["ext_match_id"], cid, name, e["event_id"], e["team_a"], e["team_b"], e["kickoff_time"], ts),
                )
                summary["new_events"] += cur.rowcount
                have = conn.execute("SELECT 1 FROM lg_odds WHERE ext_match_id = ? LIMIT 1", (e["ext_match_id"],)).fetchone()
                if have:
                    continue
                for market, selections in zoom_api.event_odds(e["event_id"]).items():
                    if market not in LEAGUE_MARKETS:
                        continue
                    for selection, price in selections.items():
                        conn.execute(
                            "INSERT OR IGNORE INTO lg_odds (ext_match_id, market, selection, price, captured_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (e["ext_match_id"], market, selection, price, ts),
                        )
                        summary["odds_rows"] += 1
            conn.commit()

        # Freeze this hour's merged cross-league tickets (track/league_slips.py).
        from track.league_slips import MERGED_LEAGUES, build_league, create_merged
        summary["merged_created"] = len(create_merged(conn, {c: build_league(conn, c) for c in MERGED_LEAGUES}))
    if not summary["skipped"]:
        summary.pop("skipped")
    return summary
