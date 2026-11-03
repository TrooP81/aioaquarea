"""Offline tests for the private Google Play Android version parser/lookup.

All network traffic is synthetic and local (127.0.0.1). Fixtures under
``tests/fixtures/android_version`` are RECONSTRUCTED from the 2026-10-28
research extract, not captured pages.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import pathlib

import aiohttp
import pytest

from aioaquarea import _android_version as av
from aioaquarea._android_version import (
    AndroidVersionLookupError,
    SendBudgetConnector,
    parse_android_version,
    validate_version,
)
from aioaquarea.const import ANDROID_APP_PACKAGE, ANDROID_APP_VERSION_URL

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "android_version"
SENTINEL = "SENTINEL-PAGE-CONTENT-7f3a"
CANONICAL = (
    '<link rel="canonical" href="https://play.google.com/store/apps/details'
    '?id=com.panasonic.ACCsmart&amp;hl=en">'
)


def page(*scripts: str, canonical: str = CANONICAL, extra: str = "") -> str:
    body = "".join(f"<script>{script}</script>" for script in scripts)
    return f"<html><head>{canonical}</head><body>{extra}{body}</body></html>"


def callback(version="5.1.0"):
    data = [None, [None, None, [None] * 140 + [[[[version]]]]], None]
    return (
        "AF_initDataCallback({key: 'ds:5', hash: '10', data:"
        + json.dumps(data, separators=(",", ":"))
        + ", sideChannel: {}});"
    )


def reason_of(call, *args) -> str:
    with pytest.raises(AndroidVersionLookupError) as exc:
        call(*args)
    assert SENTINEL not in str(exc.value)
    return exc.value.reason


class TestConstants:
    def test_fixed_google_play_source(self):
        assert ANDROID_APP_PACKAGE == "com.panasonic.ACCsmart"
        assert ANDROID_APP_VERSION_URL == (
            "https://play.google.com/store/apps/details"
            "?id=com.panasonic.ACCsmart&hl=en"
        )

    def test_sdk_sources_reference_no_apple_lookup(self):
        root = pathlib.Path(av.__file__).parent
        for name in ("_android_version.py", "auth.py", "const.py", "core.py"):
            text = (root / name).read_text(encoding="utf-8").lower()
            assert "apple.com" not in text
            assert "itunes" not in text


class TestParser:
    def test_reconstructed_fixture_yields_extract_version(self):
        data = (FIXTURES / "play_details_reconstructed.html").read_bytes()
        assert parse_android_version(data) == "5.1.0"

    @pytest.mark.parametrize(
        "version", ["5.1.0", "5.1.1", "10.0.0", "6.12.345", "999999.999999.999999"]
    )
    def test_present_and_future_versions(self, version):
        assert parse_android_version(page(callback(version))) == version

    def test_repeated_identical_data_is_ambiguous(self):
        html = page(callback(), callback())
        assert reason_of(parse_android_version, html) == "conflicting_versions"

    def test_conflicting_versions_fail(self):
        html = page(callback(), callback("5.1.1"))
        assert reason_of(parse_android_version, html) == "conflicting_versions"

    def test_missing_version_fails(self):
        assert reason_of(parse_android_version, page("a=1;")) == "version_not_found"

    def test_version_outside_script_is_ignored(self):
        html = page(
            "a=1;",
            extra=f'<!-- [[["5.1.0"]] --><p>[[["5.1.1"]] {SENTINEL}</p>',
        )
        assert reason_of(parse_android_version, html) == "version_not_found"

    def test_non_version_arrays_are_ignored(self):
        html = page('a=[[["Panasonic"]],[[["9.9.9"]],[[["v2"]];', callback())
        assert parse_android_version(html) == "5.1.0"

    def test_labeled_or_loose_version_text_is_not_parsed(self):
        html = page('a="Version 5.1.1"; b="5.1.2";')
        assert reason_of(parse_android_version, html) == "version_not_found"

    @pytest.mark.parametrize(
        "value",
        [
            "0.0.0",
            "00.000.0",
            "5.1",
            "5.1.0.1",
            "1234567.1.0",
            "5..0",
            ".5.1",
            "\uff15.1.0",  # fullwidth digit five
            "\u0665.\u0661.\u0660",  # Arabic-Indic digits
            "5.1.\u0660",
        ],
    )
    def test_numeric_like_invalid_versions_fail_closed(self, value):
        html = page(callback(value))
        assert reason_of(parse_android_version, html) == "invalid_version"

    def test_invalid_candidate_beside_valid_one_still_fails(self):
        html = page(callback(), callback("0.0.0"))
        assert reason_of(parse_android_version, html) == "invalid_version"

    @pytest.mark.parametrize(
        "canonical",
        [
            "",
            '<link rel="canonical" href="https://play.google.com/store/apps/details'
            '?id=com.panasonic.jp.ACCsmart">',
            '<link rel="canonical" href="http://play.google.com/store/apps/details'
            '?id=com.panasonic.ACCsmart">',
            '<link rel="canonical" href="https://play.google.com.evil.test/store/apps/'
            'details?id=com.panasonic.ACCsmart">',
            '<link rel="canonical" href="https://user@play.google.com/store/apps/'
            'details?id=com.panasonic.ACCsmart">',
            '<link rel="canonical" href="https://play.google.com:444/store/apps/'
            'details?id=com.panasonic.ACCsmart">',
            '<link rel="canonical" href="https://play.google.com/store/apps/'
            'details?id=com.panasonic.ACCsmart&amp;id=other">',
            '<link rel="canonical" href="https://play.google.com/store/apps/'
            'details?id=com.panasonic.ACCsmart&amp;gl=US">',
            '<link rel="canonical" href="https://play.google.com/store/search'
            '?q=com.panasonic.ACCsmart">',
            '<link rel="canonical" href="https://play.google.com/store/apps/details'
            '?id=com.panasonic.ACCsmart"><link rel="canonical" '
            'href="https://play.google.com/store/apps/details?id=other">',
        ],
    )
    def test_package_identity_must_be_proven(self, canonical):
        html = page('a=[[["5.1.0"]];', canonical=canonical)
        assert reason_of(parse_android_version, html) == "package_not_proven"

    def test_duplicate_identical_canonical_is_ambiguous(self):
        html = page(callback(), canonical=CANONICAL + CANONICAL)
        assert reason_of(parse_android_version, html) == "package_not_proven"

    def test_consent_interstitial_fails(self):
        data = (FIXTURES / "consent_interstitial_reconstructed.html").read_bytes()
        assert reason_of(parse_android_version, data) == "package_not_proven"

    def test_oversize_body_fails(self):
        data = page('a=[[["5.1.0"]];').encode() + b" " * av.MAX_BODY_BYTES
        assert reason_of(parse_android_version, data) == "body_too_large"

    def test_undecodable_body_fails(self):
        data = page('a=[[["5.1.0"]];').encode() + b"\xff\xfe"
        assert reason_of(parse_android_version, data) == "undecodable_body"

    @pytest.mark.parametrize(
        "script",
        [
            '// [[["5.1.0"]]\n',
            '/* [[["5.1.0"]] */',
            "const irrelevant = '[[[\"5.1.0\"]]';",
            'x=[[["5.1.0"]]];',
        ],
    )
    def test_non_data_script_substrings_fail(self, script):
        assert reason_of(parse_android_version, page(script)) == "version_not_found"

    @pytest.mark.parametrize(
        "transform",
        [
            lambda s: "// " + s,
            lambda s: "/* " + s + " */",
            lambda s: json.dumps(s),
            lambda s: s.replace("'ds:5'", "'ds:10'"),
            lambda s: s.replace(", sideChannel: {}});", ", sideChannel: {}});extra()"),
            lambda s: s.replace("data:[", "data:{"),
        ],
    )
    def test_only_whole_evidenced_callback_is_supported(self, transform):
        reason_of(parse_android_version, page(transform(callback())))

    @pytest.mark.parametrize(
        "suffix",
        [
            ":SENTINEL_INVALID_PORT",
            ":999999",
            ":65536",
            ":443",
            ":",
        ],
    )
    def test_invalid_or_unsupported_canonical_ports_are_sanitized(self, suffix):
        canonical = CANONICAL.replace(
            "play.google.com/", "play.google.com" + suffix + "/"
        )
        assert (
            reason_of(parse_android_version, page(callback(), canonical=canonical))
            == "package_not_proven"
        )

    def test_fragment_rejected_locale_accepted(self):
        assert (
            parse_android_version(
                page(callback(), canonical=CANONICAL.replace("hl=en", "hl=en_US"))
            )
            == "5.1.0"
        )
        assert (
            reason_of(
                parse_android_version,
                page(
                    callback(),
                    canonical=CANONICAL.replace("hl=en", "hl=en#unsupported"),
                ),
            )
            == "package_not_proven"
        )

    def test_unicode_surrogate_is_sanitized(self):
        assert reason_of(parse_android_version, "\ud800") == "undecodable_body"

    @pytest.mark.parametrize("fragment", ["#", "#unsupported"])
    def test_all_fragment_delimiters_are_unsupported(self, fragment):
        canonical = CANONICAL.replace('hl=en">', "hl=en" + fragment + '">')
        assert (
            reason_of(parse_android_version, page(callback(), canonical=canonical))
            == "package_not_proven"
        )

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_non_json_constants_are_not_evidenced_data(self, value):
        script = callback().replace("data:[null,", "data:[" + value + ",")
        assert reason_of(parse_android_version, page(script)) == "unsupported_structure"

    @pytest.mark.parametrize(
        "data",
        [
            [],
            [None, None, None],
            [None, [None, None, []], None],
            [None, [None, None, [None] * 140 + ["5.1.0"]], None],
            [None, [None, None, [None] * 140 + [[[["irrelevant", "5.1.0"]]]]], None],
        ],
    )
    def test_only_evidenced_array_path_proves_version(self, data):
        script = (
            "AF_initDataCallback({key: 'ds:5', hash: '10', data:"
            + json.dumps(data)
            + ", sideChannel: {}});"
        )
        reason_of(parse_android_version, page(script))

    def test_unbounded_script_count_fails(self):
        html = page(*(["a=1;"] * (av._MAX_SCRIPTS + 1)))
        assert reason_of(parse_android_version, html) == "unsupported_structure"

    def test_validate_version_rejects_non_strings(self):
        assert reason_of(validate_version, None) == "invalid_version"
        assert reason_of(validate_version, 510) == "invalid_version"
        assert reason_of(validate_version, " 5.1.0") == "invalid_version"
        assert reason_of(validate_version, "5.1.0\n") == "invalid_version"


class _Server:
    """Minimal raw HTTP/1.1 server on 127.0.0.1 counting physical requests."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[bytes] = []
        self.connections = 0
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.tasks: set[asyncio.Task] = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.server.wait_closed()

    async def _serve(self, reader, writer):
        self.connections += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.requests.append(head)
        try:
            await self.handler(writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/store/apps/details?id=x&hl=en"


def respond(status=200, body=b"", content_type="text/html; charset=utf-8", extra=b""):
    async def handler(writer):
        writer.write(
            b"HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\n%s"
            b"Connection: close\r\n\r\n"
            % (status, content_type.encode(), len(body), extra)
        )
        writer.write(body)
        await writer.drain()

    return handler


async def disconnect(writer):
    return None


def local_connector(**kwargs) -> SendBudgetConnector:
    kwargs.setdefault("max_sends", 1)
    return SendBudgetConnector(allowed_hosts={"127.0.0.1"}, **kwargs)


VALID = page(callback()).encode()


class TestBoundedLookup:
    async def test_valid_page(self):
        async with _Server(respond(body=VALID)) as srv:
            assert await av._bounded_lookup(srv.url, local_connector()) == "5.1.0"
        assert len(srv.requests) == 1
        head = srv.requests[0].lower()
        assert head.startswith(b"get ")
        assert b"authorization" not in head
        assert b"cookie" not in head

    async def test_redirect_is_not_followed(self):
        target = b"Location: http://127.0.0.1:9/elsewhere\r\n"
        async with _Server(respond(302, extra=target)) as srv:
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, local_connector(max_sends=5))
        assert exc.value.reason == "unexpected_status"
        assert len(srv.requests) == 1

    @pytest.mark.parametrize(
        ("status", "reason"),
        [(404, "unexpected_status"), (500, "unexpected_status"), (429, "rate_limited")],
    )
    async def test_non_200_fails(self, status, reason):
        async with _Server(respond(status, body=VALID)) as srv:
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, local_connector())
        assert exc.value.reason == reason

    async def test_non_html_fails(self):
        async with _Server(respond(body=VALID, content_type="application/json")) as srv:
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, local_connector())
        assert exc.value.reason == "unsupported_content_type"

    async def test_decompressed_size_limit(self):
        compressed = gzip.compress(b" " * (av.MAX_BODY_BYTES + 1) + VALID)
        assert len(compressed) < 100_000
        handler = respond(body=compressed, extra=b"Content-Encoding: gzip\r\n")
        async with _Server(handler) as srv:
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, local_connector())
        assert exc.value.reason == "body_too_large"

    async def test_whole_fetch_including_body_is_time_bounded(self, monkeypatch):
        monkeypatch.setattr(av, "LOOKUP_TIMEOUT_SECONDS", 0.5)

        async def slow_body(writer):
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                b"Content-Length: 100000\r\n\r\n"
            )
            for _ in range(100):
                writer.write(b" ")
                await writer.drain()
                await asyncio.sleep(0.05)

        async with _Server(slow_body) as srv:
            loop = asyncio.get_running_loop()
            started = loop.time()
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, local_connector())
            assert loop.time() - started < 3
        assert exc.value.reason == "timeout"

    async def test_proxy_environment_is_ignored(self, monkeypatch):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        async with _Server(respond(body=VALID)) as srv:
            assert await av._bounded_lookup(srv.url, local_connector()) == "5.1.0"

    async def test_cancellation_propagates(self):
        async def hang(writer):
            await asyncio.sleep(30)

        async with _Server(hang) as srv:
            task = asyncio.ensure_future(av._bounded_lookup(srv.url, local_connector()))
            while not srv.requests:
                await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_transport_error_is_sanitized(self):
        async with _Server(respond(body=VALID)) as srv:
            url = srv.url
        with pytest.raises(AndroidVersionLookupError) as exc:
            await av._bounded_lookup(url, local_connector())
        assert exc.value.reason == "transport_error"

    async def test_fetch_uses_fixed_url_and_single_send_connector(self, monkeypatch):
        seen = {}

        async def fake(url, connector):
            seen["url"] = url
            seen["max"] = connector.max_sends
            seen["hosts"] = connector.allowed_hosts
            await connector.close()
            return "5.1.0"

        monkeypatch.setattr(av, "_bounded_lookup", fake)
        assert await av.fetch_android_app_version() == "5.1.0"
        assert seen == {
            "url": ANDROID_APP_VERSION_URL,
            "max": 1,
            "hosts": frozenset({"play.google.com"}),
        }


