"""CLI orchestrator for the Zoom prediction pipeline."""
import argparse
import sys

from db.connection import get_connection
from db.upsert import (
    archive_missing_seasons,
    get_or_create_season,
    insert_odds,
    upsert_fixture,
    upsert_matches,
)
from features.build import build_features
from predict.backtest import print_backtest, run_backtest
from predict.baseline import BaselinePredictor
from predict.build import build_predictions
from predict.ml_model import MLPredictor
from scraper.driver import build_driver
from scraper.fingerprint import compute_fingerprint
from scraper.fixtures_scraper import scrape_fixtures_odds
from scraper.results_scraper import scrape_results
from track.reconcile import reconcile
from track.report import print_report
from dashboard.build import build_dashboard

PREDICTORS = {
    "baseline": BaselinePredictor,
    "ml_v1": MLPredictor,
}


def cmd_scrape(args):
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
                if not round_1:
                    print(f"   Skipping a season scrape with no Round 1 data "
                          f"({round_count} rounds seen) -- can't fingerprint")
                    continue

                fingerprint = compute_fingerprint(round_1)

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

            print("\n=== Scraping upcoming fixtures + odds (Premier-Zoom) ===")
            fixtures_raw = scrape_fixtures_odds(driver)
            next_round_number = current_season_max_round + 1

            for i, f in enumerate(fixtures_raw, start=1):
                summary["fixtures_seen"] += 1
                fixture_id, was_new = upsert_fixture(
                    conn,
                    season_id=current_season_id,
                    round_number=next_round_number,
                    match_number=i,
                    team_a=f["team_a"],
                    team_b=f["team_b"],
                    kickoff_time=f["kickoff_time"],
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


def cmd_cycle(args):
    """The full loop: scrape -> features -> predict -> track -> dashboard.
    What the scheduled GitHub Actions run calls every ~90 minutes."""
    cmd_scrape(args)
    print()
    cmd_features(args)
    print()
    cmd_predict(args)
    print()
    cmd_track(args)
    print()
    cmd_dashboard(args)


def main():
    parser = argparse.ArgumentParser(description="Zoom prediction pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    scrape_p = sub.add_parser("scrape", help="Scrape results + fixtures/odds into SQLite")
    scrape_p.add_argument("--headed", action="store_true", help="Run Chrome with a visible window")
    scrape_p.set_defaults(func=cmd_scrape)

    features_p = sub.add_parser("features", help="Build leakage-safe features for all seasons")
    features_p.set_defaults(func=cmd_features)

    predict_p = sub.add_parser("predict", help="Predict the earliest unplayed round")
    predict_p.add_argument("--model", nargs="+", choices=list(PREDICTORS), default=list(PREDICTORS))
    predict_p.set_defaults(func=cmd_predict)

    track_p = sub.add_parser("track", help="Reconcile predictions vs actuals and report accuracy")
    track_p.set_defaults(func=cmd_track)

    backtest_p = sub.add_parser("backtest", help="Cross-validated backtest of ml_v1 vs dumb baselines")
    backtest_p.set_defaults(func=cmd_backtest)

    dashboard_p = sub.add_parser("dashboard", help="Regenerate the static dashboard HTML")
    dashboard_p.set_defaults(func=cmd_dashboard)

    cycle_p = sub.add_parser("cycle", help="Run scrape -> features -> predict -> track -> dashboard")
    cycle_p.add_argument("--headed", action="store_true", help="Run Chrome with a visible window")
    cycle_p.add_argument("--model", nargs="+", choices=list(PREDICTORS), default=list(PREDICTORS))
    cycle_p.set_defaults(func=cmd_cycle)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
