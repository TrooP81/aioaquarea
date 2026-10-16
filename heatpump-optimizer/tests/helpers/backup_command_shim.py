#!/usr/bin/env python3
"""Disposable command doubles for executable shell tests; never connects to a database.

The real worker owns scheduling, publication, retention, locks and process control.
This adapter models only command/SQL responses. SQL itself still requires owned CI.
All filesystem mutations are restricted to the test's exclusively created root.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import signal

ROOT = Path(os.environ["HPO_BACKUP_FAKE_ROOT"]).resolve(strict=True)


def load(name: str, default=None):
    path = ROOT / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def save(name: str, value):
    path = ROOT / name
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(path)


def event(name: str, **fields):
    with (ROOT / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"command": name, **fields}) + "\n")


def managed(value: str) -> Path:
    path = Path(value)
    assert path.resolve().is_relative_to(ROOT), "fake write escaped disposable root"
    return path


def iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(value: str) -> int:
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def variables(args):
    result = {}
    for index, arg in enumerate(args):
        if arg == "-v":
            key, value = args[index + 1].split("=", 1)
            result[key] = value
    return result


def resist(phase):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    save(
        "resistant.json",
        {
            "pid": os.getpid(),
            "group": os.getpgrp(),
            "phase": phase,
            "timeout": float(os.environ["HPO_BACKUP_ACTIVE_TIMEOUT"]),
        },
    )
    os.execv("/bin/sh", ["sh", "-c", "trap '' TERM; while :; do /bin/sleep 1; done"])


def eligible(manifest, source: str, now: int) -> bool:
    try:
        return (
            type(manifest["schema_version"]) is int
            and manifest["schema_version"] == 1
            and manifest["eligible"] is True
            and manifest["source_id"] == source
            and epoch(manifest["snapshot_at"]) <= epoch(manifest["completed_at"]) <= now
        )
    except (KeyError, TypeError, ValueError):
        return False


def psql(args, config, now):
    values = variables(args)
    state = load("state.json")
    fail = config.get("fail")
    if "-f" in args:
        phase = "start" if values["backup_start"] == "true" else "finish"
        event("status", phase=phase, values=values)
        if fail == "status_" + phase:
            return 1
        state = state or {"schema_version": 1}
        if state.get("schema_version") != 1:
            return 1
        if phase == "start":
            state["last_attempt"] = {
                "id": values["attempt"],
                "archive_id": values["archive"],
                "started_at": values["started"],
                "status": "running",
                "replica_status": "pending",
            }
        else:
            if state["last_attempt"]["id"] != values["attempt"]:
                return 1
            state["last_attempt"].update(
                finished_at=values["finished"],
                status=values["status"],
                reason_code=values["reason"],
                replica_status=values["replica"],
            )
            if values["succeeded"] == "true":
                state["last_success"] = {
                    "archive_id": values["archive"],
                    "snapshot_at": values["started"],
                    "completed_at": values["finished"],
                    "sha256": values["checksum"],
                    "source_id": values["source"],
                    "schema_revision": values["revision"],
                }
        save("state.json", state)
        return 0
    query = sys.stdin.read()
    if "WITH defaults" in query:
        event("policy")
        print(config.get("policy", "2592000|100|2764800|3600"))
    elif "WITH h AS" in query:
        event("due")
        due = state is None
        if state and state.get("schema_version") == 1 and state.get("last_attempt"):
            attempt = state["last_attempt"]
            success = state.get("last_success")
            due = not success or epoch(success["snapshot_at"]) + int(values["interval"]) <= now
            due = due and epoch(attempt.get("finished_at", attempt["started_at"])) + 300 <= now
            if attempt["status"] == "running":
                due = due and epoch(attempt["started_at"]) + int(values["timeout"]) + 120 <= now
        print("t" if due else "f")
    elif "FROM pg_control_system()" in query:
        count = load("identities.json", 0) + 1
        save("identities.json", count)
        event("identity", count=count)
        if config.get("resistant_phase") == "identity" and count == 1:
            resist("identity")
        if fail == "identity_start" and count == 1:
            return 1
        source = "changed" if fail == "identity_changed" and count == 2 else "source"
        print(f"{source}|16|2.26.4|036|100")
    elif "'database_size_bytes'" in query:
        event("manifest")
        if fail == "manifest":
            return 1
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "eligible": True,
                    "archive_id": values["archive"],
                    "sha256": values["checksum"],
                    "source_id": values["source"],
                    "schema_revision": values["revision"],
                    "postgres_major": int(values["major"]),
                    "timescale_version": values["timescale"],
                    "database_size_bytes": int(values["size"]),
                    "snapshot_at": values["started"],
                    "completed_at": values["finished"],
                }
            )
        )
    elif "extract(epoch" in query:
        event("inventory")
        try:
            manifest = json.loads(values["manifest"])
        except ValueError:
            return 1
        if eligible(manifest, values["source"], now):
            print(f"{manifest['archive_id']}|{manifest['sha256']}|{epoch(manifest['snapshot_at'])}")
    elif "last_verified" in query:
        event("retention")
        success = (state or {}).get("last_success", {})
        verified = (state or {}).get("last_verified", {})
        verification = (state or {}).get("last_verification", {})
        if (
            verified
            and success.get("source_id") == verified.get("source_id") == values["source"]
            and epoch(verified["verified_at"]) <= now
            and verification.get("status") == "succeeded"
            and all(verification.get(key) == verified.get(key) for key in ("archive_id", "sha256"))
        ):
            print(
                "|".join(
                    (
                        success["archive_id"],
                        success["sha256"],
                        verified["archive_id"],
                        verified["sha256"],
                    )
                )
            )
    elif "@>" in query:
        manifest = json.loads(values["manifest"])
        valid = eligible(manifest, values["source"], now) and all(
            manifest.get(key) == values[param]
            for key, param in (("archive_id", "archive"), ("sha256", "checksum"))
        )
        print("t" if valid else "f")
    elif "last_success" in query:
        event("selection")
        success = (state or {}).get("last_success", {})
        if success:
            print("|".join(success[key] for key in ("archive_id", "sha256", "source_id")))
    else:
        raise AssertionError("unrecognized fake SQL operation")
    return 0


def main() -> int:
    command, args = Path(sys.argv[0]).name, sys.argv[1:]
    config, now = load("config.json", {}), load("clock.json")
    fail = config.get("fail")
    if command == "psql":
        return psql(args, config, now)
    event(command, args=args)
    if command == "date":
        print(now if args[-1] == "+%s" else iso(now))
    elif command == "pg_dump":
        path = managed(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--file=")))
        path.write_bytes(b"synthetic custom archive\n")
        if config.get("resistant") and config.get("resistant_phase", "dump") == "dump":
            resist("dump")
        if fail == "dump":
            return 1
    elif command == "pg_restore":
        if fail == "toc" or not Path(args[-1]).read_bytes().startswith(b"synthetic"):
            return 1
    elif command == "sha256sum":
        if config.get("resistant_phase") == "checksum" and args[-1].endswith(".dump.partial"):
            resist("checksum")
        if fail == "checksum" and args[-1].endswith(".dump.partial"):
            return 1
        data = Path(args[-1]).read_bytes()
        print(hashlib.sha256(data).hexdigest() + "  " + args[-1])
    elif command == "mv":
        target = args[-1]
        suffix = {
            "publish_dump": ".dump",
            "publish_checksum": ".sha256",
            "publish_manifest": ".json",
        }.get(fail)
        if suffix and target.endswith(suffix):
            return 1
        managed(args[-2]).replace(managed(target))
    elif command == "openssl":
        if fail == "replica":
            return 1
        managed(args[args.index("-out") + 1]).write_bytes(b"synthetic encrypted replica")
    elif command == "cp":
        if config.get("pause_stage") and ".stage-" in args[-1] and args[-2].endswith(".dump"):
            save("stage_child.json", {"pid": os.getpid()})
            (ROOT / "copy_entered").touch()
            deadline = time.monotonic() + 15
            while not (ROOT / "copy_release").exists():
                if time.monotonic() >= deadline:
                    return 1
                time.sleep(0.01)
        shutil.copyfile(args[-2], managed(args[-1]))
    elif command == "sleep":
        if config.get("real_sleep") or (config.get("resistant") and args == ["2"]):
            os.execv(os.environ["HPO_BACKUP_REAL_SLEEP"], ["sleep", *args])
        count = load("sleeps.json", 0) + 1
        save("sleeps.json", count)
        save("clock.json", now + int(args[0]))
        if config.get("sleep_policy"):
            config["policy"] = config.pop("sleep_policy")
            save("config.json", config)
        return 73 if count >= config.get("stop_after_sleeps", 1) else 0
    elif command == "timeout":
        # Execute the real runtime timer in-place, preserving PID/process group
        # and inherited lock descriptors. Only its duration is shortened.
        assert args[:2] == ["-k", "2"], "hard kill-after is mandatory"
        if config.get("timeout_cap"):
            args[2] = str(min(float(args[2]), config["timeout_cap"]))
        os.environ["HPO_BACKUP_ACTIVE_TIMEOUT"] = args[2]
        if config.get("pid_only_timeout"):
            # GNU foreground mode, like BusyBox, signals only the monitored PID.
            # This forces the stage's independent group watchdog to do real work.
            args.insert(0, "--foreground")
        os.execv(os.environ["HPO_BACKUP_REAL_TIMEOUT"], ["timeout", *args])
    elif command == "flock":
        # Preserve fd 9's *same open file description*: the real shell remains
        # lock owner after the flock subprocess exits.
        code = subprocess.run(
            [os.environ["HPO_BACKUP_REAL_FLOCK"], *args],
            close_fds=False,
            check=False,
        ).returncode
        if code:
            event("lock_busy")
        return code
    else:
        raise AssertionError(f"unknown fake command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
