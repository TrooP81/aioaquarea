"""OBS-5 races on receipt-proven disposable PostgreSQL, run with --noconftest.

The coordinator must provision/migrate its own services and prepare the canonical
tested-source manifest (``current_verification_source_proof``). No service creation,
adoption or Compose operation occurs here.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import dis
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import uuid
import weakref
from contextvars import ContextVar
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
from sqlalchemy import Column, MetaData, Table, event, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema, Sequence
from sqlalchemy.sql.dml import Delete, Insert, Update
from sqlalchemy.sql.selectable import Select, TableClause
from sqlalchemy.sql.elements import ClauseElement, quoted_name

from packages.core.config import settings
from packages.core.models import (
    AuditLogRecord,
    ConsumptionRecord,
    DeviceStatusRecord,
    HouseContractRevisionRecord,
    HouseInstallationRecord,
    IndoorTempReading,
    MLArtifactRecord,
    MLForecastIssueRecord,
    MLForecastOutcomeRecord,
    MLLifecycleStateRecord,
    MLMaintenanceRequestRecord,
    OverrideRecord,
    PlanActionRecord,
    PlanRecord,
    PriceRecord,
    SettingRecord,
    WeatherRecord,
)
from packages.ml.lifecycle import (
    CASConflict,
    Lifecycle,
    LeaseLost,
    LifecycleUnavailable,
    PublicationResult,
    invalidate_lifecycle,
)
from packages.ml.safe_persistence import publish_signed_artifact, verify_signed_artifact
from tests.e2e.test_manual_cop_postgresql import (
    CURRENT_HEAD_REVISION,
    current_verification_source_proof,
    guard_owned_database,
    migration_targets,
    owned_test_environment,
    owned_test_network,
    retained_storage_state,
    write_lifecycle_receipt,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _Investigation:
    """Passive, session-owned evidence. Never owns application work or transactions."""

    EVENT_LIMIT = 4096
    EVENT_BYTES = 2048
    ARENA_BYTES = 4 * 1024 * 1024
    RESERVE_BYTES = 16 * 1024
    ALIAS_LIMITS = {"engine": 128, "connection": 256, "task": 256, "operation": 128}
    # Non-borrowing lanes: 51 * (72 credits / 80 KiB) + session (16 / 16 KiB).
    LANES = {
        "candidate": (8, 12 * 1024),
        "incumbent": (8, 12 * 1024),
        "inputs": (4, 8 * 1024),
        "logging": (12, 16 * 1024),
        "sequence": (12, 16 * 1024),
        "batch": (12, 8 * 1024),
        "boundary": (16, 8 * 1024),
    }
    SELECTION_KEYS = (
        "routine_filtered",
        "classification_uncertain",
        "correlation_unknown",
        "relevance_overflow",
        "relevance_ambiguous",
        "relevance_unassociated",
        "retired_without_terminal",
        "owner_unknown",
        "saturated",
    )
    REASONS = (
        "events",
        "arena",
        "event_bytes",
        "serialization",
        "aliases",
        "sensors",
        "thread",
        "profile",
        "install",
        "restore",
        "ownership",
        "emission",
    )
    STAGES = (
        "session",
        "node",
        "node_end",
        "baseline",
        "call",
        "pre-drop",
        "drop",
        "absence",
        "post-drop",
        "engine-dispose",
        "control-dispose",
        "receipt",
        "acceptance",
    )

    def __init__(self):
        self.events = []
        self.bytes = 0
        self.drops = dict.fromkeys(self.REASONS, 0)
        self.aliases = {kind: {} for kind in self.ALIAS_LIMITS}
        self.next_alias = dict.fromkeys(self.ALIAS_LIMITS, 0)
        self.thread = threading.get_ident()
        self.node = 0
        self.stage = "session"
        self.operation = ContextVar("lifecycle-investigation-operation", default=0)
        self.scope = ContextVar("lifecycle-investigation-scope", default=None)
        self.active = 0
        self.listeners = []
        self.wrappers = []
        self.profile = None
        self.codes = {}
        self.stack_codes = {}
        self.closed = False
        self.source = {}
        self.origins = {}
        self.pending_bytes = b'{"state":"UNKNOWN"}'
        # Each lane stores retained count, encoded bytes, and capacity losses.
        self.lanes = [[[0, 0, 0] for _ in self.LANES] for _ in range(52)]
        self.selection = dict.fromkeys(self.SELECTION_KEYS, 0)
        self.relevance = {}

    def selected(self, key):
        if self.selection[key] == 2**31 - 1:
            self.selection["saturated"] = 1
        else:
            self.selection[key] += 1

    def context(self):
        scope = self.scope.get()
        return scope if scope is not None and scope["live"] else None

    def lane(self, event, scope, category, owner):
        owner = scope["owner"] if owner is None and scope is not None else owner
        owner = self.node if owner is None else owner
        if type(owner) is not int or not 1 <= owner <= 51:
            if owner != 0:
                self.selected("owner_unknown")
            owner = 0
        if category is None:
            if event.startswith("publish_") or event == "inputs_returned":
                category = scope["role"] if scope is not None else "inputs"
                if category not in ("candidate", "incumbent"):
                    category = "inputs"
            elif event.startswith("batch_"):
                category = "batch"
            else:
                category = "boundary"
        index = tuple(self.LANES).index(category) if owner else 0
        limits = self.LANES[category] if owner else (16, 16 * 1024)
        return owner, self.lanes[owner][index], limits

    def gap(self, reason):
        # Fixed keys and saturating counters also bound fault paths.
        self.drops[reason] = min(self.drops[reason] + 1, 2**31 - 1)

    def reserve(self):
        if self.closed:
            return False
        if threading.get_ident() != self.thread:
            self.gap("thread")
            return False
        if len(self.events) >= self.EVENT_LIMIT:
            self.gap("events")
            return False
        if self.bytes + self.EVENT_BYTES > self.ARENA_BYTES:
            self.gap("arena")
            return False
        return True

    def alias(self, kind, value):
        if value is None:
            return 0
        mapping = self.aliases[kind]
        key = id(value)
        previous = mapping.get(key)
        if previous is not None and previous[0]() is value:
            return previous[1]
        if self.next_alias[kind] >= self.ALIAS_LIMITS[kind]:
            self.gap("aliases")
            return 0
        number = self.next_alias[kind] + 1
        # Only weak ownership: a completed task/closed connection is not kept alive.
        mapping[key] = (weakref.ref(value), number)
        self.next_alias[kind] = number
        return number

    def task(self):
        try:
            return self.alias("task", asyncio.current_task())
        except RuntimeError:
            return 0

    def stack(self):
        result = []
        frame = sys._getframe(1)
        try:
            for _ in range(24):
                if frame is None:
                    break
                code = frame.f_code
                entry = self.stack_codes.get(id(code))
                if entry is not None and entry[0] is code:
                    result.append([entry[1], frame.f_lineno])
                    if len(result) == 6:
                        break
                frame = frame.f_back
        finally:
            del frame
        return result

    def record(self, event, detail=None, *, category=None, owner=None, association=None):
        """Detail producers run only after capacity reservation; never retain payloads."""
        try:
            if not self.reserve():
                return
            scope = self.scope.get()
            metadata = scope if scope is not None else association
            if scope is None and association is not None and association["ambiguous"]:
                # Preserve its budget owner, not an invented operation attribution.
                metadata = None
            owner, lane, limits = self.lane(event, metadata, category, owner)
            if lane[0] >= limits[0] or lane[1] + self.EVENT_BYTES > limits[1]:
                if lane[2] == 2**31 - 1:
                    self.selection["saturated"] = 1
                lane[2] = min(lane[2] + 1, 2**31 - 1)
                self.gap("events" if lane[0] >= limits[0] else "arena")
                return
            operation = self.operation.get() if scope is None or scope["live"] else 0
            record = {
                "event": event,
                "node": self.node,
                "stage": self.enum(self.stage, self.STAGES),
                "task": self.task(),
                "operation": operation,
                "operation_origin": metadata["entry"] if metadata is not None else 0,
                "reservation_owner_node": owner,
                "wrapper_kind": metadata["kind"] if metadata is not None else "UNKNOWN",
                "publication_role": metadata["role"] if metadata is not None else "UNKNOWN",
                "correlation": "known" if operation else "UNKNOWN",
                "scope_live": scope is not None and scope["live"],
                "operation_origin_kind": "wrapper_entry",
                "batch_admission_origin": "UNKNOWN",
                "task_origin": "UNKNOWN",
            }
            if detail is not None:
                record.update(detail())
            encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode(
                "ascii"
            )
            if len(encoded) > self.EVENT_BYTES:
                self.gap("event_bytes")
                return
            self.events.append(encoded)
            self.bytes += len(encoded)
            lane[0] += 1
            lane[1] += len(encoded)
        except BaseException:
            self.gap("serialization")

    @staticmethod
    def enum(value, allowed):
        return value if type(value) is str and len(value) <= 64 and value in allowed else "UNKNOWN"

    @staticmethod
    def count(value, limit):
        return (
            len(value) if type(value) in (list, tuple, set) and len(value) <= limit else "UNKNOWN"
        )

    @staticmethod
    def loaded(row, key):
        # Bypass mapped descriptors, including expired/unloaded attributes.
        comfort = sys.modules.get("packages.ml.comfort_model")
        observation = vars(comfort).get("_IndoorObservation") if comfort is not None else None
        if type(row) not in (
            DeviceStatusRecord,
            IndoorTempReading,
            SimpleNamespace,
            HouseInstallationRecord,
            observation,
        ):
            return None
        values = object.__getattribute__(row, "__dict__")
        return values.get(key) if type(values) is dict else None

    def inputs(self, frame, result):
        local = frame.f_locals
        now = local.get("now")
        values = local.get("values")
        values = values if type(values) is dict else {}
        component = self.enum(local.get("component"), ("cop", "demand", "comfort", "thermal"))
        detail = {
            "observer": self.codes[id(frame.f_code)][1],
            "result": "none" if result is None else "present",
            "component": component,
            "clock": now.timestamp() if type(now) is dt.datetime else "UNKNOWN",
            "status_count": self.count(local.get("statuses"), 1001),
            "selected_count": self.count(local.get("selected_ids"), 64),
            "sensor_count": self.count(local.get("sensor_ids"), 64),
            "fresh_count": self.count(local.get("fresh_ids"), 64),
            "reading_count": self.count(local.get("readings"), 5001),
            "filtered_count": self.count(local.get("raw_indoor"), 5001),
            "sensor_freshness_seconds": 900,
            "indoor_available": (
                local["indoor_available"]
                if type(local.get("indoor_available")) is bool
                else "NOT_REACHED"
            ),
            "reference_blocked": (
                local["reference_blocked"]
                if type(local.get("reference_blocked")) is bool
                else "NOT_REACHED"
            ),
            "provider": self.enum(values.get("weather_provider"), ("open-meteo", "smhi")),
            "timezone": self.enum(
                values.get("timezone"), ("Europe/Amsterdam", "Europe/Stockholm", "UTC")
            ),
            "reference": self.enum(local.get("reference_sensor"), ("selected-room", "second-room")),
            "selected_ids": [],
            "installation": "UNKNOWN",
            "capabilities": [],
        }
        capabilities = local.get("capabilities")
        if type(capabilities) in (tuple, list) and len(capabilities) <= 8:
            detail["capabilities"] = [
                self.enum(item, ("indoor_heating", "indoor_cooling", "dhw"))
                for item in capabilities
            ]
        for name in ("selected_ids", "sensor_ids", "fresh_ids"):
            value = local.get(name)
            if type(value) in (list, set, tuple) and len(value) > 64:
                self.gap("sensors")
            elif type(value) in (list, set, tuple):
                detail[name + "_aliases"] = sorted(
                    self.enum(item, ("selected-room", "second-room")) for item in value
                )
        for name in ("readings", "raw_indoor"):
            value = local.get(name)
            if type(value) is list and len(value) > 5001:
                self.gap("sensors")
        statuses = local.get("statuses")
        if type(statuses) is list and statuses:
            stamp = self.loaded(statuses[-1], "ts")
            detail["latest_status_age"] = (
                (now - stamp).total_seconds()
                if type(now) is dt.datetime and type(stamp) is dt.datetime
                else "UNKNOWN"
            )
        readings = local.get("readings")
        if type(readings) is list and 0 < len(readings) <= 5001:
            stamp = self.loaded(readings[-1], "timestamp")
            detail["latest_selected_age"] = (
                (now - stamp).total_seconds()
                if type(now) is dt.datetime and type(stamp) is dt.datetime
                else "UNKNOWN"
            )
        installation = local.get("installation")
        generation = self.loaded(installation, "indoor_dependency_generation")
        detail["indoor_generation"] = (
            generation if type(generation) is int and 0 <= generation < 2**63 else "UNKNOWN"
        )
        generation = self.loaded(installation, "demand_dependency_generation")
        detail["demand_generation"] = (
            generation if type(generation) is int and 0 <= generation < 2**63 else "UNKNOWN"
        )
        # Do not retain data, row collections, settings values, frames or return objects.
        return detail

    def install_profile(self, functions):
        if sys.getprofile() is not None:
            self.gap("profile")
            return
        if sys.implementation.name != "cpython" or sys.version_info[:2] not in ((3, 11), (3, 12)):
            self.gap("profile")
            return
        for alias, function in functions:
            code = function.__code__
            returns = frozenset(
                instruction.offset
                for instruction in dis.get_instructions(code)
                if instruction.opname in ("RETURN_VALUE", "RETURN_CONST")
            )
            self.codes[id(code)] = (code, alias, returns)
            self.stack_codes[id(code)] = (code, alias)

        def observe(frame, event, result):
            try:
                if event != "return":
                    return
                code = frame.f_code
                entry = self.codes.get(id(code))
                if entry is None or entry[0] is not code:
                    return
                if frame.f_lasti not in entry[2]:
                    # Includes suspension, exceptional exit and cancellation.
                    return
                self.record("inputs_returned", lambda: self.inputs(frame, result))
            except BaseException:
                self.gap("profile")

        self.profile = observe
        sys.setprofile(observe)

    def wrap(self, owner, name, role):
        original = getattr(owner, name)
        code = original.__code__
        self.stack_codes[id(code)] = (code, role)

        @wraps(original, updated=())
        async def observed(*args, **kwargs):
            token = None
            parent = self.scope.get()
            scope = {
                "kind": self.enum(role, ("publish", "batch")),
                "role": self.enum(kwargs.get("role", "candidate"), ("candidate", "incumbent"))
                if role == "publish"
                else "UNKNOWN",
                "entry": self.node,
                # Even an expired copied scope must never steal the current node's lane.
                "owner": parent["owner"] if parent is not None else self.node,
                "live": True,
            }
            scope_token = self.scope.set(scope)
            try:
                if self.next_alias["operation"] >= self.ALIAS_LIMITS["operation"]:
                    self.gap("aliases")
                    self.selected("correlation_unknown")
                    number = 0
                else:
                    self.next_alias["operation"] += 1
                    number = self.next_alias["operation"]
                    self.origins[number] = self.node
                token = self.operation.set(number)
                self.active += 1
                self.record(
                    role + "_started",
                    lambda: {
                        "origin_node": self.node,
                        "publication_role": self.enum(
                            kwargs.get("role", "candidate"), ("candidate", "incumbent")
                        )
                        if role == "publish"
                        else "UNKNOWN",
                        "qualification": (
                            "SUPPLIED" if kwargs.get("qualification") is not None else "NONE"
                        )
                        if role == "publish"
                        else "UNKNOWN",
                        "batch_count": self.count(args[0], 1000)
                        if role == "batch" and args
                        else "UNKNOWN",
                    },
                )
            except BaseException:
                self.gap("serialization")
            try:
                result = await original(*args, **kwargs)
            except asyncio.CancelledError:
                self.record(role + "_cancelled")
                raise
            except BaseException as exc:
                self.record(role + "_failed", lambda error=exc: self.failure(error))
                raise
            else:
                self.record(
                    role + "_returned",
                    (lambda: self.publication(result)) if role == "publish" else None,
                )
                return result
            finally:
                scope["live"] = False
                try:
                    self.retire_scope(scope)
                except BaseException:
                    self.gap("restore")
                finally:
                    self.scope.reset(scope_token)
                try:
                    if token is not None:
                        self.active -= 1
                        self.operation.reset(token)
                except BaseException:
                    self.gap("restore")

        self.wrappers.append((owner, name, original, observed))
        setattr(owner, name, observed)

    @staticmethod
    def publication(result):
        if type(result) is not PublicationResult:
            return {"publication_result": "UNKNOWN"}
        return {
            "publication_result": "known",
            "committed": result.committed if type(result.committed) is bool else "UNKNOWN",
            "readback": result.readback if type(result.readback) is bool else "UNKNOWN",
            "revision_present": result.revision is not None,
            "reason": _Investigation.enum(
                result.reason,
                (
                    "indoor_scope_unavailable",
                    "indoor_dependency_scope_mismatch",
                    "demand_scope_mismatch",
                    "publication_conflict",
                    "committed",
                    "readback_failed",
                    "lease_lost",
                ),
            ),
        }

    @staticmethod
    def failure(error):
        reason = "UNKNOWN"
        if type(error) is CASConflict:
            arguments = BaseException.args.__get__(error)
            if len(arguments) == 1 and type(arguments[0]) is str and len(arguments[0]) <= 64:
                reason = {
                    "indoor_scope_unavailable": "inputs_unavailable",
                    "indoor_dependency_scope_mismatch": "dependency_mismatch",
                    "demand_scope_mismatch": "dependency_mismatch",
                    "publication_conflict": "publication_fenced",
                }.get(arguments[0], "UNKNOWN")
        return {"failure_class": reason}

    def target(self, context, statement):
        compiled = context.compiled if context is not None else None
        clause = compiled.statement if compiled is not None else None
        operation = "UNKNOWN"
        table = None
        if isinstance(clause, (Insert, Delete, Update)):
            operation = (
                "insert"
                if isinstance(clause, Insert)
                else ("delete" if isinstance(clause, Delete) else "update")
            )
            table = clause.table
        elif isinstance(clause, Select):
            operation = "select"
        if isinstance(table, TableClause):
            name = "app_logs" if table.name == "app_logs" else "OTHER"
            schema = (
                "public"
                if table.schema == "public"
                else ("UNQUALIFIED" if table.schema is None else "OTHER")
            )
        else:
            name, schema = "UNKNOWN", "UNKNOWN"
        sequence = False
        if clause is not None and not isinstance(clause, ClauseElement):
            self.selected("classification_uncertain")
        # Trusted SQLAlchemy nodes only; fixed width/depth, no compilation or SQL.
        if isinstance(clause, ClauseElement):
            todo = [clause]
            seen = set()
            for _ in range(64):
                if not todo:
                    break
                current = todo.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                if type(current).__module__.startswith("sqlalchemy."):
                    # Expression/alias names are not relation provenance. Follow
                    # their children to real tables or Sequence/next_value nodes.
                    current_name = (
                        current.name if isinstance(current, (TableClause, Sequence)) else None
                    )
                    if type(current_name) in (str, quoted_name) and current_name in (
                        "app_logs",
                        "app_logs_id_seq",
                    ):
                        sequence |= current_name == "app_logs_id_seq"
                        name = current_name
                        current_schema = getattr(current, "schema", None)
                        schema = (
                            "public"
                            if current_schema == "public"
                            else ("UNQUALIFIED" if current_schema is None else "OTHER")
                        )
                    if type(current) is Sequence:
                        continue
                    children = iter(current.get_children())
                    # SQLAlchemy next_value keeps its Sequence outside get_children().
                    if (
                        type(current).__module__ == "sqlalchemy.sql.functions"
                        and type(current).__name__ == "next_value"
                    ):
                        todo.append(current.sequence)
                    for _ in range(16):
                        child = next(children, None)
                        if child is None:
                            break
                        if len(todo) < 64:
                            todo.append(child)
                        else:
                            self.selected("classification_uncertain")
                    else:
                        self.selected("classification_uncertain")
                else:
                    self.selected("classification_uncertain")
            if todo:
                self.selected("classification_uncertain")
        # One bounded lexical pass: comment markers inside literals/identifiers
        # must never hide subsequent SQL. Tokens are transient, never emitted.
        if type(statement) is str:
            if len(statement) > 2048:
                self.selected("classification_uncertain")
            prefix = statement[:2048]
            tokens = []
            index = 0
            while index < len(prefix):
                char = prefix[index]
                if char.isspace():
                    index += 1
                elif prefix.startswith("--", index):
                    index += 2
                    while index < len(prefix) and prefix[index] not in "\r\n":
                        index += 1
                elif prefix.startswith("/*", index):
                    depth = 1
                    index += 2
                    while index < len(prefix) and depth:
                        if prefix.startswith("/*", index):
                            depth += 1
                            index += 2
                        elif prefix.startswith("*/", index):
                            depth -= 1
                            index += 2
                        else:
                            index += 1
                    if depth:
                        self.selected("classification_uncertain")
                elif char in ("'", '"'):
                    quote = char
                    # E strings have escapes; ordinary PostgreSQL strings do not.
                    escaped = (
                        quote == "'"
                        and index > 0
                        and prefix[index - 1] in "eE"
                        and (
                            index < 2
                            or not (prefix[index - 2].isalnum() or prefix[index - 2] in "_$")
                        )
                    )
                    if escaped and tokens and tokens[-1] == ("word", "e"):
                        tokens.pop()
                    value = []
                    index += 1
                    closed = False
                    while index < len(prefix):
                        char = prefix[index]
                        index += 1
                        if char == quote:
                            if index < len(prefix) and prefix[index] == quote:
                                value.append(quote)
                                index += 1
                            else:
                                closed = True
                                break
                        elif escaped and char == "\\":
                            # Correctly skip escaped delimiters without claiming
                            # to decode PostgreSQL octal/Unicode/regclass values.
                            self.selected("classification_uncertain")
                            if index < len(prefix):
                                index += 1
                            value.append("?")
                        else:
                            value.append(char)
                    if not closed:
                        self.selected("classification_uncertain")
                        break
                    if len(tokens) >= 2 and tokens[-2:] == [("word", "u"), ("symbol", "&")]:
                        self.selected("classification_uncertain")
                        value = ["?"]
                    tokens.append(("string" if quote == "'" else "identifier", "".join(value)))
                elif char == "$":
                    delimiter = re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", prefix[index:])
                    if delimiter is None:
                        parameter = re.match(r"\$[0-9]+", prefix[index:])
                        if parameter is None:
                            self.selected("classification_uncertain")
                            break
                        tokens.append(("parameter", "?"))
                        index += len(parameter[0])
                    else:
                        start = index + len(delimiter[0])
                        end = prefix.find(delimiter[0], start)
                        if end < 0:
                            self.selected("classification_uncertain")
                            break
                        tokens.append(("string", prefix[start:end]))
                        index = end + len(delimiter[0])
                elif char.isascii() and (char.isalpha() or char == "_"):
                    end = index + 1
                    while (
                        end < len(prefix)
                        and prefix[end].isascii()
                        and (prefix[end].isalnum() or prefix[end] in "_$")
                    ):
                        end += 1
                    if end == len(prefix) and len(statement) > len(prefix):
                        self.selected("classification_uncertain")
                        break
                    tokens.append(("word", prefix[index:end].lower()))
                    index = end
                else:
                    if not char.isascii() or char in ("\\", "`", "[", "]"):
                        self.selected("classification_uncertain")
                        break
                    tokens.append(("symbol", char))
                    index += 1

            for position, (kind, value) in enumerate(tokens):
                if kind == "string":
                    # Only explicit sequence-function arguments / regclass casts,
                    # never arbitrary literals or result-column aliases.
                    call = tokens[max(0, position - 2) : position]
                    cast = tokens[position + 1 : position + 4]
                    if (
                        len(call) == 2
                        and call[0][0] in ("word", "identifier")
                        and call[0][1] in ("nextval", "currval", "setval")
                        and call[1] == ("symbol", "(")
                    ) or cast == [("symbol", ":"), ("symbol", ":"), ("word", "regclass")]:
                        sequence |= bool(
                            re.fullmatch(
                                r'(?:(?:public|"public")\.)?(?:app_logs_id_seq|"app_logs_id_seq")',
                                value,
                            )
                        )
                if kind != "word":
                    continue
                offset = 1
                if value == "insert" and tokens[position + 1 : position + 2] == [("word", "into")]:
                    offset = 2
                elif value not in ("from", "join", "update", "sequence"):
                    continue
                target = tokens[position + offset : position + offset + 3]
                target_schema = "UNQUALIFIED"
                if len(target) == 3 and target[1] == ("symbol", "."):
                    target_schema = "public" if target[0][1] == "public" else "OTHER"
                    target = target[2:]
                if not target or target[0][0] not in ("word", "identifier"):
                    continue
                found = target[0][1]
                if found not in ("app_logs", "app_logs_id_seq"):
                    continue
                sequence |= found == "app_logs_id_seq"
                # Structural positives still take precedence over text.
                if name not in ("app_logs", "app_logs_id_seq"):
                    name, schema = found, target_schema
                if operation == "UNKNOWN":
                    operation = (
                        value
                        if value in ("insert", "update")
                        else "delete"
                        if tokens[max(0, position - 1) : position] == [("word", "delete")]
                        else "select"
                        if value in ("from", "join")
                        else "alter"
                        if tokens[max(0, position - 1) : position] == [("word", "alter")]
                        else "UNKNOWN"
                    )
        return {
            "sql_kind": operation,
            "declared_relation": name,
            "declared_schema": schema,
            # No connection-local dependency/search-path evidence is collected by SQL.
            "resolved_relation": "UNKNOWN",
            "sequence_consumer": "UNKNOWN",
            "target_sequence": sequence,
        }

    def sql(self, label, connection, context=None, statement=None):
        try:
            if self.closed:
                return
            if threading.get_ident() != self.thread:
                self.gap("thread")
                return
            scope = self.context()
            origin = self.scope.get()
            association = self.connection_relevance(connection)
            terminal = label in ("commit_notified", "rollback_notified")
            try:
                if terminal:
                    if scope is None and association is None:
                        self.selected("relevance_unassociated")
                        return
                    result = self.target(None, None)
                    category = association["category"] if association is not None else "logging"
                else:
                    result = self.target(context, statement)
                    category = "sequence" if result["target_sequence"] else "logging"
                    if not result["target_sequence"] and result["declared_relation"] != "app_logs":
                        if scope is None or scope["kind"] != "batch":
                            self.selected("routine_filtered")
                            return
                    # Admission is independent of event retention and alias availability.
                    association = self.admit(connection, origin, category)

                def detail():
                    result.update(
                        {
                            "engine": self.alias("engine", connection.engine)
                            if connection is not None
                            else 0,
                            "connection": self.alias("connection", connection),
                            "backend": "UNKNOWN",
                            "factory": "UNKNOWN",
                            "stack": self.stack(),
                            "association": "ambiguous"
                            if association is not None and association["ambiguous"]
                            else "UNKNOWN",
                        }
                    )
                    return result

                self.record(
                    label,
                    detail,
                    category=category,
                    owner=origin["owner"]
                    if origin is not None
                    else (association["owner"] if association is not None else self.node),
                    association=association,
                )
            finally:
                if terminal and connection is not None:
                    self.relevance.pop(id(connection), None)
        except BaseException:
            self.gap("serialization")

    def connection_relevance(self, connection):
        entry = self.relevance.get(id(connection))
        return entry if entry is not None and entry["ref"]() is connection else None

    def admit(self, connection, scope, category):
        if connection is None:
            return None
        entry = self.connection_relevance(connection)
        owner = scope["owner"] if scope is not None else self.node
        if entry is not None:
            if entry["scope"] is not scope or entry["owner"] != owner:
                if not entry["ambiguous"]:
                    self.selected("relevance_ambiguous")
                entry["ambiguous"] = True
            return entry
        # Bounded dead-reference sweep; never evict a live owner at a node boundary.
        for key, value in tuple(self.relevance.items()):
            if value["ref"]() is None:
                self.relevance.pop(key, None)
        if len(self.relevance) >= 256:
            self.selected("relevance_overflow")
            return None
        try:
            reference = weakref.ref(connection)
        except TypeError:
            self.selected("relevance_unassociated")
            return None
        entry = {
            "ref": reference,
            "owner": owner,
            "entry": scope["entry"] if scope else 0,
            "kind": scope["kind"] if scope else "UNKNOWN",
            "role": scope["role"] if scope else "UNKNOWN",
            "scope": scope,
            "category": category,
            "ambiguous": False,
        }
        self.relevance[id(connection)] = entry
        return entry

    def retire_scope(self, scope):
        for key, entry in tuple(self.relevance.items()):
            if entry["scope"] is scope:
                self.selected("retired_without_terminal")
                self.relevance.pop(key, None)

    def install_events(self):
        def before(connection, cursor, statement, parameters, context, executemany):
            self.sql("sql_attempted", connection, context, statement)

        def after(connection, cursor, statement, parameters, context, executemany):
            self.sql("cursor_completed", connection, context, statement)

        def failed(context):
            try:
                self.sql(
                    "sql_failed", context.connection, context.execution_context, context.statement
                )
            except BaseException:
                self.gap("serialization")

        def commit(connection):
            self.sql("commit_notified", connection)

        def rollback(connection):
            self.sql("rollback_notified", connection)

        for name, callback in (
            ("before_cursor_execute", before),
            ("after_cursor_execute", after),
            ("handle_error", failed),
            ("commit", commit),
            ("rollback", rollback),
        ):
            # Include attempted registrations in cleanup even if installation raises.
            self.listeners.append((name, callback))
            try:
                event.listen(Engine, name, callback)
            except BaseException:
                self.gap("install")

    def pending(self):
        from packages.core import log_sink

        # No locks, task settlement, clock reads or application status callbacks.
        # Status uses the separate header/footer reserve: an exhausted event arena
        # must not discard the final pending-work boundary.
        self.pending_bytes = json.dumps(
            {
                "active_operations": self.active,
                "buffered_rows": len(log_sink._LOG_BUFFER),
                "inflight_rows": len(log_sink._INFLIGHT_BATCH),
                "flusher_present": log_sink._FLUSH_TASK is not None,
                "writer_present": log_sink._INFLIGHT_TASK is not None,
                "thread_boundary": "UNKNOWN",
                "process_boundary": "OPEN",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        self.record("observation_boundary", lambda: json.loads(self.pending_bytes))

    def emit(self, capture):
        """Synchronous capture-independent emission, with one bounded fallback."""
        global_capture = fixture_capture = None
        global_started = fixture_started = False
        fallback_attempted = False

        def fallback():
            nonlocal fallback_attempted
            if fallback_attempted:
                return
            fallback_attempted = True
            try:
                sys.stdout.write("LIFECYCLE_DIAGNOSTIC_INCOMPLETE emission\n")
                sys.stdout.flush()
            except BaseException:
                pass

        try:
            # CaptureManager's own context resumes global before fixture capture;
            # a failed global resume can otherwise prevent the fixture attempt.
            global_capture = capture._global_capturing
            fixture_capture = capture._capture_fixture
            global_started = bool(global_capture and global_capture.is_started())
            fixture_started = bool(fixture_capture and fixture_capture._is_started())
            with capture.global_and_fixture_disabled():
                try:
                    digest = hashlib.sha256()
                    for record in self.events:
                        digest.update(record)
                    stream = sys.stderr
                    header = {
                        "investigation": "lifecycle-v1",
                        "kind": "header",
                        "source": self.source,
                        "interpreter": list(sys.version_info[:3]),
                        "alias_zero": "UNKNOWN",
                        "return_observer": "installed"
                        if self.profile is not None
                        else "UNAVAILABLE",
                    }
                    footer = {
                        "investigation": "lifecycle-v1",
                        "kind": "footer",
                        "count": len(self.events),
                        "bytes": self.bytes,
                        "sha256": digest.hexdigest(),
                        "drops": self.drops,
                        "coverage_complete": False,
                        "coverage": [
                            "backend_UNKNOWN",
                            "factory_UNKNOWN",
                            "resolution_UNKNOWN",
                            "sequence_UNKNOWN",
                            "task_origin_UNKNOWN",
                            "thread_UNOBSERVED",
                            "process_OPEN",
                            "preexisting_connections_UNKNOWN",
                            "source_present" if len(self.source) == 2 else "source_UNKNOWN",
                        ],
                        "pending_operations": self.active,
                        "pending": json.loads(self.pending_bytes),
                        "selection": {
                            "policy": "origin-lanes-v2",
                            "limits": self.LANES,
                            "session": [16, 16384],
                            "credits": 3688,
                            "arena": self.ARENA_BYTES,
                            "counters": self.selection,
                            "totals": [
                                [
                                    sum(lanes[index][0] for lanes in self.lanes[1:]),
                                    sum(lanes[index][1] for lanes in self.lanes[1:]),
                                ]
                                for index in range(len(self.LANES))
                            ],
                            # Fixed-size sparse loss matrix; no operation histories.
                            "losses": [
                                [node, index, lane[2]]
                                for node, lanes in enumerate(self.lanes)
                                for index, lane in enumerate(lanes)
                                if lane[2]
                            ],
                        },
                    }
                    header_text = json.dumps(header, sort_keys=True) + "\n"
                    footer_text = json.dumps(footer, sort_keys=True) + "\n"
                    if len((header_text + footer_text).encode("ascii")) > self.RESERVE_BYTES:
                        raise ValueError("diagnostic reserve exceeded")
                    stream.write(header_text)
                    for record in self.events:
                        stream.write(record.decode("ascii"))
                    stream.write(footer_text)
                    stream.flush()
                except BaseException:
                    self.gap("emission")
                    # Still inside disabled global AND fixture capture.
                    fallback()
        except BaseException:
            self.gap("emission")
            for started, saved, attribute, check, resume in (
                (
                    global_started,
                    global_capture,
                    "_global_capturing",
                    "is_started",
                    "resume_global_capture",
                ),
                (
                    fixture_started,
                    fixture_capture,
                    "_capture_fixture",
                    "_is_started",
                    "resume_fixture",
                ),
            ):
                if started:
                    try:
                        if getattr(capture, attribute) is not saved:
                            self.gap("ownership")
                        elif not getattr(saved, check)():
                            getattr(capture, resume)()
                    except BaseException:
                        self.gap("restore")
            fallback()

    def restore(self):
        self.relevance.clear()
        # Each removal/restoration is independent, including partial installation.
        for name, callback in self.listeners:
            try:
                event.remove(Engine, name, callback)
            except BaseException:
                self.gap("restore")
        for owner, name, original, observed in self.wrappers:
            try:
                if getattr(owner, name) is observed:
                    setattr(owner, name, original)
                else:
                    self.gap("ownership")
            except BaseException:
                self.gap("restore")
        if self.profile is not None:
            try:
                if sys.getprofile() is self.profile:
                    sys.setprofile(None)
                else:
                    self.gap("ownership")
            except BaseException:
                self.gap("restore")


_INVESTIGATION = None


def _investigation_stage(stage, source=None):
    observer = _INVESTIGATION
    if observer is None:
        return
    try:
        observer.stage = observer.enum(stage, observer.STAGES)
        if type(source) is dict:
            for name, size in (("commit", 40), ("source_manifest_sha256", 64)):
                value = source.get(name)
                if (
                    type(value) is str
                    and len(value) == size
                    and all(character in "0123456789abcdef" for character in value)
                ):
                    observer.source[name] = value
        observer.record("stage")
    except BaseException:
        observer.gap("serialization")


@pytest.fixture(scope="session", autouse=True)
def _lifecycle_investigation(request):
    global _INVESTIGATION
    observer = _Investigation()
    _INVESTIGATION = observer

    def finish():
        global _INVESTIGATION
        try:
            observer.pending()
        except BaseException:
            observer.gap("serialization")
        try:
            observer.restore()
        finally:
            if _INVESTIGATION is observer:
                _INVESTIGATION = None
            else:
                observer.gap("ownership")
            observer.closed = True
            observer.emit(request.config.pluginmanager.getplugin("capturemanager"))
        if any(
            observer.drops[reason]
            for reason in ("install", "restore", "ownership", "serialization", "emission")
        ):
            raise RuntimeError("lifecycle investigation infrastructure incomplete")

    # Installed before any observer mutation, including partial installation failures.
    request.addfinalizer(finish)
    from packages.core import log_sink
    from packages.ml import forecast_quality

    for install in (
        observer.install_events,
        lambda: observer.wrap(Lifecycle, "publish", "publish"),
        lambda: observer.wrap(log_sink, "_write_batch", "batch"),
        lambda: observer.install_profile(
            (
                ("materialize", forecast_quality.materialize_native_inputs),
                ("prepare", forecast_quality._prepare_native_inputs),
            )
        ),
    ):
        try:
            install()
        except BaseException:
            observer.gap("install")
    return observer


@pytest.fixture(autouse=True)
def _lifecycle_investigation_node(_lifecycle_investigation):
    observer = _lifecycle_investigation
    observer.node += 1
    _investigation_stage("node")
    yield
    _investigation_stage("node_end")


def _migration():
    path = Path(__file__).parents[2] / "migrations/versions/033_ml_observation_lifecycle.py"
    spec = importlib.util.spec_from_file_location("migration_033_pg", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_migration(connection, direction):
    with Operations.context(MigrationContext.configure(connection)):
        getattr(_migration(), direction)()


SUPPORTING_MODELS = (
    ConsumptionRecord,
    IndoorTempReading,
    PriceRecord,
    WeatherRecord,
    SettingRecord,
    HouseInstallationRecord,
    HouseContractRevisionRecord,
    PlanRecord,
    PlanActionRecord,
    AuditLogRecord,
    OverrideRecord,
)
LIFECYCLE_MODELS = (
    MLLifecycleStateRecord,
    MLArtifactRecord,
    MLMaintenanceRequestRecord,
    MLForecastIssueRecord,
    MLForecastOutcomeRecord,
)
FIXTURE_MODELS = (DeviceStatusRecord, *SUPPORTING_MODELS, *LIFECYCLE_MODELS)


def _fixture_metadata(schema):
    """Private metadata: circular house/plan FKs must never bind to public."""
    metadata = MetaData(schema=schema)
    for model in SUPPORTING_MODELS:
        model.__table__.to_metadata(metadata, schema=schema)
    return metadata


def _pre_lifecycle_device_table(schema):
    return Table(
        "device_status",
        MetaData(schema=schema),
        *(
            Column(
                column.name,
                column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
                server_default=column.server_default.arg
                if column.server_default is not None
                else None,
            )
            for column in DeviceStatusRecord.__table__.columns
            if column.name != "native_policy_provenance"
        ),
    )


def _require_local_table(connection, table):
    """Later fixture helpers may require storage, never create via public search_path."""
    schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    local, resolved = connection.execute(
        text("SELECT to_regclass(:local)::oid, to_regclass(:name)::oid"),
        {"local": f"{schema}.{table.name}", "name": table.name},
    ).one()
    assert local is not None and local == resolved, f"nonlocal fixture table: {table.name}"


async def _assert_fixture_storage(connection, schema):
    """Prove OIDs, columns, every FK, serial ownership and token defaults before writes."""
    oids = {}
    for model in FIXTURE_MODELS:
        name = model.__tablename__
        local, resolved, kind = (
            await connection.execute(
                text(
                    "SELECT c.oid, to_regclass(:name)::oid, c.relkind::text "
                    "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=:schema AND c.relname=:name"
                ),
                {"schema": schema, "name": name},
            )
        ).one()
        assert local == resolved and kind == "r", f"nonlocal fixture relation: {name}"
        oids[name] = local
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT attname FROM pg_attribute "
                        "WHERE attrelid=:oid AND attnum>0 AND NOT attisdropped"
                    ),
                    {"oid": local},
                )
            )
            .scalars()
            .all()
        )
        assert set(columns) == set(model.__table__.columns.keys()), name
    for model in FIXTURE_MODELS:
        expected = sorted(
            (
                tuple(element.parent.name for element in fk.elements),
                oids[fk.referred_table.name],
                tuple(element.column.name for element in fk.elements),
            )
            for fk in model.__table__.foreign_key_constraints
        )
        actual = (
            await connection.execute(
                text(
                    "SELECT ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY "
                    "k(num,ord) JOIN pg_attribute a ON a.attrelid=c.conrelid "
                    "AND a.attnum=k.num ORDER BY k.ord), c.confrelid, "
                    "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY "
                    "k(num,ord) JOIN pg_attribute a ON a.attrelid=c.confrelid "
                    "AND a.attnum=k.num ORDER BY k.ord) "
                    "FROM pg_constraint c WHERE c.contype='f' AND c.conrelid=:oid"
                ),
                {"oid": oids[model.__tablename__]},
            )
        ).all()
        assert sorted((tuple(a), b, tuple(c)) for a, b, c in actual) == expected, (
            f"nonlocal/missing fixture FK: {model.__tablename__}"
        )
        for column in model.__table__.columns:
            # PostgreSQL SERIAL is generated for these autoincrement PKs.
            if (
                column is not model.__table__.autoincrement_column
                or column.server_default is not None
            ):
                continue
            sequence = (
                await connection.execute(
                    text("SELECT pg_get_serial_sequence(:table, :column)::regclass::oid"),
                    {"table": f"{schema}.{model.__tablename__}", "column": column.name},
                )
            ).scalar_one()
            assert sequence is not None, f"missing owned sequence: {model.__tablename__}"
            await _assert_local_sequence(
                connection, schema, sequence, oids[model.__tablename__], column.name
            )
    token = (
        await connection.execute(
            text("SELECT to_regclass(:name)::oid"),
            {"name": f"{schema}.ml_lifecycle_token_seq"},
        )
    ).scalar_one()
    assert token is not None
    for column in ("revision", "fence"):
        await _assert_local_sequence(
            connection, schema, token, oids["ml_lifecycle_state"], column, owned=False
        )


async def _assert_local_sequence(connection, schema, sequence, table, column, *, owned=True):
    namespace, kind, resolved, default_bound, ownership_bound = (
        await connection.execute(
            text(
                "SELECT n.nspname, c.relkind::text, to_regclass(c.relname)::oid, "
                "EXISTS(SELECT 1 FROM pg_attrdef ad "
                "JOIN pg_attribute a ON a.attrelid=ad.adrelid AND a.attnum=ad.adnum "
                "JOIN pg_depend d ON d.classid='pg_attrdef'::regclass AND d.objid=ad.oid "
                "WHERE ad.adrelid=:table AND a.attname=:column "
                "AND d.refclassid='pg_class'::regclass AND d.refobjid=c.oid), "
                "EXISTS(SELECT 1 FROM pg_depend d JOIN pg_attribute a "
                "ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid "
                "WHERE d.classid='pg_class'::regclass AND d.objid=c.oid "
                "AND d.refclassid='pg_class'::regclass AND d.refobjid=:table "
                "AND a.attname=:column AND d.deptype IN ('a','i')) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=:seq"
            ),
            {"seq": sequence, "table": table, "column": column},
        )
    ).one()
    assert namespace == schema and kind == "S" and resolved == sequence and default_bound, (
        "nonlocal sequence default"
    )
    assert ownership_bound is owned, "incorrect sequence ownership"


def _diagnostic_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _public_fixture_components(state, content, bindings):
    """Allowlisted observations only. Never serialize the raw acceptance state."""
    result = {}
    for name, value in state.items():
        qualified = "public." + name.removeprefix("sequence:")
        if value is None:
            continue
        if name.startswith("sequence:"):
            oid, configuration, position, dependencies = value
            result[qualified] = {
                "kind": "sequence",
                "oid": oid,
                "configuration": _diagnostic_hash(configuration),
                "value_state": _diagnostic_hash(position),
                "dependencies": dependencies,
            }
        else:
            oid, identity, rows, catalog, constraints, defaults = value
            result[qualified] = {
                "kind": "relation",
                "oid": oid,
                "count": rows[0],
                "identity": _diagnostic_hash(identity),
                "content": content[name],
                "versions": rows[1],
                "catalog": catalog,
                "constraints": constraints,
                "defaults": defaults,
                "bindings": bindings[name],
            }
    return result


def _public_fixture_delta(before, after):
    changes = []
    for name in sorted(before.keys() | after.keys()):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        changes.append(
            {
                "object": name,
                "change": "added" if old is None else "removed" if new is None else "changed",
                "before_hash": _diagnostic_hash(old),
                "after_hash": _diagnostic_hash(new),
                "components": {
                    key: {"before": (old or {}).get(key), "after": (new or {}).get(key)}
                    for key in sorted((old or {}).keys() | (new or {}).keys())
                    if (old or {}).get(key) != (new or {}).get(key)
                },
            }
        )
    return changes


def _emit_public_diagnostic(phase, source, before, after, unchanged):
    print(
        json.dumps(
            {
                "probe": "public-fixture-drift",
                "phase": phase,
                "source": source,
                "unchanged": unchanged,
                "writer": "UNKNOWN",
                "deltas": _public_fixture_delta(before, after),
            },
            sort_keys=True,
        )
    )


async def _public_fixture_state(connection, *, diagnostics=None, schema="public"):
    """Only identities/digests leave PostgreSQL; never log settings or payloads."""
    state = {}
    content, bindings = {}, {}
    quoted_schema = connection.dialect.identifier_preparer.quote_identifier(schema)
    for model in FIXTURE_MODELS:
        name = model.__tablename__
        oid = (
            await connection.execute(
                text("SELECT to_regclass(:name)::oid"), {"name": f"{schema}.{name}"}
            )
        ).scalar_one()
        if oid is None:
            state[name] = None
            continue
        # Include row versions as well as content: even a reverted write is drift.
        rows = (
            await connection.execute(
                text(
                    "SELECT count(*), md5(coalesce(string_agg(digest, '' ORDER BY digest),'')), "
                    "md5(coalesce(string_agg(content, '' ORDER BY content),'')) "
                    f"FROM (SELECT md5(row_to_json(r)::text || r.xmin::text || r.ctid::text) "
                    f"AS digest, md5(row_to_json(r)::text) AS content "
                    f'FROM {quoted_schema}."{name}" r) versions'
                )
            )
        ).one()
        catalog = (
            await connection.execute(
                text(
                    "SELECT md5(coalesce(string_agg(row_to_json(a)::text, '' "
                    "ORDER BY a.attnum),'')) FROM pg_attribute a WHERE a.attrelid=:oid"
                ),
                {"oid": oid},
            )
        ).scalar_one()
        constraints = (
            await connection.execute(
                text(
                    "SELECT md5(coalesce(string_agg(row_to_json(c)::text, '' "
                    "ORDER BY c.oid),'')) FROM pg_constraint c WHERE c.conrelid=:oid"
                ),
                {"oid": oid},
            )
        ).scalar_one()
        defaults = (
            await connection.execute(
                text(
                    "SELECT md5(coalesce(string_agg(row_to_json(d)::text, '' "
                    "ORDER BY d.adnum),'')) FROM pg_attrdef d WHERE d.adrelid=:oid"
                ),
                {"oid": oid},
            )
        ).scalar_one()
        identity = (
            await connection.execute(
                text(
                    "SELECT relnamespace,relkind::text,relowner,relfilenode "
                    "FROM pg_class WHERE oid=:oid"
                ),
                {"oid": oid},
            )
        ).one()
        state[name] = (oid, tuple(identity), tuple(rows[:2]), catalog, constraints, defaults)
        content[name] = rows[2]
        # Structural identities only: no expressions, row JSON or SQL parameters.
        references = (
            await connection.execute(
                text(
                    "SELECT DISTINCT n.nspname,c.relname,c.oid FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid IN ("
                    "SELECT confrelid FROM pg_constraint WHERE conrelid=:oid AND contype='f' "
                    "UNION SELECT d.refobjid FROM pg_depend d JOIN pg_attrdef a "
                    "ON d.classid='pg_attrdef'::regclass AND d.objid=a.oid "
                    "WHERE a.adrelid=:oid AND d.refclassid='pg_class'::regclass "
                    "UNION SELECT d.objid FROM pg_depend d "
                    "WHERE d.refclassid='pg_class'::regclass AND d.refobjid=:oid "
                    "AND d.classid='pg_class'::regclass AND d.deptype IN ('a','i')) "
                    "ORDER BY n.nspname,c.relname,c.oid"
                ),
                {"oid": oid},
            )
        ).all()
        resolved = (
            await connection.execute(text("SELECT to_regclass(:name)::oid"), {"name": name})
        ).scalar_one()
        bindings[name] = {
            "qualified": f"{schema}.{name}",
            "local_oid": oid,
            "resolved_oid": resolved,
            "references": [
                {"qualified": f"{namespace}.{relation}", "oid": reference}
                for namespace, relation, reference in references
            ],
        }
    sequences = (
        await connection.execute(
            text(
                "SELECT c.oid, c.relname, row_to_json(s)::text FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_sequence s "
                "ON s.seqrelid=c.oid WHERE n.nspname=:schema ORDER BY c.relname"
            ),
            {"schema": schema},
        )
    ).all()
    for oid, name, configuration in sequences:
        # Identifier quoting from the dialect, not values from settings.
        quoted = connection.dialect.identifier_preparer.quote_identifier(name)
        value = (
            await connection.execute(
                text(f"SELECT last_value,is_called FROM {quoted_schema}.{quoted}")
            )
        ).one()
        dependencies = (
            await connection.execute(
                text(
                    "SELECT md5(coalesce(string_agg(row_to_json(d)::text, '' "
                    "ORDER BY d.classid,d.objid,d.objsubid,d.refclassid,d.refobjid,"
                    "d.refobjsubid,d.deptype),'')) FROM pg_depend d "
                    "WHERE (d.refclassid='pg_class'::regclass AND d.refobjid=:oid) "
                    "OR (d.classid='pg_class'::regclass AND d.objid=:oid)"
                ),
                {"oid": oid},
            )
        ).scalar_one()
        state["sequence:" + name] = (oid, configuration, tuple(value), dependencies)
    if diagnostics is not None:
        components = _public_fixture_components(state, content, bindings)
        diagnostics.update(
            {key.replace("public.", schema + ".", 1): value for key, value in components.items()}
        )
    return state


def _source_receipt(receipt, environment=None):
    """Bind ownership to the one canonical prepared manifest; never a second producer."""
    environment = os.environ if environment is None else environment
    proof = current_verification_source_proof(environment)
    # Legacy aliases may only restate the canonical proof, never substitute for it.
    alias = environment.get("HPO_OBS5_TESTED_REVISION")
    assert alias is None or alias == proof["digest"], "stale tested source fingerprint"
    receipt_alias = environment.get("HPO_OBS5_SOURCE_RECEIPT")
    assert receipt_alias is None or Path(receipt_alias).resolve(strict=True) == Path(
        proof["path"]
    ).resolve(strict=True), "foreign tested source receipt"
    assert (receipt["commit"], receipt["source_manifest_sha256"]) == (
        proof["commit"],
        proof["digest"],
    ), "ownership/source mismatch"
    return {"commit": proof["commit"], "source_manifest_sha256": proof["digest"]}


async def _cleanup_lifecycle_storage(
    *,
    engine,
    control,
    schema,
    created,
    retained,
    public_baseline,
    diagnostic_baseline,
    source,
    emit_receipt,
):
    """Every cleanup obligation is independent, including when diagnostics fail."""
    state = {
        "schema_removed": False,
        "public_manual_storage_unchanged": False,
        "public_fixture_storage_unchanged": False,
    }
    observations = {}

    async def compare(phase):
        diagnostics = {}
        async with control.begin() as connection:
            await guard_owned_database(connection)
            manual = await retained_storage_state(connection) == retained
            unchanged = (
                await _public_fixture_state(connection, diagnostics=diagnostics) == public_baseline
            )
        # Record acceptance before attempting potentially failing serialization.
        observations[phase] = (manual, unchanged)
        state["public_manual_storage_unchanged"] = (
            all(value[0] for value in observations.values()) and len(observations) == 2
        )
        state["public_fixture_storage_unchanged"] = (
            all(value[1] for value in observations.values()) and len(observations) == 2
        )
        _emit_public_diagnostic(phase, source, diagnostic_baseline, diagnostics, unchanged)

    async def drop():
        async with control.begin() as connection:
            await guard_owned_database(connection)
            await connection.execute(DropSchema(schema, cascade=True))

    async def verify_absence():
        async with control.begin() as connection:
            await guard_owned_database(connection)
            state["schema_removed"] = (
                await connection.execute(
                    text("SELECT to_regnamespace(:schema)"), {"schema": schema}
                )
            ).scalar_one() is None

    async def receipt():
        emit_receipt(state)

    async def check():
        if created:
            public_unchanged = state["public_manual_storage_unchanged"]
            fixture_public_unchanged = state["public_fixture_storage_unchanged"]
            removed = state["schema_removed"]
            assert public_unchanged, "public manual storage drift"
            assert fixture_public_unchanged, "public fixture storage/sequence drift"
            assert removed, "owned fixture schema remains"

    stages = []
    if created:
        stages.extend(
            [
                ("pre-drop", lambda: compare("pre-drop")),
                ("drop", drop),
                ("absence", verify_absence),
                ("post-drop", lambda: compare("post-drop")),
            ]
        )
    stages.extend(
        [
            ("engine-dispose", engine.dispose),
            ("control-dispose", control.dispose),
            ("receipt", receipt),
            ("acceptance", check),
        ]
    )
    failures, failed_stages = [], []
    for phase, action in stages:
        _investigation_stage(phase, source)
        try:
            await action()
        except BaseException as exc:
            failures.append(exc)
            failed_stages.append(phase)
            try:
                print(
                    json.dumps(
                        {
                            "probe": "public-fixture-drift-error",
                            "phase": phase,
                            "source": source,
                            "exception": type(exc).__name__,
                            "writer": "UNKNOWN",
                        }
                    )
                )
            except BaseException as diagnostic_error:
                failures.append(diagnostic_error)
    if failures:
        raise BaseExceptionGroup("fixture cleanup failed: " + ", ".join(failed_stages), failures)


@pytest_asyncio.fixture
async def lifecycle_database(request, tmp_path, monkeypatch):
    assert request.config.option.noconftest, "coordinator-owned suite requires --noconftest"
    assert os.name == "posix", "owned harness requires Linux Unix-socket/process semantics"
    receipt = owned_test_environment()
    source = _source_receipt(receipt)
    _investigation_stage("baseline", source)
    root = Path(receipt["temp_root"])
    assert tmp_path.resolve().is_relative_to(root), "pytest basetemp must be coordinator-owned"
    model_dir = tmp_path / "artifact-fixtures"
    model_dir.mkdir()
    monkeypatch.setattr(settings, "model_dir", str(model_dir))
    schema = "obs5_" + uuid.uuid4().hex
    control = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    engine = create_async_engine(
        os.environ["DATABASE_URL"],
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema + ", public"}},
    )
    started = dt.datetime.now(dt.timezone.utc)
    created = False
    ownership_checked = False
    public_baseline = None
    diagnostic_baseline = {}
    retained = None
    with owned_test_network():
        try:
            async with control.begin() as connection:
                await guard_owned_database(connection)
                head, _ = migration_targets()
                assert head == CURRENT_HEAD_REVISION
                assert (
                    await connection.execute(text("SELECT version_num FROM public.alembic_version"))
                ).scalar_one() == head
                retained = await retained_storage_state(connection)
                public_baseline = await _public_fixture_state(
                    connection, diagnostics=diagnostic_baseline
                )
            async with control.begin() as connection:
                await guard_owned_database(connection)
                immediate = {}
                stable = (
                    await _public_fixture_state(connection, diagnostics=immediate)
                    == public_baseline
                )
            _emit_public_diagnostic(
                "baseline-repeat", source, diagnostic_baseline, immediate, stable
            )
            assert stable, "public fixture baseline unstable"
            async with control.begin() as connection:
                await guard_owned_database(connection)
                await connection.execute(CreateSchema(schema))
            created = True
            async with engine.begin() as connection:
                await connection.run_sync(
                    lambda current: _pre_lifecycle_device_table(schema).create(current)
                )
                await connection.run_sync(lambda current: _run_migration(current, "upgrade"))
                await connection.run_sync(
                    lambda current: _fixture_metadata(schema).create_all(current, checkfirst=False)
                )
                await _assert_fixture_storage(connection, schema)
                ownership_checked = True
            maker = async_sessionmaker(engine, expire_on_commit=False)
            from packages.core import database as database_module

            monkeypatch.setattr(database_module, "async_session_factory", maker)

            @asynccontextmanager
            async def sessions():
                async with maker() as session:
                    try:
                        yield session
                        await session.commit()
                    except BaseException:
                        await session.rollback()
                        raise

            _investigation_stage("call", source)
            yield SimpleNamespace(
                lifecycle=Lifecycle(sessions),
                sessions=sessions,
                maker=maker,
                engine=engine,
                model_dir=model_dir,
                schema=schema,
            )
        finally:

            def emit_receipt(state):
                write_lifecycle_receipt(
                    root / "evidence",
                    {
                        "test": request.node.nodeid,
                        **source,
                        "ownership_token": receipt["token"],
                        "schema": schema,
                        "started_at": started.isoformat(),
                        "ended_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "schema_owned": created,
                        "fixture_storage_ownership_checked": ownership_checked,
                        **state,
                    },
                )

            await _cleanup_lifecycle_storage(
                engine=engine,
                control=control,
                schema=schema,
                created=created,
                retained=retained,
                public_baseline=public_baseline,
                diagnostic_baseline=diagnostic_baseline,
                source=source,
                emit_receipt=emit_receipt,
            )


async def _readiness_source_fixture(database, monkeypatch):
    from packages.core import database as database_module, settings_service
    from packages.core.models import (
        ConsumptionRecord,
        IndoorTempReading,
        PlanActionRecord,
        PlanRecord,
        PriceRecord,
        SettingRecord,
        WeatherRecord,
    )
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute

    async with database.engine.begin() as connection:
        for model in (
            SettingRecord,
            ConsumptionRecord,
            IndoorTempReading,
            PlanRecord,
            PlanActionRecord,
            PriceRecord,
            WeatherRecord,
        ):
            await connection.run_sync(
                lambda current, table=model.__table__: _require_local_table(current, table)
            )
    monkeypatch.setattr(database_module, "async_session_factory", database.maker)
    values = {
        key: spec.serialize_default()
        for key, spec in settings_service.SETTING_SPECS.items()
        if not settings_service.SETTINGS_SCHEMA[key].get("read_only")
    }
    values.update(
        {
            "timezone": "UTC",
            "comfort_temp_min": "18",
            "comfort_temp_target": "20.5",
            "comfort_temp_max": "25",
            "comfort_reference_sensor_id": "selected-room",
            "smartthings_device_ids": "selected-room",
            "weather_provider": "open-meteo",
            "seasonal_calibration_enabled": "true",
            "seasonal_calibration_max_outdoor_c": "12",
            "seasonal_calibration_window_days": "7",
            "aquarea_username": "public-readiness-test",
            "aquarea_password": "public-readiness-test",
        }
    )
    await settings_service.set_settings_bulk(values)
    async with database.sessions() as session:
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    records, readings, weather = TestR06ThermalQualificationRoute.cohort()
    shift = now - TestR06ThermalQualificationRoute.NOW
    for row in records:
        row.ts += shift
    for row in readings:
        row.timestamp += shift
    for row in weather:
        row.ts += shift
        row.forecast_issued_at += shift
    start = records[0].ts
    async with database.sessions() as session:
        session.add_all([*records, *readings, *weather])
        session.add_all(
            [
                IndoorTempReading(
                    timestamp=row.timestamp,
                    device_id="second-room",
                    temperature=23.0,
                    is_stale=False,
                )
                for row in readings
            ]
        )
        session.add_all(
            [
                WeatherRecord(
                    ts=row.ts,
                    source="smhi",
                    temperature=8.0,
                    forecast_issued_at=row.forecast_issued_at,
                    wind_speed=3.0,
                    irradiance=0.0,
                )
                for row in weather
            ]
        )
        session.add_all(
            [
                ConsumptionRecord(
                    device_id="audit-device",
                    ts=start + dt.timedelta(hours=index),
                    heat_kwh=float(index % 24),
                    cool_kwh=0.0,
                    tank_kwh=0.0,
                    outdoor_temp=5.0,
                    source_date=(start + dt.timedelta(hours=index)).date(),
                )
                for index in range(97)
            ]
        )
    return SimpleNamespace(now=now, start=start, values=values)


async def test_UR2_SR1_repeatable_read_in_place_edit(lifecycle_database, monkeypatch):
    from packages.core import settings_service
    from packages.core.models import (
        ConsumptionRecord,
        IndoorTempReading,
        WeatherRecord,
    )
    from packages.ml import model_status, thermal

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    lease = await _owner(database)
    old_artifact = await _artifact(database, "ur2-old-reference", device_id="audit-device")
    before = await database.lifecycle.read()
    assert (
        await database.lifecycle.publish(
            lease,
            old_artifact,
            role="incumbent",
            expected_revision=before.revision,
            expected_incumbent=None,
        )
    ).committed
    reached, changed = asyncio.Event(), asyncio.Event()
    original = thermal.materialize_thermal_inputs
    calls = 0

    async def pause_before_history(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            reached.set()
            await changed.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(thermal, "materialize_thermal_inputs", pause_before_history)
    first = asyncio.create_task(
        model_status.get_readiness_snapshot(
            route="ur2-real-pg", as_of=source.now, components=("demand", "thermal")
        )
    )
    try:
        await asyncio.wait_for(reached.wait(), 3)
        async with database.sessions() as session:
            latest_before = (
                await session.execute(select(func.max(IndoorTempReading.timestamp)))
            ).scalar_one()
            await settings_service.set_settings_bulk(
                {
                    "comfort_reference_sensor_id": "second-room",
                    "smartthings_device_ids": "second-room",
                    "weather_provider": "smhi",
                    "timezone": "Europe/Stockholm",
                },
                session=session,
            )
            await session.execute(
                update(IndoorTempReading)
                .where(
                    IndoorTempReading.device_id == "selected-room",
                    IndoorTempReading.timestamp == source.start + dt.timedelta(hours=1),
                )
                .values(temperature=29.0)
            )
            await session.execute(
                update(DeviceStatusRecord)
                .where(DeviceStatusRecord.ts == source.start + dt.timedelta(hours=1))
                .values(defrost_active=True)
            )
            await session.execute(
                update(WeatherRecord)
                .where(
                    WeatherRecord.source == "open-meteo",
                    WeatherRecord.ts == source.start + dt.timedelta(hours=1),
                )
                .values(temperature=22.0)
            )
            await session.execute(
                update(ConsumptionRecord)
                .where(ConsumptionRecord.ts == source.start + dt.timedelta(hours=1))
                .values(heat_kwh=2.0)
            )
        new_artifact = await _artifact(database, "ur2-new-reference", device_id="audit-device")
        current = await database.lifecycle.read()
        assert (
            await database.lifecycle.publish(
                lease,
                new_artifact,
                role="incumbent",
                expected_revision=current.revision,
                expected_incumbent=old_artifact.artifact_hash,
            )
        ).committed
        changed.set()
        old = await asyncio.wait_for(first, 8)
        new = await asyncio.wait_for(
            model_status.get_readiness_snapshot(
                route="ur2-real-pg", as_of=source.now, components=("demand", "thermal")
            ),
            8,
        )
        assert (
            old["context"]["reference_sensor_id"] == "selected-room"
            and old["context"]["weather_provider"] == "open-meteo"
        )
        assert old["context"]["settings"]["timezone"] == "UTC"
        assert old["model_lifecycle"]["demand"]["committed_hash"] == old_artifact.artifact_hash
        assert (
            new["context"]["reference_sensor_id"] == "second-room"
            and new["context"]["weather_provider"] == "smhi"
        )
        assert new["context"]["settings"]["timezone"] == "Europe/Stockholm"
        assert new["model_lifecycle"]["demand"]["committed_hash"] == new_artifact.artifact_hash
        assert old["readiness"]["generation"] != new["readiness"]["generation"]
        assert old["readiness"]["context_digest"] != new["readiness"]["context_digest"]
        for section in ("indoor", "thermal_status", "consumption"):
            left, right = (
                result["readiness"]["source_watermarks"][section] for result in (old, new)
            )
            assert left["last_at"] == right["last_at"]
            assert left["content_digest"] != right["content_digest"]
        async with database.sessions() as session:
            assert (
                await session.execute(select(func.max(IndoorTempReading.timestamp)))
            ).scalar_one() == latest_before
        assert calls == 2 and old["as_of"] == new["as_of"] == source.now
    finally:
        changed.set()
        await asyncio.gather(first, return_exceptions=True)
        await database.lifecycle.release(lease)


async def test_UR4_SR3_real_get_is_read_only(lifecycle_database, monkeypatch):
    from sqlalchemy import event
    from unittest.mock import AsyncMock, Mock
    from packages.api.routers.optimizer import get_optimizer_status
    from packages.core.models import SettingRecord
    from packages.core.learning_state import get_learning_state_details
    from packages.ml import lifecycle, model_status, safe_persistence, seasonal_learning, thermal

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    monkeypatch.setattr(model_status, "_now", lambda: source.now)
    forbidden = []
    for owner, symbol, asynchronous in (
        (lifecycle, "enqueue_maintenance_request", True),
        (safe_persistence, "publish_signed_artifact", False),
        (thermal.thermal_model, "arefresh_if_changed", True),
        (thermal.ThermalModel, "_fit_rate", False),
    ):
        spy = (AsyncMock if asynchronous else Mock)(
            side_effect=AssertionError("readiness GET performed maintenance")
        )
        monkeypatch.setattr(owner, symbol, spy)
        forbidden.append(spy)
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert not re.search(
            r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", statement, re.I
        ), statement

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        learning, _ = await get_learning_state_details()
        status = await seasonal_learning.get_seasonal_calibration_status(now=source.now)
        progress = await seasonal_learning.get_seasonal_calibration_progress()
        response = await get_optimizer_status()
        assert learning.reliable and not learning.active
        assert status["heating_season_detected"] is True and status["observe_only_active"] is False
        assert progress["indoor_heating"]["train_samples"] >= 20
        assert response["seasonal_calibration"]["readiness"]["maintenance"]["enabled"] is False
        assert response["ordinary_writes_allowed"] is False and "data_freshness" in response
        async with database.sessions() as session:
            assert (
                await session.execute(select(func.count()).select_from(MLLifecycleStateRecord))
            ).scalar_one() == 0
            assert (
                await session.execute(
                    select(SettingRecord.value).where(
                        SettingRecord.key == "_seasonal_calibration_campaign_since"
                    )
                )
            ).scalar_one() == ""
        assert any("statement_timeout" in statement for statement in statements)
        for spy in forbidden:
            spy.assert_not_called()
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)


async def test_UR4_SR3_real_asgi_cold_off_get_has_no_side_effects(lifecycle_database, monkeypatch):
    import httpx
    from sqlalchemy import event
    from unittest.mock import AsyncMock, Mock
    from packages.api.main import app
    from packages.core.models import SettingRecord
    from packages.core.services import AquareaWrapper
    from packages.ml import lifecycle, model_status, safe_persistence, thermal

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    monkeypatch.setattr(model_status, "_now", lambda: source.now)
    assert settings.api_token == "test-token"
    forbidden = []
    for owner, symbol, asynchronous in (
        (lifecycle, "enqueue_maintenance_request", True),
        (safe_persistence, "publish_signed_artifact", False),
        (thermal.thermal_model, "arefresh_if_changed", True),
        (thermal.ThermalModel, "_fit_rate", False),
        (thermal.ThermalModel, "_record_indoor_validation", False),
        (AquareaWrapper, "start", True),
    ):
        spy = (AsyncMock if asynchronous else Mock)(
            side_effect=AssertionError("cold/off ASGI GET performed a forbidden side effect")
        )
        monkeypatch.setattr(owner, symbol, spy)
        forbidden.append(spy)
    statements = []
    before = repr(thermal.thermal_model.params)

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert not re.search(
            r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|TRUNCATE)\b", statement, re.I
        ), statement

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://readiness-fixture.invalid"
        ) as client:
            denied = await client.get("/api/optimizer/status")
            assert denied.status_code == 401 and not statements
            denied = await client.get(
                "/api/optimizer/status", headers={"Authorization": "Bearer wrong-test-token"}
            )
            assert denied.status_code == 403 and not statements
            headers = {"Authorization": "Bearer test-token"}
            response = await client.get("/api/optimizer/status", headers=headers)
            assert response.status_code == 200, response.text
            body = response.json()
            readiness = body["seasonal_calibration"]["readiness"]
            indoor = readiness["components"]["indoor_heating"]
            assert indoor["collection"]["total"] > 0
            assert indoor["collection"]["train"] >= 20
            assert indoor["collection"]["holdout"] >= 3
            assert indoor["fit"]["status"] == "awaiting_fit"
            assert indoor["reporting"]["counts"] is None
            assert readiness["maintenance"]["enabled"] is False
            assert body["thermal_model"]["indoor_heating_samples"] is None
            assert body["ordinary_writes_allowed"] is False
            learning = await client.get("/api/learning-mode", headers=headers)
            assert learning.status_code == 200, learning.text
            assert learning.json()["effective_active"] is False
        async with database.sessions() as session:
            assert (
                await session.execute(select(func.count()).select_from(MLLifecycleStateRecord))
            ).scalar_one() == 0
            assert (
                await session.execute(
                    select(SettingRecord.value).where(
                        SettingRecord.key == "_seasonal_calibration_campaign_since"
                    )
                )
            ).scalar_one() == ""
        assert statements and before == repr(thermal.thermal_model.params)
        for spy in forbidden:
            spy.assert_not_called()
        print(
            json.dumps(
                {
                    "proof": "UR4-real-ASGI-cold-off",
                    "auth_statuses": [401, 403, 200],
                    "collection": indoor["collection"],
                    "fit": indoor["fit"]["status"],
                    "maintenance_enabled": readiness["maintenance"]["enabled"],
                    "sql_dml": 0,
                    "forbidden_calls": 0,
                }
            )
        )
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("race", ["optout", "restore_lock"])
async def test_UR4_SR3_cycle_atomic_latch_and_precedence(lifecycle_database, monkeypatch, race):
    from packages.core.models import PlanActionRecord, SettingRecord
    from packages.core import settings_service
    from packages.ml import seasonal_learning

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    writer = None
    if race == "optout":
        reached, changed = asyncio.Event(), asyncio.Event()
        original = seasonal_learning.get_session

        @asynccontextmanager
        async def pause_transition(**kwargs):
            if not kwargs.get("read_only"):
                reached.set()
                await changed.wait()
            async with original(**kwargs) as session:
                yield session

        monkeypatch.setattr(seasonal_learning, "get_session", pause_transition)
        cycle = asyncio.create_task(
            seasonal_learning.run_seasonal_calibration_cycle(now=source.now)
        )
        await asyncio.wait_for(reached.wait(), 3)
        try:
            await settings_service.set_settings_bulk({"seasonal_calibration_enabled": "false"})
            changed.set()
            result = await asyncio.wait_for(cycle, 3)
            assert result["enabled"] is False and result["observe_only_active"] is False
        finally:
            changed.set()
            await asyncio.gather(cycle, return_exceptions=True)
    else:
        writer = database.maker()
        try:
            source_action = PlanActionRecord(
                plan_id=1,
                action_type="force_dhw_on",
                payload_json="{}",
                device_id="audit-device",
                status="executed",
                executed_at=source.now - dt.timedelta(hours=1),
                scheduled_ts=source.now - dt.timedelta(hours=1),
            )
            writer.add(source_action)
            await writer.flush()
            writer.add(
                PlanActionRecord(
                    plan_id=1,
                    action_type="force_dhw_off",
                    payload_json="{}",
                    device_id="audit-device",
                    status="pending",
                    reverts_action_id=source_action.id,
                    scheduled_ts=source.now,
                )
            )
            await writer.flush()
            started = asyncio.get_running_loop().time()
            result = await asyncio.wait_for(
                seasonal_learning.run_seasonal_calibration_cycle(now=source.now), 3
            )
            assert result["transition"] == "unavailable" and result["observe_only_active"] is False
            assert asyncio.get_running_loop().time() - started < 2
            await asyncio.wait_for(
                settings_service.set_settings_bulk({"learning_mode_enabled": "false"}), 1
            )
            await writer.commit()
            blocked = await seasonal_learning.run_seasonal_calibration_cycle(now=source.now)
            assert blocked["observe_only_active"] is False
            assert blocked["reason"] == "blocked_by_unresolved_safety_revert"
            assert blocked["seasonal_first_deferred_at"] == source.now.isoformat()
        finally:
            await writer.rollback()
            await writer.close()
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(SettingRecord.value).where(
                    SettingRecord.key == "_seasonal_calibration_campaign_since"
                )
            )
        ).scalar_one() == ""
    if race == "restore_lock":
        async with database.sessions() as session:
            await session.execute(
                update(PlanActionRecord)
                .where(PlanActionRecord.reverts_action_id.is_not(None))
                .values(status="executed")
            )
        active = await seasonal_learning.run_seasonal_calibration_cycle(now=source.now)
        assert active["observe_only_active"] is True and active["cycle"] == "maintenance_disabled"
        async with database.sessions() as session:
            await session.execute(update(DeviceStatusRecord).values(outdoor_temp=18.0))
        warm = await seasonal_learning.get_seasonal_calibration_status(now=source.now)
        assert warm["heating_season_detected"] is False and warm["observe_only_active"] is True
        still_active = await seasonal_learning.run_seasonal_calibration_cycle(now=source.now)
        assert still_active["observe_only_active"] is True and still_active["auto_exit"] is False
        await settings_service.set_settings_bulk({"learning_mode_enabled": "true"})
        async with database.sessions() as session:
            await session.execute(
                update(SettingRecord)
                .where(SettingRecord.key == "seasonal_calibration_window_days")
                .values(value="invalid")
            )
        from packages.core.learning_state import get_learning_state_details

        manual, seasonal = await get_learning_state_details()
        assert manual.active and manual.manual_enabled and manual.reliable and seasonal == {}
        await settings_service.set_settings_bulk(
            {"seasonal_calibration_window_days": "7", "learning_mode_enabled": "false"}
        )
        await settings_service.set_settings_bulk({"seasonal_calibration_enabled": "false"})
        assert (await seasonal_learning.get_seasonal_calibration_status(now=source.now))[
            "observe_only_active"
        ] is False


async def test_UR3_SR2_native_dto_to_qualification_no_incumbent_R06(
    lifecycle_database, monkeypatch
):
    from packages.core import database as database_module
    from packages.core.models import PlanActionRecord, PlanRecord

    async with lifecycle_database.engine.begin() as connection:
        for model in (PlanRecord, PlanActionRecord):
            await connection.run_sync(
                lambda current, table=model.__table__: _require_local_table(current, table)
            )
    monkeypatch.setattr(database_module, "async_session_factory", lifecycle_database.maker)
    await test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse(
        lifecycle_database, monkeypatch, real_outcomes=True
    )


async def test_UR3_UR4_SR2_SR3_real_enabled_cycle_and_owner_without_incumbent(
    lifecycle_database, monkeypatch
):
    from unittest.mock import AsyncMock, Mock
    from packages.core import settings_service
    from packages.core.models import OverrideRecord, PlanActionRecord, SettingRecord
    from packages.ml import forecast_quality, lifecycle as module, model_status, seasonal_learning
    from packages.ml import thermal

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    async with database.engine.begin() as connection:
        await connection.run_sync(
            lambda current: _require_local_table(current, OverrideRecord.__table__)
        )
    timer = {
        "status": "known",
        "enabled": False,
        "slots": [],
        "timezone": "UTC",
        "fetched_at": source.now.isoformat(),
    }
    async with database.sessions() as session:
        await session.execute(
            update(DeviceStatusRecord).values(
                force_dhw=0,
                force_heater=0,
                holiday_mode=0,
                native_policy_provenance={"timer": timer},
            )
        )
        session.add(
            PlanActionRecord(
                plan_id=1,
                action_type="force_dhw_on",
                payload_json="{}",
                device_id="audit-device",
                status="pending",
                scheduled_ts=source.now,
            )
        )
    await settings_service.set_settings_bulk(
        {"ml_maintenance_enabled": "true", "seasonal_calibration_auto_train": "true"}
    )
    original_enqueue = module.enqueue_maintenance_request
    committed_enqueues = []

    async def checked_enqueue(*args, **kwargs):
        async with database.sessions() as session:
            campaign = (
                await session.execute(
                    select(SettingRecord.value).where(
                        SettingRecord.key == "_seasonal_calibration_campaign_since"
                    )
                )
            ).scalar_one()
            assert campaign == source.now.isoformat(), "enqueue preceded activation commit"
        committed_enqueues.append(args[0])
        return await original_enqueue(*args, **kwargs)

    monkeypatch.setattr(module, "enqueue_maintenance_request", checked_enqueue)
    owner = module.MaintenanceRunner(database.lifecycle)
    hook = owner.hooks["thermal"]
    materialize = AsyncMock(wraps=hook.materialize)
    fit = Mock(wraps=hook.fit)
    publish = AsyncMock(wraps=hook.commit_candidate)
    owner.hooks = {
        "thermal": module.ComponentHooks(materialize, fit, publish, hook.review_candidate)
    }
    try:
        assert await settings_service.get_bool_setting("ml_maintenance_enabled") is True
        cycle = await seasonal_learning.run_seasonal_calibration_cycle(now=source.now)
        assert cycle["observe_only_active"] is True and cycle["cycle"] == "queued"
        assert cycle["open_revert_obligations"]["count"] == 0
        assert committed_enqueues == ["thermal"]
        fit.assert_not_called()
        assert (await database.lifecycle.read()).reference("thermal") is None
        result = await owner.run()
        assert result["components"]["thermal"] == "succeeded", result
        materialize.assert_awaited_once()
        fit.assert_called_once()
        publish.assert_awaited_once()
        claimed = materialize.await_args.args[0]
        assert isinstance(claimed, module.MaintenanceRequest) and claimed.status == "running"
        assert claimed.id in {request["request_id"] for request in cycle["requests"]}
        completed = await database.lifecycle.get_request(claimed.id)
        assert completed.status == "succeeded"
        state = await database.lifecycle.read()
        assert state.reference("thermal") is None
        candidate = state.reference("thermal", "candidate")
        assert candidate is not None and not module.reference_is_qualified(candidate)
        payload = await database.lifecycle.load_committed("thermal", role="candidate")
        forecast_quality.check_exact_fit("thermal", payload)
        data = fit.call_args.args[0]["data"]
        samples = thermal._extract_indoor_rate_samples(
            data["readings"], data["records"], data["weather"]
        )
        counts = {
            name: thermal._rate_sample_counts(group)
            for name, group in zip(("indoor_heating", "indoor_cooling"), samples)
        }
        for name, measured in counts.items():
            assert measured["train"] >= 20 and measured["holdout"] >= 3
            assert candidate.manifest["metrics"][name]["train_samples"] == measured["train"]
            assert candidate.manifest["metrics"][name]["holdout_samples"] == measured["holdout"]
        dto = await model_status.get_readiness_snapshot(route="ur3-real-enabled-owner")
        indoor = dto["readiness"]["components"]["indoor_heating"]
        assert indoor["collection"]["total"] == counts["indoor_heating"]["total"]
        assert indoor["fit"]["status"] == "validated"
        assert indoor["fit"]["artifact_hash"] == candidate.artifact_hash
        assert indoor["fit"]["request_id"] == claimed.id
        assert (
            dto["ordinary_writes_allowed"] is False
            if "ordinary_writes_allowed" in dto
            else (dto["model_lifecycle"]["thermal"]["ordinary_writes_allowed"] is False)
        )
        print(
            json.dumps(
                {
                    "proof": "UR3-UR4-real-enabled-cycle-owner",
                    "committed_enqueues": committed_enqueues,
                    "claimed_dto": True,
                    "source_counts": counts,
                    "fit": indoor["fit"]["status"],
                    "published_candidate": True,
                    "qualified_incumbent": False,
                    "ordinary_writes_allowed": False,
                }
            )
        )
    finally:
        await owner.close()


async def test_UR3_SR2_real_owner_off_and_rejection_counts(lifecycle_database, monkeypatch):
    from sqlalchemy import delete
    from unittest.mock import AsyncMock, Mock
    from packages.core import settings_service
    from packages.core.models import IndoorTempReading
    from packages.ml import lifecycle as module, thermal

    database = lifecycle_database
    source = await _readiness_source_fixture(database, monkeypatch)
    cutoff = source.now - dt.timedelta(hours=4)
    async with database.sessions() as session:
        await session.execute(delete(DeviceStatusRecord).where(DeviceStatusRecord.ts < cutoff))
        await session.execute(delete(IndoorTempReading).where(IndoorTempReading.timestamp < cutoff))
    owner = module.MaintenanceRunner(database.lifecycle)
    hook = owner.hooks["thermal"]
    materialize = AsyncMock(wraps=hook.materialize)
    fit = Mock(wraps=hook.fit)
    publish = AsyncMock(side_effect=AssertionError("disabled/rejected artifact publication"))
    owner.hooks = {"thermal": module.ComponentHooks(materialize, fit, publish)}
    try:
        off = await owner.run()
        assert off["status"] == "disabled"
        materialize.assert_not_awaited()
        fit.assert_not_called()
        publish.assert_not_awaited()
        async with database.sessions() as session:
            assert (
                await session.execute(select(func.count()).select_from(MLMaintenanceRequestRecord))
            ).scalar_one() == 0
            assert (
                await session.execute(select(func.count()).select_from(MLArtifactRecord))
            ).scalar_one() == 0
        await settings_service.set_settings_bulk({"ml_maintenance_enabled": "true"})
        rejected = await owner.run()
        assert rejected["components"]["thermal"] == "queued"
        fit.assert_called_once()
        publish.assert_not_awaited()
        captured = fit.call_args.args[0]
        heating, _cooling = thermal._extract_indoor_rate_samples(
            captured["data"]["readings"],
            captured["data"]["records"],
            captured["data"]["weather"],
        )
        counts = thermal._rate_sample_counts(heating)
        async with database.sessions() as session:
            request = (
                await session.execute(
                    select(MLMaintenanceRequestRecord).where(
                        MLMaintenanceRequestRecord.component == "thermal"
                    )
                )
            ).scalar_one()
            assert request.status == "queued" and request.attempted_at is not None
            diagnostics = request.details["readiness_rejection"]
            measured = diagnostics["components"]["indoor_heating"]
            assert (measured["total"], measured["train"], measured["holdout"]) == (
                counts["total"],
                counts["train"],
                counts["holdout"],
            )
            assert measured["total"] > 0 and measured["train"] < 20
            assert diagnostics["reason"] == request.details["reason"]
            assert (
                await session.execute(select(func.count()).select_from(MLArtifactRecord))
            ).scalar_one() == 0
        assert not list(database.model_dir.rglob("*.pkl"))
    finally:
        await owner.close()


async def _artifact(database, fit_id, *, device_id="fixture-device", checkpoint=None):
    from packages.ml.forecast_quality import locked_demand_scope

    async with database.sessions() as session:
        async with locked_demand_scope(device_id, session=session) as (scope, _session):
            manifest_scope = scope
    return publish_signed_artifact(
        {"fit_id": fit_id, "weights": [1.25, 3.5]},
        component="demand",
        schema_version="demand_lifecycle_v1",
        manifest={
            "fit_id": fit_id,
            "device_id": device_id,
            "scope": manifest_scope,
            "data_cutoff": dt.datetime.now(dt.timezone.utc).isoformat(),
            "input_schema": "native_inputs_v1",
            "output_schema": "demand_v1",
            "runtime_versions": {"python": "3.12"},
            "evidence_hashes": [],
        },
        round_trip_check=lambda restored: _exact_fit(restored, fit_id),
        checkpoint=checkpoint,
    )


def _exact_fit(restored, fit_id):
    assert restored == {"fit_id": fit_id, "weights": [1.25, 3.5]}


async def _prepare_demand_fixture(database):
    """Opt-in current inputs, never added to readiness/thermal or empty-authority cohorts."""
    from packages.core import settings_service
    from packages.core.house_contract import ensure_current_contract

    async with database.sessions() as session:
        await settings_service.set_settings_bulk(
            {
                "timezone": "UTC",
                "comfort_temp_min": "18",
                "comfort_temp_target": "20.5",
                "comfort_temp_max": "25",
            },
            session=session,
        )
        await ensure_current_contract(session)
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        session.add(DeviceStatusRecord(ts=now, device_id="fixture-device"))


async def _owner(database, *, demand=False):
    if demand:
        await _prepare_demand_fixture(database)
    await database.lifecycle.initialize()
    await database.lifecycle.set_maintenance_enabled(True)
    lease = await database.lifecycle.acquire("worker-" + uuid.uuid4().hex)
    assert lease is not None
    return lease


async def _native_fixture(database, count, *, incumbent_fit=None):
    lease = await _owner(database, demand=True)
    if incumbent_fit is not None:
        incumbent = await _artifact(database, incumbent_fit)
        state = await database.lifecycle.read()
        assert (
            await database.lifecycle.publish(
                lease,
                incumbent,
                role="incumbent",
                expected_revision=state.revision,
                expected_incumbent=None,
            )
        ).committed
    artifact = await _artifact(database, "native-fixture")
    state = await database.lifecycle.read()
    assert (
        await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=state.revision,
            expected_incumbent=state.reference("demand").artifact_hash
            if state.reference("demand")
            else None,
        )
    ).committed
    reference = (await database.lifecycle.read()).reference("demand", "candidate")
    issued = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    snapshot = {"scope": {"policy_schema": "native_unchanged_v1"}, "capability": "demand"}
    snapshot_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    rows = [
        {
            "epoch": lease.epoch,
            "device_id": "fixture-device",
            "component": "demand",
            "cohort_key": "cohort-" + str(index),
            "horizon_minutes": 60,
            "issue_kind": "prospective_native",
            "policy_schema": "native_unchanged_v1",
            "issued_at": issued,
            "target_at": issued + dt.timedelta(hours=1),
            "device_day": issued.date(),
            "candidate_hash": artifact.artifact_hash,
            "incumbent_hash": None,
            "snapshot_hash": snapshot_hash,
            "input_snapshot": snapshot,
            "predictions": {"candidate": 1.0, "incumbent": 2.0, "baseline": 2.0},
            "expected_horizons": [60],
        }
        for index in range(count)
    ]
    return lease, reference, rows


@contextmanager
def _trace_thermal_materializers(monkeypatch, observations, *, source="actual-hook"):
    """Observe the real hook; return objects and exceptions are never replaced."""
    from packages.ml import forecast_quality as quality, thermal

    native = quality.materialize_native_inputs
    history = thermal.materialize_thermal_inputs

    async def observed_native(*args, **kwargs):
        entry = {
            "call": "native",
            "capabilities": list(kwargs.get("capabilities", ())),
        }
        observations.append(entry)
        try:
            result = await native(*args, **kwargs)
        except BaseException as exc:
            entry["exception"] = type(exc).__name__
            raise
        entry["result"] = "None" if result is None else "present"
        if result is not None:
            entry["sensor_kind"] = result["sensor_basis"]["kind"]
            entry["capability"] = result["capability"]
        return result

    async def observed_history(*args, **kwargs):
        entry = {"call": "thermal_history"}
        observations.append(entry)
        try:
            result = await history(*args, **kwargs)
        except BaseException as exc:
            entry["exception"] = type(exc).__name__
            raise
        entry["result"] = "None" if result is None else "present"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(quality, "materialize_native_inputs", observed_native)
        patch.setattr(thermal, "materialize_thermal_inputs", observed_history)
        try:
            yield
        finally:
            print(
                json.dumps({"probe": "G3-materializers", "source": source, "calls": observations})
            )


async def _thermal_prerequisite_probe(database, monkeypatch, now):
    """Prerequisite-only differential. Every probe rolls back; no hook fit/commit."""
    from packages.ml import forecast_quality as quality, thermal

    async with database.sessions() as session:
        installation = await session.get(HouseInstallationRecord, 1)
        local, resolved = (
            await session.execute(
                text("SELECT to_regclass(:local)::oid,to_regclass('house_installations')::oid"),
                {"local": database.schema + ".house_installations"},
            )
        ).one()
    print(
        json.dumps(
            {
                "probe": "G3-prerequisite",
                "local_oid": local,
                "resolved_oid": resolved,
                "singleton_present": installation is not None,
            }
        )
    )
    assert local is not None and local == resolved
    if installation is not None:
        print(json.dumps({"probe": "G3-boundary", "reason": "installation_already_present"}))
        return
    for present in (False, True):
        observations = []
        rolled_back = False
        try:
            async with database.engine.connect() as connection:
                transaction = await connection.begin()
                try:
                    maker = async_sessionmaker(
                        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
                    )

                    @asynccontextmanager
                    async def diagnostic_sessions():
                        async with maker() as session:
                            yield session
                            await session.commit()

                    with monkeypatch.context() as patch:
                        patch.setattr(quality, "get_session", diagnostic_sessions)
                        patch.setattr(thermal, "get_session", diagnostic_sessions)
                        if present:
                            async with diagnostic_sessions() as session:
                                session.add(
                                    HouseInstallationRecord(
                                        singleton_id=1,
                                        installation_id=uuid.UUID(int=1),
                                        house_id=uuid.UUID(int=2),
                                        demand_dependency_generation=1,
                                        indoor_dependency_generation=1,
                                    )
                                )
                        with _trace_thermal_materializers(
                            patch,
                            observations,
                            source="prerequisite-present" if present else "prerequisite-absent",
                        ):
                            # Same family selection as the hook, but never fit/publish/qualify.
                            inputs = await quality.materialize_native_inputs("thermal", now=now)
                            indoor = await quality.materialize_native_inputs(
                                "thermal",
                                now=now,
                                device_id=inputs["device_id"] if inputs else None,
                                capabilities=("indoor_heating", "indoor_cooling"),
                            )
                            if indoor is not None:
                                await thermal.materialize_thermal_inputs(
                                    indoor["device_id"],
                                    indoor["reference_sensor_id"],
                                    indoor["weather_provider"],
                                    now,
                                    selected_ids=indoor["configured_sensor_ids"],
                                )
                finally:
                    await transaction.rollback()
                    rolled_back = True
        except Exception as exc:
            # Diagnostic failure is evidence, never a replacement for the actual hook result.
            observations.append({"probe_exception": type(exc).__name__})
        print(
            json.dumps(
                {
                    "probe": "G3-installation-differential",
                    "installation_present": present,
                    "rolled_back": rolled_back,
                    "calls": observations,
                }
            )
        )


async def test_issue_outcome_uniqueness_and_immutable_snapshot_under_concurrency(
    lifecycle_database,
):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 20)
    await asyncio.gather(
        *(database.lifecycle.record_native_issues(lease, reference, rows, slot=0) for _ in range(6))
    )
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLForecastIssueRecord))
        ).scalar_one() == 20
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 20
        record = (await session.execute(select(MLForecastIssueRecord).limit(1))).scalar_one()
        issue_id = record.id
    with pytest.raises(DBAPIError, match="ml_forecast_issue_immutable"):
        async with database.sessions() as session:
            await session.execute(
                update(MLForecastIssueRecord)
                .where(MLForecastIssueRecord.id == issue_id)
                .values(predictions={"candidate": 99.0})
            )
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(MLForecastIssueRecord.predictions).where(
                    MLForecastIssueRecord.id == issue_id
                )
            )
        ).scalar_one() == rows[0]["predictions"]


async def test_jittered_cycle_admission_uses_fixed_utc_grid(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database)
    for _attempt in range(2):
        async with database.sessions() as session:
            await session.execute(
                update(MLLifecycleStateRecord).values(
                    next_maintenance_at=func.clock_timestamp() - dt.timedelta(seconds=3)
                )
            )
        assert await database.lifecycle.begin_cycle(lease)
        admitted = await database.lifecycle.read()
        assert admitted.next_maintenance_at.timestamp() % (6 * 3600) == 0
        async with database.sessions() as session:
            clock = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        assert dt.timedelta() < admitted.next_maintenance_at - clock <= dt.timedelta(hours=6)
        assert await database.lifecycle.begin_cycle(lease) is False


async def test_native_collection_recovers_after_gap_without_fabricated_slots(lifecycle_database):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 3)
    checkpoints = []
    for slot, row in zip((0, 2, 3), rows):
        values = dict(row, cohort_key=f"slot-{slot}")
        await database.lifecycle.record_native_issues(lease, reference, [values], slot=slot)
        checkpoints.append(
            (await database.lifecycle.read()).checkpoints["collection:" + reference.artifact_hash]
        )
    assert [row["complete"] for row in checkpoints] == [True, False, True]
    assert checkpoints[-1]["gaps"] == 1
    assert checkpoints[-1]["expected_slots"] == 4 and checkpoints[-1]["collected_slots"] == 3
    async with database.sessions() as session:
        cohorts = (await session.execute(select(MLForecastIssueRecord.cohort_key))).scalars().all()
        assert set(cohorts) == {"slot-0", "slot-2", "slot-3"}
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 3


@pytest.mark.parametrize("reader", ["primary", "shadow"])
@pytest.mark.parametrize("first_hours,valid_first", [((True,), False), ((True, 1), True)])
async def test_R03_saved_plan_JSON_readers_preserve_integer_identity(
    lifecycle_database, monkeypatch, reader, first_hours, valid_first
):
    from packages.core.models import IndoorTempReading, PlanActionRecord, PlanRecord
    from tests import test_forecast_quality as score_tests

    database = lifecycle_database
    async with database.engine.begin() as connection:
        for model in (PlanRecord, PlanActionRecord, IndoorTempReading):
            await connection.run_sync(
                lambda current, table=model.__table__: _require_local_table(current, table)
            )
    async with database.sessions() as session:

        def saved_history(plans, readings, _unresolved):
            for plan in plans:
                plan.status = "superseded"
            session.add_all([*plans, *readings])
            return session

        monkeypatch.setattr(score_tests, "_scorecard_session", saved_history)
        await score_tests.test_R03_saved_history_requires_canonical_integer_hours(
            monkeypatch, reader, first_hours, valid_first
        )
        assert (
            await session.execute(select(func.count()).select_from(PlanRecord))
        ).scalar_one() == 12
        assert (
            await session.execute(select(func.count()).select_from(IndoorTempReading))
        ).scalar_one() == 60


@pytest.mark.parametrize("family", ["tank_", "indoor_"])
async def test_R06_family_identity_survives_queue_transitions_and_claim(lifecycle_database, family):
    from packages.ml.lifecycle import MaintenanceRequest, update_maintenance_gate

    database = lifecycle_database
    lease = await _owner(database)
    dedup_key = "r06-opaque-" + family
    queued = await database.lifecycle.enqueue(
        "thermal", "scheduled", family=family, dedup_key=dedup_key, lease=lease
    )
    assert isinstance(queued, MaintenanceRequest)
    assert queued.details == {"family": family} and queued.dedup_key == dedup_key
    duplicate = await database.lifecycle.enqueue(
        "thermal", "scheduled", family=family, dedup_key=dedup_key, lease=lease
    )
    assert duplicate.id == queued.id
    other_family = "indoor_" if family == "tank_" else "tank_"
    with pytest.raises(ValueError, match="family conflicts"):
        await database.lifecycle.enqueue(
            "thermal", "scheduled", family=other_family, dedup_key=dedup_key, lease=lease
        )
    await database.lifecycle.defer_component(lease, "thermal", "candidate_frozen")
    deferred = await database.lifecycle.get_request(queued.id)
    assert deferred.details == {"family": family, "reason": "candidate_frozen"}
    claimed = await database.lifecycle.claim_request(lease, "thermal")
    assert isinstance(claimed, MaintenanceRequest)
    assert claimed.status == "running" and claimed.details == {"family": family}
    assert claimed.dedup_key == dedup_key
    await database.lifecycle.finish_request(
        lease, queued.id, "queued", {"reason": "temporary_unavailable"}
    )
    retry = await database.lifecycle.get_request(queued.id)
    assert retry.details == {"family": family, "reason": "temporary_unavailable"}
    assert (await database.lifecycle.claim_request(lease, "thermal")).details == {"family": family}
    await database.lifecycle.release(lease)
    restarted = await database.lifecycle.acquire("r06-restarted-" + family)
    assert restarted is not None and restarted.fence != lease.fence
    assert await database.lifecycle.begin_cycle(restarted)
    recovered = await database.lifecycle.get_request(queued.id)
    assert recovered.status == "queued"
    assert recovered.details == {"family": family, "reason": "owner_restarted"}
    running = await database.lifecycle.claim_request(restarted, "thermal")
    assert running.details == {"family": family} and running.dedup_key == dedup_key
    with pytest.raises(ValueError, match="cannot change family"):
        await database.lifecycle.finish_request(
            restarted, queued.id, "succeeded", {"family": other_family}
        )
    async with database.sessions() as session:
        await update_maintenance_gate(session, False)
    disabled = await database.lifecycle.get_request(queued.id)
    assert disabled.status == "queued"
    assert disabled.details == {"family": family, "reason": "maintenance_disabled"}
    assert disabled.response(False)["control_ready"] is False
    assert disabled.response(False)["ordinary_writes_allowed"] is False
    assert await database.lifecycle.acquire("r06-disabled") is None
    async with database.sessions() as session:
        persisted = (await session.execute(select(MLMaintenanceRequestRecord))).scalar_one()
        assert persisted.id == queued.id and persisted.dedup_key == dedup_key
        assert persisted.details["family"] == family
        assert (
            await session.execute(select(func.count()).select_from(MLArtifactRecord))
        ).scalar_one() == 0


async def _initialize_thermal_installation(database):
    """Opt-in prerequisite only; callers must already have proved local storage."""
    installation_id, house_id = uuid.uuid4(), uuid.uuid4()
    async with database.sessions() as session:
        assert await session.get(HouseInstallationRecord, 1) is None
        session.add(
            HouseInstallationRecord(
                singleton_id=1,
                installation_id=installation_id,
                house_id=house_id,
                demand_dependency_generation=1,
                indoor_dependency_generation=1,
                current_revision_id=None,
            )
        )
        await session.flush()
        await session.refresh(await session.get(HouseInstallationRecord, 1))
        row = await session.get(HouseInstallationRecord, 1)
        assert row is not None
        assert (row.installation_id, row.house_id) == (installation_id, house_id)
        assert (row.demand_dependency_generation, row.indoor_dependency_generation) == (1, 1)
        assert row.current_revision_id is None
    async with database.sessions() as session:
        row = await session.get(HouseInstallationRecord, 1)
        assert row is not None and row.installation_id == installation_id
        assert row.house_id == house_id and row.current_revision_id is None
        assert (row.demand_dependency_generation, row.indoor_dependency_generation) == (1, 1)


async def _thermal_route_owner(database, monkeypatch, *, foreign_count=0, strong_skill=False):
    from unittest.mock import AsyncMock
    from packages.core import settings_service
    from packages.core.models import (
        ConsumptionRecord,
        IndoorTempReading,
        PriceRecord,
        WeatherRecord,
    )
    from packages.ml import forecast_quality as quality, lifecycle as module, thermal
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute

    async with database.engine.begin() as connection:
        for model in (
            ConsumptionRecord,
            IndoorTempReading,
            PriceRecord,
            WeatherRecord,
            HouseInstallationRecord,
        ):
            await connection.run_sync(
                lambda current, table=model.__table__: _require_local_table(current, table)
            )
    await _initialize_thermal_installation(database)
    async with database.sessions() as session:
        current = (await session.execute(select(func.clock_timestamp()))).scalar_one()
    frozen_at = current - dt.timedelta(hours=360)
    shift = frozen_at - TestR06ThermalQualificationRoute.NOW
    clock = [frozen_at]
    records, readings, weather = TestR06ThermalQualificationRoute.cohort()
    if strong_skill:
        temperature = 21.0
        for index, (record, reading) in enumerate(zip(records, readings)):
            heating = index % 36 < 12
            record.space_heating_active = heating
            record.device_action = "HEATING" if heating else "IDLE"
            record.outdoor_temp = float((8, 10, 12)[(index // 144) % 3])
            reading.temperature = round(temperature, 4)
            temperature += (2.0 if heating else -1.0) * 5 / 60
        for index, point in enumerate(weather):
            point.temperature = float((8, 10, 12)[(index // 12) % 3])
    timer = {
        "status": "known",
        "enabled": False,
        "slots": [],
        "timezone": "UTC",
        "fetched_at": frozen_at.isoformat(),
    }
    for row in records:
        row.ts += shift
        row.force_dhw = row.force_heater = row.holiday_mode = 0
        row.native_policy_provenance = {"timer": timer}
    for row in readings:
        row.timestamp += shift
    for row in weather:
        row.ts += shift
        row.forecast_issued_at += shift
    future = WeatherRecord(
        ts=frozen_at + dt.timedelta(hours=1),
        source="open-meteo",
        temperature=99.0,
        forecast_issued_at=frozen_at + dt.timedelta(minutes=1),
    )
    async with database.sessions() as session:
        session.add_all([*records, *readings, *weather, future])
        session.add_all(
            [
                IndoorTempReading(
                    timestamp=frozen_at - dt.timedelta(minutes=115),
                    temperature=28.0,
                    device_id="deselected-room",
                    is_stale=False,
                )
                for _row_number in range(foreign_count)
            ]
        )
    monkeypatch.setattr(thermal, "get_session", database.sessions)
    monkeypatch.setattr(quality, "get_session", database.sessions)
    monkeypatch.setattr(
        settings_service,
        "get_all_settings",
        AsyncMock(
            return_value={
                "comfort_reference_sensor_id": "selected-room",
                "smartthings_device_ids": "selected-room",
                "weather_provider": "open-meteo",
                "price_provider": "manual",
                "timezone": "UTC",
            }
        ),
    )
    monkeypatch.setattr(module, "_now", lambda: clock[0])
    lease = await _owner(database)
    owner = None
    try:
        observations = []
        with _trace_thermal_materializers(monkeypatch, observations):
            owner = module.MaintenanceRunner(
                database.lifecycle, enabled_reader=AsyncMock(return_value=True)
            )
            hook = owner.hooks["thermal"]
            heating, cooling = thermal._extract_indoor_rate_samples(readings, records, weather)
            source_counts = {
                name: thermal._rate_sample_counts(samples)
                for name, samples in (("indoor_heating", heating), ("indoor_cooling", cooling))
            }
            assert all(
                counts["train"] >= 20 and counts["holdout"] >= 3
                for counts in source_counts.values()
            )
            assert (await database.lifecycle.read()).reference("thermal") is None
            queued = await database.lifecycle.enqueue(
                "thermal", "scheduled", family="indoor_", dedup_key="r06-indoor-fit", lease=lease
            )
            assert isinstance(queued, module.MaintenanceRequest)
            assert queued.details == {"family": "indoor_"}
            claimed = await database.lifecycle.claim_request(lease, "thermal")
            assert isinstance(claimed, module.MaintenanceRequest)
            assert claimed.id == queued.id and claimed.details == {"family": "indoor_"}
            try:
                materialized = await hook.materialize(claimed)
            except BaseException as exc:
                print(json.dumps({"probe": "G3-actual-hook", "exception": type(exc).__name__}))
                raise
        print(
            json.dumps(
                {
                    "probe": "G3-actual-hook",
                    "result": "None" if materialized is None else "present",
                    "requested_family": claimed.details.get("family"),
                }
            )
        )
        if materialized is None:
            try:
                await _thermal_prerequisite_probe(database, monkeypatch, clock[0])
            except Exception as exc:
                print(
                    json.dumps({"probe": "G3-diagnostic-failure", "exception": type(exc).__name__})
                )
        assert materialized is not None
        assert materialized["requested_family"] == "indoor_"
        fitted = hook.fit(materialized)
        assert fitted is not None and fitted["capabilities"] == ("indoor_heating", "indoor_cooling")
        assert len(fitted["data"]["records"]) == 1153
        assert all(row.temperature != 99.0 for row in fitted["data"]["weather"])
        assert fitted["inputs"]["sensor_basis"]["sensor_ids"] == ["selected-room"]
        assert fitted["inputs"]["schemas"]["input"] == "frozen_native_inputs_v2"
        before = await database.lifecycle.read()
        publication = await hook.commit_candidate(lease, fitted, before)
        assert publication.committed
        await database.lifecycle.finish_request(
            lease, claimed.id, "succeeded", {"revision": publication.revision}
        )
        completed = await database.lifecycle.get_request(claimed.id)
        assert completed.status == "succeeded" and completed.details["family"] == "indoor_"
        reference = (await database.lifecycle.read()).reference("thermal", "candidate")
        assert reference is not None and not module.reference_is_qualified(reference)
        payload = await database.lifecycle.load_committed("thermal", role="candidate")
        quality.check_exact_fit("thermal", payload)
        return SimpleNamespace(
            owner=owner,
            lease=lease,
            fitted=fitted,
            reference=reference,
            clock=clock,
            current=current,
            frozen_at=frozen_at,
            payload=payload,
            source_counts=source_counts,
        )
    except BaseException:
        try:
            if owner is not None:
                await owner.close()
        finally:
            await database.lifecycle.release(lease)
        raise


def _thermal_native_snapshot(route, issued, capability):
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute

    inputs = TestR06ThermalQualificationRoute.native_inputs(issued)
    frozen = route.fitted["inputs"]
    assert capability in frozen["capabilities"] and capability.startswith("indoor_")
    for key in (
        "installation_id",
        "indoor_dependency_generation",
        "configured_sensor_ids",
        "reference_sensor_id",
        "sensor_basis",
        "native_settings",
        "schemas",
        "capabilities",
        "device_id",
        "weather_provider",
        "timezone",
    ):
        inputs[key] = deepcopy(frozen[key])
    if "tzdata_version" in frozen:
        inputs["tzdata_version"] = deepcopy(frozen["tzdata_version"])
    inputs.update(
        {
            "capability": capability,
            "timer": {**deepcopy(frozen["timer"]), "fetched_at": issued.isoformat()},
        }
    )
    active = capability == "indoor_heating"
    for row in inputs["status_history"]:
        row.pop("space_heating_evidence", None)
        row.update(
            {
                "space_heating_active": active,
                "zone1_temp": 35.0,
                "device_action": "HEATING" if active else "IDLE",
                "direction": "PUMP" if active else "IDLE",
                "mode": "1",
                "operation_status": 1,
                "pump_duty": 1 if active else 0,
                "zone1_operation_status": 1,
                "zone2_operation_status": 0,
            }
        )
    for row in inputs["weather"]:
        row["temperature"] = 5.0
    return inputs


async def test_R06_R07_R34_automatic_indoor_fit_has_positive_prospective_sql_route(
    lifecycle_database, monkeypatch
):
    from unittest.mock import AsyncMock
    from packages.core.models import IndoorTempReading, OverrideRecord, PlanActionRecord, PlanRecord
    from packages.ml import forecast_quality as quality

    database = lifecycle_database
    route = await _thermal_route_owner(database, monkeypatch, foreign_count=5002)
    first = route.frozen_at + dt.timedelta(hours=2)
    second = route.frozen_at + dt.timedelta(hours=8)
    try:
        async with database.engine.begin() as connection:
            for model in (PlanRecord, PlanActionRecord, OverrideRecord):
                await connection.run_sync(
                    lambda current, table=model.__table__: _require_local_table(current, table)
                )
        for issued, capability, rate in (
            (first, "indoor_heating", 0.6),
            (second, "indoor_cooling", -0.4),
        ):
            inputs = _thermal_native_snapshot(route, issued, capability)
            async with database.sessions() as session:
                for minutes in range(-60, 61, 5):
                    timestamp = issued + dt.timedelta(minutes=minutes)
                    active = capability == "indoor_heating"
                    session.add(
                        DeviceStatusRecord(
                            ts=timestamp,
                            device_id="audit-device",
                            space_heating_active=active,
                            mode="1",
                            operation_status=1,
                            direction="PUMP" if active else "IDLE",
                            pump_duty=1 if active else 0,
                            device_action="HEATING" if active else "IDLE",
                            defrost_active=False,
                            zone1_operation_status=1,
                            zone2_operation_status=0,
                            zone1_temp=35.0,
                            zone1_target_temp=40.0,
                            tank_target_temp=50.0,
                            outdoor_temp=5.0,
                            tank_temp=45.0,
                            force_dhw=0,
                            force_heater=0,
                            holiday_mode=0,
                            native_policy_provenance={
                                "schema": "native_unchanged_v1",
                                "settings": inputs["native_settings"],
                                "timer": inputs["timer"],
                            },
                        )
                    )
                    session.add(
                        IndoorTempReading(
                            timestamp=timestamp,
                            temperature=21.0 + rate * minutes / 60,
                            device_id="selected-room",
                            is_stale=False,
                        )
                    )
        async with database.sessions() as session:
            session.add_all(
                [
                    IndoorTempReading(
                        timestamp=first + dt.timedelta(minutes=1),
                        temperature=28.0,
                        device_id="deselected-room",
                        is_stale=False,
                    )
                    for _row_number in range(30002)
                ]
            )

        async def inputs(component, *, now, **_options):
            return _thermal_native_snapshot(
                route, now, "indoor_heating" if now == first else "indoor_cooling"
            )

        monkeypatch.setattr(quality, "materialize_native_inputs", AsyncMock(side_effect=inputs))
        for issued in (first, second, route.current):
            route.clock[0] = issued
            await route.owner.hooks["thermal"].review_candidate(route.lease, route.reference)
        state = await database.lifecycle.read()
        decision = state.checkpoints["decision:" + route.reference.artifact_hash + ":0"]
        assert decision["status"] == "evaluated" and decision["result"]["qualified"] is False
        assert decision["result"]["reason"] in {"incomplete_coverage", "insufficient_evidence"}
        assert (
            state.reference("thermal", "candidate").artifact_hash == route.reference.artifact_hash
        )
        async with database.sessions() as session:
            issues = (await session.execute(select(MLForecastIssueRecord))).scalars().all()
            outcomes = (
                (
                    await session.execute(
                        select(MLForecastOutcomeRecord).where(
                            MLForecastOutcomeRecord.status == "valid"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(issues) == 9 and len(outcomes) == 6
        assert {row.input_snapshot["capability"] for row in issues} == {
            "indoor_heating",
            "indoor_cooling",
        }
        assert all(
            row.input_snapshot["scope"] == route.reference.manifest["scope"] for row in issues
        )
        assert all(set(row.predictions) == {"candidate", "incumbent", "baseline"} for row in issues)
        assert {row.result["capability"] for row in outcomes} == {
            "indoor_heating",
            "indoor_cooling",
        }
        assert all(abs(row.result["candidate"] - row.result["observed"]) < 0.02 for row in outcomes)
    finally:
        await route.owner.close()
        await database.lifecycle.release(route.lease)


async def test_R06_scoped_qualification_keeps_heads_and_refuses_v1_reuse(
    lifecycle_database, monkeypatch, *, real_outcomes=False
):
    from packages.ml import forecast_quality as quality, lifecycle as module
    from tests.test_ml_lifecycle import TestR06ThermalQualificationRoute

    database = lifecycle_database
    route = await _thermal_route_owner(database, monkeypatch, strong_skill=real_outcomes)
    scope = route.reference.manifest["scope"]
    shift = route.frozen_at - TestR06ThermalQualificationRoute.NOW
    pairs = TestR06ThermalQualificationRoute.prospective_pairs(route.reference.artifact_hash, scope)
    oracle = {}
    try:
        if real_outcomes:
            from packages.core.models import IndoorTempReading, OverrideRecord

            async with database.engine.begin() as connection:
                await connection.run_sync(
                    lambda current: _require_local_table(current, OverrideRecord.__table__)
                )
        for index in range(0, len(pairs), 3):
            group = pairs[index : index + 3]
            issued = dt.datetime.fromisoformat(group[0]["issued_at"]) + shift
            if real_outcomes:
                issued += dt.timedelta(hours=2)
            capability = group[0]["capability"]
            inputs = _thermal_native_snapshot(route, issued, capability)
            if real_outcomes:
                inputs["indoor_temperature"] = group[0]["observed"]
                for reading in inputs["sensor_history"]:
                    reading["temperature"] = inputs["indoor_temperature"]
            for point in inputs["weather"]:
                point["temperature"] = float(group[0]["outdoor_bin"])
            by_horizon = {pair["horizon_minutes"]: pair for pair in group}
            issue = quality.freeze_native_pair(
                inputs,
                epoch=route.lease.epoch,
                component="thermal",
                candidate_hash=route.reference.artifact_hash,
                incumbent_hash=None,
                horizons=(15, 30, 60),
                candidate_predictor=lambda snapshot, horizons: (
                    quality.predict_native("thermal", route.payload, snapshot, horizons)
                    if real_outcomes
                    else {horizon: by_horizon[horizon]["candidate"] for horizon in horizons}
                ),
                incumbent_predictor=lambda snapshot, horizons: (
                    quality.predict_native("thermal", None, snapshot, horizons)
                    if real_outcomes
                    else {horizon: by_horizon[horizon]["incumbent"] for horizon in horizons}
                ),
                baseline_predictor=lambda snapshot, horizons: (
                    quality.predict_native("thermal", None, snapshot, horizons, baseline=True)
                    if real_outcomes
                    else {horizon: by_horizon[horizon]["baseline"] for horizon in horizons}
                ),
            )
            assert issue is not None
            rows = issue.rows()
            assert all(row["input_snapshot"]["scope"] == scope for row in rows)
            if real_outcomes:
                initial = inputs["indoor_temperature"]
                slope = next(
                    row["predictions"]["candidate"] - initial
                    for row in rows
                    if row["horizon_minutes"] == 60
                )
                active = capability == "indoor_heating"
                error = (0.005 + 0.001 * ((index // 3) % 4)) * (-1 if (index // 6) % 2 else 1)
                async with database.sessions() as session:
                    for minutes in range(-15, 71, 5):
                        timestamp = issued + dt.timedelta(minutes=minutes)
                        session.add(
                            DeviceStatusRecord(
                                ts=timestamp,
                                device_id="audit-device",
                                space_heating_active=active,
                                mode="1",
                                operation_status=1,
                                direction="PUMP" if active else "IDLE",
                                pump_duty=1 if active else 0,
                                device_action="HEATING" if active else "IDLE",
                                defrost_active=False,
                                zone1_operation_status=1,
                                zone2_operation_status=0,
                                zone1_temp=35.0,
                                zone1_target_temp=40.0,
                                tank_target_temp=50.0,
                                outdoor_temp=float(group[0]["outdoor_bin"]),
                                tank_temp=45.0,
                                force_dhw=0,
                                force_heater=0,
                                holiday_mode=0,
                                native_policy_provenance={
                                    "schema": quality.NATIVE_POLICY_SCHEMA,
                                    "settings": inputs["native_settings"],
                                    "timer": {
                                        **inputs["timer"],
                                        "fetched_at": timestamp.isoformat(),
                                    },
                                },
                            )
                        )
                        session.add(
                            IndoorTempReading(
                                timestamp=timestamp,
                                temperature=initial + slope * minutes / 60 + error,
                                device_id="selected-room",
                                is_stale=False,
                            )
                        )
            await database.lifecycle.record_native_issues(
                route.lease, route.reference, rows, slot=index // 3
            )
            for row in rows:
                oracle[(row["issued_at"], capability, row["horizon_minutes"])] = by_horizon[
                    row["horizon_minutes"]
                ]
        route.clock[0] = route.current
        key = "scan:" + route.reference.artifact_hash
        await database.lifecycle.begin_evidence_scan(
            route.lease,
            route.reference,
            key=key,
            window_start=route.frozen_at,
            window_end=route.current,
        )
        for _page_number in range(4):
            page = await database.lifecycle.evidence_page(route.lease, key)
            outcomes = []
            if real_outcomes:
                execution = await quality.materialize_execution_page(page, now=route.current)
                assert execution["complete"] is True
                outcomes = quality.reconcile_materialized_page(
                    page, execution, now=route.current, scope=scope
                )
                assert len(outcomes) == len(page["rows"])
                assert all(outcome["status"] == "valid" for outcome in outcomes), outcomes
            for entry in () if real_outcomes else page["rows"]:
                issue = entry["issue"]
                capability = issue["input_snapshot"]["capability"]
                expected = oracle[(issue["issued_at"], capability, issue["horizon_minutes"])]
                outcomes.append(
                    {
                        "status": "valid",
                        "exclusion_reason": None,
                        "observed_at": issue["target_at"],
                        "result": {
                            **issue["predictions"],
                            "observed": expected["observed"],
                            "candidate_hash": route.reference.artifact_hash,
                            "incumbent_hash": None,
                            "capability": capability,
                            "scope": scope,
                            "outdoor_bin": expected["outdoor_bin"],
                        },
                    }
                )
            await database.lifecycle.commit_evidence_page(route.lease, key, page, outcomes)
            if page["complete"]:
                break
        materialized, complete = await database.lifecycle.decision_evidence(
            route.lease,
            route.reference,
            window_end=route.frozen_at + dt.timedelta(hours=336),
        )
        assert complete is True and len(materialized) == 168
        result = quality.qualify_prospective(
            materialized,
            component="thermal",
            candidate_hash=route.reference.artifact_hash,
            frozen_at=route.frozen_at,
            data_cutoff=route.frozen_at,
            now=route.current,
            horizons=(15, 30, 60),
            scope=scope,
            capabilities=("indoor_heating", "indoor_cooling"),
            coverage_complete=complete,
            checkpoint=0,
        )
        assert result["qualified"] is True and result["counts"]["pairs"] == 168, json.dumps(
            result, sort_keys=True
        )
        assert result["control_ready"] is False and result["ordinary_writes_allowed"] is False
        assert await database.lifecycle.claim_decision(route.lease, route.reference, 0)
        await database.lifecycle.finish_decision(route.lease, route.reference, 0, result)
        before = await database.lifecycle.read()
        artifact, _payload = module.verify_reference(route.reference)
        publication = await database.lifecycle.publish(
            route.lease,
            artifact,
            role="incumbent",
            expected_revision=before.revision,
            expected_incumbent=None,
            qualification=module.Qualification(
                route.current,
                dt.datetime.fromisoformat(result["last_valid_outcome_at"]),
                result["scope"],
            ),
        )
        assert publication.committed
        qualified = (await database.lifecycle.read()).reference("thermal")
        assert module.reference_is_qualified(qualified, now=route.current, scope=scope)
        assert set(qualified.qualification_scope["capabilities"]) == {
            "indoor_heating",
            "indoor_cooling",
        }
        from packages.ml import model_status
        from packages.core import settings_service
        from packages.core import database as database_module
        from packages.core.models import PlanActionRecord, PlanRecord
        from unittest.mock import AsyncMock

        captured_values = {
            key: spec.serialize_default() for key, spec in settings_service.SETTING_SPECS.items()
        }
        captured_values.update(
            {
                "comfort_temp_min": "18",
                "comfort_temp_target": "20.5",
                "comfort_temp_max": "25",
                "comfort_reference_sensor_id": "selected-room",
                "smartthings_device_ids": "selected-room",
                "weather_provider": "open-meteo",
                "price_provider": "manual",
                "timezone": "UTC",
                "ml_maintenance_enabled": "true",
                "seasonal_calibration_enabled": "true",
            }
        )
        monkeypatch.setattr(
            settings_service, "get_all_settings", AsyncMock(return_value=captured_values)
        )
        async with database.engine.begin() as connection:
            for model in (PlanRecord, PlanActionRecord):
                await connection.run_sync(
                    lambda current, table=model.__table__: _require_local_table(current, table)
                )
        monkeypatch.setattr(database_module, "async_session_factory", database.maker)
        dto = await model_status.get_readiness_snapshot(
            route="ur3-positive-native-dto", as_of=route.current, components=("thermal",)
        )
        indoor = dto["readiness"]["components"]["indoor_heating"]
        assert indoor["collection"]["train"] >= 20 and indoor["collection"]["holdout"] >= 3
        assert (
            indoor["fit"]["status"] == "validated"
            and indoor["fit"]["artifact_hash"] == qualified.artifact_hash
        )
        assert indoor["reporting"]["counts"]["pairs"] == 168
        assert dto["model_lifecycle"]["thermal"]["status"] == "qualified-for-reporting"
        assert dto["model_lifecycle"]["thermal"]["control_ready"] is False
        for component in ("indoor_heating", "indoor_cooling"):
            head = dto["readiness"]["components"][component]
            assert head["reporting"]["status"] == "qualified-for-reporting"
            assert head["next_step"] == "ready_for_reporting"
        legacy_scope = {
            **scope,
            "schema_fingerprint": quality.evidence_hash(
                {
                    **route.fitted["inputs"]["schemas"],
                    "input": "frozen_native_inputs_v1",
                }
            ),
        }
        assert not module.reference_is_qualified(qualified, now=route.current, scope=legacy_scope)
        restarted = await Lifecycle(database.sessions).read()
        assert (
            restarted.reference("thermal") == qualified
            and restarted.reference("thermal", "candidate") is None
        )
        assert restarted.checkpoints[key]["processed_rows"] == 168
    finally:
        await route.owner.close()
        await database.lifecycle.release(route.lease)


async def test_R13_entry_swap_after_confirmation_before_CAS_has_zero_DML(
    lifecycle_database, monkeypatch
):
    from sqlalchemy import event
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    before = await database.lifecycle.read()
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    signed_bytes = path.read_bytes()
    path.unlink()
    path.mkdir()
    with pytest.raises(module.ArtifactIntegrityError, match="Confirmed non-file artifact entry"):
        await database.lifecycle.load_committed("demand", role="candidate")

    admission_started = asyncio.Event()
    admit = asyncio.Event()
    original_lock = database.lifecycle._lock
    writes = []

    async def paused_admission(session, attempt):
        admission_started.set()
        await asyncio.wait_for(admit.wait(), timeout=5)
        await original_lock(session, attempt)

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement.split()[0].upper())

    monkeypatch.setattr(database.lifecycle, "_lock", paused_admission)
    event.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    invalidation = asyncio.create_task(
        database.lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")
    )
    try:
        await asyncio.wait_for(admission_started.wait(), timeout=5)
        path.rmdir()
        path.write_bytes(signed_bytes)
        module.verify_reference(reference)
        admit.set()
        outcome = (await asyncio.wait_for(asyncio.gather(invalidation, return_exceptions=True), 5))[
            0
        ]
        after = await database.lifecycle.read()
        print(
            json.dumps(
                {
                    "probe": "R13-confirmation-to-CAS-entry-swap",
                    "writes": writes,
                    "candidate_preserved": after.reference("demand", "candidate") == reference,
                    "revision_unchanged": after.revision == before.revision,
                    "valid_signed_entry_restored": True,
                }
            )
        )
        assert writes == [], "Entry changed after confirmation: invalidation must perform zero DML"
        assert after == before
        assert outcome is None or isinstance(
            outcome, (module.ArtifactUnavailableError, CASConflict)
        )
        assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
    finally:
        admit.set()
        if not invalidation.done():
            invalidation.cancel()
        await asyncio.gather(invalidation, return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_write)


@pytest.mark.parametrize(
    "condition", ["replaced_directory", "vanished_entry", "inaccessible_entry"]
)
async def test_R13_admission_entry_change_preserves_incumbent_and_progress(
    lifecycle_database, monkeypatch, condition
):
    from sqlalchemy import event
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    await database.lifecycle.begin_evidence_scan(
        lease,
        reference,
        key="scan:" + reference.artifact_hash,
        window_start=rows[0]["issued_at"],
        window_end=dt.datetime.now(dt.timezone.utc),
    )
    before = await database.lifecycle.read()
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    path.unlink()
    path.mkdir()
    replacement = path.with_name("replacement-directory")
    if condition == "replaced_directory":
        replacement.mkdir()
    with pytest.raises(module.ArtifactIntegrityError, match="Confirmed non-file artifact entry"):
        await database.lifecycle.load_committed("demand", role="candidate")

    admission_started = asyncio.Event()
    admit = asyncio.Event()
    original_lock = database.lifecycle._lock
    original_lstat = Path.lstat
    writes = []

    async def paused_admission(session, attempt):
        admission_started.set()
        await asyncio.wait_for(admit.wait(), timeout=5)
        await original_lock(session, attempt)

    def inaccessible_lstat(current, *arguments, **keywords):
        if current == path:
            raise PermissionError("owned admission leaf is inaccessible")
        return original_lstat(current, *arguments, **keywords)

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement.split()[0].upper())

    monkeypatch.setattr(database.lifecycle, "_lock", paused_admission)
    event.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    invalidation = asyncio.create_task(
        database.lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")
    )
    try:
        await asyncio.wait_for(admission_started.wait(), timeout=5)
        with monkeypatch.context() as fault:
            if condition == "replaced_directory":
                path.rmdir()
                replacement.replace(path)
            elif condition == "vanished_entry":
                path.rmdir()
            else:
                fault.setattr(Path, "lstat", inaccessible_lstat)
            admit.set()
            outcome = (
                await asyncio.wait_for(asyncio.gather(invalidation, return_exceptions=True), 5)
            )[0]
        after = await Lifecycle(database.sessions).read()
        assert isinstance(outcome, (module.ArtifactUnavailableError, CASConflict))
        assert writes == [] and after == before
        assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
        print(json.dumps({"probe": "R13-admission-entry-change", "case": condition, "DML": writes}))
    finally:
        admit.set()
        if not invalidation.done():
            invalidation.cancel()
        await asyncio.gather(invalidation, return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_write)


@pytest.mark.parametrize("damage", ["bytes", "directory"])
async def test_R13_damaged_candidate_invalidation_is_durable_and_fences_publication(
    lifecycle_database, damage
):
    from packages.ml.lifecycle import ArtifactIntegrityError

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    key = "scan:" + reference.artifact_hash
    await database.lifecycle.begin_evidence_scan(
        lease,
        reference,
        key=key,
        window_start=rows[0]["issued_at"],
        window_end=dt.datetime.now(dt.timezone.utc),
    )
    before = await database.lifecycle.read()
    incumbent_hash = before.reference("demand").artifact_hash
    stale_reference = reference
    artifact, _payload = verify_signed_artifact(
        reference.artifact_hash,
        component="demand",
        schema_version=reference.schema_version,
    )
    assert (
        await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=before.revision,
            expected_incumbent=incumbent_hash,
        )
    ).committed
    republished = await database.lifecycle.read()
    with pytest.raises(CASConflict, match="invalidation_reference_changed"):
        await database.lifecycle.invalidate_component(
            lease, stale_reference, "artifact_integrity_failure"
        )
    assert await database.lifecycle.read() == republished
    reference = republished.reference("demand", "candidate")
    before = republished
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    if damage == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.write_bytes(b"damaged disposable candidate")
    expected = (
        "Confirmed non-file artifact entry" if damage == "directory" else "Artifact hash mismatch"
    )
    with pytest.raises(ArtifactIntegrityError, match=expected):
        await database.lifecycle.load_committed("demand", role="candidate")
    await database.lifecycle.invalidate_component(lease, reference, "artifact_integrity_failure")
    repaired_state = await Lifecycle(database.sessions).read()
    assert repaired_state.epoch == before.epoch and repaired_state.revision > before.revision
    assert repaired_state.reference("demand", "candidate") is None
    assert repaired_state.reference("demand") == before.reference("demand")
    assert repaired_state.checkpoints[key] == before.checkpoints[key]
    assert (
        repaired_state.checkpoints["collection:" + reference.artifact_hash]
        == before.checkpoints["collection:" + reference.artifact_hash]
    )
    assert "availability:" + reference.artifact_hash not in repaired_state.checkpoints
    assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
    assert (
        repaired_state.checkpoints["invalidated:" + reference.artifact_hash]["reason"]
        == "artifact_integrity_failure"
    )
    replacement = await _artifact(database, "repair-after-invalidation")
    stale = await database.lifecycle.publish(
        lease, replacement, expected_revision=before.revision, expected_incumbent=incumbent_hash
    )
    assert stale.committed is False and stale.reason == "publication_conflict"
    fresh = await database.lifecycle.publish(
        lease,
        replacement,
        expected_revision=repaired_state.revision,
        expected_incumbent=incumbent_hash,
    )
    assert fresh.committed
    assert (await database.lifecycle.load_committed("demand", role="candidate"))[
        "fit_id"
    ] == "repair-after-invalidation"


@pytest.mark.parametrize("damage", ["hmac", "schema"])
async def test_R13_stable_authenticated_or_schema_damage_remains_fenced(
    lifecycle_database, monkeypatch, damage
):
    from sqlalchemy import event
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    if damage == "schema":
        async with database.sessions() as session:
            await session.execute(
                update(MLArtifactRecord)
                .where(MLArtifactRecord.artifact_hash == reference.artifact_hash)
                .values(schema_version="unsupported-disposable-schema")
            )
        reference = (await database.lifecycle.read()).reference("demand", "candidate")
    assert reference is not None
    before = await database.lifecycle.read()
    assert before.checkpoints is not None
    writes = []

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement.split()[0].upper())

    event.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    try:
        with monkeypatch.context() as fault:
            if damage == "hmac":
                fault.setattr(settings, "secret_key", "owned-test-wrong-signing-key")
            with pytest.raises(module.ArtifactIntegrityError, match="HMAC|schema"):
                await database.lifecycle.load_committed("demand", role="candidate")
            await database.lifecycle.invalidate_component(
                lease, reference, "artifact_integrity_failure"
            )
        after = await Lifecycle(database.sessions).read()
        assert writes == ["UPDATE", "UPDATE"]
        assert after.revision > before.revision and after.epoch == before.epoch
        assert after.fence == before.fence and after.reference("demand", "candidate") is None
        assert after.reference("demand") == before.reference("demand")
        assert after.checkpoints is not None
        assert (
            after.checkpoints["collection:" + reference.artifact_hash]
            == before.checkpoints["collection:" + reference.artifact_hash]
        )
        assert after.checkpoints["invalidated:" + reference.artifact_hash]["fence"] == lease.fence
        assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
        print(json.dumps({"probe": "R13-stable-integrity", "case": damage, "DML": writes}))
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_write)


@pytest.mark.parametrize("winner", ["publication", "invalidation"])
async def test_R13_atomic_publication_and_invalidation_have_one_fenced_winner(
    lifecycle_database, monkeypatch, winner
):
    from sqlalchemy import event

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    before = await database.lifecycle.read()
    assert before.checkpoints is not None
    payload = await database.lifecycle.load_committed("demand", role="candidate")
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    path.write_bytes(b"stable disposable corruption before concurrent publication")
    incumbent_reference = before.reference("demand")
    assert incumbent_reference is not None
    incumbent = incumbent_reference.artifact_hash
    loser = "invalidation" if winner == "publication" else "publication"
    blocked = asyncio.Event()
    admit = asyncio.Event()
    original_lock = database.lifecycle._lock
    invalidation_writes = []
    phases = []
    tasks = {}

    async def ordered_admission(session, attempt):
        current = asyncio.current_task()
        assert current is not None
        if current.get_name() == "s2-r13-" + loser:
            blocked.set()
            await asyncio.wait_for(admit.wait(), timeout=5)
        await original_lock(session, attempt)

    def atomic_republication():
        path.rename(path.with_name("quarantined-disposable-damage"))
        return publish_signed_artifact(
            payload,
            component=reference.component,
            schema_version=reference.schema_version,
            manifest=reference.manifest,
            round_trip_check=lambda restored: _exact_fit(restored, "native-fixture"),
            checkpoint=phases.append,
        )

    async def publish_replacement():
        if loser == "publication":
            blocked.set()
            await asyncio.wait_for(admit.wait(), timeout=5)
        artifact = await asyncio.to_thread(atomic_republication)
        assert artifact.artifact_hash == reference.artifact_hash
        publication = await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=before.revision,
            expected_incumbent=incumbent,
        )
        return publication, artifact

    def record_write(connection, cursor, statement, parameters, context, executemany):
        current = asyncio.current_task()
        if (
            current is not None
            and current.get_name() == "s2-r13-invalidation"
            and statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ):
            invalidation_writes.append(statement.split()[0].upper())

    def start(operation):
        coroutine = (
            publish_replacement()
            if operation == "publication"
            else database.lifecycle.invalidate_component(
                lease, reference, "artifact_integrity_failure"
            )
        )
        tasks[operation] = asyncio.create_task(coroutine, name="s2-r13-" + operation)

    monkeypatch.setattr(database.lifecycle, "_lock", ordered_admission)
    event.listen(database.engine.sync_engine, "before_cursor_execute", record_write)
    try:
        start(loser)
        await asyncio.wait_for(blocked.wait(), timeout=5)
        start(winner)
        await asyncio.wait_for(asyncio.shield(tasks[winner]), timeout=5)
        admit.set()
        publication_result, invalidation_result = await asyncio.wait_for(
            asyncio.gather(tasks["publication"], tasks["invalidation"], return_exceptions=True), 5
        )
        publication, artifact = publication_result
        after = await Lifecycle(database.sessions).read()
        assert [
            phase for phase in phases if phase in {"renamed", "directory_synced", "verified"}
        ] == ["renamed", "directory_synced", "verified"]
        assert after.reference("demand") == before.reference("demand")
        assert after.checkpoints is not None
        assert (
            after.checkpoints["collection:" + reference.artifact_hash]
            == before.checkpoints["collection:" + reference.artifact_hash]
        )
        assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
        if winner == "publication":
            assert publication.committed and isinstance(invalidation_result, CASConflict)
            assert invalidation_writes == []
            candidate = after.reference("demand", "candidate")
            assert candidate is not None and candidate.artifact_hash == artifact.artifact_hash
        else:
            assert not publication.committed and publication.reason == "publication_conflict"
            assert invalidation_result is None and invalidation_writes == ["UPDATE", "UPDATE"]
            assert after.reference("demand", "candidate") is None
        print(
            json.dumps(
                {
                    "probe": "R13-atomic-publication-race",
                    "winner": winner,
                    "DML": invalidation_writes,
                }
            )
        )
    finally:
        admit.set()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        event.remove(database.engine.sync_engine, "before_cursor_execute", record_write)


async def test_R13_real_OS_permission_denial_is_unavailable_not_integrity(lifecycle_database):
    import subprocess
    import sys

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1, incumbent_fit="healthy-comparator")
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    before = await database.lifecycle.read()
    path = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    environment = dict(os.environ)
    environment.update(
        HPO_TEST_OFFLINE="1",
        DATABASE_URL="postgresql+asyncpg://s2:s2@127.0.0.1:1/s2_refused",
        REDIS_URL="redis://127.0.0.1:1/15",
        MODEL_DIR=str(database.model_dir),
        SECRET_KEY=settings.secret_key,
        PERMISSION_REFERENCE=json.dumps(
            {
                "artifact_hash": reference.artifact_hash,
                "epoch": reference.epoch,
                "component": reference.component,
                "schema_version": reference.schema_version,
                "manifest": reference.manifest,
                "role": reference.role,
                "committed_revision": reference.committed_revision,
            }
        ),
    )
    code = """import json, os
