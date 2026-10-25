from __future__ import annotations

import asyncio
import datetime as dt
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core import external_heartbeat as heartbeat


SENTINEL = "https://hc-ping.com/sentinel-secret-token"


def _settings(**overrides):
    values = {
        "external_heartbeat_enabled": "true",
        "external_heartbeat_url": SENTINEL,
        "external_heartbeat_interval_seconds": "60",
    }
    values.update(overrides)
    return values


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.mark.asyncio
async def test_disabled_default_never_calls_external_transport():
    condition = AsyncMock(return_value=True)
    resolver = AsyncMock()
    sender = AsyncMock()
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings(external_heartbeat_enabled="false")),
        condition_check=condition,
        url_resolver=resolver,
        sender=sender,
        monotonic=FakeClock(),
    )

    assert await runner.tick() is False
    condition.assert_not_awaited()
    resolver.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_health_signal_is_required_before_send(monkeypatch):
    current = dt.datetime.now(dt.timezone.utc)
    monkeypatch.setattr(
        heartbeat,
        "_read_service_heartbeats",
        AsyncMock(return_value={"poller": current, "optimizer": current}),
    )
    monkeypatch.setattr(heartbeat, "_probe_redis", AsyncMock(return_value=True))
    monkeypatch.setattr(heartbeat, "_probe_internal_api", AsyncMock(return_value=True))
    monkeypatch.setattr(
        heartbeat, "get_device_data_quality", AsyncMock(return_value={"ready": True})
    )
    assert await heartbeat.evaluate_reporting_conditions(_settings()) is True

    for bad_rows in (
        {"optimizer": current},
        {"poller": current - dt.timedelta(seconds=181), "optimizer": current},
        {"poller": current + dt.timedelta(seconds=1), "optimizer": current},
    ):
        monkeypatch.setattr(heartbeat, "_read_service_heartbeats", AsyncMock(return_value=bad_rows))
        assert await heartbeat.evaluate_reporting_conditions(_settings()) is False

    monkeypatch.setattr(
        heartbeat,
        "_read_service_heartbeats",
        AsyncMock(return_value={"poller": current, "optimizer": current}),
    )
    for name, replacement in (
        ("_probe_redis", AsyncMock(return_value=False)),
        ("_probe_internal_api", AsyncMock(return_value=False)),
        ("get_device_data_quality", AsyncMock(return_value={"ready": False})),
    ):
        original = getattr(heartbeat, name)
        monkeypatch.setattr(heartbeat, name, replacement)
        assert await heartbeat.evaluate_reporting_conditions(_settings()) is False
        monkeypatch.setattr(heartbeat, name, original)


@pytest.mark.asyncio
async def test_db_read_failure_suppresses_send_and_consumes_attempt_slot():
    clock = FakeClock()
    reader = AsyncMock(side_effect=RuntimeError(SENTINEL))
    sender = AsyncMock()
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=reader, sender=sender, monotonic=clock
    )

    assert await runner.tick() is False
    assert await runner.tick() is False
    assert reader.await_count == 1
    sender.assert_not_awaited()
    clock.advance(60)
    assert await runner.tick() is False
    assert reader.await_count == 2
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_http_attempts_consume_interval_and_never_catch_up():
    clock = FakeClock()
    sender = AsyncMock(return_value=503)
    resolver = AsyncMock(
        return_value=heartbeat.HeartbeatEndpoint(
            SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
        )
    )
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings()),
        condition_check=AsyncMock(return_value=True),
        url_resolver=resolver,
        sender=sender,
        monotonic=clock,
    )

    assert await runner.tick() is False
    clock.advance(30)
    assert await runner.tick() is False
    sender.assert_awaited_once()
    clock.advance(30)
    assert await runner.tick() is False
    assert sender.await_count == 2
    clock.advance(30)
    assert await runner.tick() is False
    assert sender.await_count == 2


