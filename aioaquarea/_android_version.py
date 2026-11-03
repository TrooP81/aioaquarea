"""Private, fail-closed Google Play lookup for the Android app version.

Requests to Panasonic are sent as the Android app (``x-app-type: 1``), and the
API only accepts the version currently published for that platform. This
module is the single parser and bounded lookup shared by the SDK and the
version-update workflow (``python -m aioaquarea._android_version``).

Supported public page structure (documented from the 2026-10-28 research
extracts; not a complete page capture):

* exactly one ``<link rel="canonical">`` URL, which must be
  ``https://play.google.com/store/apps/details`` with the single query value
  ``id=com.panasonic.ACCsmart`` and optionally one ``hl`` (package identity /
  details-page proof; consent or interstitial pages fail here);
* exactly one whole ``AF_initDataCallback`` script for ``ds:5``, with a JSON
  data array and the version at ``data[1][2][140][0][0][0]``. This location and
  wrapper were extracted from the authorized public GET on 2026-10-28.

Exactly one distinct version must be found, made of three ASCII decimal
components of 1-6 digits that are not all zero. Anything missing, malformed,
conflicting or unsupported raises :class:`AndroidVersionLookupError`; there is
no default, stale, iOS or "highest version" fallback and no JavaScript
evaluation. Error messages never contain page content.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from html.parser import HTMLParser
import json
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .const import ANDROID_APP_PACKAGE, ANDROID_APP_VERSION_URL

MAX_BODY_BYTES = 8 * 1024 * 1024
LOOKUP_TIMEOUT_SECONDS = 30.0
_READ_CHUNK = 64 * 1024
_MAX_SCRIPTS = 4096
_CALLBACK_RE = re.compile(
    r"\AAF_initDataCallback\(\{key: 'ds:5', hash: '[0-9]{1,6}', data:"
)
_CALLBACK_SUFFIX = ", sideChannel: {}});"
_VERSION_RE = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", re.ASCII)
_PLAY_HOST = "play.google.com"
_DETAILS_PATH = "/store/apps/details"


class AndroidVersionLookupError(Exception):
    """Sanitized lookup failure; ``reason`` is a fixed, content-free token."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Android app version lookup failed: {reason}")


class SendBudgetExceeded(AndroidVersionLookupError, aiohttp.ClientError):
    """A connector refused a physical send beyond its budget or allowlist."""


class SendBudgetConnector(aiohttp.TCPConnector):
    """TCP connector that bounds *physical* sends, including aiohttp retries.

    ``ClientSession`` may transparently resend an idempotent request after a
    server disconnect. Every physical attempt must obtain a connection through
    :meth:`connect`, so counting and refusing here bounds real sends rather
    than logical API calls. Connections are never reused (``force_close``).
    """

    def __init__(
        self,
        *,
        max_sends: int,
        allowed_hosts: frozenset[str] | set[str] | tuple[str, ...],
        per_target_limits: dict[tuple[str, str], int] | None = None,
        dispatch_fence: bool = False,
        **kwargs: Any,
    ):
        kwargs["force_close"] = True
        kwargs.setdefault("limit", 1)
        super().__init__(**kwargs)
        self.max_sends = max_sends
        self.allowed_hosts = frozenset(allowed_hosts)
        self.per_target_limits = dict(per_target_limits or {})
        self.sends = 0
        self.target_sends: dict[tuple[str, str], int] = {}
        self.host_sends: dict[str, int] = {}
        self.refused = 0
        self.dispatch_fence = dispatch_fence
        self._dispatch = ContextVar("owned_dispatch", default=None)
        self._attempted_dispatches: set[object] = set()

    @contextmanager
    def dispatch(self):
        """One owned logical request; internal retries retain the same token.

        Only the probe adapter opens this scope, outside session.request().
        Unscoped callers share one lifetime token and cannot reset the fence.
        No aiohttp private flags or caller-owned SDK sessions are modified.
        """
        if self._dispatch.get() is not None:
            raise SendBudgetExceeded("nested_dispatch")
        token = self._dispatch.set(object())
        try:
            yield
        finally:
            self._dispatch.reset(token)

    async def connect(self, req, traces, timeout):  # type: ignore[override]
        host = (req.url.host or "").lower()
        target = (host, req.url.path)
        if host not in self.allowed_hosts:
            self.refused += 1
            raise SendBudgetExceeded("forbidden_host")
        dispatch = self._dispatch.get()
        if self.dispatch_fence and dispatch in self._attempted_dispatches:
            self.refused += 1
            raise SendBudgetExceeded("dispatch_replay")
        if self.sends >= self.max_sends:
            self.refused += 1
            raise SendBudgetExceeded("send_budget")
        limit = self.per_target_limits.get(target)
        if limit is not None and self.target_sends.get(target, 0) >= limit:
            self.refused += 1
            raise SendBudgetExceeded("target_send_budget")
        self.sends += 1
        if self.dispatch_fence:
            self._attempted_dispatches.add(dispatch)
        self.host_sends[host] = self.host_sends.get(host, 0) + 1
        self.target_sends[target] = self.target_sends.get(target, 0) + 1
        return await super().connect(req, traces, timeout)


def validate_version(value: object) -> str:
    """Return ``value`` if it is a supported version string, else raise."""

    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise AndroidVersionLookupError("invalid_version")
    if all(int(part) == 0 for part in value.split(".")):
        raise AndroidVersionLookupError("invalid_version")
    return value


