"""Upcoming fixtures + odds scraper.

- https://sports.bet9ja.com/mobile/sport/zoomsoccer/101 lists events across
  ALL zoom leagues combined (World Cup-Zoom, Premier-Zoom, Liga-Zoom, ...).
  There is no text-labelled league tab -- the selector is a row of
  icon-only flags at `div.nav__options-flags > div.nav__options-item-flag`,
  each wrapping an `<i class="flag flag-XX">` (flag-en = Premier League
  Zoom, flag-zoomworldcup = World Cup, etc). The active one carries
  `.selected`. As with the results page, only a real (ActionChains) click
  switches leagues -- a JS-dispatched click is ignored.
- Each fixture card is a `div.home-page-nav__content` with two children:
    1. `div.table-f > .match-content__row--league` -- "Premier-Zoom - Premier-Zoom"
    2. `div.table-a` containing `div.match-content__info#match_info_<id>`
       with two `.match-content__row--team` divs (home, away) and a
       `.match-content__row--info` with kickoff time text.
  `div.table-f` is NOT a unique card selector on its own -- it's reused
  throughout the page (headers, per-market panels). Anchor everything off
  `.match-content__row--league` / `.match-content__info` instead. The
  numeric id in `match_info_<id>` (the "match_ext_id") is bet9ja's own
  event id, stable across the list page and the per-event detail page.
- Per-event detail page: every fixture card is clickable through to
  https://sports.bet9ja.com/mobile/eventdetail/zoomsoccer/<slug>/<slug>/<slug>/<match_ext_id>/VS_1X2
  -- but the slug segments turn out to be cosmetic. Navigating straight to
  .../eventdetail/zoomsoccer/x/x/x/<match_ext_id>/VS_1X2 with garbage
  placeholders resolves to the exact same page (confirmed live), so this
  page never needs to be reached by clicking a card -- direct navigation
  using just the id already scraped off the list page works, is far more
  robust than re-locating+clicking a card per fixture, and needs no waiting
  for market-tab JS to re-render.
- The detail page renders EVERY market at once (confirmed ~26 for a Zoom
  fixture: 1X2, 1X2 1UP/2UP, Double Chance, Over/Under Goals at 4 lines,
  Correct Score, BTTS, Home/Away O/U, 1X2 & O/U combos, Half Time/Full
  Time, Handicap, DC & O/U, Multi Goal, Draw No Bet, Multi Correct Score,
  and several combo/early-minute markets) -- no clicking or tab-switching
  needed, unlike the old global-market-tab approach this replaces (which
  could only ever get 1X2 + BTTS, and never cracked O/U Goals at all).
  Each market lives in a `div.accordion-box` under `div.match__markets`,
  already expanded (`.open`) with no interaction required. Each box's
  `div.accordion-toggle` carries a stable `data-anchor` (e.g. "VS_1X2",
  "VS_GGNG", "VS_OU", "VS_CS", "VS_HTFT") -- keying off that, not the
  human-readable title text, since the title text's case/wording varies
  ("Correct score" vs "CORRECT SCORE" is just CSS text-transform) and
  isn't guaranteed stable across bet9ja UI tweaks the way an internal
  anchor key is.
  - Flat markets (1X2, BTTS, Half Time/Full Time, Correct Score) render as
    `div.match-scores__row > div.match-scores__item > div.table-f` pairs
    of `div.elem` (label) + `div.odd` (price) -- one item per selection.
  - The Over/Under Goals market is the one exception: each
    `div.match-scores__row` bundles a `div.match-scores__item.selection`
    (the goal line, e.g. "1.5") followed by two plain `.match-scores__item`
    entries (Over, Under) -- the goal line has to be carried as context for
    the two prices that follow it in the same row.
  1X2 selections come back as "1"/"X"/"2" on this page (not the list page's
  positional Home/Draw/Away) -- mapped to Home/Draw/Away here to keep every
  downstream consumer (ODDS_FIELD_MAP, reconcile.py) on one convention.
  Correct Score selections come back "1:0" -- normalized to "1-0" to match
  actual_labels()'s `f"{ft_a}-{ft_b}"` format in track/reconcile.py.
- The page shows no round number, only kickoff time -- round_number for a
  scraped fixture has to be inferred by the caller (next round after the
  latest played round in the current season), not read off this page.
"""
import re
import time

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains

