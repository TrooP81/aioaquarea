"""Shared ML model utilities and configuration."""

from __future__ import annotations

from pathlib import Path

import structlog

from packages.core.config import settings as app_settings

_logger = structlog.get_logger()

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_score
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False
    GradientBoostingRegressor = None
    cross_val_score = None
    Pipeline = None
    StandardScaler = None

MODEL_DIR = Path(app_settings.model_dir)
MODEL_DIR.mkdir(parents=True, exist_ok=True)
