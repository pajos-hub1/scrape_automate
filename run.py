"""CLI orchestrator for the Zoom prediction pipeline."""
import argparse
import sys

from config import BASE_DIR, DB_PATH, PREMIER_COMPETITION_ID, ROUNDS_PER_SEASON
from db.archive import archive_if_needed, merge_archives_if_needed
from db.connection import get_connection
from db.storage import pack
from track.leagues import collect_leagues
from scraper import zoom_api
from db.upsert import (
    archive_missing_seasons,
    find_season_by_results,
    get_or_create_season,
    insert_odds,
    now_iso,
    upsert_fixture,
    upsert_matches,
)
from features.build import build_features
from predict.backtest import print_backtest, run_backtest
from predict.baseline import BaselinePredictor
from predict.blend import BlendPredictor
from predict.build import build_predictions
from predict.ml_model import MLPredictor
from scraper.driver import build_driver
from scraper.fingerprint import compute_fingerprint
from scraper.fixtures_scraper import scrape_fixtures_odds
from scraper.results_scraper import scrape_live_round, scrape_results
from track.notify import notify
from track.reconcile import reconcile
from track.report import print_report
from dashboard.build import build_dashboard

PREDICTORS = {
    "baseline": BaselinePredictor,
    "ml_v1": MLPredictor,
    "blend_v1": BlendPredictor,
}

# What `predict`/`cycle` run by default. Only the odds-based baseline: Zoom's
# odds turned out to be near-perfectly calibrated (see track/report.py
# profit_stats), and over ~20k live predictions ml_v1 and blend_v1 never
# beat them beyond noise -- so they're retired from the live cycle (2026-10)
# to save the per-run training time. Their code and history stay; pass
# --model ml_v1 blend_v1 to run them again.
DEFAULT_MODELS = ["baseline"]


def cmd_scrape(args):
    """Premier results + upcoming fixtures/odds. Uses bet9ja's JSON APIs
    (scraper/zoom_api.py); falls back to the browser scraper if they fail."""
    if not getattr(args, "browser", False):
        try:
            _scrape_via_api()
            return
        except Exception as e:  # noqa: BLE001 -- any API problem: fall back, don't lose the cycle
            print(f"\n!!! API scrape failed ({type(e).__name__}: {e}) -- falling back to the browser scraper\n")
    _scrape_via_browser(args)


