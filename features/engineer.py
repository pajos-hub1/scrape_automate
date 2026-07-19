"""Vectorized feature engineering.

Replaces the notebook's O(n^2) row-loops (`df[df['Team A']==x].tail(5)` per
row, repeated for every row) with pandas groupby + shift + rolling /
expanding / cumsum -- each feature group is one pass over the data.

Design choices, both deliberate departures from the old notebook:

- Season-scoped. Every feature (form, H2H, home/away splits, season-to-date
  rates) only looks within the current season. Each season is an
  independent RNG realization -- the standings table resets to 0 points at
  the start of every season -- so a team's history from a prior season
  carries no real signal for the current one, and mixing them would blur
  the leakage-safety story.

- Leakage-safety is enforced at ROUND granularity, not row/match-number
  granularity. All 10 matches in a round happen simultaneously, so a
  match's features may never depend on another match from its own round,
  even one with a smaller match_number -- the notebook's row-index-based
  cutoff (`df.index < idx`) was technically leaky here, since rows were
  sorted by (round, match_number) and a later match_number in the same
  round would still satisfy `index < idx` for an even-later one.

Upcoming fixtures reuse the exact same pipeline: they're appended as a
virtual extra round with unknown scores (NaN), so every shift/rolling/
cumsum op -- which only ever pulls from strictly prior rows -- produces
correct pre-kickoff features for them "for free," with no separate code
path.
"""
import numpy as np
import pandas as pd

FORM_WINDOW = 5


def _long_format(all_matches):
    """One row per (team, round) instead of one row per match."""
    home = all_matches[["row_id", "round_number", "team_a", "team_b", "ft_a", "ft_b"]].rename(
        columns={"team_a": "team", "team_b": "opponent", "ft_a": "gf", "ft_b": "ga"}
    )
    home["is_home"] = 1
    away = all_matches[["row_id", "round_number", "team_b", "team_a", "ft_b", "ft_a"]].rename(
        columns={"team_b": "team", "team_a": "opponent", "ft_b": "gf", "ft_a": "ga"}
    )
    away["is_home"] = 0
    long_df = pd.concat([home, away], ignore_index=True)

    played = long_df["gf"].notna() & long_df["ga"].notna()
    long_df["pts"] = np.where(
        played, np.select([long_df.gf > long_df.ga, long_df.gf == long_df.ga], [3, 1], default=0), np.nan
    )
    long_df["win"] = np.where(played, (long_df.gf > long_df.ga).astype(float), np.nan)
    long_df["draw"] = np.where(played, (long_df.gf == long_df.ga).astype(float), np.nan)
    long_df["loss"] = np.where(played, (long_df.gf < long_df.ga).astype(float), np.nan)
    return long_df.sort_values(["team", "round_number"]).reset_index(drop=True)


def _form_and_season_state(long_df):
    g = long_df.groupby("team", sort=False)

    long_df["form_games"] = g.cumcount().clip(upper=FORM_WINDOW)
    for col, out in [("pts", "form_pts"), ("win", "form_wins"), ("draw", "form_draws"),
                      ("loss", "form_losses"), ("gf", "form_gf_sum"), ("ga", "form_ga_sum")]:
        long_df[out] = g[col].transform(lambda s: s.shift(1).rolling(FORM_WINDOW, min_periods=0).sum())
    long_df["form_gf"] = long_df["form_gf_sum"] / long_df["form_games"].replace(0, np.nan)
    long_df["form_ga"] = long_df["form_ga_sum"] / long_df["form_games"].replace(0, np.nan)

    long_df["season_games"] = g.cumcount()
    for col, out in [("pts", "season_pts_sum"), ("gf", "season_gf_sum"), ("ga", "season_ga_sum")]:
        long_df[out] = g[col].transform(lambda s: s.shift(1).expanding().sum())
    games_or_nan = long_df["season_games"].replace(0, np.nan)
    long_df["season_pts_rate"] = long_df["season_pts_sum"] / games_or_nan
    long_df["season_gf_avg"] = long_df["season_gf_sum"] / games_or_nan
    long_df["season_ga_avg"] = long_df["season_ga_sum"] / games_or_nan
    return long_df


def _asof_venue_state(long_df, is_home, teams, max_round):
    """As-of-round cumulative record for a team's home (is_home=True) or
    away (is_home=False) appearances only. One row per (team, round) for
    every round 1..max_round, giving the record strictly BEFORE that round
    -- forward-filled across rounds played at the other venue, so it
    always reflects "record so far," not just "record as of last home game."
    """
    venue_flag = 1 if is_home else 0
    sub = long_df[long_df["is_home"] == venue_flag].copy()
    sub["played"] = sub["gf"].notna().astype(float)
    for c in ["played", "win", "draw", "loss", "gf", "ga"]:
        sub[c] = sub[c].fillna(0)

    cum_cols = ["played", "win", "draw", "loss", "gf", "ga"]
    sub = sub.sort_values(["team", "round_number"])
    g = sub.groupby("team", sort=False)
    for c in cum_cols:
        sub[c] = g[c].cumsum()

    full_index = pd.MultiIndex.from_product([teams, range(1, max_round + 1)], names=["team", "round_number"])
    grid = sub.set_index(["team", "round_number"])[cum_cols].reindex(full_index)
    grid = grid.groupby(level="team").ffill().fillna(0)
    grid = grid.groupby(level="team").shift(1).fillna(0)
    return grid.reset_index()


