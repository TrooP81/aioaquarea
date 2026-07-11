from packages.api.main import app


def _route_paths(routes):
    """Yield route paths from both direct routes and FastAPI included routers."""
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            yield path
        nested_routes = getattr(route, "routes", None)
        if nested_routes is None:
            nested_routes = getattr(getattr(route, "original_router", None), "routes", None)
        if nested_routes:
            yield from _route_paths(nested_routes)


def test_expected_route_paths_present():
    paths = set(_route_paths(app.routes))
    expected = {
        "/health",
        "/api/version",
        "/api/dashboard",
        "/api/status/history",
        "/api/device/settings",
        "/api/consumption/history",
        "/api/prices",
        "/api/weather",
        "/api/plans",
        "/api/plans/{plan_id}",
        "/api/plan-activity",
        "/api/overrides",
        "/api/overrides/{override_id}",
        "/api/settings",
        "/api/comfort-schedule",
        "/api/comfort-schedule/learned",
        "/api/comfort-schedule/apply-learned",
        "/api/audit",
        "/api/logs",
        "/api/currency",
        "/api/time-format",
        "/api/indoor-temp",
        "/api/indoor-temp/latest",
        "/api/smartthings/devices",
        "/api/smartthings/oauth/authorize",
        "/api/smartthings/oauth/callback",
        "/api/smartthings/oauth/status",
        "/api/smartthings/oauth/disconnect",
        "/api/comfort-model/status",
        "/api/comfort-model/train",
        "/api/comfort-model/predict",
        "/api/optimizer/status",
        "/api/learning-mode",
        "/api/ml/train",
        "/api/thermal/status",
        "/api/thermal/calibrate",
        "/api/optimize-now",
        "/api/thermal/curve",
        "/api/thermal/indoor-forecast",
        "/api/poll-now",
        "/api/test-connection",
        "/api/faults",
        "/api/faults/{fault_id}/resolve",
        "/api/cop/history",
        "/api/cop/stats",
        "/api/cop/compute",
        "/api/compressor/activity",
        "/api/admin/reset",
    }
    assert expected.issubset(paths)
