"""Actionable operational alerts and optional, throttled webhook delivery."""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import httpx
import structlog
from sqlalchemy import select

from packages.core.database import get_session
from packages.core.models import (
    DeviceStatusRecord,
    PlanActionRecord,
    ServiceHeartbeatRecord,
)
from packages.core.planning_data_quality import get_planning_data_quality
from packages.core.panasonic_diagnostics import project_panasonic_adapter_state
from packages.core.service_health import service_heartbeat_details
from packages.core.settings_service import (
    get_bool_setting,
    get_int_setting,
    get_setting,
    set_setting,
)

logger = structlog.get_logger()
_WEBHOOK_THROTTLE = dt.timedelta(minutes=30)


def device_status_is_fresh(
    observed_at: dt.datetime | None,
    *,
    now: dt.datetime,
    poll_interval_seconds: int,
) -> bool:
    """Return whether a device observation is recent enough to call live."""

    if observed_at is None:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
    cutoff = now - dt.timedelta(seconds=max(poll_interval_seconds * 3, 15 * 60))
    return observed_at >= cutoff


def _alert(
    alert_id: str,
    severity: str,
    title: str,
    detail: str,
    *,
    action: str | None = None,
    plan_id: int | None = None,
    action_id: int | None = None,
    href: str | None = None,
) -> dict[str, object]:
    return {
        "id": alert_id,
        "severity": severity,
        "title": title,
        "detail": detail,
        "action": action,
        "plan_id": plan_id,
        "action_id": action_id,
        "href": href,
    }


def _panasonic_adapter_alert(adapter: dict[str, object]) -> dict[str, object] | None:
    """Build one actionable outage alert from fresh projected diagnostics."""

    if not adapter.get("state_fresh") or adapter.get("status") not in {
        "unavailable",
        "backoff",
    }:
        return None
    failures = int(adapter.get("consecutive_failures") or 0)
    detail = f"The Panasonic adaptor has failed {failures} consecutive live-status attempt(s)."
    retry_at = adapter.get("retry_at")
    if retry_at:
        detail += f" Next retry is scheduled for {retry_at}."
    return _alert(
        "panasonic_adapter_unavailable",
        "warning",
        "Panasonic adaptor is unavailable",
        detail,
        action="Check the adaptor power and network connection; automatic commands remain paused.",
    )