def _h2h_features(all_matches):
    """Bounded per pair (two teams meet at most twice a season under a
    double round-robin), so a small inner loop per group is O(n) overall,
    not O(n^2) -- just grouped instead of globally vectorized, since the
    "which side is my opponent's win attributed to" logic needs per-pair
    chronological state.
    """
    df = all_matches.sort_values(["round_number"]).copy()
    df["pair_key"] = [tuple(sorted((a, b))) for a, b in zip(df.team_a, df.team_b)]

    rows = []
    for _, group in df.groupby("pair_key", sort=False):
        history = []
        for row in group.itertuples():
            games = len(history)
            a_wins = b_wins = draws = 0
            goal_sum = 0
            for (ha, hb, hfa, hfb) in history:
                goal_sum += hfa + hfb
                if hfa == hfb:
                    draws += 1
                elif (ha if hfa > hfb else hb) == row.team_a:
                    a_wins += 1
                else:
                    b_wins += 1
            rows.append({
                "row_id": row.row_id,
                "h2h_games": games,
                "h2h_a_wins": a_wins,
                "h2h_b_wins": b_wins,
                "h2h_draws": draws,
                "h2h_avg_goals": (goal_sum / games) if games else None,
            })
            if pd.notna(row.ft_a) and pd.notna(row.ft_b):
                history.append((row.team_a, row.team_b, row.ft_a, row.ft_b))
    return pd.DataFrame(rows)


def build_season_features(played_matches, fixture_rows=None):
    """played_matches: DataFrame for ONE season, columns
    [match_id, round_number, match_number, team_a, team_b, ft_a, ft_b].
    fixture_rows: optional DataFrame of upcoming fixtures for the SAME
    season, columns [fixture_id, round_number, match_number, team_a, team_b]
    -- scores unknown, appended as a virtual extra round.

    Returns one row per input row with every a_*/b_*/h2h_*/season_* feature
    column, plus match_id/fixture_id to key the upsert (exactly one is set
    per row).
    """
    played = played_matches.copy()
    played["match_id"] = played["match_id"]
    played["fixture_id"] = None
    played["ft_a"] = played["ft_a"].astype(float)
    played["ft_b"] = played["ft_b"].astype(float)

    if fixture_rows is not None and len(fixture_rows):
        fx = fixture_rows.copy()
        fx["match_id"] = None
        fx["ft_a"] = np.nan
        fx["ft_b"] = np.nan
        all_matches = pd.concat([played, fx], ignore_index=True, sort=False)
    else:
        all_matches = played

    all_matches = all_matches.sort_values(["round_number", "match_number"]).reset_index(drop=True)
    all_matches["row_id"] = range(len(all_matches))
    max_round = int(all_matches["round_number"].max())
    teams = sorted(set(all_matches["team_a"]) | set(all_matches["team_b"]))

    long_df = _form_and_season_state(_long_format(all_matches))

    state_cols = ["form_games", "form_pts", "form_wins", "form_draws", "form_losses", "form_gf", "form_ga",
                  "season_games", "season_pts_rate", "season_gf_avg", "season_ga_avg"]
    team_state = long_df.set_index(["team", "round_number"])[state_cols]

    home_state = _asof_venue_state(long_df, True, teams, max_round)
    away_state = _asof_venue_state(long_df, False, teams, max_round)

    feat = all_matches[["row_id", "match_id", "fixture_id", "round_number", "match_number",
                         "team_a", "team_b"]].copy()

    for prefix, side_col in [("a", "team_a"), ("b", "team_b")]:
        lookup = team_state.reset_index().rename(columns={"team": side_col})
        lookup = lookup.rename(columns={c: f"{prefix}_{c}" for c in state_cols})
        feat = feat.merge(lookup, how="left", on=[side_col, "round_number"])

    home_lookup = home_state.rename(columns={
        "team": "team_a", "played": "a_home_played", "win": "a_home_wins",
        "draw": "a_home_draws", "loss": "a_home_losses", "gf": "a_home_gf_sum", "ga": "a_home_ga_sum",
    })
    feat = feat.merge(home_lookup, how="left", on=["team_a", "round_number"])
    feat["a_home_gf"] = feat["a_home_gf_sum"] / feat["a_home_played"].replace(0, np.nan)
    feat["a_home_ga"] = feat["a_home_ga_sum"] / feat["a_home_played"].replace(0, np.nan)

    away_lookup = away_state.rename(columns={
        "team": "team_b", "played": "b_away_played", "win": "b_away_wins",
        "draw": "b_away_draws", "loss": "b_away_losses", "gf": "b_away_gf_sum", "ga": "b_away_ga_sum",
    })
    feat = feat.merge(away_lookup, how="left", on=["team_b", "round_number"])
    feat["b_away_gf"] = feat["b_away_gf_sum"] / feat["b_away_played"].replace(0, np.nan)
    feat["b_away_ga"] = feat["b_away_ga_sum"] / feat["b_away_played"].replace(0, np.nan)

    h2h = _h2h_features(all_matches)
    feat = feat.merge(h2h, how="left", on="row_id")

    feat = feat.drop(columns=["a_home_gf_sum", "a_home_ga_sum", "b_away_gf_sum", "b_away_ga_sum"])
    return feat
