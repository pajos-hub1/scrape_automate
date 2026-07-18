"""Results-page scraper (played rounds).

Live DOM findings this is built against (bet9ja Zoom, mobile Premier-Zoom
results page, verified 2026-07-18):

- Loading the results URL cold always boots into the "TABLE" (standings)
  tab, regardless of URL path -- the app ignores the sub-route on first
  paint. You have to click the "Results" nav tab (a.top-nav__list-item,
  text "Results") to reach the round carousel. A JS-dispatched
  `element.click()` does NOT trigger the SPA's router; a real
  ActionChains click does.
- The season toggle is `div.switch__wrap` containing two
  `div.switch__btn` ("Current Season" / "Previous Season"), the active one
  carries `.is-active`. Same click-dispatch caveat applies.
- The carousel keeps exactly 3 <table class="l-table mt10"> nodes mounted
  at all times (a recycled/virtualized window), and WHICH one currently
  shows the round named by .carousel__control-txt rotates -- it is NOT a
  fixed DOM index. The reliable signal is each table's own inline
  `transform: translate(Xpx, ...)`: the active/visible slide always sits
  at translateX ≈ 0px; the other two sit at +490px / -490px (off-screen).
  This was verified by walking three consecutive rounds and confirming
  the tx≈0 table's DOM index changes each time while its content always
  matches the current round label.
"""
import re
import time

from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config import RESULTS_URL, ROUNDS_PER_SEASON

ROUND_LABEL_SELECTOR = ".carousel__control-txt"
NEXT_BUTTON_SELECTOR = ".carousel__control-right"
TABLE_SELECTOR = "table.l-table.mt10"
NAV_ITEM_SELECTOR = "a.top-nav__list-item"
SWITCH_BTN_SELECTOR = "div.switch__btn"

SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")
TRANSLATE_RE = re.compile(r"translate\(\s*([-\d.]+)px")


def parse_score(text):
    if not text:
        return None, None
    m = SCORE_RE.search(text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _real_click(driver, element):
    """The site's router only reacts to trusted click events -- a plain
    element.click() / JS-dispatched click is silently ignored."""
    ActionChains(driver).move_to_element(element).pause(0.2).click(element).perform()


def _wait_hydrated(driver, selector, timeout=10):
    """Poll until elements matching selector exist and carry a non-empty
    class attribute (Vue has attached its reactive classes / listeners)."""
    end = time.time() + timeout
    while time.time() < end:
        els = driver.find_elements(By.CSS_SELECTOR, selector)
        if els and all(e.get_attribute("class") for e in els):
            return els
        time.sleep(0.3)
    return driver.find_elements(By.CSS_SELECTOR, selector)


def _open_results_tab(driver):
    nav_items = _wait_hydrated(driver, NAV_ITEM_SELECTOR)
    results_tab = next((a for a in nav_items if "esult" in a.text.lower()), None)
    if results_tab is None:
        raise RuntimeError("Results nav tab not found -- page structure may have changed")
    _real_click(driver, results_tab)
    WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ROUND_LABEL_SELECTOR))
    )
    time.sleep(1)


def _get_round_label(driver):
    el = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ROUND_LABEL_SELECTOR))
    )
    m = re.search(r"Round\s+(\d+)", el.text)
    return (int(m.group(1)) if m else None), el


def _find_active_table(driver):
    """The active slide is the table whose inline transform translateX is
    ~0px (see module docstring). Falls back to a visibility heuristic if
    no table's style matches, which would mean the DOM changed and needs a
    fresh look."""
    tables = WebDriverWait(driver, 10).until(
        EC.presence_of_all_elements_located((By.CSS_SELECTOR, TABLE_SELECTOR))
    )

    best, best_abs_tx = None, None
    for t in tables:
        style = t.get_attribute("style") or ""
        m = TRANSLATE_RE.search(style)
        if not m:
            continue
        tx = abs(float(m.group(1)))
        if best_abs_tx is None or tx < best_abs_tx:
            best, best_abs_tx = t, tx

    if best is not None and best_abs_tx < 5:
        return best

    print("   [fallback] no table at translateX~0 -- DOM structure may have changed, "
          "using first displayed table")
    visible = [t for t in tables if t.is_displayed() and t.size["width"] > 0]
    return visible[0] if visible else (tables[0] if tables else None)


