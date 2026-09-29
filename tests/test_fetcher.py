from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx2
import pytest

from coffee_aggregator.http import (
    FetchDisallowed,
    FetchError,
    FetchResult,
    PoliteFetcher,
    parse_robots,
)
from coffee_aggregator.http import fetcher as fetcher_module

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


BASE = "https://shop.example.sk"
HOST = "shop.example.sk"
ROBOTS = f"{BASE}/robots.txt"

#: What `responses` used when a registration named no content type.
DEFAULT_CONTENT_TYPE = "text/plain"


@dataclass(slots=True)
class _Route:
    """One canned answer, and whether it has been handed out yet."""

    key: str
    status: int
    content: bytes
    headers: dict[str, str]
    error: Exception | None
    body_of: Callable[[httpx2.Request], str] | None
    used: bool = False


def _key(url: str) -> str:
    """Return what a registration and a request are matched on: no query."""
    parts = urlsplit(str(url))
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


class MockHTTP:
    """The network, replaced by a list of canned answers and a call log.

    ``respx`` does not intercept ``httpx2``, so interception happens where the
    fetcher builds its transport: :func:`install` swaps
    ``fetcher._build_transport`` for one that returns an
    :class:`httpx2.MockTransport` driven by this object. Registrations are
    matched the way ``responses`` matched them — on everything but the query
    string, first unused one first, the last one repeating forever — so a test
    that registers two answers for a URL still gets them in order.
    """

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self._lock = threading.Lock()
        self.calls: list[httpx2.Request] = []

    def add(  # noqa: PLR0913  (one keyword per thing a canned answer can say)
        self,
        url: str,
        *,
        body: str | bytes | None = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        content_type: str = DEFAULT_CONTENT_TYPE,
        error: Exception | None = None,
        body_of: Callable[[httpx2.Request], str] | None = None,
    ) -> None:
        """Register one answer for a URL.

        Args:
            url: The URL it answers, query string ignored.
            body: The body, as text or as the exact bytes to put on the wire.
            status: The status code.
            headers: Extra response headers.
            content_type: The Content-Type; pass one with a charset to state one.
            error: Raised instead of answering, for a transport failure.
            body_of: Called with the request to build the body, for a test that
                needs to watch when the answer is produced.
        """
        content = body.encode("utf-8") if isinstance(body, str) else (body or b"")
        with self._lock:
            self._routes.append(
                _Route(
                    key=_key(url),
                    status=status,
                    content=content,
                    headers={"Content-Type": content_type, **(headers or {})},
                    error=error,
                    body_of=body_of,
                )
            )

    def reset(self) -> None:
        """Forget every registration, the way ``http.reset()`` did."""
        with self._lock:
            self._routes.clear()

    def urls(self) -> list[str]:
        """Return every URL asked for so far, in order."""
        with self._lock:
            return [str(request.url) for request in self.calls]

    def requests_for(self, url: str) -> list[httpx2.Request]:
        """Return the requests that went to one URL, in order."""
        with self._lock:
            return [request for request in self.calls if str(request.url) == url]

    def _match(self, request: httpx2.Request) -> _Route:
        key = _key(str(request.url))
        matching = [route for route in self._routes if route.key == key]
        if not matching:
            message = f"no registered response for {request.url}"
            raise AssertionError(message)
        for route in matching:
            if not route.used:
                route.used = True
                return route
        return matching[-1]

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request, recording it first.

        Args:
            request: What the client is asking for.

        Returns:
            The registered response.

        Raises:
            Exception: Whatever ``error=`` registered, to mimic a dead host.
        """
        with self._lock:
            self.calls.append(request)
            route = self._match(request)
        if route.error is not None:
            raise route.error
        content = (
            route.body_of(request).encode("utf-8") if route.body_of is not None else route.content
        )
        return httpx2.Response(route.status, headers=route.headers, content=content)


def install(monkeypatch: pytest.MonkeyPatch) -> MockHTTP:
    """Put a :class:`MockHTTP` in front of every fetcher built from now on."""
    mock = MockHTTP()

    def build(limits: httpx2.Limits) -> httpx2.BaseTransport:
        return httpx2.MockTransport(mock.handle)

    monkeypatch.setattr(fetcher_module, "_build_transport", build)
    return mock


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> MockHTTP:
    """The network, replaced for the duration of one test."""
    return install(monkeypatch)


def record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace every wait in the fetcher with a recording no-op."""
    slept: list[float] = []
    monkeypatch.setattr(fetcher_module, "_sleep", slept.append)
    return slept