from config import FIXTURES_URL, LEAGUE_FILTER

FLAG_EN_SELECTOR = "i.flag-en"
LEAGUE_ROW_SELECTOR = ".match-content__row--league"
MATCH_INFO_SELECTOR = ".match-content__info"
TEAM_SELECTOR = ".match-content__row--team"
INFO_TIME_SELECTOR = ".match-content__row--info"

EVENT_DETAIL_URL = "https://sports.bet9ja.com/mobile/eventdetail/zoomsoccer/x/x/x/{match_ext_id}/VS_1X2"
# The event detail page has several TABS (Popular Markets, 1st Half Markets,
# Goal Markets, Home/Away, Combined Markets, Corner Markets) -- each swaps
# out div.match__markets' entire contents rather than adding to it, so a
# single page load only ever sees one tab's markets. Popular Markets is the
# default tab (reached by /VS_1X2 above); 1st Half Markets -- confirmed via
# live recon -- has a REAL "1st Half - 1X2" market (not derivable from HTFT,
# an actual bookmaker-priced market) and "Halftime - Over/Under" at FIVE
# goal lines (0.5/1.5/2.5/3.5/4.5) -- both markets previously assumed not to
# exist on bet9ja at all. Same direct-URL trick works for this tab too
# (confirmed: any anchor within a tab's own market group loads that whole
# group, garbage slugs and all), so it's a second page load per fixture
# rather than a click.
EVENT_DETAIL_1ST_HALF_URL = "https://sports.bet9ja.com/mobile/eventdetail/zoomsoccer/x/x/x/{match_ext_id}/VS_HTOU"

# data-anchor -> our own market key. VS_OU/VS_HTOU are handled separately (multi-line).
FLAT_MARKETS = {
    "VS_1X2": "1X2",
    "VS_GGNG": "BTTS",
    "VS_HTFT": "HTFT",
    "VS_CS": "CorrectScore",
    "VS_1X21T": "HT_1X2",
}
ONEX2_LABEL_MAP = {"1": "Home", "X": "Draw", "2": "Away"}


def _real_click(driver, element):
    ActionChains(driver).move_to_element(element).pause(0.2).click(element).perform()


def _open_premier_league(driver):
    flag = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, FLAG_EN_SELECTOR))
    )
    container = flag.find_element(By.XPATH, "./..")
    _real_click(driver, container)
    WebDriverWait(driver, 10).until(
        lambda d: any(
            LEAGUE_FILTER in el.text
            for el in d.find_elements(By.CSS_SELECTOR, LEAGUE_ROW_SELECTOR)
        )
    )
    time.sleep(1)


def _extract_fixture_cards(driver):
    """Returns list of dicts: match_ext_id, team_a, team_b, kickoff_time,
    for every fixture currently belonging to LEAGUE_FILTER. No prices here
    any more -- odds all come from each fixture's own detail page.
    """
    soup = BeautifulSoup(driver.page_source, "html.parser")
    cards = []

    for info in soup.select(MATCH_INFO_SELECTOR):
        table_a = info.find_parent(class_="table-a")
        if table_a is None:
            continue

        league_row = table_a.find_previous_sibling()
        league_text = league_row.get_text(strip=True) if league_row else ""
        if LEAGUE_FILTER not in league_text:
            continue

        teams = table_a.select(TEAM_SELECTOR)
        if len(teams) < 2:
            continue
        team_a, team_b = teams[0].get_text(strip=True), teams[1].get_text(strip=True)

        info_row = table_a.select_one(INFO_TIME_SELECTOR)
        kickoff_time = info_row.get_text(" ", strip=True) if info_row else None
        if kickoff_time:
            kickoff_time = re.sub(r"\s*●.*$", "", kickoff_time).strip()

        match_ext_id = (info.get("id") or "").replace("match_info_", "") or None
        if match_ext_id is None:
            continue

        cards.append({
            "match_ext_id": match_ext_id,
            "team_a": team_a,
            "team_b": team_b,
            "kickoff_time": kickoff_time,
        })

    return cards


def _parse_flat_market(box):
    """{selection_label: price} for a market whose rows are plain
    elem+odd pairs (1X2, BTTS, HTFT, CorrectScore)."""
    out = {}
    for item in box.select("div.match-scores__item"):
        if "selection" in (item.get("class") or []):
            continue
        elem = item.select_one("div.elem")
        odd = item.select_one("div.odd")
        if elem is None or odd is None:
            continue
        try:
            out[elem.get_text(strip=True)] = float(odd.get_text(strip=True))
        except ValueError:
            continue
    return out