def _scrape_table(table):
    """Team names and scores are NOT both present in the same <tr> in this
    markup (per-row pairing finds zero matches) -- as in the original
    notebook, collect each flat across the whole table and pair
    positionally in twos instead.
    """
    rows = table.find_elements(By.TAG_NAME, "tr")
    html_rows = [r.get_attribute("outerHTML") for r in rows]
    soup = BeautifulSoup("\n".join(html_rows), "html.parser")

    team_spans = soup.find_all("span", class_="tbl-team-name")
    score_cells = soup.find_all("td", class_="txt-y")

    teams = [team_spans[i:i + 2] for i in range(0, len(team_spans), 2)]
    scores = [score_cells[i:i + 2] for i in range(0, len(score_cells), 2)]

    matches = []
    for match_number, (t_pair, s_pair) in enumerate(zip(teams, scores), start=1):
        if len(t_pair) < 2 or len(s_pair) < 2:
            continue
        ft_a, ft_b = parse_score(s_pair[0].get_text(strip=True))
        ht_a, ht_b = parse_score(s_pair[1].get_text(strip=True))
        matches.append({
            "match_number": match_number,
            "team_a": t_pair[0].get_text(strip=True),
            "team_b": t_pair[1].get_text(strip=True),
            "ft_a": ft_a, "ft_b": ft_b,
            "ht_a": ht_a, "ht_b": ht_b,
        })
    return matches


def _label_text_changed(driver, prev_round_no):
    try:
        el = driver.find_element(By.CSS_SELECTOR, ROUND_LABEL_SELECTOR)
        m = re.search(r"Round\s+(\d+)", el.text)
        return bool(m) and int(m.group(1)) != prev_round_no
    except Exception:
        return False


def walk_carousel(driver, max_rounds=ROUNDS_PER_SEASON + 2):
    """Round-label-driven walk of one season's carousel. Returns
    {round_number: [match dicts]}. Stops when the round sequence loops back
    to a round already seen -- works regardless of which round the carousel
    happens to open on (verified: current season opened on Round 4, walk
    correctly wrapped through Round 1/2/3 and stopped back at 4).
    """
    rounds = {}
    seen = []

    for _ in range(max_rounds):
        round_no, _label_el = _get_round_label(driver)
        if round_no is None:
            print("   Could not read round label, stopping walk")
            break
        if round_no in seen:
            break
        seen.append(round_no)

        table = _find_active_table(driver)
        if table is None:
            print(f"   No table found for Round {round_no}, skipping")
        else:
            matches = _scrape_table(table)
            if matches:
                rounds[round_no] = matches
            else:
                print(f"   Round {round_no}: table found but no rows parsed")

        advanced = False
        for attempt in range(2):
            try:
                next_btn = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, NEXT_BUTTON_SELECTOR))
                )
                _real_click(driver, next_btn)
            except Exception as e:
                print(f"   Could not click next (attempt {attempt + 1}): {e}")
                continue

            try:
                WebDriverWait(driver, 10).until(lambda d: _label_text_changed(d, round_no))
                advanced = True
                break
            except Exception:
                print(f"   Round label didn't change after next-click (attempt {attempt + 1}), retrying")

        if not advanced:
            print("   Giving up advancing the carousel -- this pass may be incomplete")
            break
        time.sleep(0.3)  # let the translateX transition settle

    return rounds


def _switch_season(driver, want_text):
    """want_text: 'Previous' or 'Current'. Returns True if a switch/click
    happened, False if that season was already active or the control
    wasn't found."""
    btns = _wait_hydrated(driver, SWITCH_BTN_SELECTOR)
    target = next((b for b in btns if want_text.lower() in b.text.lower()), None)
    if target is None:
        return False
    if "is-active" in (target.get_attribute("class") or ""):
        return False
    _real_click(driver, target)
    time.sleep(1.5)
    return True


def scrape_results(driver):
    """Loads the results page, opens the Results tab, and scrapes both
    seasons the season-switch toggle exposes (Current + Previous).
    Returns a list of {"rounds": {round_no: [...]}, "round_count": int}.
    """
    driver.get(RESULTS_URL)
    _open_results_tab(driver)

    seasons_scraped = []

    rounds = walk_carousel(driver)
    if rounds:
        seasons_scraped.append({"rounds": rounds, "round_count": len(rounds)})

    switched = _switch_season(driver, "Previous")
    if switched:
        rounds2 = walk_carousel(driver)
        if rounds2:
            seasons_scraped.append({"rounds": rounds2, "round_count": len(rounds2)})

    return seasons_scraped