class _PageScanner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical: list[str] = []
        self.scripts: list[str] = []
        self._in_script = False
        self._script_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            values = dict(attrs)
            rel = (values.get("rel") or "").lower().split()
            if "canonical" in rel:
                self.canonical.append(values.get("href") or "")
        elif tag == "script":
            if len(self.scripts) >= _MAX_SCRIPTS:
                raise AndroidVersionLookupError("unsupported_structure")
            self._in_script = True
            self._script_parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self.scripts.append("".join(self._script_parts))
            self._in_script = False
            self._script_parts = []

    def handle_data(self, data):
        if self._in_script:
            self._script_parts.append(data)


def _require_package_proof(canonical: list[str]) -> None:
    if len(canonical) != 1:
        raise AndroidVersionLookupError("package_not_proven")
    try:
        parsed = urlsplit(canonical[0])
        port = parsed.port
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AndroidVersionLookupError("package_not_proven") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != _PLAY_HOST
        or port is not None
        or parsed.netloc != _PLAY_HOST
        or "#" in canonical[0]
        or parsed.username
        or parsed.password
        or parsed.path != _DETAILS_PATH
        or query.get("id") != [ANDROID_APP_PACKAGE]
        or not set(query) <= {"id", "hl"}
        or len(query.get("hl", [""])) != 1
        or (
            "hl" in query
            and not re.fullmatch(r"[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?", query["hl"][0])
        )
    ):
        raise AndroidVersionLookupError("package_not_proven")


def _reject_json_constant(_value):
    raise ValueError


def parse_android_version(page: str | bytes) -> str:
    """Parse the Android version from a Google Play details page (pure)."""

    if isinstance(page, (bytes, bytearray)):
        if len(page) > MAX_BODY_BYTES:
            raise AndroidVersionLookupError("body_too_large")
        try:
            page = bytes(page).decode("utf-8")
        except UnicodeDecodeError:
            raise AndroidVersionLookupError("undecodable_body") from None
    if not isinstance(page, str):
        raise AndroidVersionLookupError("unsupported_structure")
    try:
        size = len(page.encode("utf-8"))
    except UnicodeError:
        raise AndroidVersionLookupError("undecodable_body") from None
    if size > MAX_BODY_BYTES:
        raise AndroidVersionLookupError("body_too_large")

    scanner = _PageScanner()
    try:
        scanner.feed(page)
        scanner.close()
    except AndroidVersionLookupError:
        raise
    except Exception:  # noqa: BLE001 - parser failure is an unsupported page
        raise AndroidVersionLookupError("unsupported_structure") from None
    _require_package_proof(scanner.canonical)

    values = []
    for script in scanner.scripts:
        match = _CALLBACK_RE.match(script)
        if not match:
            continue
        try:
            data, end = json.JSONDecoder(
                parse_constant=_reject_json_constant
            ).raw_decode(script, match.end())
            if (
                script[end:] != _CALLBACK_SUFFIX
                or not isinstance(data, list)
                or len(data) != 3
            ):
                raise ValueError
            value = data
            for index in (1, 2, 140, 0, 0, 0):
                if not isinstance(value, list):
                    raise ValueError
                value = value[index]
        except (ValueError, IndexError, RecursionError):
            raise AndroidVersionLookupError("unsupported_structure") from None
        values.append(validate_version(value))
    if not values:
        raise AndroidVersionLookupError("version_not_found")
    if len(values) != 1:
        raise AndroidVersionLookupError("conflicting_versions")
    return values[0]


async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await response.content.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise AndroidVersionLookupError("body_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _lookup(url: str, connector: SendBudgetConnector) -> str:
    async with aiohttp.ClientSession(
        connector=connector,
        connector_owner=True,
        timeout=aiohttp.ClientTimeout(total=LOOKUP_TIMEOUT_SECONDS),
        trust_env=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        auto_decompress=True,
    ) as session:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise AndroidVersionLookupError(
                    "rate_limited" if response.status == 429 else "unexpected_status"
                )
            if response.content_type != "text/html":
                raise AndroidVersionLookupError("unsupported_content_type")
            body = await _read_bounded(response)
    return parse_android_version(body)


async def _bounded_lookup(url: str, connector: SendBudgetConnector) -> str:
    try:
        return await asyncio.wait_for(_lookup(url, connector), LOOKUP_TIMEOUT_SECONDS)
    except AndroidVersionLookupError:
        raise
    except asyncio.TimeoutError:
        raise AndroidVersionLookupError("timeout") from None
    except aiohttp.ClientError:
        raise AndroidVersionLookupError("transport_error") from None
    except OSError:
        raise AndroidVersionLookupError("transport_error") from None


def lookup_connector() -> SendBudgetConnector:
    """Connector for the single permitted Google Play physical send."""

    return SendBudgetConnector(max_sends=1, allowed_hosts=frozenset({_PLAY_HOST}))


async def fetch_android_app_version() -> str:
    """Fetch and validate the public Android version (one physical send max).

    Credential-free, no redirects, HTTP 200 ``text/html`` only, at most
    8 MiB decompressed and 30 seconds for the whole fetch including the body.
    Cancellation propagates unchanged.
    """

    return await _bounded_lookup(ANDROID_APP_VERSION_URL, lookup_connector())


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aioaquarea._android_version",
        description="Print the validated Google Play Android app version.",
    )
    parser.add_argument(
        "--html-file",
        help="Parse a saved page offline instead of fetching (tests only).",
    )
    args = parser.parse_args(argv)
    try:
        if args.html_file:
            with open(args.html_file, "rb") as handle:
                data = handle.read(MAX_BODY_BYTES + 1)
            version = parse_android_version(data)
        else:
            version = asyncio.run(fetch_android_app_version())
    except AndroidVersionLookupError as exc:
        print(f"android_version_lookup_failed: {exc.reason}", file=sys.stderr)
        return 2
    except OSError:
        print("android_version_lookup_failed: input_unreadable", file=sys.stderr)
        return 2
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
