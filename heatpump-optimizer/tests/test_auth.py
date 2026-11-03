from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
import pytest
from starlette.requests import Request

from packages.api.auth import require_auth
from packages.core.config import settings


def _request(
    path: str, *, method: str = "GET", headers: list[tuple[bytes, bytes]] | None = None
) -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": headers or []})


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


@pytest.mark.asyncio
async def test_control_api_skips_auth_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "disabled")

    await require_auth(_request("/api/dashboard"), None)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/settings"),
        ("PUT", "/api/settings"),
        ("GET", "/api/comfort-schedule"),
        ("PUT", "/api/comfort-schedule"),
        ("GET", "/api/comfort-schedule/learned"),
        ("POST", "/api/comfort-schedule/apply-learned"),
        ("GET", "/api/plans"),
        ("GET", "/api/plans/17"),
    ],
)
@pytest.mark.parametrize("role", ["viewer", "admin", "owner"])
@pytest.mark.asyncio
async def test_planning_and_plan_routes_keep_global_bearer_authority(
    monkeypatch, method, path, role
):
    monkeypatch.setattr(settings, "api_token", "test-production-token")
    request = _request(
        path,
        method=method,
        headers=[(b"x-role", role.encode()), (b"x-user", b"untrusted-user")],
    )

    with pytest.raises(HTTPException) as missing:
        await require_auth(request, None)
    assert missing.value.status_code == 401
    assert missing.value.headers == {"WWW-Authenticate": "Bearer"}

    with pytest.raises(HTTPException) as invalid:
        await require_auth(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="wrong-token"),
        )
    assert invalid.value.status_code == 403

    await require_auth(
        request,
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-production-token"),
    )
