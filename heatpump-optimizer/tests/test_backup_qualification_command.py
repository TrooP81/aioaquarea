"""Qualification diagnostics preserve synthetic stderr and bounded cancellation."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.helpers import backup_qualification_command as commands


@pytest.mark.parametrize("stdout", ["", "not json", "{"])
def test_invalid_json_names_command_and_retains_stderr(stdout):
    with pytest.raises(AssertionError, match="docker exec: invalid JSON") as error:
        commands.command_json(stdout, command="docker exec", stderr="synthetic SQL failure")
    assert "synthetic SQL failure" in str(error.value)
    assert isinstance(error.value.__cause__, json.JSONDecodeError)


def test_successful_command_retains_stderr_for_empty_sql_diagnostic():
    result = commands.command_result(
        [sys.executable, "-c", "import sys; print('synthetic SQL failure', file=sys.stderr)"],
        timeout=10,
    )
    assert result.returncode == 0 and result.stdout == ""
    with pytest.raises(AssertionError, match="synthetic SQL failure"):
        commands.command_json(result.stdout, command="docker exec psql", stderr=result.stderr)


def test_failed_command_reports_exit_and_stderr():
    with pytest.raises(AssertionError, match="exit=3") as error:
        commands.command_result(
            [
                sys.executable,
                "-c",
                "import sys; print('synthetic tool failure', file=sys.stderr); sys.exit(3)",
            ],
            timeout=10,
        )
    assert "synthetic tool failure" in str(error.value)


@pytest.mark.parametrize("error_type", [subprocess.TimeoutExpired, KeyboardInterrupt])
@pytest.mark.parametrize("leader_exited", [False, True])
def test_command_cancellation_settles_group_before_propagating(
    monkeypatch, error_type, leader_exited
):
    calls = []
    failure = (
        subprocess.TimeoutExpired(["synthetic"], 1)
        if error_type is subprocess.TimeoutExpired
        else KeyboardInterrupt()
    )

    class Process:
        pid = 12345

        def __init__(self):
            self.waits = 0

        def communicate(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise failure
            if self.waits == 2 and error_type is subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(["synthetic"], 2)
            return "", ""

        def poll(self):
            return 0 if leader_exited else None

    def popen(args, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()

    monkeypatch.setattr(commands.subprocess, "Popen", popen)
    # POSIX signal model only; Windows has no SIGKILL/killpg. No real signals.
    monkeypatch.setattr(commands, "signal", SimpleNamespace(SIGTERM=signal.SIGTERM, SIGKILL=9))
    monkeypatch.setattr(
        commands.os, "killpg", lambda pid, sig: calls.append((pid, sig)), raising=False
    )
    with pytest.raises(error_type) as error:
        commands.command_result(["synthetic"], timeout=1)
    assert error.value is failure
    assert calls == [(12345, signal.SIGTERM)] + (
        [(12345, 9)] if error_type is subprocess.TimeoutExpired else []
    )