def _scrape_via_api():
    summary = {"seasons_seen": 0, "new_seasons": 0, "seasons_archived": 0, "new_rounds": 0,
               "new_matches": 0, "fixtures_seen": 0, "fixtures_new": 0, "odds_captured": 0}
    comps = zoom_api.competitions()
    premier = comps[PREMIER_COMPETITION_ID]
    seasons = {prev: zoom_api.season_results(PREMIER_COMPETITION_ID, premier["teams"], previous=prev)
               for prev in (True, False)}
    groups = zoom_api.league_groups()
    events = zoom_api.upcoming_events(groups[premier["name"]])
    odds = {e["ext_match_id"]: zoom_api.event_odds(e["event_id"]) for e in events}

    with get_connection() as conn:
        print("=== Results via API (Premier-Zoom) ===")
        touched, season_id_for = [], {}
        for prev, season in seasons.items():
            rounds = season["rounds"]
            if not rounds:
                continue
            summary["seasons_seen"] += 1
            if 1 not in rounds:
                print(f"   {'previous' if prev else 'current'} season: no Round 1 -- skipped")
                continue
            fingerprint = compute_fingerprint(rounds[1])
            is_new = conn.execute("SELECT 1 FROM seasons WHERE fingerprint = ?", (fingerprint,)).fetchone() is None
            season_id = get_or_create_season(conn, fingerprint, len(rounds))
            touched.append(season_id)
            season_id_for[prev] = season_id
            summary["new_seasons"] += is_new
            print(f"   {'previous' if prev else 'current'} season -> season_id={season_id}"
                  f"{' (new)' if is_new else ''}, {len(rounds)} rounds")
            for round_number, matches in rounds.items():
                for m in matches:
                    m["round_number"] = round_number
                    m["played_at"] = season["round_times"].get(round_number)
                inserted = upsert_matches(conn, season_id, matches)
                if inserted:
                    summary["new_rounds"] += 1
                    summary["new_matches"] += inserted

        archived = archive_missing_seasons(conn, touched)
        summary["seasons_archived"] = len(archived)

        print("\n=== Upcoming fixtures + odds via API (Premier-Zoom) ===")
        cur = seasons[False]
        ref = cur if cur["first_id"] else seasons[True]        # new season with no results yet
        scrape_ts = now_iso()
        batch_min = min((e["ext_match_id"] for e in events), default=0)
        for e in events:
            place = zoom_api.place_in_season(e["ext_match_id"], cur["first_id"], cur["games_per_round"],
                                             ROUNDS_PER_SEASON)
            if place is not None:
                (round_number, match_number), season_id = place, season_id_for.get(False)
            else:
                # Past the current season's range: a new season's game, whose
                # season isn't known until its Round 1 finishes. (Its result
                # still joins exactly, by ext_match_id.)
                gpr = ref["games_per_round"] or 10
                round_number = 1
                match_number = (e["ext_match_id"] - batch_min) % gpr + 1
                season_id = None
            summary["fixtures_seen"] += 1
            fixture_id, was_new = upsert_fixture(
                conn, season_id=season_id, round_number=round_number, match_number=match_number,
                team_a=e["team_a"], team_b=e["team_b"], kickoff_time=e["kickoff_time"],
                scraped_at=scrape_ts, ext_match_id=e["ext_match_id"],
            )
            summary["fixtures_new"] += was_new
            for market, selections in odds[e["ext_match_id"]].items():
                for selection, price in selections.items():
                    if price and insert_odds(conn, fixture_id, market, selection, price, round(1.0 / price, 4)):
                        summary["odds_captured"] += 1

    print("\n=== Scrape summary (API) ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def cmd_leagues(args):
    """Collect every Zoom league's results and upcoming odds into the lg_*
    tables, for analysis only (see db/schema.py)."""
    summary = collect_leagues()
    print("=== Other leagues (analysis data) ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def _scrape_via_browser(args):
    driver = build_driver(headless=not args.headed)
    summary = {
        "seasons_seen": 0,
        "new_seasons": 0,
        "seasons_archived": 0,
        "new_rounds": 0,
        "new_matches": 0,
        "fixtures_seen": 0,
        "fixtures_new": 0,
        "odds_captured": 0,
    }

    try:
        with get_connection() as conn:
            print("=== Scraping results (played rounds) ===")
            season_scrapes = scrape_results(driver)

            touched_season_ids = []

            for season in season_scrapes:
                rounds = season["rounds"]
                round_count = season["round_count"]
                summary["seasons_seen"] += 1

                round_1 = rounds.get(1)
                if round_1:
                    fingerprint = compute_fingerprint(round_1)
                else:
                    # Round 1 couldn't be read this pass: recognise the season
                    # from any round we've already stored instead. Skipping it
                    # used to get the season archived as "gone" (2026-10-23).
                    known = find_season_by_results(conn, rounds)
                    if known is None:
                        print(f"   Skipping a season scrape with no Round 1 data "
                              f"({round_count} rounds seen) -- can't fingerprint or recognise it")
                        continue
                    fingerprint = conn.execute(
                        "SELECT fingerprint FROM seasons WHERE season_id = ?", (known,)
                    ).fetchone()["fingerprint"]
                    print(f"   No Round 1 this pass -- recognised season_id={known} from stored results")

                existing = conn.execute(
                    "SELECT season_id FROM seasons WHERE fingerprint = ?", (fingerprint,)
                ).fetchone()
                is_new_season = existing is None

                season_id = get_or_create_season(conn, fingerprint, round_count)
                touched_season_ids.append(season_id)

                stored = conn.execute(
                    "SELECT status, round_count FROM seasons WHERE season_id = ?", (season_id,)
                ).fetchone()
                if is_new_season:
                    summary["new_seasons"] += 1
                    print(f"   New season detected: season_id={season_id} "
                          f"status={stored['status']} round_count={stored['round_count']} "
                          f"(this pass saw {round_count})")
                else:
                    print(f"   Season recognized: season_id={season_id} "
                          f"status={stored['status']} round_count={stored['round_count']} "
                          f"(this pass saw {round_count})")

                for round_number, matches in rounds.items():
                    for m in matches:
                        m["round_number"] = round_number
                    inserted = upsert_matches(conn, season_id, matches)
                    if inserted:
                        summary["new_rounds"] += 1
                        summary["new_matches"] += inserted

            live_round = scrape_live_round(driver)
            if live_round is not None:
                print(f"   Live round right now: {live_round}")

            archived = archive_missing_seasons(conn, touched_season_ids)
            summary["seasons_archived"] = len(archived)
            if archived:
                print(f"   Archived season(s) no longer visible on the site: {archived}")

            current = conn.execute(
                "SELECT season_id FROM seasons WHERE status = 'current'"
            ).fetchone()
            current_season_id = current["season_id"] if current else None
            max_round_row = None
            if current_season_id is not None:
                max_round_row = conn.execute(
                    "SELECT MAX(round_number) AS mr FROM matches WHERE season_id = ?",
                    (current_season_id,),
                ).fetchone()
            current_season_max_round = (max_round_row["mr"] if max_round_row and max_round_row["mr"] else 0)

            # Prefer the live round straight from the site over our own
            # inference -- it's ground truth (scrape_live_round), not a
            # guess, and correctly handles the season-boundary case where
            # our recorded history hasn't caught up yet.
            if live_round is not None and live_round >= ROUNDS_PER_SEASON:
                # Live round is the season finale -- whatever the fixtures
                # page shows next is a NEW season's Round 1. We can't attach
                # a season_id to it yet: that season won't exist in our DB
                # until its own Round 1 finishes and gets fingerprinted.
                # Leaving season_id unset (None) is the honest state --
                # track/reconcile.py's cross-season fallback finds it later
                # once the new season is known.
                next_round_number = 1
                fixtures_season_id = None
                print(f"   Live round {live_round} is the season finale -- next fixtures "
                      f"are a new season's Round 1 (season_id unresolved for now)")
            elif live_round is not None:
                next_round_number = live_round + 1
                fixtures_season_id = current_season_id
            else:
                next_round_number = current_season_max_round + 1
                fixtures_season_id = current_season_id

            print("\n=== Scraping upcoming fixtures + odds (Premier-Zoom) ===")
            fixtures_raw = scrape_fixtures_odds(driver)
            # One shared timestamp for this whole batch -- see upsert_fixture's
            # docstring: every fixture from this poll must carry the identical
            # scraped_at so db/queries.get_current_fixture_batch can tell this
            # poll apart from an older, superseded one.
            scrape_ts = now_iso()

            for i, f in enumerate(fixtures_raw, start=1):
                summary["fixtures_seen"] += 1
                fixture_id, was_new = upsert_fixture(
                    conn,
                    season_id=fixtures_season_id,
                    round_number=next_round_number,
                    match_number=i,
                    team_a=f["team_a"],
                    team_b=f["team_b"],
                    kickoff_time=f["kickoff_time"],
                    scraped_at=scrape_ts,
                )
                if was_new:
                    summary["fixtures_new"] += 1

                for market, selections in f["odds"].items():
                    for selection, price in selections.items():
                        if price is None:
                            continue
                        implied_prob = round(1.0 / price, 4)
                        if insert_odds(conn, fixture_id, market, selection, price, implied_prob):
                            summary["odds_captured"] += 1

        print("\n=== Scrape summary ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")

    finally:
        driver.quit()


def cmd_features(args):
    with get_connection() as conn:
        summary = build_features(conn)
    print("=== Feature engineering summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def cmd_predict(args):
    with get_connection() as conn:
        for name in args.model:
            predictor = PREDICTORS[name](conn)
            summary = build_predictions(conn, predictor)
            print(f"=== Prediction summary (model={predictor.model_version}) ===")
            for k, v in summary.items():
                print(f"  {k}: {v}")
            print()


def cmd_track(args):
    with get_connection() as conn:
        summary = reconcile(conn)
        print("=== Reconciliation summary ===")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print()
        print_report(conn)


def cmd_backtest(args):
    with get_connection() as conn:
        results = run_backtest(conn)
    print_backtest(results)


def cmd_dashboard(args):
    with get_connection() as conn:
        path = build_dashboard(conn)
    print(f"=== Dashboard written to {path} ===")


def cmd_archive(args):
    with get_connection() as conn:
        summary = archive_if_needed(conn, force=getattr(args, "force", False))
    print("=== Archive summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    # After the connection above closes -- it has the archives attached.
    merge = merge_archives_if_needed(force=getattr(args, "merge", False))
    if merge["merged"]:
        print("=== Archive merge ===")
        for k, v in merge.items():
            print(f"  {k}: {v}")


def cmd_pack(args):
    """Gzip the live DB for committing -- git only tracks data/zoom.db.gz
    (GitHub rejects files over 100 MiB; see config.py)."""
    gz = pack(DB_PATH)
    print(f"=== Packed {DB_PATH.name} -> {gz.name} ({gz.stat().st_size / 1048576:.1f} MB) ===")


def cmd_notify(args):
    with get_connection() as conn:
        summary = notify(conn)
    print("=== Telegram ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    # If even the owner's chat can't be reached, Telegram can't tell anyone --
    # leave a marker so the workflow marks the run failed after saving data.
    marker = BASE_DIR / "telegram_owner_failed"
    if summary.get("owner_failed"):
        marker.write_text("\n".join(summary.get("failed", [])))
    elif marker.exists():
        marker.unlink()


def cmd_cycle(args):
    """The full loop: scrape -> features -> predict -> track -> archive ->
    dashboard -> notify -> pack. What the GitHub Actions run calls."""
    cmd_scrape(args)
    print()
    try:
        cmd_leagues(args)              # analysis-only; must never break the Premier cycle
    except Exception as e:  # noqa: BLE001
        print(f"!!! other-leagues collection failed ({type(e).__name__}: {e}) -- continuing")
    print()
    cmd_features(args)
    print()
    cmd_predict(args)
    print()
    cmd_track(args)
    print()
    cmd_archive(args)
    print()
    cmd_dashboard(args)
    print()
    cmd_notify(args)
    print()
    cmd_pack(args)


def main():
    parser = argparse.ArgumentParser(description="Zoom prediction pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    scrape_p = sub.add_parser("scrape", help="Scrape results + fixtures/odds into SQLite")
    scrape_p.add_argument("--headed", action="store_true", help="Run Chrome with a visible window")
    scrape_p.add_argument("--browser", action="store_true", help="Use the old browser scraper instead of the APIs")
    scrape_p.set_defaults(func=cmd_scrape)

    features_p = sub.add_parser("features", help="Build leakage-safe features for all seasons")
    features_p.set_defaults(func=cmd_features)

    predict_p = sub.add_parser("predict", help="Predict the earliest unplayed round")
    predict_p.add_argument("--model", nargs="+", choices=list(PREDICTORS), default=DEFAULT_MODELS)
    predict_p.set_defaults(func=cmd_predict)

    track_p = sub.add_parser("track", help="Reconcile predictions vs actuals and report accuracy")
    track_p.set_defaults(func=cmd_track)

    backtest_p = sub.add_parser("backtest", help="Cross-validated backtest of ml_v1 vs dumb baselines")
    backtest_p.set_defaults(func=cmd_backtest)

    dashboard_p = sub.add_parser("dashboard", help="Regenerate the static dashboard HTML")
    dashboard_p.set_defaults(func=cmd_dashboard)

    archive_p = sub.add_parser("archive", help="Move old reconciled rows into a frozen archive file if the DB is large")
    archive_p.add_argument("--force", action="store_true", help="Archive even if under the size trigger")
    archive_p.add_argument("--merge", action="store_true", help="Merge all archive files into one now")
    archive_p.set_defaults(func=cmd_archive)

    notify_p = sub.add_parser("notify", help="Send pending Telegram messages (needs TELEGRAM_* env)")
    notify_p.set_defaults(func=cmd_notify)

    pack_p = sub.add_parser("pack", help="Gzip data/zoom.db -> data/zoom.db.gz for committing")
    pack_p.set_defaults(func=cmd_pack)

    cycle_p = sub.add_parser("cycle", help="Run scrape -> features -> predict -> track -> archive -> dashboard -> pack")
    cycle_p.add_argument("--headed", action="store_true", help="Run Chrome with a visible window")
    cycle_p.add_argument("--browser", action="store_true", help="Use the old browser scraper instead of the APIs")
    leagues_p = sub.add_parser("leagues", help="Collect all Zoom leagues' results + odds (analysis only)")
    leagues_p.set_defaults(func=cmd_leagues)
    cycle_p.add_argument("--model", nargs="+", choices=list(PREDICTORS), default=DEFAULT_MODELS)
    cycle_p.set_defaults(func=cmd_cycle)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
