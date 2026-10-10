"""Shared helpers every Predictor implementation formats its output with,
so every model's `predict_markets()` return shape is identical regardless
of what's happening internally.
"""


def normalize(probs: dict) -> dict:
    total = sum(probs.values())
    if total <= 0:
        n = len(probs)
        return {k: 1 / n for k in probs}
    return {k: v / total for k, v in probs.items()}


def market_result(probs: dict) -> dict:
    # "Other" (predict/poisson.top_scorelines' catch-all for every
    # scoreline outside the top N) usually holds the most mass combined,
    # but it can never match a real result -- reconcile records exact
    # scores like "1-1". Picking it scored CorrectScore at ~0% for months.
    candidates = [k for k in probs if k != "Other"] or list(probs)
    label = max(candidates, key=probs.get)
    return {"label": label, "probabilities": probs, "confidence": probs[label]}
