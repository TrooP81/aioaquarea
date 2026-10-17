"""Diagnostic command capture for synthetic qualification only, never public health output."""

from __future__ import annotations

import json
import os
import signal
import subprocess


def command_result(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        # A dead leader may still have live descendants holding the pipes.
        # Settle the recorded group even when poll() already reports exit.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
        raise
    result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    assert result.returncode == 0, (
        f"{' '.join(args[:2])}: exit={result.returncode}; stderr={stderr!r}; stdout={stdout!r}"
    )
    return result


def command_json(stdout: str, *, command: str, stderr: str):
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"{command}: invalid JSON; stderr={stderr!r}; stdout={stdout!r}"
        ) from error