def count_acquisitions(
    fetcher: PoliteFetcher,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Record every host the limiter is acquired for."""
    acquired: list[str] = []
    real = fetcher.limiter.acquire

    def counting(host: str) -> float:
        acquired.append(host)
        return real(host)

    monkeypatch.setattr(fetcher.limiter, "acquire", counting)
    return acquired


def make_fetcher(**kwargs: Any) -> PoliteFetcher:  # noqa: ANN401  (transport knobs are heterogeneous)
    defaults: dict[str, Any] = {
        "user_agent": "coffee-aggregator/0.1.0",
        "contact": "https://example.org/contact",
        "delay_s": 0.0,
        "jitter_s": 0.0,
        "workers": 4,
        "timeout_s": 5.0,
        "retries": 2,
    }
    defaults.update(kwargs)
    return PoliteFetcher(**defaults)


def test_robots_disallow_blocks_one_url_and_allows_another(http: MockHTTP) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/public/x", body="<html>ok</html>", status=200)
    fetcher = make_fetcher()
    assert fetcher.get(f"{BASE}/public/x").text == "<html>ok</html>"
    with pytest.raises(FetchDisallowed):
        fetcher.get(f"{BASE}/private/x")


def test_missing_robots_allows_everything(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", body="a", status=200)
    assert make_fetcher().get(f"{BASE}/a").status == 200


def test_unreachable_robots_fails_closed_and_warns(
    http: MockHTTP, caplog: pytest.LogCaptureFixture
) -> None:
    http.add(ROBOTS, status=503)
    http.add(f"{BASE}/a", body="a", status=200)
    with (
        caplog.at_level(logging.WARNING, logger="coffee_aggregator.http.fetcher"),
        pytest.raises(FetchDisallowed),
    ):
        make_fetcher().get(f"{BASE}/a")
    assert any("disallowing the host" in record.getMessage() for record in caplog.records)


def test_crawl_delay_raises_the_limiter_interval(http: MockHTTP) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nCrawl-delay: 3\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/a", body="a", status=200)
    fetcher = make_fetcher(delay_s=0.01)
    fetcher.get(f"{BASE}/a")
    assert fetcher.limiter.delay_for("shop.example.sk") == 3.0


def test_user_agent_and_language_headers_are_sent(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", body="a", status=200)
    fetcher = make_fetcher()
    fetcher.get(f"{BASE}/a")
    request = http.calls[-1]
    assert request.headers["User-Agent"].startswith("coffee-aggregator/")
    assert "+https://example.org/contact" in request.headers["User-Agent"]
    assert request.headers["Accept-Language"] == "sk,cs;q=0.9,en;q=0.5"


def test_retry_on_503_then_success(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=503)
    http.add(f"{BASE}/a", body="finally", status=200)
    result = make_fetcher().get(f"{BASE}/a")
    assert result.text == "finally"
    assert result.from_cache is False


def test_permanent_failure_raises_fetch_error(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=503)
    with pytest.raises(FetchError):
        make_fetcher(retries=1).get(f"{BASE}/a")


def test_404_raises_fetch_error(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/gone", status=404)
    with pytest.raises(FetchError):
        make_fetcher().get(f"{BASE}/gone")


def test_disk_cache_round_trip_serves_304_from_disk(http: MockHTTP, tmp_path: Path) -> None:
    http.add(ROBOTS, status=404)
    http.add(
        f"{BASE}/a",
        body="cached body",
        status=200,
        headers={"ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
    )
    first = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/a")
    assert first.from_cache is False

    http.add(f"{BASE}/a", status=304)
    second = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/a")
    assert second.from_cache is True
    assert second.text == "cached body"
    assert http.calls[-1].headers["If-None-Match"] == '"v1"'
    assert http.calls[-1].headers["If-Modified-Since"]


def test_fetch_many_keeps_pacing_and_order(http: MockHTTP) -> None:
    starts: list[float] = []

    def record(request: httpx2.Request) -> str:
        starts.append(time.monotonic())
        return str(request.url)

    http.add(ROBOTS, status=404)
    for index in range(4):
        http.add(f"{BASE}/p{index}", body_of=record)

    delay = 0.05
    urls = [f"{BASE}/p{index}" for index in range(4)]
    results = make_fetcher(delay_s=delay, workers=4).fetch_many(urls)

    assert all(isinstance(result, FetchResult) for result in results)
    assert [cast("FetchResult", result).text for result in results] == urls
    # What is under test is that four workers on one host still take their turn,
    # not the accuracy of the clock: the scheduler can hand a thread back a few
    # milliseconds early, and asserting to the millisecond makes this fail for
    # reasons that have nothing to do with politeness. Without pacing the gaps
    # would be near zero, which this still catches.
    gaps = [b - a for a, b in pairwise(starts)]
    assert all(gap >= delay * 0.8 for gap in gaps), gaps


def test_fetch_many_returns_errors_in_place(http: MockHTTP) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nDisallow: /bad/\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/ok", body="ok", status=200)
    results = make_fetcher(retries=0).fetch_many([f"{BASE}/ok", f"{BASE}/bad/x"])
    first = results[0]
    assert isinstance(first, FetchResult)
    assert first.text == "ok"
    assert isinstance(results[1], FetchDisallowed)


def test_fetch_many_with_no_urls_makes_no_requests() -> None:
    assert make_fetcher().fetch_many([]) == []


def test_fetch_many_turns_an_unexpected_exception_into_a_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker blowing up on one URL must not take the whole batch with it."""
    fetcher = make_fetcher()

    boom = RuntimeError("transport went sideways")

    def explode(url: str) -> FetchResult:
        if url.endswith("/boom"):
            raise boom
        return FetchResult(url, url, 200, "ok", from_cache=False, elapsed_s=0.0)

    monkeypatch.setattr(fetcher, "get", explode)
    results = fetcher.fetch_many([f"{BASE}/ok", f"{BASE}/boom"])

    first, second = results
    assert isinstance(first, FetchResult)
    assert first.text == "ok"
    assert isinstance(second, FetchError)
    assert second.reason == "RuntimeError: transport went sideways"
    assert second.url == f"{BASE}/boom"


# --- item 6: retries never bypass the limiter --------------------------------


def test_a_retry_re_enters_the_limiter_and_waits_at_least_the_host_delay(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=503)
    http.add(f"{BASE}/a", body="finally", status=200)
    fetcher = make_fetcher(delay_s=0.25, retries=2)
    # take the robots.txt request out of the count: it is fetched once per origin
    assert fetcher.robots.can_fetch(fetcher.ua_token, f"{BASE}/a") is True

    slept = record_sleeps(monkeypatch)
    acquired = count_acquisitions(fetcher, monkeypatch)
    assert fetcher.get(f"{BASE}/a").text == "finally"

    assert acquired == [HOST, HOST]
    assert max(slept) >= 0.25


def test_the_transport_never_retries_anything_on_its_own() -> None:
    """A retry under the limiter is a retry nobody paced and nobody logged."""
    transport = cast("Any", fetcher_module._build_transport(httpx2.Limits(max_connections=1)))

    assert isinstance(transport, httpx2.HTTPTransport)
    assert transport._pool._retries == 0


def test_a_connect_error_is_retried_by_our_loop_which_re_enters_the_limiter(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """urllib3 retried these inside the adapter; the loop above does it now."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", error=httpx2.ConnectError("no route"))
    http.add(f"{BASE}/a", body="finally", status=200)
    fetcher = make_fetcher(delay_s=0.25, retries=2)
    assert fetcher.robots.can_fetch(fetcher.ua_token, f"{BASE}/a") is True

    slept = record_sleeps(monkeypatch)
    acquired = count_acquisitions(fetcher, monkeypatch)

    assert fetcher.get(f"{BASE}/a").text == "finally"
    assert acquired == [HOST, HOST]
    assert max(slept) >= 0.25


def test_a_connect_error_that_never_clears_becomes_a_fetch_error(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", error=httpx2.ConnectTimeout("timed out"))
    record_sleeps(monkeypatch)

    with pytest.raises(FetchError) as caught:
        make_fetcher(delay_s=0.0, retries=1).get(f"{BASE}/a")

    assert "ConnectTimeout" in caught.value.reason
    assert len(http.requests_for(f"{BASE}/a")) == 2


def test_a_429_waits_out_the_retry_after_header(
    http: MockHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/b", status=429, headers={"Retry-After": "2"})
    http.add(f"{BASE}/b", body="ok", status=200)
    fetcher = make_fetcher(delay_s=0.05, retries=2)
    slept = record_sleeps(monkeypatch)

    assert fetcher.get(f"{BASE}/b").text == "ok"
    assert max(slept) >= 2.0


def test_a_retry_after_date_is_read_as_seconds() -> None:
    assert fetcher_module.retry_after_seconds("2") == 2.0
    assert fetcher_module.retry_after_seconds(None) is None
    assert fetcher_module.retry_after_seconds("not a delay") is None
    assert fetcher_module.retry_after_seconds("Wed, 01 Jan 2020 00:00:00 GMT") == 0.0


# --- item 7: the crawl delay applies to the first content request ------------


def test_a_crawl_delay_defers_the_first_content_request(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots.txt itself only booked the configured 0.2 s; 3 s is what was asked."""
    http.add(
        ROBOTS,
        body="User-agent: *\nCrawl-delay: 3\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    fetcher = make_fetcher(delay_s=0.2)
    record_sleeps(monkeypatch)

    before = time.monotonic()
    assert fetcher.robots.can_fetch(fetcher.ua_token, f"{BASE}/a") is True
    booked = fetcher.limiter.next_start(HOST)

    assert booked is not None
    assert booked - before >= 3.0
    assert fetcher.limiter.delay_for(HOST) == 3.0


# --- item 8: robots.txt redirects are walked by hand -------------------------


def test_a_redirected_robots_txt_is_followed_hop_by_hop(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http.add(
        ROBOTS,
        status=301,
        headers={"Location": "https://cdn.example.org/shop-robots.txt"},
    )
    http.add(
        "https://cdn.example.org/shop-robots.txt",
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/public/x", body="ok", status=200)
    fetcher = make_fetcher()
    acquired = count_acquisitions(fetcher, monkeypatch)

    assert fetcher.get(f"{BASE}/public/x").text == "ok"
    with pytest.raises(FetchDisallowed):
        fetcher.get(f"{BASE}/private/x")

    requested = http.urls()
    assert requested[:2] == [ROBOTS, "https://cdn.example.org/shop-robots.txt"]
    # both robots hops were paced, each against its own host
    assert acquired[:3] == [HOST, "cdn.example.org", HOST]


# --- item 9: the pacing is auditable from the log ----------------------------


def test_the_debug_log_reports_the_waited_seconds_after_acquiring(
    http: MockHTTP,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", body="a", status=200)
    http.add(f"{BASE}/b", body="b", status=200)
    fetcher = make_fetcher(delay_s=0.05)
    with caplog.at_level(logging.DEBUG, logger="coffee_aggregator.http.fetcher"):
        fetcher.get(f"{BASE}/a")
        fetcher.get(f"{BASE}/b")

    lines = [
        record.getMessage() for record in caplog.records if record.getMessage().startswith("GET ")
    ]
    assert len(lines) == 2
    assert "waited" in lines[-1]
    waited = float(lines[-1].rsplit("waited ", 1)[1].rstrip("s)"))
    assert waited >= 0.04


# --- item 10: validators are filed under the final URL -----------------------


def test_a_redirected_page_sends_its_validators_to_the_final_url(
    http: MockHTTP, tmp_path: Path
) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/old", status=302, headers={"Location": "/new"})
    http.add(
        f"{BASE}/new",
        body="the real body",
        status=200,
        headers={"ETag": '"v9"'},
    )
    first = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/old")
    assert first.final_url == f"{BASE}/new"
    assert first.text == "the real body"

    http.reset()
    http.add(
        ROBOTS,
        body="User-agent: *\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/old", status=302, headers={"Location": "/new"})
    http.add(f"{BASE}/new", status=304)
    second = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/old")

    assert second.from_cache is True
    assert second.text == "the real body"
    conditional = http.requests_for(f"{BASE}/new")
    assert conditional[-1].headers["If-None-Match"] == '"v9"'
    redirecting = http.requests_for(f"{BASE}/old")
    assert "If-None-Match" not in redirecting[-1].headers


# --- item 11: a missing charset never mangles Slovak diacritics --------------


SLOVAK = "<html><body>Etiópia čerešne</body></html>"


def test_a_body_without_a_charset_is_decoded_as_utf8(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/sk", body=SLOVAK.encode(), status=200, content_type="text/html")
    result = make_fetcher().get(f"{BASE}/sk")
    assert "Etiópia čerešne" in result.text


def test_a_cp1250_body_without_a_charset_is_not_mojibake(http: MockHTTP) -> None:
    """httpx2 would decode this as UTF-8 and replace every diacritic, silently."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/sk", body=SLOVAK.encode("cp1250"), status=200, content_type="text/html")

    assert "Etiópia čerešne" in make_fetcher().get(f"{BASE}/sk").text


def test_a_declared_latin1_is_not_believed_for_a_slovak_page(http: MockHTTP) -> None:
    """RFC 2616's dropped default is what a framework sends when nobody set one."""
    http.add(ROBOTS, status=404)
    http.add(
        f"{BASE}/sk",
        body=SLOVAK.encode(),
        status=200,
        content_type="text/html; charset=iso-8859-1",
    )

    assert "Etiópia čerešne" in make_fetcher().get(f"{BASE}/sk").text


def test_the_pages_own_meta_charset_is_read_when_the_header_is_silent(http: MockHTTP) -> None:
    page = '<html><head><meta charset="windows-1250"></head><body>čerešne</body></html>'
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/sk", body=page.encode("cp1250"), status=200, content_type="text/html")

    assert "čerešne" in make_fetcher().get(f"{BASE}/sk").text


def test_a_declared_charset_is_obeyed(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(
        f"{BASE}/sk",
        body=SLOVAK.encode("cp1250"),
        status=200,
        content_type="text/html; charset=windows-1250",
    )

    assert "Etiópia čerešne" in make_fetcher().get(f"{BASE}/sk").text


# --- item 12: the robots token is logged, and a browser UA warned about ------


def test_the_robots_token_is_logged_at_startup(
    http: MockHTTP, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="coffee_aggregator.http.fetcher"):
        fetcher = make_fetcher()
    assert fetcher.ua_token == "coffee-aggregator"
    assert "coffee-aggregator" in caplog.text
    assert "robots.txt rules are matched on" in caplog.text


def test_an_explicit_token_overrides_the_derived_one() -> None:
    assert make_fetcher(ua_token="grinder-bot").ua_token == "grinder-bot"


# --- a group survives the notice a shop writes above its rules ----------------


def test_a_comment_block_does_not_swallow_the_rules_below_it() -> None:
    """RFC 9309 ends a group at the next User-agent line, not at a blank line.

    CPython's own parser stops at the blank line, which silently turns a shop's
    Disallow into permission. ebenica.sk writes exactly such a notice.
    """
    text = (
        "# a licence notice a shop puts at the top\n"
        "#\n"
        "\n"
        "User-agent: *\n"
        "\n"
        "# and another note, right above the rules\n"
        "\n"
        "Disallow: /wp-json/\n"
        "Allow: /kava/\n"
    )

    parser = parse_robots(text)

    assert not parser.can_fetch("coffee-aggregator", "https://shop.sk/wp-json/products")
    assert parser.can_fetch("coffee-aggregator", "https://shop.sk/kava/")


def test_a_later_user_agent_still_opens_its_own_group() -> None:
    """Dropping blank lines must not merge one shop's groups into each other."""
    text = "User-agent: Googlebot\nDisallow: /only-google/\n\nUser-agent: *\nDisallow: /everyone/\n"

    parser = parse_robots(text)

    assert parser.can_fetch("coffee-aggregator", "https://shop.sk/only-google/")
    assert not parser.can_fetch("coffee-aggregator", "https://shop.sk/everyone/")


# --- the retry wait is capped, and the cap ends the URL -----------------------


def test_a_retry_after_over_the_cap_gives_the_url_up_instead_of_sleeping(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An hour-long Retry-After is a shop saying go away, not a wait to take."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=429, headers={"Retry-After": "3600"})
    fetcher = make_fetcher(delay_s=0.0, retries=3, max_retry_wait_s=60.0)
    slept = record_sleeps(monkeypatch)

    with pytest.raises(FetchError) as caught:
        fetcher.get(f"{BASE}/a")

    assert "cap" in caught.value.reason
    assert max(slept, default=0.0) <= 60.0
    assert len(http.requests_for(f"{BASE}/a")) == 1


def test_a_wait_exactly_at_the_cap_is_still_taken(
    http: MockHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=429, headers={"Retry-After": "60"})
    http.add(f"{BASE}/a", body="ok", status=200)
    fetcher = make_fetcher(delay_s=0.0, retries=2, max_retry_wait_s=60.0)
    slept = record_sleeps(monkeypatch)

    assert fetcher.get(f"{BASE}/a").text == "ok"
    assert max(slept) == 60.0


def test_a_lower_cap_shortens_the_worst_case(
    http: MockHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A short-lived caller lowers the cap and a hostile host stops costing it."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=503, headers={"Retry-After": "120"})
    fetcher = make_fetcher(delay_s=0.0, retries=1, max_retry_wait_s=10.0)
    slept = record_sleeps(monkeypatch)

    with pytest.raises(FetchError):
        fetcher.get(f"{BASE}/a")

    assert max(slept, default=0.0) <= 10.0


# --- a 429 slows the whole host, not one URL ---------------------------------


def test_a_429_pushes_the_delay_onto_the_shared_limiter(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=429, headers={"Retry-After": "5"})
    http.add(f"{BASE}/a", body="ok", status=200)
    fetcher = make_fetcher(delay_s=0.01, retries=2)
    record_sleeps(monkeypatch)

    before = time.monotonic()
    assert fetcher.get(f"{BASE}/a").text == "ok"

    assert fetcher.limiter.delay_for(HOST) == 5.0
    booked = fetcher.limiter.next_start(HOST)
    assert booked is not None
    assert booked - before >= 5.0


def test_a_sibling_url_waits_out_the_delay_the_429_bought(
    http: MockHTTP,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other fifty products of that shop must not rediscover the refusal."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=429, headers={"Retry-After": "4"})
    http.add(f"{BASE}/a", body="ok", status=200)
    http.add(f"{BASE}/b", body="b", status=200)
    fetcher = make_fetcher(delay_s=0.01, retries=1)
    slept = record_sleeps(monkeypatch)

    fetcher.get(f"{BASE}/a")
    slept.clear()
    assert fetcher.get(f"{BASE}/b").text == "b"

    assert max(slept) >= 4.0


def test_a_503_over_the_cap_still_slows_the_host_before_giving_up(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=503, headers={"Retry-After": "900"})
    fetcher = make_fetcher(delay_s=0.01, retries=1, max_retry_wait_s=30.0)

    with pytest.raises(FetchError):
        fetcher.get(f"{BASE}/a")

    assert fetcher.limiter.delay_for(HOST) == 30.0


def test_a_plain_500_does_not_slow_the_whole_host(
    http: MockHTTP, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken page is not the host asking us to back off."""
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/a", status=500)
    http.add(f"{BASE}/a", body="ok", status=200)
    fetcher = make_fetcher(delay_s=0.02, retries=2)
    record_sleeps(monkeypatch)

    assert fetcher.get(f"{BASE}/a").text == "ok"
    assert fetcher.limiter.delay_for(HOST) == 0.02


# --- the connection pools are sized on the right axis ------------------------


def test_the_connection_ceiling_is_hosts_times_workers() -> None:
    """httpx2 caps the client as a whole, so the two axes multiply into one."""
    fetcher = make_fetcher(workers=3, hosts_in_flight=6)

    assert fetcher.hosts_in_flight == 6
    assert fetcher.limits.max_connections == 18
    # every one of them may stay warm: that is what pool_maxsize really bought
    assert fetcher.limits.max_keepalive_connections == 18


def test_a_single_shop_run_keeps_one_connection_per_worker() -> None:
    """A 6-host run opened 6 handshakes for 24 requests; a floor of 8 buys nothing."""
    fetcher = make_fetcher(workers=2)

    assert fetcher.limits.max_connections == 2
    assert not hasattr(fetcher_module, "_MIN_POOLS")


def test_an_idle_connection_outlives_the_gap_between_two_visits() -> None:
    """httpx2 expires one after 5 s, which is inside a polite crawler's window."""
    assert make_fetcher().limits.keepalive_expiry == fetcher_module.KEEPALIVE_EXPIRY_S
    assert fetcher_module.KEEPALIVE_EXPIRY_S >= 30.0


def test_a_disallow_rule_is_reported_as_the_shop_speaking(http: MockHTTP) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )

    with pytest.raises(FetchDisallowed) as caught:
        make_fetcher().get(f"{BASE}/private/x")

    assert caught.value.robots_reachable is True


def test_a_403_robots_txt_is_a_refusal_not_an_outage(http: MockHTTP) -> None:
    """RFC 9309 makes 401/403 mean stay out; the shop did answer."""
    http.add(ROBOTS, status=403)

    fetcher = make_fetcher()
    with pytest.raises(FetchDisallowed) as caught:
        fetcher.get(f"{BASE}/a")

    assert caught.value.robots_reachable is True
    assert fetcher.robots.unreachable_origins() == ()


def test_an_unreachable_robots_txt_is_reported_as_our_failure(http: MockHTTP) -> None:
    http.add(ROBOTS, status=503)

    fetcher = make_fetcher()
    with pytest.raises(FetchDisallowed) as caught:
        fetcher.get(f"{BASE}/a")

    assert caught.value.robots_reachable is False
    assert fetcher.robots.unreachable_origins() == (BASE,)


def test_a_connection_error_on_robots_txt_is_also_our_failure(http: MockHTTP) -> None:
    http.add(ROBOTS, error=httpx2.ConnectError("no route"))

    fetcher = make_fetcher()
    with pytest.raises(FetchDisallowed) as caught:
        fetcher.get(f"{BASE}/a")

    assert caught.value.robots_reachable is False


def test_an_unreachable_robots_txt_is_tried_again_after_the_interval(http: MockHTTP) -> None:
    """A two-second blip must not close the shop for the whole process."""
    http.add(ROBOTS, status=503)
    http.add(
        ROBOTS,
        body="User-agent: *\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    http.add(f"{BASE}/a", body="a", status=200)
    fetcher = make_fetcher(robots_retry_s=0.0)

    with pytest.raises(FetchDisallowed):
        fetcher.get(f"{BASE}/a")
    assert fetcher.robots.unreachable_origins() == (BASE,)

    assert fetcher.get(f"{BASE}/a").text == "a"
    assert fetcher.robots.unreachable_origins() == ()


def test_an_unreachable_robots_txt_is_not_refetched_within_the_interval(http: MockHTTP) -> None:
    http.add(ROBOTS, status=503)
    fetcher = make_fetcher(robots_retry_s=600.0)

    for _ in range(3):
        with pytest.raises(FetchDisallowed):
            fetcher.get(f"{BASE}/a")

    assert len(http.requests_for(ROBOTS)) == 1


def test_a_shops_own_verdict_is_never_refetched(http: MockHTTP) -> None:
    http.add(
        ROBOTS,
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )
    fetcher = make_fetcher(robots_retry_s=0.0)

    for _ in range(3):
        with pytest.raises(FetchDisallowed):
            fetcher.get(f"{BASE}/private/x")

    assert len(http.requests_for(ROBOTS)) == 1


# --- the worst case per URL is the caller's to choose ------------------------


def test_retries_bound_how_often_one_dead_url_is_asked(http: MockHTTP) -> None:
    http.add(ROBOTS, status=404)
    http.add(f"{BASE}/dead", status=503)
    fetcher = make_fetcher(delay_s=0.0, timeout_s=8.0, retries=1)

    with pytest.raises(FetchError):
        fetcher.get(f"{BASE}/dead")

    assert fetcher.timeout_s == 8.0
    assert len(http.requests_for(f"{BASE}/dead")) == 2


def test_the_inner_fetch_pool_defaults_to_two_workers() -> None:
    """Every URL of a batch is one host, so more workers only queue up."""
    fetcher = PoliteFetcher(user_agent="coffee-aggregator/0.1.0", contact="https://example.org/c")

    assert fetcher.workers == 2
