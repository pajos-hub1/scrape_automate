"""Upcoming fixtures + odds scraper.

Live DOM findings this is built against (bet9ja mobile sportsbook, Zoom
Soccer hub, verified 2026-07-18):

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
    2. `div.table-a` containing:
       - `div.match-content__info#match_info_<id>` with two
         `.match-content__row--team` divs (home, away) and a
         `.match-content__row--info` with kickoff time text
       - `div.bets > .bets__row > .bets__item` (one per selection, in
         column order) with the price in `a.bets__item--link > span`
  `div.table-f` is NOT a unique card selector on its own -- it's reused
  throughout the page (headers, per-market panels). Anchor everything off
  `.match-content__row--league` / `.match-content__info` instead.
- Market switching: `div.filter--btn` rows across the top change which
  columns `.bets__row` renders. Confirmed working for "Both Teams to
  Score" (columns become Yes/No). "Over/Under Goals" did NOT change the
  rendered columns within a plain click + wait in testing -- it likely
  needs a goal-line sub-selection step not yet reverse-engineered. Only
  1X2 (default) and BTTS are scraped for now; O/U 2.5 is a known gap to
  pick up in a follow-up pass.
- The page shows no round number, only kickoff time -- round_number for a
  scraped fixture has to be inferred by the caller (next round after the
  latest played round in the current season), not read off this page.
"""
import re
import time

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import FIXTURES_URL, LEAGUE_FILTER

FLAG_EN_SELECTOR = "i.flag-en"
LEAGUE_ROW_SELECTOR = ".match-content__row--league"
MATCH_INFO_SELECTOR = ".match-content__info"
TEAM_SELECTOR = ".match-content__row--team"
INFO_TIME_SELECTOR = ".match-content__row--info"
BETS_ROW_SELECTOR = ".bets__row"
BET_ITEM_SELECTOR = ".bets__item"
FILTER_BTN_SELECTOR = "div.filter--btn"


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


def _switch_market(driver, market_label):
    btns = driver.find_elements(By.CSS_SELECTOR, FILTER_BTN_SELECTOR)
    target = next((b for b in btns if b.text.strip() == market_label), None)
    if target is None:
        return False
    _real_click(driver, target)
    time.sleep(2)
    return True


def _extract_fixture_cards(driver):
    """Returns list of dicts: match_ext_id, team_a, team_b, kickoff_time,
    prices (list of float, in on-screen column order), for every fixture
    currently belonging to LEAGUE_FILTER.
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

        bets_row = table_a.select_one(BETS_ROW_SELECTOR)
        prices = []
        if bets_row is not None:
            for item in bets_row.select(BET_ITEM_SELECTOR):
                text = item.get_text(strip=True)
                try:
                    prices.append(float(text))
                except ValueError:
                    prices.append(None)

        match_ext_id = (info.get("id") or "").replace("match_info_", "") or None

        cards.append({
            "match_ext_id": match_ext_id,
            "team_a": team_a,
            "team_b": team_b,
            "kickoff_time": kickoff_time,
            "prices": prices,
        })

    return cards


def scrape_fixtures_odds(driver):
    """Loads the fixtures page, filters to Premier-Zoom, and scrapes
    upcoming fixtures with 1X2 and BTTS odds.

    Returns list of dicts:
        {match_ext_id, team_a, team_b, kickoff_time,
         odds: {"1X2": {"Home": p, "Draw": p, "Away": p},
                "BTTS": {"Yes": p, "No": p}}}
    match_number (position within the round) is NOT assigned here --
    the caller assigns it from scrape order, matching how round_number
    is inferred (this page has no round label to key off of).
    """
    driver.get(FIXTURES_URL)
    time.sleep(4)
    _open_premier_league(driver)

    base_cards = _extract_fixture_cards(driver)
    by_id = {c["match_ext_id"]: c for c in base_cards if c["match_ext_id"]}

    fixtures = []
    for c in base_cards:
        prices = c["prices"]
        odds = {}
        if len(prices) >= 3 and all(p is not None for p in prices[:3]):
            odds["1X2"] = {"Home": prices[0], "Draw": prices[1], "Away": prices[2]}
        fixtures.append({
            "match_ext_id": c["match_ext_id"],
            "team_a": c["team_a"],
            "team_b": c["team_b"],
            "kickoff_time": c["kickoff_time"],
            "odds": odds,
        })

    if _switch_market(driver, "Both Teams to Score"):
        btts_cards = _extract_fixture_cards(driver)
        btts_by_id = {c["match_ext_id"]: c for c in btts_cards if c["match_ext_id"]}
        for f in fixtures:
            bc = btts_by_id.get(f["match_ext_id"])
            if bc and len(bc["prices"]) >= 2 and all(p is not None for p in bc["prices"][:2]):
                f["odds"]["BTTS"] = {"Yes": bc["prices"][0], "No": bc["prices"][1]}

    return fixtures