from pathlib import Path
from packages.ml import lifecycle as module
reference = module.ArtifactReference(**json.loads(os.environ['PERMISSION_REFERENCE']))
path = Path(os.environ['MODEL_DIR']) / 'lifecycle' / (reference.artifact_hash + '.pkl')
if os.geteuid() == 0:
    os.seteuid(65534)
try:
    path.read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('The OS did not deny access')
try:
    module.verify_reference(reference)
except module.ArtifactUnavailableError as error:
    assert isinstance(error.__cause__, PermissionError), repr(error.__cause__)
else:
    raise AssertionError('Genuine PermissionError was not classified unavailable')
print(json.dumps({'probe': 'R13-real-OS-permission', 'PermissionError': True, 'unavailable': True}))
"""
    path.chmod(0o000)
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", code],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["unavailable"] is True
        print(completed.stdout.strip())
    finally:
        path.chmod(0o600)
    assert await Lifecycle(database.sessions).read() == before
    assert (await database.lifecycle.load_committed("demand"))["fit_id"] == "healthy-comparator"
    assert await database.lifecycle.load_committed("demand", role="candidate") == {
        "fit_id": "native-fixture",
        "weights": [1.25, 3.5],
    }


@pytest.mark.parametrize("case", ["valid_integers", "true", "false", "duplicate_true_one"])
async def test_R03_horizon_identity_and_declared_JSON_keys_survive_PostgreSQL(
    lifecycle_database, case
):
    from unittest.mock import AsyncMock
    from packages.ml.forecast_quality import (
        control_adjustments_for_weather,
        evaluate_live_control_gate,
        prediction_intervals_for_weather,
    )
    from tests.test_forecast_quality import _passing_horizon

    database = lifecycle_database
    horizons = [_passing_horizon(hour) for hour in (1, 3, 6, 12, 24)]
    horizons[0].update(p10_signed_error=-0.5, p90_signed_error=0.8)
    if case == "duplicate_true_one":
        horizons.append(_passing_horizon(True))
    elif case != "valid_integers":
        horizons[0]["hours"] = case == "true"
    scorecard = {
        "overall": {**_passing_horizon(0), "samples": 30},
        "horizons": horizons,
        "horizon_quality": {"1": {"status": "passed"}},
        "bias_correction": {"by_horizon_c": {"1": 0.25}},
    }

    async def load_scorecard():
        async with database.sessions() as session:
            return (
                await session.execute(
                    text("SELECT CAST(:scorecard AS jsonb)"),
                    {"scorecard": json.dumps(scorecard, allow_nan=False)},
                )
            ).scalar_one()

    restored = await load_scorecard()
    expected_type = bool if case in {"true", "false"} else int
    assert type(restored["horizons"][0]["hours"]) is expected_type
    if case == "duplicate_true_one":
        assert restored["horizons"][0]["hours"] == 1
        assert restored["horizons"][-1]["hours"] is True
    result = await evaluate_live_control_gate(
        model_metrics={},
        record_gate=lambda gate, **context: {**gate, **context},
        scorecard_loader=load_scorecard,
        get_float=AsyncMock(side_effect=lambda key: 0.5 if key.endswith("bias_c") else 0.15),
        get_int=AsyncMock(return_value=2),
        get_string=AsyncMock(return_value="off"),
    )
    async with database.sessions() as session:
        serialized = (
            await session.execute(
                text("SELECT CAST(:result AS jsonb)"),
                {"result": json.dumps(result, allow_nan=False)},
            )
        ).scalar_one()
    assert serialized["control_allowed"] is (case == "valid_integers")
    if case == "valid_integers":
        assert set(json.loads(serialized["evaluation_id"])["horizons"]) == {
            "1",
            "3",
            "6",
            "12",
            "24",
        }
        adjustments = control_adjustments_for_weather(restored, [{"temperature": 8.0}])
        intervals = prediction_intervals_for_weather(restored, [{"temperature": 8.0}])
        assert adjustments["control_allowed"] is True
        assert intervals[0]["source"] == "1h"
        assert intervals[0]["lower_offset_c"] == pytest.approx(-1.05)
    print(
        json.dumps(
            {
                "probe": "R03-PostgreSQL-horizons",
                "case": case,
                "allowed": serialized["control_allowed"],
            }
        )
    )


async def test_artifact_outage_checkpoint_is_durable_and_recovery_keeps_candidate(
    lifecycle_database, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import Mock
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1)
    await database.lifecycle.record_native_issues(lease, reference, rows, slot=0)
    before = await database.lifecycle.read()
    payload = await database.lifecycle.load_committed("demand", role="candidate")
    with monkeypatch.context() as outage:
        outage.setattr(
            module.safe_persistence,
            "verify_signed_artifact",
            Mock(side_effect=PermissionError("owned offline storage probe")),
        )
        with pytest.raises(module.ArtifactUnavailableError) as failure:
            await database.lifecycle.load_committed("demand", role="candidate")
        assert (
            await database.lifecycle.record_artifact_unavailability(lease, failure.value) is False
        )
    restarted = Lifecycle(database.sessions)
    unavailable = await restarted.read()
    key = "availability:" + reference.artifact_hash
    assert unavailable.checkpoints[key]["status"] == "unavailable"
    assert unavailable.checkpoints[key]["attempts"] == 1
    assert unavailable.references == before.references
    assert (unavailable.epoch, unavailable.revision, unavailable.fence) == (
        before.epoch,
        before.revision,
        before.fence,
    )
    assert (
        unavailable.checkpoints["collection:" + reference.artifact_hash]
        == before.checkpoints["collection:" + reference.artifact_hash]
    )
    assert await restarted.load_committed("demand", role="candidate") == payload
    await restarted.clear_artifact_unavailability(lease, reference)
    recovered = await restarted.read()
    assert recovered.checkpoints[key]["status"] == "available"
    assert recovered.references == before.references and recovered.revision == before.revision
    assert (
        recovered.checkpoints["collection:" + reference.artifact_hash]
        == before.checkpoints["collection:" + reference.artifact_hash]
    )
    with pytest.raises(LeaseLost):
        await restarted.record_artifact_unavailability(
            replace(lease, fence=lease.fence - 1), failure.value
        )
    assert (await restarted.read()).checkpoints == recovered.checkpoints


async def test_cycle_retention_is_bounded_and_preserves_protected_evidence(
    lifecycle_database, monkeypatch
):
    from packages.ml import lifecycle as module

    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 1)
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(days=100)
    protected = dict(
        rows[0],
        issued_at=old,
        target_at=old + dt.timedelta(hours=1),
        device_day=old.date(),
        created_at=old,
    )
    await database.lifecycle.record_native_issues(lease, reference, [protected], slot=0)
    unreferenced = await _artifact(database, "expired-unreferenced-fit")
    async with database.sessions() as session:
        session.add(
            MLArtifactRecord(
                artifact_hash=unreferenced.artifact_hash,
                epoch=lease.epoch,
                component=unreferenced.component,
                schema_version=unreferenced.schema_version,
                fit_id=unreferenced.manifest["fit_id"],
                manifest=unreferenced.manifest,
                role="unreferenced",
                committed_revision=lease.revision,
                created_at=old,
            )
        )
        await session.flush()
        for index in range(6):
            stamp = now if index == 5 else old
            issue = MLForecastIssueRecord(
                **dict(
                    protected,
                    cohort_key=f"unprotected-{index}",
                    candidate_hash=unreferenced.artifact_hash,
                    issued_at=stamp,
                    target_at=stamp + dt.timedelta(hours=1),
                    device_day=stamp.date(),
                    created_at=stamp,
                )
            )
            session.add(issue)
            await session.flush()
            session.add(
                MLForecastOutcomeRecord(
                    issue_id=issue.id,
                    epoch=lease.epoch,
                    device_id=issue.device_id,
                    component=issue.component,
                    horizon_minutes=60,
                    canonical_key=issue.cohort_key,
                    status="excluded",
                    exclusion_reason="unknown_execution",
                    result={},
                )
            )
            session.add(
                MLMaintenanceRequestRecord(
                    id=str(uuid.uuid4()),
                    epoch=lease.epoch,
                    component="demand",
                    source="test",
                    dedup_key=f"retention-{index}",
                    status="queued" if index == 5 else "succeeded",
                    requested_at=old,
                    finished_at=old,
                    details={},
                )
            )
    monkeypatch.setattr(module, "RETENTION_BATCH_SIZE", 2)
    results = [await database.lifecycle.prune_evidence(lease, now=now) for _pass in range(3)]
    assert results == [
        {"outcomes": 2, "issues": 2, "requests": 2},
        {"outcomes": 2, "issues": 2, "requests": 2},
        {"outcomes": 1, "issues": 1, "requests": 1},
    ]
    async with database.sessions() as session:
        remaining = (
            (await session.execute(select(MLForecastIssueRecord.cohort_key))).scalars().all()
        )
        assert set(remaining) == {protected["cohort_key"], "unprotected-5"}
        assert (
            await session.execute(select(func.count()).select_from(MLForecastOutcomeRecord))
        ).scalar_one() == 2
        assert (
            await session.execute(select(MLMaintenanceRequestRecord.status))
        ).scalar_one() == "queued"
    artifact = database.model_dir / "lifecycle" / (reference.artifact_hash + ".pkl")
    orphan = artifact.parent / ("1" * 64 + ".pkl")
    recent = artifact.parent / ("2" * 64 + ".pkl")
    orphan.write_bytes(b"expired orphan")
    recent.write_bytes(b"grace-period orphan")
    old_file = (now - dt.timedelta(hours=25)).timestamp()
    for path in (artifact, orphan):
        os.utime(path, (old_file, old_file))
    deleted = []
    for _pass in range(2):
        deleted.extend(await database.lifecycle.prune_artifacts(lease, now=now))
    assert deleted == [orphan.name] and artifact.exists() and recent.exists()
    await database.lifecycle.set_maintenance_enabled(False)
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_evidence(lease, now=now)
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_artifacts(lease, now=now)


async def test_pagination_concurrent_progress_late_targets_and_retry(lifecycle_database):
    database = lifecycle_database
    lease, reference, rows = await _native_fixture(database, 251)
    for slot, offset in enumerate(range(0, len(rows), 100)):
        await database.lifecycle.record_native_issues(
            lease, reference, rows[offset : offset + 100], slot=slot
        )
    key = "fixture-scan"
    window_start = rows[0]["issued_at"] - dt.timedelta(hours=1)
    window_end = dt.datetime.now(dt.timezone.utc)
    checkpoint = await database.lifecycle.begin_evidence_scan(
        lease, reference, key=key, window_start=window_start, window_end=window_end
    )
    assert checkpoint["expected_rows"] == 251
    pages = await asyncio.gather(*(database.lifecycle.evidence_page(lease, key) for _ in range(2)))
    assert [entry["issue"]["id"] for entry in pages[0]["rows"]] == [
        entry["issue"]["id"] for entry in pages[1]["rows"]
    ]
    incomplete = {
        "status": "incomplete",
        "exclusion_reason": "missing_target",
        "observed_at": None,
        "result": {},
    }
    results = await asyncio.gather(
        *(
            database.lifecycle.commit_evidence_page(
                lease, key, page, [dict(incomplete) for _ in page["rows"]]
            )
            for page in pages
        ),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, CASConflict) for result in results) == 1
    seen = {entry["issue"]["id"] for entry in pages[0]["rows"]}
    while True:
        page = await database.lifecycle.evidence_page(lease, key)
        if page["checkpoint"]["complete"]:
            break
        ids = {entry["issue"]["id"] for entry in page["rows"]}
        assert not seen & ids
        seen.update(ids)
        await database.lifecycle.commit_evidence_page(
            lease, key, page, [dict(incomplete) for _ in page["rows"]]
        )
    assert len(seen) == 251
    await database.lifecycle.begin_evidence_scan(
        lease, reference, key=key, window_start=window_start, window_end=window_end
    )
    while True:
        page = await database.lifecycle.evidence_page(lease, key)
        if page["checkpoint"]["complete"]:
            break
        outcomes = [
            {
                "status": "valid",
                "exclusion_reason": None,
                "observed_at": entry["issue"]["target_at"],
                "result": {"observed": 1.1, **entry["issue"]["predictions"]},
            }
            for entry in page["rows"]
        ]
        await database.lifecycle.commit_evidence_page(lease, key, page, outcomes)
    async with database.sessions() as session:
        assert (
            await session.execute(
                select(func.count())
                .select_from(MLForecastOutcomeRecord)
                .where(MLForecastOutcomeRecord.status == "valid")
            )
        ).scalar_one() == 251
        assert (
            await session.execute(select(func.count()).select_from(MLForecastIssueRecord))
        ).scalar_one() == 251


async def test_concurrent_init_keeps_one_epoch(lifecycle_database):
    database = lifecycle_database
    states = await asyncio.gather(*(database.lifecycle.initialize() for _ in range(8)))
    assert len({state.epoch for state in states}) == 1
    assert len({state.revision for state in states}) == 1
    assert all(not state.maintenance_enabled for state in states)
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLLifecycleStateRecord))
        ).scalar_one() == 1
        assert (
            await session.execute(
                text(
                    "SELECT cycle FROM pg_sequences WHERE schemaname=current_schema() AND sequencename='ml_lifecycle_token_seq'"
                )
            )
        ).scalar_one() is False


async def test_two_maintenance_owners_contend_for_one_lease(lifecycle_database):
    database = lifecycle_database
    await database.lifecycle.initialize()
    await database.lifecycle.set_maintenance_enabled(True)
    owners = ["poller-" + uuid.uuid4().hex for _ in range(2)]
    claims = await asyncio.gather(*(database.lifecycle.acquire(owner) for owner in owners))
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    loser = owners[claims.index(None)]
    assert (await database.lifecycle.read()).lease_owner == winner.owner
    assert await database.lifecycle.acquire(loser) is None
    await database.lifecycle.release(winner)
    successor = await database.lifecycle.acquire(loser)
    assert successor is not None and successor.epoch == winner.epoch
    assert successor.fence > winner.fence
    with pytest.raises(LeaseLost):
        await database.lifecycle.renew(winner)
    await database.lifecycle.release(successor)


async def test_concurrent_publication_has_one_CAS_winner(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database, demand=True)
    artifacts = [await _artifact(database, "fit-1"), await _artifact(database, "fit-2")]
    results = await asyncio.gather(
        *(
            database.lifecycle.publish(
                lease, artifact, expected_revision=lease.revision, expected_incumbent=None
            )
            for artifact in artifacts
        )
    )
    assert sum(result.committed for result in results) == 1
    winner = artifacts[next(index for index, result in enumerate(results) if result.committed)]
    state = await database.lifecycle.read()
    assert state.reference("demand", "candidate").artifact_hash == winner.artifact_hash
    assert await database.lifecycle.load_committed("demand", role="candidate") == {
        "fit_id": winner.manifest["fit_id"],
        "weights": [1.25, 3.5],
    }
    async with database.sessions() as session:
        assert (
            await session.execute(select(func.count()).select_from(MLArtifactRecord))
        ).scalar_one() == 1
    assert all(artifact.path.exists() for artifact in artifacts)


async def test_lease_loss_fences_late_worker(lifecycle_database):
    database = lifecycle_database
    old = await _owner(database, demand=True)
    async with database.sessions() as session:
        await session.execute(
            update(MLLifecycleStateRecord).values(
                lease_expires_at=func.clock_timestamp() - dt.timedelta(seconds=1),
            )
        )
    current = await database.lifecycle.acquire("replacement-worker")
    assert current is not None and current.fence > old.fence and current.revision > old.revision
    stale = await database.lifecycle.publish(
        old,
        await _artifact(database, "late-fit"),
        expected_revision=old.revision,
        expected_incumbent=None,
    )
    assert not stale.committed and stale.reason == "lease_lost"
    with pytest.raises(LeaseLost):
        await database.lifecycle.renew(old)
    renewed = await database.lifecycle.renew(current)
    assert renewed.fence == current.fence
    result = await database.lifecycle.publish(
        renewed,
        await _artifact(database, "current-fit"),
        expected_revision=current.revision,
        expected_incumbent=None,
    )
    assert result.committed


@pytest.mark.parametrize(
    "stage",
    [
        "before_file",
        "file_synced",
        "renamed",
        "directory_synced",
        "before_commit",
        "after_commit",
        "lost_ack",
    ],
)
async def test_crash_before_file_after_file_before_commit_after_commit_and_lost_ack(
    lifecycle_database, stage
):
    database = lifecycle_database
    lease = await _owner(database, demand=stage != "before_file")
    installed = []
    if stage == "before_file":
        with pytest.raises(LifecycleUnavailable):
            await database.lifecycle.load_committed("demand", role="candidate")
        assert not list(database.model_dir.rglob("*.pkl"))
        return
    if stage in {"file_synced", "renamed", "directory_synced"}:

        def crash(checkpoint):
            if checkpoint == stage:
                raise OSError("injected publication interruption")

        with pytest.raises(OSError, match="injected publication interruption"):
            await _artifact(database, "interrupted-fit", checkpoint=crash)
        with pytest.raises(LifecycleUnavailable):
            await database.lifecycle.load_committed("demand", role="candidate")
        assert len(list(database.model_dir.rglob("*.pkl"))) == int(stage != "file_synced")
        return
    artifact = await _artifact(database, "commit-fit")
    fired = False

    @asynccontextmanager
    async def interrupted_sessions():
        nonlocal fired
        async with database.maker() as session:
            try:
                yield session
                if not fired:
                    fired = True
                    if stage == "before_commit":
                        await session.rollback()
                    else:
                        await session.commit()
                    raise OSError("injected commit acknowledgement loss")
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    if stage in {"before_commit", "lost_ack"}:
        lifecycle = Lifecycle(interrupted_sessions)
        result = await lifecycle.publish(
            lease,
            artifact,
            expected_revision=lease.revision,
            expected_incumbent=None,
            on_committed=installed.append,
        )
        assert result.readback
        assert result.committed is (stage == "lost_ack")
        assert len(installed) == int(stage == "lost_ack")
    else:

        def after_commit(reference):
            raise OSError("injected post-commit process interruption")

        with pytest.raises(OSError, match="post-commit"):
            await database.lifecycle.publish(
                lease,
                artifact,
                expected_revision=lease.revision,
                expected_incumbent=None,
                on_committed=after_commit,
            )
    restarted = Lifecycle(database.sessions)
    if stage == "before_commit":
        with pytest.raises(LifecycleUnavailable):
            await restarted.load_committed("demand", role="candidate")
    else:
        assert (await restarted.load_committed("demand", role="candidate"))[
            "fit_id"
        ] == "commit-fit"
    assert artifact.path.exists()


async def test_pruning_races_and_reset_never_recreate_epoch(lifecycle_database):
    database = lifecycle_database
    lease = await _owner(database, demand=True)
    artifact = await _artifact(database, "racing-fit")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).timestamp()
    os.utime(artifact.path, (old, old))
    enumerated = asyncio.Event()
    resume = asyncio.Event()

    class PausedPruner(Lifecycle):
        async def _lock(self, session, current_lease):
            enumerated.set()
            await resume.wait()
            await super()._lock(session, current_lease)

    task = asyncio.create_task(PausedPruner(database.sessions).prune_artifacts(lease))
    try:
        await asyncio.wait_for(enumerated.wait(), timeout=10)
        committed = await database.lifecycle.publish(
            lease,
            artifact,
            expected_revision=lease.revision,
            expected_incumbent=None,
        )
        assert committed.committed
    finally:
        resume.set()
        deleted = await asyncio.wait_for(task, timeout=10)
    assert deleted == [] and artifact.path.exists()
    await database.lifecycle.retain_artifact(
        lease,
        artifact.artifact_hash,
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
    )
    before = await database.lifecycle.read()
    async with database.sessions() as session:
        reset = await invalidate_lifecycle(session)
    after = await database.lifecycle.initialize()
    assert reset["epoch"] == after.epoch == before.epoch
    assert after.revision > before.revision and after.fence > before.fence
    assert not after.references and after.lease_owner is None and not after.maintenance_enabled
    with pytest.raises(LifecycleUnavailable):
        await database.lifecycle.load_committed("demand", role="candidate")
    with pytest.raises(LeaseLost):
        await database.lifecycle.prune_artifacts(lease)
    assert artifact.path.exists()
    await database.lifecycle.set_maintenance_enabled(True)
    current = await database.lifecycle.acquire("new-owner")
    assert current is not None
    assert await database.lifecycle.prune_artifacts(current) == []
    assert artifact.path.exists()


async def test_migration_033_empty_downgrade_and_populated_refusal(lifecycle_database):
    database = lifecycle_database
    async with database.engine.begin() as connection:
        await connection.run_sync(lambda current: _run_migration(current, "downgrade"))
        assert (
            await connection.execute(
                text("SELECT to_regclass(current_schema() || '.ml_lifecycle_state')")
            )
        ).scalar_one() is None
        await connection.run_sync(lambda current: _run_migration(current, "upgrade"))
    state = await database.lifecycle.initialize()
    with pytest.raises(RuntimeError, match="ml_lifecycle_retained"):
        async with database.engine.begin() as connection:
            await connection.run_sync(lambda current: _run_migration(current, "downgrade"))
    retained = await database.lifecycle.read()
    assert (
        retained.epoch == state.epoch
        and retained.revision == state.revision
        and retained.fence == state.fence
    )
    assert retained.maintenance_enabled is False


async def test_restart_and_rollback_preserve_restore_and_embargo(lifecycle_database):
    from unittest.mock import AsyncMock
    from packages.core.models import PlanActionRecord, PlanRecord
    from packages.core.plan_lifecycle import supersede_active_plan
    from packages.core.safety_reverts import dhw_embargoed, unresolved_revert_summary
    from packages.core.services.aquarea import AquareaWrapper

    database = lifecycle_database
    now = dt.datetime.now(dt.timezone.utc)
    before = await database.lifecycle.initialize()
    async with database.engine.begin() as connection:
        await connection.run_sync(
            lambda current: _require_local_table(current, PlanRecord.__table__)
        )
        await connection.run_sync(
            lambda current: _require_local_table(current, PlanActionRecord.__table__)
        )
    async with database.sessions() as session:
        session.add(
            PlanRecord(
                id=1,
                horizon_start=now,
                horizon_end=now + dt.timedelta(days=1),
                plan_json='{"actions": []}',
                status="active",
            )
        )
        session.add(
            PlanActionRecord(
                id=11762,
                plan_id=1,
                device_id="fixture-device",
                action_type="force_dhw_on",
                payload_json="{}",
                status="executed",
                scheduled_ts=now - dt.timedelta(hours=1),
            )
        )
        await session.flush()
        session.add(
            PlanActionRecord(
                id=11763,
                plan_id=1,
                device_id="fixture-device",
                action_type="force_dhw_off",
                payload_json="{}",
                status="pending",
                reverts_action_id=11762,
                scheduled_ts=now - dt.timedelta(minutes=30),
            )
        )
    async with database.sessions() as session:
        await supersede_active_plan(session, reason="guarded_rollback_fixture")
    for _phase in ("restart", "guarded_rollback"):
        state = await Lifecycle(database.sessions).read()
        assert state.epoch == before.epoch
        assert state.maintenance_enabled is False
        async with database.sessions() as session:
            restore = await session.get(PlanActionRecord, 11763)
            source = await session.get(PlanActionRecord, 11762)
            obligations = await unresolved_revert_summary(session, now=now)
        assert restore.status == "pending" and restore.reverts_action_id == source.id
        assert source.status == "executed"
        assert obligations["count"] == 1
        assert dhw_embargoed([restore], "fixture-device") is True
        wrapper = AquareaWrapper(read_only=False)
        wrapper._preflight_control_check = AsyncMock()
        with pytest.raises(PermissionError, match="observe-only build"):
            await wrapper.force_dhw(True)
        wrapper._preflight_control_check.assert_not_awaited()
