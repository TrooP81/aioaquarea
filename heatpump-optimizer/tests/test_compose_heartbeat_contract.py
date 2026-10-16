from __future__ import annotations

from pathlib import Path

import yaml


COMPOSE = Path(__file__).resolve().parents[1] / "docker-compose.yml"


def test_database_and_redis_restart_and_healthcheck_contract():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    db = services["db"]
    redis = services["redis"]
    assert db["restart"] == "unless-stopped"
    assert db["healthcheck"] == {
        "test": [
            "CMD-SHELL",
            'pg_isready -U "$${POSTGRES_USER}" -d "$${POSTGRES_DB}"',
        ],
        "interval": "5s",
        "timeout": "3s",
        "retries": 5,
        "start_period": "60s",
    }
    assert redis["restart"] == "unless-stopped"
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]
    assert redis["healthcheck"]["start_period"] == "10s"


def test_api_healthcheck_and_one_shot_restart_contract():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    api = services["api"]
    assert api["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:8500/health', timeout=3)",
    ]
    assert api["healthcheck"]["start_period"] == "30s"
    assert services["migrate"]["restart"] == "no"
    assert services["restore-tool"]["restart"] == "no"


def test_all_existing_startup_dependencies_are_preserved_without_restart_edges():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    assert services["migrate"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    for name in ("poller", "optimizer", "api"):
        assert services[name]["depends_on"] == {
            "db": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
            "migrate": {"condition": "service_completed_successfully"},
        }
    assert services["web"]["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert services["backup"]["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert all(
        "restart" not in dependency
        for service in services.values()
        for dependency in service.get("depends_on", {}).values()
    )
    assert {
        name: services[name]["restart"] for name in ("poller", "optimizer", "api", "web", "backup")
    } == {name: "unless-stopped" for name in ("poller", "optimizer", "api", "web", "backup")}
