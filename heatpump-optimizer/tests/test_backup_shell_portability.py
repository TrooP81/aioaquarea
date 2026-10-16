"""Small actual-shell probes; these do not replace the real Linux worker matrix."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def shell_probe(route, text):
    environment = {**os.environ, "HPO_SHELL_PROBE": text}
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if not git_bash.is_file():
            pytest.skip("Git POSIX shells unavailable; Linux matrix still required")
        binary = "dash" if route == "sh" else "bash"
        command = [
            str(git_bash),
            "--noprofile",
            "--norc",
            "-c",
            f'PATH=/usr/bin:/bin:$PATH; export PATH; exec /usr/bin/{binary} -c "$HPO_SHELL_PROBE"',
        ]
    else:
        binary = shutil.which(route)
        assert binary, f"required POSIX shell missing: {route}"
        command = [binary, "-c", text]
    return subprocess.run(
        command, env=environment, capture_output=True, text=True, timeout=10, check=False
    )


@pytest.mark.parametrize("route", ["bash", "sh"])
def test_actual_owned_function_preserves_caller_sql_stdin(route):
    worker = (SCRIPTS / "backup_db.sh").read_text(encoding="utf-8")
    owned = worker[worker.index("owned() {") : worker.index("\nbounded() {")]
    # Isolate stdin forwarding only. Never fake a successful group/lock test:
    # Linux's full executable matrix retains the real setsid/timeout/flock.
    script = (
        'setsid() { [ "$1" = timeout ] || return 2; shift 4; "$@"; }\n'
        "kill() { return 0; }\n"
        + owned
        + "\nowned 5 cat <<'SQL'\nSELECT original_sql_stdin;\nSQL\n"
    )
    result = shell_probe(route, script)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "SELECT original_sql_stdin;\n"


@pytest.mark.parametrize("route", ["bash", "sh"])
def test_group_signal_syntax_is_accepted_by_actual_shell(route):
    for name in ("backup_db.sh", "stage_backup.sh"):
        script = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "kill -KILL --" not in script and "kill -TERM --" not in script
        assert "kill -s KILL --" in script
    # Signal zero only queries a nonexistent group; no process is signalled.
    result = shell_probe(route, "kill -s 0 -- -2147483647")
    assert result.returncode == 1, result.stderr
    assert "Illegal" not in result.stderr and "invalid" not in result.stderr.lower()
