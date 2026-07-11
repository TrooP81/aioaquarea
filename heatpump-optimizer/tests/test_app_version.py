import json
import tomllib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from packages.api.main import app
from packages.core.version import APP_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent_across_services():
    """The dashboard and API must report the same release version."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    web_package = json.loads((PROJECT_ROOT / "web" / "package.json").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == APP_VERSION
    assert web_package["version"] == APP_VERSION
    assert app.version == APP_VERSION


@pytest.mark.asyncio
async def test_version_endpoint_reports_running_api_version():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/version")

    assert response.status_code == 200
    assert response.json() == {"version": APP_VERSION}
