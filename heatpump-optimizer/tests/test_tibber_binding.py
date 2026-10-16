"""P16b installation identity, fail-closed selection and immutable provenance."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from packages.core import price_scope
from packages.core.price_scope import tibber_scope
from packages.poller import feeds


def home(identity, totals=(0.0, -0.2), *, active=True):
    return {
        "id": identity,
        "currentSubscription": {
            "priceInfo": {
                "today": [
                    {"total": value, "currency": "SEK", "startsAt": f"2026-10-15T{hour:02}:00:00Z"}
                    for hour, value in enumerate(totals)
                ],
                "tomorrow": [],
            }
        }
        if active
        else None,
    }


@pytest.fixture
def boundary(monkeypatch):
    values = {"price_provider": "tibber", "tibber_api_token": "offline", "tibber_home_id": "A"}
    snapshot = AsyncMock(side_effect=lambda: dict(values))
    monkeypatch.setattr(feeds, "get_all_settings", snapshot)
    binding = AsyncMock(return_value=True)
    monkeypatch.setattr(feeds, "record_tibber_binding", binding)
    response = MagicMock()
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = response
    monkeypatch.setattr(feeds.httpx, "AsyncClient", MagicMock(return_value=client))
    return values, response, client, snapshot, binding


@pytest.mark.parametrize("reverse", [False, True])
async def test_explicit_home_is_order_independent_and_totals_unchanged(boundary, reverse):
    _, response, client, snapshot, _ = boundary
    homes = [home("A"), home("B", (99.0,))]
    response.json.return_value = {"data": {"viewer": {"homes": homes[::-1] if reverse else homes}}}
    result = await feeds._fetch_prices_tibber()
    assert [value for _, value in result] == [0, -0.2]
    assert result.context.area == tibber_scope("A")
    assert result.context.tibber_home_id == "A"
    assert "id" in client.post.await_args.kwargs["json"]["query"]
    snapshot.assert_awaited_once()


@pytest.mark.parametrize(
    "homes,reason",
    [
        ([], "selected_home_not_found"),
        ([home("B")], "selected_home_not_found"),
        ([home("A", active=False), home("B")], "selected_home_has_no_active_subscription"),
        ([home("A", ()), home("B")], "selected_home_has_no_prices"),
    ],
)
async def test_explicit_unavailable_never_uses_other_home(boundary, homes, reason):
    _, response, _, _, binding = boundary
    response.json.return_value = {"data": {"viewer": {"homes": homes}}}
    result = await feeds._fetch_prices_tibber()
    assert result == []
    assert result.rejection_reason == reason
    assert binding.await_args.args == ("A", "A", reason)


@pytest.mark.parametrize(
    "homes,allowed",
    [
        ([], False),
        ([home("inactive", active=False)], False),
        ([home("B"), home("inactive", active=False)], True),
        ([home("B"), home("C")], False),
    ],
)
async def test_owner_decision_a_only_one_active_subscription_autobinds(boundary, homes, allowed):
    values, response, _, _, binding = boundary
    values["tibber_home_id"] = ""
    response.json.return_value = {"data": {"viewer": {"homes": homes}}}
    result = await feeds._fetch_prices_tibber()
    assert bool(result) is allowed
    if allowed:
        assert result.context.area == tibber_scope("B")
        assert binding.await_args_list[0].args == ("", "B", None)
        assert binding.await_args_list[0].kwargs == {"auto_bind": True}
    else:
        assert result.rejection_reason == "home_binding_requires_exactly_one_active_subscription"
        assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA


async def test_auto_binding_cannot_overwrite_concurrent_explicit_selection(boundary):
    values, response, _, _, binding = boundary
    values["tibber_home_id"] = ""
    binding.return_value = False
    response.json.return_value = {"data": {"viewer": {"homes": [home("A")]}}}
    result = await feeds._fetch_prices_tibber()
    assert result == []
    assert result.rejection_reason == "home_binding_changed_during_fetch"


async def test_inflight_switch_keeps_original_home_and_planner_uses_new_home(boundary, monkeypatch):
    from packages.poller import main
    from packages.core import planning_data_quality, pricing

    values, response, client, _, _ = boundary
    response.json.return_value = {"data": {"viewer": {"homes": [home("A"), home("B")]}}}

    async def switch(*args, **kwargs):
        values["tibber_home_id"] = "B"
        return response

    client.post.side_effect = switch
    feed = await feeds._fetch_prices_tibber()
    monkeypatch.setattr(
        main,
        "fetch_price_feed",
        AsyncMock(
            return_value=feeds.PriceFeed(
                list(feed),
                feed.currency,
                feed.source,
                feed.context,
                feed.interval_schema,
            )
        ),
    )
    monkeypatch.setattr(
        planning_data_quality, "get_planning_data_quality", AsyncMock(return_value={})
    )
    monkeypatch.setattr(main, "_queue_price_horizon_reoptimization", AsyncMock())
    session = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "get_session", MagicMock(return_value=context))
    registry = AsyncMock()
    monkeypatch.setattr(price_scope, "ensure_tibber_scope", registry)
    await main.poll_prices()
    registry.assert_awaited_once_with(session, area=tibber_scope("A"), home_id="A", currency="SEK")
    statement = session.execute.await_args.args[0].compile(dialect=postgresql.dialect())
    assert statement.params["area_m0"] == tibber_scope("A")
    from packages.core import settings_service

    monkeypatch.setattr(settings_service, "get_all_settings", AsyncMock(return_value=values))
    resolver = AsyncMock(return_value=tibber_scope("B"))
    monkeypatch.setattr(price_scope, "resolve_price_area", resolver)
    assert await pricing.get_active_price_area() == tibber_scope("B")
    assert resolver.await_args.args[0]["tibber_home_id"] == "B"


def test_registry_identity_encoding_and_settings_validation():
    import hashlib
    from packages.core.settings_service import validate_setting_value

    assert tibber_scope("A") == "tb:" + hashlib.sha256(b'["tibber","A"]').hexdigest()[:28]
    assert len(tibber_scope("A")) == 31
    assert tibber_scope("A") != tibber_scope("a")
    for invalid in (" A", "A "):
        with pytest.raises(ValueError):
            validate_setting_value("tibber_home_id", invalid)
    validate_setting_value("tibber_home_id", "")
    values = {
        "price_provider": "tibber",
        "tibber_home_id": "A",
        "tibber_feed_diagnostic": json.dumps({"home_id": "A", "reason": "selected_home_not_found"}),
    }
    assert price_scope.price_area_from_settings(values) == price_scope.UNAVAILABLE_AREA
    values["tibber_home_id"] = "B"
    assert price_scope.price_area_from_settings(values) == tibber_scope("B")


@pytest.mark.parametrize("conflict", [False, True, "missing", "currency"])
async def test_registry_insert_or_verify_never_updates(conflict):
    expected = dict(
        area=tibber_scope("A"), provider="tibber", home_id="A", **price_scope.tariff_metadata("SEK")
    )
    record = SimpleNamespace(**expected)
    if conflict is True:
        record.home_id = "collision"
    if conflict == "currency":
        record.currency = "EUR"
    result = MagicMock()
    result.scalar_one_or_none.return_value = None if conflict == "missing" else record
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    if conflict:
        with pytest.raises(ValueError, match="collision_or_metadata_conflict"):
            await price_scope.ensure_tibber_scope(
                session, area=expected["area"], home_id="A", currency="SEK"
            )
    else:
        await price_scope.ensure_tibber_scope(
            session, area=expected["area"], home_id="A", currency="SEK"
        )
    sql = str(session.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT DO NOTHING" in sql
    assert "DO UPDATE" not in sql


@pytest.mark.parametrize("legacy", [False, True])
async def test_historical_measurement_uses_persisted_identity_not_current_settings(
    monkeypatch, legacy
):
    from packages.core import plan_outcome

    now = dt.datetime(2026, 10, 15, 2, tzinfo=dt.timezone.utc)
    plan = SimpleNamespace(
        horizon_start=now - dt.timedelta(hours=2),
        horizon_end=now,
        price_source="tibber",
        price_currency="SEK",
        input_provenance_json=json.dumps(
            {
                "price": {
                    "area": "tibber" if legacy else tibber_scope("A"),
                    "source": "tibber",
                    "currency": "SEK",
                }
            }
        ),
    )
    measured = AsyncMock(return_value={"cost": {"actual_cost": 1.0}, "comfort": {"ok": True}})
    monkeypatch.setattr(plan_outcome, "measured_window_outcome", measured)
    outcome = await plan_outcome.plan_measurement(
        None, plan=plan, price_area=tibber_scope("B"), comfort_min_c=19, comfort_max_c=22, now=now
    )
    assert measured.await_args.kwargs["price_area"] == (
        price_scope.UNAVAILABLE_AREA if legacy else tibber_scope("A")
    )
    assert outcome["comfort"] == {"ok": True}
    if legacy:
        assert outcome["cost"]["reason"] == "legacy_tibber_scope_ambiguous"
        assert outcome["cost"]["actual_cost"] is None
    else:
        assert outcome["cost"]["actual_cost"] == 1.0


def test_migration_is_additive_and_registry_is_immutable(monkeypatch):
    path = Path(__file__).parents[1] / "migrations" / "versions" / "037_tibber_price_scopes.py"
    spec = importlib.util.spec_from_file_location("scope_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    op = MagicMock()
    monkeypatch.setattr(migration, "op", op)
    migration.upgrade()
    assert migration.down_revision == "036"
    assert op.create_table.call_args.args[0] == "price_scopes"
    sql = " ".join(call.args[0] for call in op.execute.call_args_list)
    assert "BEFORE UPDATE OR DELETE OR TRUNCATE" in sql
    assert "ALTER" not in sql
    migration.downgrade()
    op.drop_table.assert_called_once_with("price_scopes")


@pytest.mark.parametrize("registered_home", ["A", "B", None])
async def test_reader_rejects_hash_collision_or_unregistered_scope(registered_home):
    values = {"price_provider": "tibber", "tibber_home_id": "A"}
    record = (
        SimpleNamespace(provider="tibber", home_id=registered_home) if registered_home else None
    )
    result = SimpleNamespace(scalar_one_or_none=lambda: record)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    area = await price_scope.resolve_price_area(values, session=session)
    assert area == (tibber_scope("A") if registered_home == "A" else price_scope.UNAVAILABLE_AREA)


@pytest.mark.parametrize("selected", ["A", None])
async def test_transactional_autobinding_compares_empty_setting_and_preserves_provenance(
    monkeypatch, selected
):
    result = MagicMock()
    result.scalar_one_or_none.return_value = selected
    previous = MagicMock()
    previous.scalar_one_or_none.return_value = None
    session = SimpleNamespace(execute=AsyncMock(side_effect=[result, previous, MagicMock()]))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(price_scope, "get_session", MagicMock(return_value=context))
    accepted = await price_scope.record_tibber_binding("", "A", None, auto_bind=True)
    assert accepted is (selected == "A")
    statement = session.execute.await_args_list[0].args[0].compile(dialect=postgresql.dialect())
    assert "WHERE settings.value =" in str(statement)
    assert "" in statement.params.values()
    assert "RETURNING settings.value" in str(statement)
    if accepted:
        diagnostic = (
            session.execute.await_args_list[-1].args[0].compile(dialect=postgresql.dialect())
        )
        assert (
            json.loads(diagnostic.params["value"])["binding_method"] == "single_active_subscription"
        )
    else:
        assert session.execute.await_count == 1


async def test_stale_poll_cannot_write_diagnostic_for_replacement_home(monkeypatch):
    current = MagicMock()
    current.scalar_one.return_value = "B"
    session = SimpleNamespace(execute=AsyncMock(side_effect=[MagicMock(), current]))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(price_scope, "get_session", MagicMock(return_value=context))
    assert not await price_scope.record_tibber_binding("A", "A", "selected_home_not_found")
    sql = str(session.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert session.execute.await_count == 2


async def test_switched_home_has_no_old_feed_and_planning_admission_is_suspended(monkeypatch):
    from packages.core import planning_data_quality

    now = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)
    proof = SimpleNamespace(
        scalar_one_or_none=lambda: SimpleNamespace(provider="tibber", home_id="B")
    )
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(side_effect=[proof, empty, empty]))
    monkeypatch.setattr(
        planning_data_quality,
        "get_all_settings",
        AsyncMock(
            return_value={
                "price_provider": "tibber",
                "tibber_home_id": "B",
                "weather_provider": "open-meteo",
            }
        ),
    )
    batch = await planning_data_quality.load_planning_input_batch(
        session,
        now,
        now + dt.timedelta(hours=24),
        now=now,
    )
    price_query = session.execute.await_args_list[1].args[0].compile()
    assert price_query.params["area_1"] == tibber_scope("B")
    assert tibber_scope("A") not in price_query.params.values()
    assert batch.prices == ()
    assert planning_data_quality.planning_batch_quality(batch)["control_allowed"] is False


async def test_binding_diagnostic_is_visible_through_settings_api(monkeypatch):
    from packages.api.routers import settings as settings_router

    diagnostic = json.dumps({"home_id": "A", "reason": "selected_home_not_found"})
    monkeypatch.setattr(
        settings_router,
        "get_all_settings",
        AsyncMock(
            return_value={
                "tibber_home_id": "A",
                "tibber_feed_diagnostic": diagnostic,
            }
        ),
    )
    response = await settings_router.get_settings()
    assert response["tibber_home_id"]["value"] == "A"
    assert response["tibber_feed_diagnostic"]["value"] == diagnostic