class TestPhysicalSends:
    """aiohttp may resend an idempotent GET after a disconnect."""

    async def test_negative_control_plain_session_resends_after_disconnect(self):
        async with _Server(disconnect) as srv:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True)
            ) as session:
                with pytest.raises(aiohttp.ClientError):
                    async with session.get(srv.url):
                        pass
        # Documents the hazard on the installed aiohttp: >1 physical send.
        assert len(srv.requests) == 2, aiohttp.__version__

    async def test_budget_connector_allows_one_physical_send(self):
        async with _Server(disconnect) as srv:
            connector = local_connector()
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, connector)
        assert exc.value.reason == "send_budget"
        assert len(srv.requests) == 1
        assert srv.connections == 1
        assert connector.sends == 1
        assert connector.refused == 1

    async def test_forbidden_host_is_never_contacted(self):
        async with _Server(respond(body=VALID)) as srv:
            connector = SendBudgetConnector(
                max_sends=1, allowed_hosts={"play.google.com"}
            )
            with pytest.raises(AndroidVersionLookupError) as exc:
                await av._bounded_lookup(srv.url, connector)
        assert exc.value.reason == "forbidden_host"
        assert srv.connections == 0

    async def test_per_target_limit(self):
        async with _Server(respond(body=VALID)) as srv:
            path = "/store/apps/details"
            connector = SendBudgetConnector(
                max_sends=5,
                allowed_hosts={"127.0.0.1"},
                per_target_limits={("127.0.0.1", path): 1},
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(srv.url) as response:
                    await response.read()
                with pytest.raises(AndroidVersionLookupError) as exc:
                    await session.get(srv.url)
        assert exc.value.reason == "target_send_budget"
        assert len(srv.requests) == 1


class TestCli:
    def test_cli_prints_only_version(self, capsys):
        path = FIXTURES / "play_details_reconstructed.html"
        assert av._main(["--html-file", str(path)]) == 0
        captured = capsys.readouterr()
        assert captured.out == "5.1.0\n"
        assert captured.err == ""

    def test_cli_failure_is_sanitized(self, tmp_path, capsys):
        bad = tmp_path / "bad.html"
        bad.write_text(page(f'a="{SENTINEL}";'), encoding="utf-8")
        assert av._main(["--html-file", str(bad)]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "android_version_lookup_failed: version_not_found\n"

    def test_cli_unreadable_input(self, tmp_path, capsys):
        assert av._main(["--html-file", str(tmp_path / "missing.html")]) == 2
        assert "input_unreadable" in capsys.readouterr().err
