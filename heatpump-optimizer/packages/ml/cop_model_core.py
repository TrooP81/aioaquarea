"""Inert compatibility API for the retired, unverified COP model."""

from __future__ import annotations

MODEL_DIR = None


class COPModel:
    COP_MIN = 1.5
    COP_MAX = 6.0

    def __init__(self):
        self._model = None
        self._version: str = "untrained"
        self._metrics: dict[str, float] = {}
        self._last_data_quality: dict[str, int] = {}

    def reset(self) -> None:
        """Discard inert compatibility state without touching artifacts."""
        self._model = None
        self._version = "untrained"
        self._metrics = {}

    @property
    def is_trained(self) -> bool:
        return False

    @property
    def metrics(self) -> dict[str, float]:
        """Retired COP has no active validation evidence."""
        return {}

    @property
    def last_data_quality(self) -> dict[str, int]:
        """Retired COP has no active training evidence."""
        return {}

    @property
    def version(self) -> str:
        return "retired"

    async def train(self) -> dict[str, object]:
        return {
            "status": "retired",
            "supported": False,
            "error": "cop_model_retired",
            "reason": "unverified_thermal_output",
        }

    def predict(
        self,
        outdoor_temp: float,
        tank_target: int,
        hour: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> float:
        raise RuntimeError("cop_model_retired")

    def predict_cop(
        self,
        outdoor_temp: float,
        tank_target: int,
        hour: int,
        precipitation: float = 0.0,
        humidity: float = 60.0,
        cloud_cover: float = 0.5,
    ) -> float:
        raise RuntimeError("cop_model_retired")

    def load_latest(self) -> bool:
        return False
