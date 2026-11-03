"""Static checks for the Android version updater workflow."""

from __future__ import annotations

import pathlib
import re

WORKFLOW = (
    pathlib.Path(__file__).parents[1]
    / ".github"
    / "workflows"
    / "update-appstore-version.yml"
)


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps(text: str) -> dict[str, str]:
    steps: dict[str, str] = {}
    for block in re.split(r"\n      - ", text)[1:]:
        match = re.match(r"name: (.+)", block)
        if match:
            steps[match.group(1).strip()] = block
    return steps


def test_workflow_uses_shared_sdk_parser_and_google_play_only():
    text = _text()
    lowered = text.lower()
    assert "python -m aioaquarea._android_version" in text
    assert "itunes" not in lowered
    assert "apple" not in lowered
    assert "jq " not in text
    assert "curl" not in lowered


def test_lookup_failure_stops_before_edit_or_pull_request():
    steps = _steps(_text())
    lookup = steps["Get Google Play Android version"]
    assert "set -euo pipefail" in lookup
    assert "id: android" in lookup
    assert "continue-on-error" not in _text()
    for name in ("Update const.py", "Create Pull Request"):
        assert "steps.android.outcome == 'success'" in steps[name]
        assert "always()" not in steps[name]
        assert "failure()" not in steps[name]


def test_lookup_step_revalidates_format_and_logs_no_page_content():
    lookup = _steps(_text())["Get Google Play Android version"]
    assert "^[0-9]{1,6}\\.[0-9]{1,6}\\.[0-9]{1,6}$" in lookup
    assert "^0+\\.0+\\.0+$" in lookup
    assert "Response" not in lookup
    assert "html" not in lookup.lower().replace("--html-file", "")


def test_summary_names_google_play_source():
    summary = _steps(_text())["Summary"]
    assert "Google Play" in summary
    assert "App Store" not in summary