async def get_operational_alerts(
    *,
    now: dt.datetime | None = None,
) -> dict[str, object]:
    """Evaluate live conditions without changing pump state or plan state."""

    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    else:
        now = now.astimezone(dt.timezone.utc)
    enabled = await get_bool_setting("operational_alerts_enabled")
    if not enabled:
        return {"enabled": False, "generated_at": now.isoformat(), "alerts": []}

    poll_interval = await get_int_setting("poll_interval_seconds")
    service_cutoff = now - dt.timedelta(minutes=3)
    action_since = now - dt.timedelta(hours=24)
    async with get_session() as session:
        heartbeat_rows = (
            (
                await session.execute(
                    select(ServiceHeartbeatRecord).where(
                        ServiceHeartbeatRecord.service.in_(["poller", "optimizer"])
                    )
                )
            )
            .scalars()
            .all()
        )
        latest_device = (
            await session.execute(
                select(DeviceStatusRecord.ts).order_by(DeviceStatusRecord.ts.desc()).limit(1)
            )
        ).scalar_one_or_none()
        failed_action_rows = (
            (
                await session.execute(
                    select(PlanActionRecord)
                    .where(PlanActionRecord.scheduled_ts >= action_since)
                    .where(PlanActionRecord.status.in_(["failed", "expired"]))
                    .order_by(PlanActionRecord.scheduled_ts.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )

    alerts: list[dict[str, object]] = []
    by_service = {row.service: row for row in heartbeat_rows}
    for service in ("poller", "optimizer"):
        heartbeat = getattr(by_service.get(service), "updated_at", None)
        if heartbeat is None or heartbeat < service_cutoff:
            alerts.append(
                _alert(
                    f"{service}_stale",
                    "critical",
                    f"{service.title()} service is stale",
                    "No heartbeat has been received in the last three minutes.",
                    action="Check the service container and its logs.",
                )
            )
    poller_details = service_heartbeat_details(by_service.get("poller"))
    adapter = project_panasonic_adapter_state(
        poller_details.get("panasonic_adapter"),
        now=now,
        stale_after_seconds=max(poll_interval * 3, 15 * 60),
    )
    adapter_alert = _panasonic_adapter_alert(adapter)
    if adapter_alert:
        alerts.append(adapter_alert)
    if not device_status_is_fresh(
        latest_device,
        now=now,
        poll_interval_seconds=poll_interval,
    ) and adapter_alert is None:
        alerts.append(
            _alert(
                "device_data_stale",
                "warning",
                "Heat-pump status is stale",
                "Automatic commands stay paused until a fresh device status is collected.",
                action="Check the Panasonic connection and poller.",
            )
        )
    if failed_action_rows:
        affected = failed_action_rows[0]
        alerts.append(
            _alert(
                "plan_actions_failed",
                "warning",
                "Recent plan actions need attention",
                f"{len(failed_action_rows)} action(s) failed or expired in the last 24 hours.",
                action="Open Plan history to review the affected actions.",
                plan_id=affected.plan_id,
                action_id=affected.id,
                href=f"/?view=plan&activity=failed#plan-action-{affected.id}",
            )
        )

    planning_quality = await get_planning_data_quality(now=now)
    if not planning_quality["control_allowed"]:
        alerts.append(
            _alert(
                "planning_inputs_degraded",
                "warning",
                "Planning inputs are incomplete",
                " ".join(str(reason) for reason in planning_quality.get("reasons", [])),
                action="Wait for fresh price and weather data before relying on a new plan.",
            )
        )

    from packages.ml.forecast_quality import get_forecast_scorecard

    scorecard = await get_forecast_scorecard(now=now)
    gate = scorecard.get("quality_gate", {})
    if gate.get("status") == "failed":
        alerts.append(
            _alert(
                "forecast_quality_failed",
                "warning",
                "Forecast quality fell below the control threshold",
                str(gate.get("reason", "Forecast validation failed")).replace("_", " "),
                action="Rules remain active until new observations improve the forecast score.",
            )
        )
    fallback_gate = (
        scorecard.get("fallback", {}).get("quality_gate", {})
        if isinstance(scorecard.get("fallback"), dict)
        else {}
    )
    if fallback_gate.get("status") == "failed":
        alerts.append(
            _alert(
                "fallback_forecast_quality_failed",
                "warning",
                "Rule-based indoor forecast needs recalibration",
                str(fallback_gate.get("reason", "Fallback forecast validation failed")).replace(
                    "_", " "
                ),
                action="This is a visible forecast-quality issue; it does not approve or reject the learned comfort controller.",
            )
        )
    return {
        "enabled": True,
        "generated_at": now.isoformat(),
        "alerts": alerts,
        "summary": {
            "critical": sum(alert["severity"] == "critical" for alert in alerts),
            "warning": sum(alert["severity"] == "warning" for alert in alerts),
        },
    }


async def deliver_operational_alert_webhook() -> dict[str, object]:
    """Deliver changed active alerts to an explicitly configured webhook.

    External delivery is fully opt-in.  Identical alert sets are throttled so a
    long outage cannot generate a notification storm.
    """

    result = await get_operational_alerts()
    webhook_url = (await get_setting("operational_alert_webhook_url")).strip()
    if not webhook_url or not result["alerts"]:
        return {**result, "webhook": "not_configured_or_no_alerts"}
    if not webhook_url.lower().startswith("https://"):
        logger.warning("operational_alert_webhook_rejected", reason="https_required")
        return {**result, "webhook": "https_required"}

    payload = {"source": "heatpump-optimizer", **result}
    fingerprint = hashlib.sha256(
        json.dumps(payload["alerts"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    now = dt.datetime.now(dt.timezone.utc)
    previous_raw = await get_setting("_operational_alert_delivery_state")
    try:
        previous = json.loads(previous_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    previous_at = previous.get("sent_at") if isinstance(previous, dict) else None
    try:
        sent_at = dt.datetime.fromisoformat(previous_at) if isinstance(previous_at, str) else None
    except ValueError:
        sent_at = None
    if sent_at is not None and sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
    if previous.get("fingerprint") == fingerprint and sent_at and now - sent_at < _WEBHOOK_THROTTLE:
        return {**result, "webhook": "throttled"}

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.post(webhook_url, json=payload)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("operational_alert_webhook_failed", error=str(exc))
        return {**result, "webhook": "failed"}
    await set_setting(
        "_operational_alert_delivery_state",
        json.dumps({"fingerprint": fingerprint, "sent_at": now.isoformat()}),
    )
    logger.info("operational_alert_webhook_delivered", alerts=len(result["alerts"]))
    return {**result, "webhook": "delivered"}