@pytest.mark.asyncio
async def test_interval_setting_controls_rate_without_early_retry():
    clock = FakeClock()
    sender = AsyncMock(return_value=200)
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings(external_heartbeat_interval_seconds="30")),
        condition_check=AsyncMock(return_value=True),
        url_resolver=AsyncMock(
            return_value=heartbeat.HeartbeatEndpoint(
                SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
            )
        ),
        sender=sender,
        monotonic=clock,
    )
    assert await runner.tick() is True
    clock.advance(29)
    assert await runner.tick() is False
    sender.assert_awaited_once()
    clock.advance(1)
    assert await runner.tick() is True
    assert sender.await_count == 2


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://hc-ping.com/token", "invalid_url"),
        ("https://user:password@hc-ping.com/token", "invalid_url"),
        ("https://other.example/token", "authority_not_allowed"),
        ("https://hc-ping.com:444/token", "authority_not_allowed"),
        ("https://hc-ping.com.evil.example/token", "authority_not_allowed"),
        ("https://user@hc-ping.com/token", "invalid_url"),
        ("https://user:password@hc-ping.com/token", "invalid_url"),
        ("https://127.0.0.1/token", "ip_literal"),
        ("https://hc-ping.com/#token", "invalid_url"),
        ("https://hc-ping.com/token#", "invalid_url"),
        ("https://127.1/token", "ip_literal"),
        ("https://2130706433/token", "ip_literal"),
        ("https://0177.0.0.1/token", "ip_literal"),
        ("https://0x7f000001/token", "authority_not_allowed"),
        ("https://hc-ping.com/", "invalid_url"),
        ("https://hc-ping.com:bad/token", "invalid_url"),
        ("https://hc-ping.com:/token", "invalid_url"),
        ("https://hc-ping.com:0443/token", "invalid_url"),
    ],
)
def test_url_structure_rejects_unsafe_or_unapproved_destinations(url, reason):
    with pytest.raises(heartbeat.HeartbeatValidationError) as error:
        heartbeat.validate_heartbeat_url_structure(url)
    assert error.value.reason == reason
    assert url not in str(error.value)


def test_url_structure_accepts_only_exact_approved_authority():
    endpoint = heartbeat.validate_heartbeat_url_structure(SENTINEL)
    assert endpoint.host == "hc-ping.com"
    assert endpoint.port == 443
    assert endpoint.url == SENTINEL
    with pytest.raises(heartbeat.HeartbeatValidationError):
        heartbeat.validate_heartbeat_url_structure(
            "https://hc-ping.com:8443/token", allowed_authorities="hc-ping.com:443"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "accepted"),
    [
        (["8.8.8.8"], True),
        (["127.0.0.1"], False),
        (["10.0.0.5"], False),
        (["8.8.8.8", "10.0.0.5"], False),
        (["192.0.2.1"], False),
        (["0.0.0.0"], False),
        (["224.0.0.1"], False),
        (["::1"], False),
        (["fc00::1"], False),
        (["::ffff:8.8.8.8"], False),
        (["fe80::1"], False),
        ([], False),
    ],
)
async def test_dns_resolution_requires_only_global_non_mapped_addresses(
    monkeypatch, answers, accepted
):
    class FakeLoop:
        async def getaddrinfo(self, host, port, **kwargs):
            return [
                (
                    socket.AF_INET6 if ":" in address else socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    (address, port),
                )
                for address in answers
            ]

    endpoint = heartbeat.validate_heartbeat_url_structure(SENTINEL)
    if accepted:
        addresses = await heartbeat._resolve_public_addresses(
            endpoint.host, endpoint.port, resolver=FakeLoop().getaddrinfo
        )
        assert tuple(map(str, addresses)) == tuple(answers)
    else:
        with pytest.raises(heartbeat.HeartbeatValidationError):
            await heartbeat._resolve_public_addresses(
                endpoint.host, endpoint.port, resolver=FakeLoop().getaddrinfo
            )


