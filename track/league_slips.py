"""Paper tickets for the other Zoom leagues, plus merged cross-league
tickets -- built from the lg_* tables (track/leagues.py collects them).

Per league (Liga, Bundes, Serie A, Ligue 1, Primeira, Eredivisie): the
same 3 tickets as Premier -- 1X2, HT Over/Under 0.5 and BTTS top 5, ₦500
each, ranked by chance exactly like Premier (track/betslips.py). Money is
counted per league season (from ₦0), staked since these tickets were switched
on (2026-10-23) -- from each league's round that was upcoming at switch-on;
earlier rounds are tracking only.

Merged (all 7 leagues incl. Premier, staked from the start): for each of
the 3 markets, the strongest 1 or 2 picks from every league's upcoming
round in one ticket -- 6 tickets, ₦500 each, one set per Premier round,
frozen in lg_merged so later data can't change them.

Expected (analysis 2026-10-23, from Premier's real top picks): merged top 1
about -31%, merged top 2 about -52% -- each pick adds bet9ja's ~5% cut.
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from db.upsert import now_iso
from scraper.zoom_api import place_in_season
from track.betslips import build_slips, played
from track.reconcile import actual_labels

LEAGUES = {2: "Liga", 3: "Bundes", 4: "Serie A", 5: "Ligue 1", 6: "Primeira", 7: "Eredivisie"}
FULL_NAMES = {1: "PREMIER (ENGLAND)", 2: "LIGA (SPAIN)", 3: "BUNDESLIGA (GERMANY)", 4: "SERIE A (ITALY)",
              5: "LIGUE 1 (FRANCE)", 6: "PRIMEIRA (PORTUGAL)", 7: "EREDIVISIE (NETHERLANDS)"}
MERGED_LEAGUES = {1: "Premier", **LEAGUES}
FLAGS = {1: "🏴󠁧󠁢󠁥󠁮󠁧󠁿", 2: "🇪🇸", 3: "🇩🇪", 4: "🇮🇹", 5: "🇫🇷", 6: "🇵🇹", 7: "🇳🇱"}
LEAGUE_PLAN = {"1X2 top 5": 500, "HT_OU0.5 top 5": 500, "BTTS top 5": 500}
MERGED_MARKETS = ["1X2", "HT_OU0.5", "BTTS"]
MERGED_STAKE = 500
MERGED_MIN_LEAGUES = 4
SHORT = {"1X2": "1X2", "HT_OU0.5": "HT O/U 0.5", "BTTS": "BTTS"}


def _rounds_per_season(gpr):
    return 38 if gpr == 10 else 34


def load_league_rounds(conn, cid, now=None):
    """Chronological rounds for one league, in track/betslips.load_rounds'
    shape: settled rounds that have odds, the round being played, and the
    upcoming round. Rounds' "season" is the season's first matchId."""
    now = now or datetime.now(timezone.utc)
    name = MERGED_LEAGUES.get(cid, f"#{cid}")
    odds = defaultdict(lambda: defaultdict(dict))
    for r in conn.execute("""SELECT o.ext_match_id, o.market, o.selection, o.price FROM lg_odds o
                             JOIN lg_events e ON e.ext_match_id = o.ext_match_id WHERE e.competition_id = ?""", (cid,)):
        odds[r["ext_match_id"]][r["market"]][r["selection"]] = r["price"]

    season_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT season_first_id FROM lg_matches WHERE competition_id = ? ORDER BY 1", (cid,))]
    season_no = {sid: i for i, sid in enumerate(season_ids, start=1)}

    def candidates(ext_id, labels):
        out = {}
        for market, book in odds.get(ext_id, {}).items():
            total = sum(1 / p for p in book.values())
            cands = []
            for sel, price in book.items():
                won = None
                if labels is not None:
                    actual = labels.get(market)
                    if actual is None:
                        continue
                    won = actual == sel
                p = (1 / price) / total
                cands.append({"selection": sel, "price": price, "model_p": p, "odds_p": p, "won": won,
                              "ext_match_id": ext_id})
            if cands:
                out[market] = cands
        return out

    rounds = {}
    for m in conn.execute("""SELECT * FROM lg_matches WHERE competition_id = ? ORDER BY season_first_id, round_number""", (cid,)):
        if m["ext_match_id"] not in odds:
            continue
        key = (m["season_first_id"], m["round_number"])
        rnd = rounds.setdefault(key, {
            "key": key, "season": m["season_first_id"], "number": m["round_number"], "league": cid,
            "label": f"S{season_no[m['season_first_id']]} R{m['round_number']}", "status": "settled",
            "season_no": season_no[m["season_first_id"]],
            "sort": f"{m['season_first_id']:012d}-{m['round_number']:02d}", "games": {}})
        labels = actual_labels({"ft_a": m["ft_a"], "ft_b": m["ft_b"], "ht_a": m["ht_a"], "ht_b": m["ht_b"]})
        rnd["games"][m["ext_match_id"]] = {"teams": (m["team_a"], m["team_b"]),
                                           "markets": candidates(m["ext_match_id"], labels)}
    out = [rounds[k] for k in sorted(rounds)]

    # Games without a result yet: place them in their round by matchId.
    cur = conn.execute("""SELECT season_first_id, COUNT(*) AS n FROM lg_matches
                          WHERE competition_id = ? AND round_number = 1 GROUP BY 1 ORDER BY 1 DESC LIMIT 1""",
                       (cid,)).fetchone()
    pending = {}
    for e in conn.execute("""SELECT e.* FROM lg_events e LEFT JOIN lg_matches m ON m.ext_match_id = e.ext_match_id
                             WHERE e.competition_id = ? AND m.ext_match_id IS NULL ORDER BY e.ext_match_id""", (cid,)):
        place = place_in_season(e["ext_match_id"], cur["season_first_id"], cur["n"],
                                _rounds_per_season(cur["n"])) if cur else None
        season, number = (cur["season_first_id"], place[0]) if place else (None, 1)   # None: next season
        try:
            kickoff = datetime.fromisoformat((e["kickoff_time"] or "").replace("Z", "+00:00"))
        except ValueError:
            kickoff = now                                       # unknown: treat as already started
        key = (season, number)
        rnd = pending.setdefault(key, {
            "key": None, "season": season, "number": number, "league": cid, "kickoff": e["kickoff_time"],
            "label": (f"S{season_no.get(season, len(season_no))} R{number}" if season else f"New season R{number}"),
            "season_no": season_no.get(season, len(season_no) + 1),
            "status": "upcoming" if kickoff > now else "playing",
            "sort": f"{season or 10**12 - 1:012d}-{number:02d}", "games": {}})
        rnd["games"][e["ext_match_id"]] = {"teams": (e["team_a"], e["team_b"]), "markets": candidates(e["ext_match_id"], None)}
    for k in sorted(pending, key=lambda k: pending[k]["sort"]):
        if pending[k]["games"]:
            out.append(pending[k])
    return out


def start_from(conn, cid, rounds):
    """(season first matchId, round) a league's money starts at: its
    upcoming round when staking was switched on (user's choice
    2026-10-23: start right away, not at the next season). Recorded once."""
    key = f"start_from:{cid}"
    row = conn.execute("SELECT value FROM lg_settings WHERE key = ?", (key,)).fetchone()
    if row:
        season, number = row["value"].split(":")
        return int(season), int(number)
    up = [r for r in rounds if r["status"] == "upcoming" and r["season"] is not None]
    if not up:
        return None             # e.g. right at a season change: record it on a later run
    season, number = up[-1]["season"], up[-1]["number"]
    conn.execute("INSERT INTO lg_settings (key, value) VALUES (?, ?)", (key, f"{season}:{number}"))
    return season, number


def staked_round(rnd, start):
    """Is this round on or after the league's start point?"""
    if start is None:
        return False
    s0, n0 = start
    season = rnd.get("season")
    if season is None:
        return True                                  # a newer season than the start
    return season > s0 or (season == s0 and rnd["number"] >= n0)


