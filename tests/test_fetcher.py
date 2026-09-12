from __future__ import annotations

import logging
import time
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast

import pytest
import responses

from coffee_aggregator.http import FetchDisallowed, FetchError, FetchResult, PoliteFetcher
from coffee_aggregator.http import fetcher as fetcher_module

if TYPE_CHECKING:
    from pathlib import Path

    from requests import PreparedRequest
    from requests.adapters import HTTPAdapter

BASE = "https://shop.example.sk"
HOST = "shop.example.sk"
ROBOTS = f"{BASE}/robots.txt"


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


@responses.activate
def test_robots_disallow_blocks_one_url_and_allows_another() -> None:
    responses.add(
        responses.GET,
        ROBOTS,
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/public/x", body="<html>ok</html>", status=200)
    fetcher = make_fetcher()
    assert fetcher.get(f"{BASE}/public/x").text == "<html>ok</html>"
    with pytest.raises(FetchDisallowed):
        fetcher.get(f"{BASE}/private/x")


@responses.activate
def test_missing_robots_allows_everything() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", body="a", status=200)
    assert make_fetcher().get(f"{BASE}/a").status == 200


@responses.activate
def test_unreachable_robots_fails_closed_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    responses.add(responses.GET, ROBOTS, status=503)
    responses.add(responses.GET, f"{BASE}/a", body="a", status=200)
    with (
        caplog.at_level(logging.WARNING, logger="coffee_aggregator.http.fetcher"),
        pytest.raises(FetchDisallowed),
    ):
        make_fetcher().get(f"{BASE}/a")
    assert any("disallowing the host" in record.getMessage() for record in caplog.records)


@responses.activate
def test_crawl_delay_raises_the_limiter_interval() -> None:
    responses.add(
        responses.GET,
        ROBOTS,
        body="User-agent: *\nCrawl-delay: 3\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/a", body="a", status=200)
    fetcher = make_fetcher(delay_s=0.01)
    fetcher.get(f"{BASE}/a")
    assert fetcher.limiter.delay_for("shop.example.sk") == 3.0


@responses.activate
def test_user_agent_and_language_headers_are_sent() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", body="a", status=200)
    fetcher = make_fetcher()
    fetcher.get(f"{BASE}/a")
    request = responses.calls[-1].request
    assert request.headers["User-Agent"].startswith("coffee-aggregator/")
    assert "+https://example.org/contact" in request.headers["User-Agent"]
    assert request.headers["Accept-Language"] == "sk,cs;q=0.9,en;q=0.5"


@responses.activate
def test_retry_on_503_then_success() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", status=503)
    responses.add(responses.GET, f"{BASE}/a", body="finally", status=200)
    result = make_fetcher().get(f"{BASE}/a")
    assert result.text == "finally"
    assert result.from_cache is False


@responses.activate
def test_permanent_failure_raises_fetch_error() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", status=503)
    with pytest.raises(FetchError):
        make_fetcher(retries=1).get(f"{BASE}/a")


@responses.activate
def test_404_raises_fetch_error() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/gone", status=404)
    with pytest.raises(FetchError):
        make_fetcher().get(f"{BASE}/gone")


@responses.activate
def test_disk_cache_round_trip_serves_304_from_disk(tmp_path: Path) -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(
        responses.GET,
        f"{BASE}/a",
        body="cached body",
        status=200,
        headers={"ETag": '"v1"', "Last-Modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
    )
    first = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/a")
    assert first.from_cache is False

    responses.add(responses.GET, f"{BASE}/a", status=304)
    second = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/a")
    assert second.from_cache is True
    assert second.text == "cached body"
    assert responses.calls[-1].request.headers["If-None-Match"] == '"v1"'
    assert responses.calls[-1].request.headers["If-Modified-Since"]


@responses.activate
def test_fetch_many_keeps_pacing_and_order() -> None:
    starts: list[float] = []

    def record(request: PreparedRequest) -> tuple[int, dict[str, str], str]:
        starts.append(time.monotonic())
        return (200, {}, str(request.url))

    responses.add(responses.GET, ROBOTS, status=404)
    for index in range(4):
        responses.add_callback(responses.GET, f"{BASE}/p{index}", callback=record)

    urls = [f"{BASE}/p{index}" for index in range(4)]
    results = make_fetcher(delay_s=0.05, workers=4).fetch_many(urls)

    assert all(isinstance(result, FetchResult) for result in results)
    assert [cast("FetchResult", result).text for result in results] == urls
    gaps = [b - a for a, b in pairwise(starts)]
    assert all(gap >= 0.045 for gap in gaps), gaps


@responses.activate
def test_fetch_many_returns_errors_in_place() -> None:
    responses.add(
        responses.GET,
        ROBOTS,
        body="User-agent: *\nDisallow: /bad/\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/ok", body="ok", status=200)
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


@responses.activate
def test_a_retry_re_enters_the_limiter_and_waits_at_least_the_host_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", status=503)
    responses.add(responses.GET, f"{BASE}/a", body="finally", status=200)
    fetcher = make_fetcher(delay_s=0.25, retries=2)
    # take the robots.txt request out of the count: it is fetched once per origin
    assert fetcher.robots.can_fetch(fetcher.ua_token, f"{BASE}/a") is True

    slept = record_sleeps(monkeypatch)
    acquired = count_acquisitions(fetcher, monkeypatch)
    assert fetcher.get(f"{BASE}/a").text == "finally"

    assert acquired == [HOST, HOST]
    assert max(slept) >= 0.25


@responses.activate
def test_the_transport_never_retries_a_status_on_its_own() -> None:
    """urllib3 would repeat the request immediately, behind the limiter's back."""
    adapter = cast("HTTPAdapter", make_fetcher().session.get_adapter(BASE))
    retry = adapter.max_retries
    assert not retry.status_forcelist
    # on its own this one still retries 413/429/503 that carry a Retry-After
    assert retry.respect_retry_after_header is False
    assert retry.total == 2  # connect and read errors are still retried


@responses.activate
def test_a_429_waits_out_the_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/b", status=429, headers={"Retry-After": "2"})
    responses.add(responses.GET, f"{BASE}/b", body="ok", status=200)
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


@responses.activate
def test_a_crawl_delay_defers_the_first_content_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """robots.txt itself only booked the configured 0.2 s; 3 s is what was asked."""
    responses.add(
        responses.GET,
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


@responses.activate
def test_a_redirected_robots_txt_is_followed_hop_by_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses.add(
        responses.GET,
        ROBOTS,
        status=301,
        headers={"Location": "https://cdn.example.org/shop-robots.txt"},
    )
    responses.add(
        responses.GET,
        "https://cdn.example.org/shop-robots.txt",
        body="User-agent: *\nDisallow: /private/\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/public/x", body="ok", status=200)
    fetcher = make_fetcher()
    acquired = count_acquisitions(fetcher, monkeypatch)

    assert fetcher.get(f"{BASE}/public/x").text == "ok"
    with pytest.raises(FetchDisallowed):
        fetcher.get(f"{BASE}/private/x")

    requested = [call.request.url for call in responses.calls]
    assert requested[:2] == [ROBOTS, "https://cdn.example.org/shop-robots.txt"]
    # both robots hops were paced, each against its own host
    assert acquired[:3] == [HOST, "cdn.example.org", HOST]


# --- item 9: the pacing is auditable from the log ----------------------------


@responses.activate
def test_the_debug_log_reports_the_waited_seconds_after_acquiring(
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(responses.GET, f"{BASE}/a", body="a", status=200)
    responses.add(responses.GET, f"{BASE}/b", body="b", status=200)
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


@responses.activate
def test_a_redirected_page_sends_its_validators_to_the_final_url(tmp_path: Path) -> None:
    responses.add(
        responses.GET,
        ROBOTS,
        body="User-agent: *\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/old", status=302, headers={"Location": "/new"})
    responses.add(
        responses.GET,
        f"{BASE}/new",
        body="the real body",
        status=200,
        headers={"ETag": '"v9"'},
    )
    first = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/old")
    assert first.final_url == f"{BASE}/new"
    assert first.text == "the real body"

    responses.reset()
    responses.add(
        responses.GET,
        ROBOTS,
        body="User-agent: *\nAllow: /\n",
        status=200,
        content_type="text/plain",
    )
    responses.add(responses.GET, f"{BASE}/old", status=302, headers={"Location": "/new"})
    responses.add(responses.GET, f"{BASE}/new", status=304)
    second = make_fetcher(cache_dir=tmp_path).get(f"{BASE}/old")

    assert second.from_cache is True
    assert second.text == "the real body"
    conditional = [call.request for call in responses.calls if call.request.url == f"{BASE}/new"]
    assert conditional[-1].headers["If-None-Match"] == '"v9"'
    redirecting = [call.request for call in responses.calls if call.request.url == f"{BASE}/old"]
    assert "If-None-Match" not in redirecting[-1].headers


# --- item 11: a missing charset never mangles Slovak diacritics --------------


@responses.activate
def test_a_body_without_a_charset_is_decoded_as_utf8() -> None:
    responses.add(responses.GET, ROBOTS, status=404)
    responses.add(
        responses.GET,
        f"{BASE}/sk",
        body="<html><body>Etiópia čerešne</body></html>".encode(),
        status=200,
        content_type="text/html",
    )
    result = make_fetcher().get(f"{BASE}/sk")
    assert "Etiópia čerešne" in result.text


# --- item 12: the robots token is logged, and a browser UA warned about ------


@responses.activate
def test_the_robots_token_is_logged_at_startup(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="coffee_aggregator.http.fetcher"):
        fetcher = make_fetcher()
    assert fetcher.ua_token == "coffee-aggregator"
    assert "coffee-aggregator" in caplog.text
    assert "robots.txt rules are matched on" in caplog.text


def test_an_explicit_token_overrides_the_derived_one() -> None:
    assert make_fetcher(ua_token="grinder-bot").ua_token == "grinder-bot"