@pytest.mark.asyncio
async def test_dns_resolution_is_bounded(monkeypatch):
    class SlowLoop:
        async def getaddrinfo(self, host, port, **kwargs):
            await asyncio.sleep(0.02)
            return []

    monkeypatch.setattr(heartbeat, "_DNS_TIMEOUT_SECONDS", 0.001)
    endpoint = heartbeat.validate_heartbeat_url_structure(SENTINEL)
    with pytest.raises(heartbeat.HeartbeatValidationError) as error:
        await heartbeat._resolve_public_addresses(
            endpoint.host, endpoint.port, resolver=SlowLoop().getaddrinfo
        )
    assert error.value.reason == "dns_failed"


@pytest.mark.asyncio
async def test_pinned_resolver_uses_approved_ip_and_rejects_other_host():
    endpoint = heartbeat.HeartbeatEndpoint(
        SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
    )
    resolver = heartbeat._PinnedResolver(endpoint)
    result = await resolver.resolve("hc-ping.com", 443)
    assert result[0]["hostname"] == "hc-ping.com"
    assert result[0]["host"] == "8.8.8.8"
    assert result[0]["port"] == 443
    with pytest.raises(OSError):
        await resolver.resolve("attacker.example", 443)


@pytest.mark.asyncio
async def test_save_time_validation_is_applied_to_direct_bulk_setting(monkeypatch):
    from packages.core import settings_service

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: []))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=fake_session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(settings_service, "get_session", lambda: context)
    validate = AsyncMock()
    monkeypatch.setattr(heartbeat, "validate_heartbeat_url", validate)

    await settings_service.set_settings_bulk({"external_heartbeat_url": SENTINEL})
    validate.assert_awaited_once_with(SENTINEL)


@pytest.mark.asyncio
async def test_api_secret_is_fully_redacted_and_mask_round_trip_preserves_it(monkeypatch):
    from packages.api.routers import settings as api_settings

    session = SimpleNamespace(add=MagicMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(api_settings, "get_session", lambda: context)
    monkeypatch.setattr(
        api_settings,
        "ensure_current_contract",
        AsyncMock(
            return_value=SimpleNamespace(
                id=7,
                contract_json={"installation_id": "00000000-0000-0000-0000-000000000001"},
            )
        ),
    )
    monkeypatch.setattr(
        api_settings,
        "get_all_settings",
        AsyncMock(return_value={"external_heartbeat_url": SENTINEL}),
    )
    result = await api_settings.get_settings()
    assert result["external_heartbeat_url"]["value"] == "***"
    assert SENTINEL not in str(result)

    writes = AsyncMock(return_value=8)
    monkeypatch.setattr(api_settings, "set_settings_bulk", writes)
    await api_settings.update_settings(
        api_settings.SettingsUpdate(settings={"external_heartbeat_url": "***"})
    )
    writes.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_api_validation_error_does_not_echo_ping_url(monkeypatch):
    from fastapi import HTTPException
    from packages.api.routers import settings as api_settings

    hostile = "http://127.0.0.1/super-secret-token"
    with pytest.raises(HTTPException) as error:
        await api_settings.update_settings(
            api_settings.SettingsUpdate(settings={"external_heartbeat_url": hostile})
        )
    assert error.value.status_code == 400
    assert hostile not in str(error.value.detail)
    assert "super-secret-token" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_failure_logs_never_include_ping_url_or_exception_text(monkeypatch):
    calls = []
    fake_logger = SimpleNamespace(
        warning=lambda event, **fields: calls.append((event, fields)),
        info=lambda event, **fields: calls.append((event, fields)),
    )
    monkeypatch.setattr(heartbeat, "logger", fake_logger)
    endpoint = heartbeat.HeartbeatEndpoint(
        SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
    )
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings()),
        condition_check=AsyncMock(return_value=True),
        url_resolver=AsyncMock(return_value=endpoint),
        sender=AsyncMock(side_effect=RuntimeError(f"request={endpoint!r}; {SENTINEL}")),
        monotonic=FakeClock(),
    )
    assert await runner.tick() is False
    assert SENTINEL not in repr(calls)
    assert "sentinel-secret-token" not in repr(calls)
    assert calls == [("external_heartbeat_failed", {"reason": "attempt_failed"})]