def _parse_ou_market(box):
    """{goal_line: {"Over": price, "Under": price}}."""
    out = {}
    for row in box.select("div.match-scores__row"):
        sel = row.select_one("div.match-scores__item.selection")
        if sel is None:
            continue
        line = sel.get_text(strip=True)
        line_odds = _parse_flat_market(row)
        if line_odds:
            out[line] = line_odds
    return out


ONEX2_MARKETS = {"VS_1X2": "1X2", "VS_1X21T": "HT_1X2"}


def _boxes_by_anchor(driver):
    soup = BeautifulSoup(driver.page_source, "html.parser")
    boxes_by_anchor = {}
    for box in soup.select("div.match__markets > div.accordion-box"):
        toggle = box.select_one("div.accordion-toggle")
        if toggle is None:
            continue
        anchor = toggle.get("data-anchor")
        if anchor:
            boxes_by_anchor[anchor] = box
    return boxes_by_anchor


def _scrape_event_odds(driver, match_ext_id):
    """Loads one fixture's detail page (two tabs: Popular Markets, then 1st
    Half Markets) directly by id and returns its odds dict:
    {"1X2": {...}, "BTTS": {...}, "OU1.5": {...}, ..., "OU4.5": {...},
    "CorrectScore": {...}, "HTFT": {...}, "HT_1X2": {...},
    "HT_OU0.5": {...}, ..., "HT_OU4.5": {...}}.
    """
    driver.get(EVENT_DETAIL_URL.format(match_ext_id=match_ext_id))
    time.sleep(2.5)
    popular_boxes = _boxes_by_anchor(driver)

    driver.get(EVENT_DETAIL_1ST_HALF_URL.format(match_ext_id=match_ext_id))
    time.sleep(2.5)
    first_half_boxes = _boxes_by_anchor(driver)

    boxes_by_anchor = {**popular_boxes, **first_half_boxes}
    odds = {}

    for anchor, market_key in ONEX2_MARKETS.items():
        box = boxes_by_anchor.get(anchor)
        if box is None:
            continue
        raw = _parse_flat_market(box)
        mapped = {ONEX2_LABEL_MAP[k]: v for k, v in raw.items() if k in ONEX2_LABEL_MAP}
        if mapped:
            odds[market_key] = mapped

    for anchor, market_key in FLAT_MARKETS.items():
        if anchor in ONEX2_MARKETS:
            continue
        box = boxes_by_anchor.get(anchor)
        if box is None:
            continue
        raw = _parse_flat_market(box)
        if not raw:
            continue
        if market_key == "CorrectScore":
            raw = {k.replace(":", "-"): v for k, v in raw.items()}
        odds[market_key] = raw

    ou_box = boxes_by_anchor.get("VS_OU")
    if ou_box is not None:
        for line, line_odds in _parse_ou_market(ou_box).items():
            odds[f"OU{line}"] = line_odds

    htou_box = boxes_by_anchor.get("VS_HTOU")
    if htou_box is not None:
        for line, line_odds in _parse_ou_market(htou_box).items():
            odds[f"HT_OU{line}"] = line_odds

    return odds


def scrape_fixtures_odds(driver):
    """Loads the fixtures page to find every current Premier-Zoom fixture,
    then loads each fixture's own detail page directly (by id) to pull
    every market bet9ja offers for it in one pass.

    Returns list of dicts:
        {match_ext_id, team_a, team_b, kickoff_time, odds: {market: {selection: price}}}
    match_number (position within the round) is NOT assigned here --
    the caller assigns it from scrape order, matching how round_number
    is inferred (this page has no round label to key off of).
    """
    driver.get(FIXTURES_URL)
    time.sleep(4)
    _open_premier_league(driver)

    cards = _extract_fixture_cards(driver)

    fixtures = []
    for c in cards:
        odds = _scrape_event_odds(driver, c["match_ext_id"])
        fixtures.append({
            "match_ext_id": c["match_ext_id"],
            "team_a": c["team_a"],
            "team_b": c["team_b"],
            "kickoff_time": c["kickoff_time"],
            "odds": odds,
        })

    return fixtures
