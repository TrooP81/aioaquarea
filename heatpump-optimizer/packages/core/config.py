"""Application configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+asyncpg://heatpump:changeme_in_production@db:5432/heatpump"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Panasonic Aquarea
    aquarea_username: str = ""
    aquarea_password: str = ""

    # Price feed
    price_provider: str = "entsoe"  # "entsoe" or "tibber"
    entsoe_api_token: str = ""
    entsoe_area: str = "10YNL----------L"  # Netherlands default
    tibber_api_token: str = ""

    # Weather
    latitude: float = 52.37
    longitude: float = 4.89

    # SmartThings
    smartthings_client_id: str = ""
    smartthings_client_secret: str = ""
    smartthings_pat: str = ""  # Legacy fallback

    # Optimizer constraints
    tank_min_temp: int = 45
    tank_max_temp: int = 55
    comfort_temp_min: float = 20.0
    comfort_temp_max: float = 22.0

    # App
    secret_key: str = "change-this-to-a-random-string"
    api_token: str = "disabled"  # Set to a strong token to enable API auth; "disabled" = no auth
    model_dir: str = "/app/models"
    cors_origins: str = "http://localhost:3500"  # Comma-separated allowed origins
    log_level: str = "INFO"
    poll_interval_seconds: int = 300

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

if settings.secret_key == "change-this-to-a-random-string":
    import warnings
    warnings.warn(
        "SECRET_KEY is using the insecure default. "
        "Set SECRET_KEY environment variable to a random string in production.",
        stacklevel=1,
    )