@pytest.mark.asyncio
async def test_cleared_database_url_uses_environment_fallback(monkeypatch):
    from packages.core import settings_service

    db_session = MagicMock()
    db_session.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                all=lambda: [SimpleNamespace(key="external_heartbeat_url", value="")]
            )
        )
    )
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db_session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(settings_service, "get_session", lambda **kwargs: context)
    monkeypatch.setattr(
        settings_service,
        "_default_setting_value",
        lambda spec: SENTINEL if spec.key == "external_heartbeat_url" else spec.default,
    )

    values = await settings_service.get_all_settings()
    assert values["external_heartbeat_url"] == SENTINEL


@pytest.mark.asyncio
async def test_pinned_http_transport_preserves_host_and_disables_proxy_and_redirects(monkeypatch):
    endpoint = heartbeat.HeartbeatEndpoint(
        SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
    )
    calls = {}

    class ResponseContext:
        status = 302

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class FakeSession:
        def __init__(self, **kwargs):
            calls["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls["request"] = (url, kwargs)
            return ResponseContext()

    monkeypatch.setattr(heartbeat.aiohttp, "ClientSession", FakeSession)
    status = await heartbeat._send_http_heartbeat(endpoint)
    assert status == 302
    assert calls["session"]["trust_env"] is False
    assert calls["session"]["connector"]._ssl is True
    timeout = calls["session"]["timeout"]
    assert (timeout.total, timeout.connect, timeout.sock_connect, timeout.sock_read) == (5, 5, 5, 5)
    assert calls["request"] == (SENTINEL, {"allow_redirects": False})
    resolver = calls["session"]["connector"]._resolver
    pinned = await resolver.resolve("hc-ping.com", 443)
    assert pinned[0]["host"] == "8.8.8.8"


def test_heartbeat_defaults_are_disabled_and_interval_is_bounded():
    from packages.core.config import Settings
    from packages.core.settings_service import SETTINGS_SCHEMA

    assert Settings().external_heartbeat_enabled is False
    assert Settings().external_heartbeat_interval_seconds == 60
    assert SETTINGS_SCHEMA["external_heartbeat_enabled"]["default"] == "false"
    assert SETTINGS_SCHEMA["external_heartbeat_url"]["redaction"] == "full"
    assert SETTINGS_SCHEMA["external_heartbeat_interval_seconds"]["min_value"] == 30
    assert SETTINGS_SCHEMA["external_heartbeat_interval_seconds"]["max_value"] == 300


@pytest.mark.parametrize("url", ["https://hc-ping.com:0/token", "https://hc-ping.com./token"])
def test_url_structure_rejects_ambiguous_port_and_hostname(url):
    with pytest.raises(heartbeat.HeartbeatValidationError):
        heartbeat.validate_heartbeat_url_structure(url)


def test_full_redaction_mask_round_trip_requires_exact_mask():
    from packages.core.settings_service import get_setting_spec, is_masked_secret

    spec = get_setting_spec("external_heartbeat_url")
    assert is_masked_secret(spec, "***") is True
    assert is_masked_secret(spec, "https://hc-ping.com/***token") is False


@pytest.mark.asyncio
async def test_api_success_and_audit_log_never_echo_ping_url(monkeypatch):
    import json

    from packages.api.routers import settings as api_settings

    records = []
    fake_session = SimpleNamespace(add=records.append)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=fake_session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(api_settings, "set_settings_bulk", AsyncMock())
    monkeypatch.setattr(api_settings, "get_session", lambda: context)

    result = await api_settings.update_settings(
        api_settings.SettingsUpdate(settings={"external_heartbeat_url": SENTINEL})
    )
    assert SENTINEL not in str(result)
    payload = json.loads(records[0].payload_json)
    assert payload["external_heartbeat_url"] == "***"
    assert SENTINEL not in records[0].payload_json


@pytest.mark.asyncio
async def test_async_dns_save_error_is_sanitized_by_settings_api(monkeypatch):
    from fastapi import HTTPException
    from packages.api.routers import settings as api_settings

    monkeypatch.setattr(
        api_settings,
        "set_settings_bulk",
        AsyncMock(side_effect=ValueError("external_heartbeat_url is not allowed")),
    )
    with pytest.raises(HTTPException) as error:
        await api_settings.update_settings(
            api_settings.SettingsUpdate(settings={"external_heartbeat_url": SENTINEL})
        )
    assert error.value.status_code == 400
    assert "not allowed" in str(error.value.detail)
    assert SENTINEL not in str(error.value.detail)
    assert "sentinel-secret-token" not in str(error.value.detail)


@pytest.mark.asyncio
async def test_reporting_success_is_rechecked_and_never_replayed_from_cache():
    clock = FakeClock()
    conditions = AsyncMock(side_effect=[True, False])
    sender = AsyncMock(return_value=200)
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings()),
        condition_check=conditions,
        url_resolver=AsyncMock(
            return_value=heartbeat.HeartbeatEndpoint(
                SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
            )
        ),
        sender=sender,
        monotonic=clock,
    )

    assert await runner.tick() is True
    clock.advance(60)
    assert await runner.tick() is False
    assert conditions.await_count == 2
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_dns_save_rejection_precedes_any_setting_write(monkeypatch):
    from packages.core import settings_service

    fake_session = MagicMock()
    fake_session.execute = AsyncMock(return_value=SimpleNamespace(scalars=lambda: []))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=fake_session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(settings_service, "get_session", lambda: context)
    validate = AsyncMock(side_effect=ValueError("external_heartbeat_url is not allowed"))
    monkeypatch.setattr(heartbeat, "validate_heartbeat_url", validate)

    with pytest.raises(ValueError, match="external_heartbeat_url is not allowed"):
        await settings_service.set_setting("external_heartbeat_url", SENTINEL)

    validate.assert_awaited_once_with(SENTINEL)
    statements = [
        str(call.args[0]).strip().upper() for call in fake_session.execute.await_args_list
    ]
    assert statements
    assert all(statement.startswith("SELECT ") for statement in statements)
    assert "FROM SETTINGS" in statements[-1]
    fake_session.add.assert_not_called()
    fake_session.flush.assert_not_called()
    fake_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_send_time_dns_rejection_suppresses_request_and_redacts_logs(monkeypatch):
    calls = []
    monkeypatch.setattr(
        heartbeat,
        "logger",
        SimpleNamespace(warning=lambda event, **fields: calls.append((event, fields))),
    )
    sender = AsyncMock()
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings()),
        condition_check=AsyncMock(return_value=True),
        url_resolver=AsyncMock(side_effect=heartbeat.HeartbeatValidationError("unsafe_address")),
        sender=sender,
        monotonic=FakeClock(),
    )

    assert await runner.tick() is False
    sender.assert_not_awaited()
    assert calls == [("external_heartbeat_failed", {"reason": "unsafe_address"})]
    assert SENTINEL not in repr(calls)
    assert "sentinel-secret-token" not in repr(calls)


@pytest.mark.asyncio
async def test_overall_attempt_deadline_bounds_stalled_sender(monkeypatch):
    calls = []
    monkeypatch.setattr(heartbeat, "_ATTEMPT_DEADLINE_SECONDS", 0.001)
    monkeypatch.setattr(
        heartbeat,
        "logger",
        SimpleNamespace(warning=lambda event, **fields: calls.append((event, fields))),
    )

    async def stall(_endpoint):
        await asyncio.Event().wait()

    endpoint = heartbeat.HeartbeatEndpoint(
        SENTINEL, "hc-ping.com", 443, (heartbeat.ipaddress.ip_address("8.8.8.8"),)
    )
    sender = AsyncMock(side_effect=stall)
    runner = heartbeat.ExternalHeartbeatRunner(
        settings_reader=AsyncMock(return_value=_settings()),
        condition_check=AsyncMock(return_value=True),
        url_resolver=AsyncMock(return_value=endpoint),
        sender=sender,
        monotonic=FakeClock(),
    )

    assert await runner.tick() is False
    sender.assert_awaited_once_with(endpoint)
    assert calls == [("external_heartbeat_failed", {"reason": "attempt_timeout"})]
    assert SENTINEL not in repr(calls)
