"""The swappable model interface. Nothing downstream (build.py, run.py, the
predictions table) knows or cares which concrete Predictor is plugged in --
only that it returns this shape. Swap BaselinePredictor for a real model
later by adding a new class here and registering it in run.py; no other
code changes.
"""
from abc import ABC, abstractmethod


class Predictor(ABC):
    model_version: str

    @abstractmethod
    def predict_markets(self, features_row: dict) -> dict:
        """features_row: one row from the `features` table, as a dict.

        Returns {market: {"label": str, "probabilities": {selection: float},
        "confidence": float}} -- one entry per market this predictor
        supports. "confidence" is the probability of "label" (its own top
        pick), not a separate calibration score.
        """
        raise NotImplementedError
