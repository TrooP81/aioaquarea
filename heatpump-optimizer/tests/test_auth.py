from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from starlette.requests import Request

from packages.api.auth import require_auth
from packages.core.config import settings


def _request(path: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path, "headers": []})


@pytest.mark.asyncio
async def test_health_paths_stay_available_with_api_auth(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-production-token")

    await require_auth(_request("/health"), None)
    await require_auth(_request("/health/ready"), None)


@pytest.mark.asyncio
async def test_control_api_requires_matching_bearer_token(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "test-production-token")

    with pytest.raises(HTTPException) as exc:
        await require_auth(_request("/api/dashboard"), None)
    assert exc.value.status_code == 401

    await require_auth(
        _request("/api/dashboard"),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-production-token"),
    )