def build_league(conn, cid, now=None):
    rounds = load_league_rounds(conn, cid, now=now)
    start = start_from(conn, cid, rounds)
    return build_slips(rounds, plan_fn=lambda rnd: LEAGUE_PLAN if staked_round(rnd, start) else {})


def league_totals(built):
    """Staked money per league season (from ₦0), plus overall."""
    t = defaultdict(lambda: {"rounds": 0, "slips": 0, "won": 0, "staked": 0.0, "returned": 0.0})
    for e in built:
        if e["round"]["status"] != "settled":
            continue
        slips = [s for s in played(e["slips"]) if s["settled"]]
        if not slips:
            continue
        x = t[e["round"]["season"]]
        x["rounds"] += 1
        for s in slips:
            x["slips"] += 1
            x["won"] += bool(s["won"])
            x["staked"] += s["stake"]
            x["returned"] += s["returned"]
    for x in t.values():
        x["profit"] = x["returned"] - x["staked"]
    return dict(t)


# ---- merged tickets ---------------------------------------------------------

def create_merged(conn, built_by_league, now=None):
    """One set of 6 merged tickets per Premier round: when Premier's next
    round first appears, take every league's next round at that moment.
    All leagues play a round about every 100 minutes, so this uses each
    league round once; a league whose round is already in an earlier set
    is left out rather than bet twice. (Was once an hour until 2026-10-23,
    which could reuse a round in two consecutive hours.) Returns the keys
    created."""
    now = now or datetime.now(timezone.utc)
    premier_up = [e for e in built_by_league.get(1, []) if e["round"]["status"] == "upcoming"]
    if not premier_up:
        return []
    anchor = premier_up[-1]["round"]
    set_id = f"P{anchor['season'] or 'new'}R{anchor['number']}"
    if conn.execute("SELECT 1 FROM lg_merged WHERE hour = ? LIMIT 1", (set_id,)).fetchone():
        return []
    kick = (anchor.get("kickoff") or "")[11:16]
    label = f"Premier R{anchor['number']}" + (f" · from {kick} UTC" if kick else "")
    used = {leg["ext_match_id"] for r in conn.execute(
        "SELECT legs FROM lg_merged ORDER BY created_at DESC LIMIT 60") for leg in json.loads(r["legs"])}
    picks = defaultdict(list)            # market -> [(league, [legs sorted by score])]
    for cid, built in built_by_league.items():
        up = [e for e in built if e["round"]["status"] == "upcoming"]
        if not up or used & set(up[-1]["round"]["games"]):
            continue                     # no next round, or it's already in an earlier set
        for slip in up[-1]["slips"]:
            market = slip["name"].replace(" top 5", "")
            if market in MERGED_MARKETS and slip["name"].endswith(" top 5"):
                picks[market].append((cid, slip["legs"]))
    created = []
    for market in MERGED_MARKETS:
        if len(picks[market]) < MERGED_MIN_LEAGUES:
            continue
        for k in (1, 2):
            legs = [{"league": cid, "teams": list(l["teams"]), "market": market, "selection": l["selection"],
                     "price": l["price"], "odds_p": l["odds_p"], "ext_match_id": l["ext_match_id"]}
                    for cid, ls in picks[market] for l in ls[:k]]
            key = f"{set_id}|{market}|{k}"
            conn.execute("INSERT OR IGNORE INTO lg_merged (ticket_key, hour, market, per_league, legs, created_at, label) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (key, set_id, market, k, json.dumps(legs), now_iso(), label))
            created.append(key)
    return created


def merged_tickets(conn):
    """Every merged ticket, settled where all its games have results."""
    rows = conn.execute("SELECT * FROM lg_merged ORDER BY created_at, market, per_league").fetchall()
    ids = {leg["ext_match_id"] for r in rows for leg in json.loads(r["legs"])}
    results = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        results = {r["ext_match_id"]: dict(r) for r in conn.execute(
            f"SELECT ext_match_id, ft_a, ft_b, ht_a, ht_b FROM lg_matches WHERE ext_match_id IN ({marks})", list(ids))}
    out = []
    for r in rows:
        legs = json.loads(r["legs"])
        for leg in legs:
            res = results.get(leg["ext_match_id"])
            leg["won"] = None if res is None else actual_labels(res).get(leg["market"]) == leg["selection"]
        settled = all(l["won"] is not None for l in legs)
        won = settled and all(l["won"] for l in legs)
        odds = math.prod(l["price"] for l in legs)
        label = r["label"] or f"{r['hour'][11:13]}:00 UTC"     # sets made hourly before 2026-10-23 21:00
        out.append({"key": r["ticket_key"], "hour": r["hour"], "label": label, "market": r["market"], "k": r["per_league"],
                    "legs": legs, "odds": odds, "stake": MERGED_STAKE, "settled": settled,
                    "won": won if settled else None, "returned": MERGED_STAKE * odds if won else 0.0,
                    "legs_won": sum(1 for l in legs if l["won"])})
    return out


def merged_name(t):
    return f"{SHORT[t['market']]} merged top {t['k']}"
