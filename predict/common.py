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
    label = max(probs, key=probs.get)
    return {"label": label, "probabilities": probs, "confidence": probs[label]}
