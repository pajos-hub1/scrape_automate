from predict.common import market_result
from track.reconcile import actual_labels, odds_implied_label


def test_market_result_never_picks_other_bucket():
    # "Other" holds the most mass but can never match a real exact score.
    res = market_result({"1-1": 0.12, "1-0": 0.10, "Other": 0.40})
    assert res["label"] == "1-1"
    assert res["confidence"] == 0.12
    assert "Other" in res["probabilities"]


def test_market_result_plain_argmax():
    assert market_result({"Home": 0.5, "Draw": 0.3, "Away": 0.2})["label"] == "Home"


def test_actual_labels():
    labels = actual_labels({"ft_a": 2, "ft_b": 1, "ht_a": 0, "ht_b": 0})
    assert labels["1X2"] == "Home"
    assert labels["BTTS"] == "Yes"
    assert labels["OU2.5"] == "Over"
    assert labels["OU1.5"] == "Over"
    assert labels["CorrectScore"] == "2-1"
    assert labels["HT_1X2"] == "Draw"
    assert labels["HT_OU0.5"] == "Under"


def test_actual_labels_without_halftime():
    labels = actual_labels({"ft_a": 0, "ft_b": 0, "ht_a": None, "ht_b": None})
    assert labels["1X2"] == "Draw" and labels["BTTS"] == "No"
    assert "HT_1X2" not in labels


def test_odds_implied_label():
    odds = {("1X2", "Home"): 0.5, ("1X2", "Draw"): 0.3, ("1X2", "Away"): 0.25}
    assert odds_implied_label(odds, "1X2") == "Home"
    assert odds_implied_label({("1X2", "Home"): 0.5}, "1X2") is None   # incomplete market
