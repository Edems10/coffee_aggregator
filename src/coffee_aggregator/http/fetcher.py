from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import os
import random
import threading
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

logger = logging.getLogger(__name__)

ACCEPT_LANGUAGE = "sk,cs;q=0.9,en;q=0.5"
RETRY_STATUSES = (429, 500, 502, 503, 504)
#: Multiplier of the exponential backoff between two attempts at the same URL.
RETRY_BACKOFF_FACTOR = 0.5
#: Redirects are followed by hand so every hop passes robots.txt and the limiter.
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
MAX_REDIRECTS = 5
#: Product tokens that mean "this User-Agent is pretending to be a browser".
BROWSER_TOKENS = ("mozilla", "opera", "safari", "chrome")
#: robots.txt's wildcard agent, used when nobody said who we are.
ANY_USER_AGENT = "*"
_HTTP_ERROR_FLOOR = 400
_SERVER_ERROR_FLOOR = 500
_NOT_MODIFIED = 304
#: RFC 9309 §2.3.1.3: an unauthorized or forbidden robots.txt means "stay out".
_ROBOTS_CLOSED_STATUSES = (401, 403)
#: requests' fallback for a ``text/*`` body whose Content-Type names no charset.
_LATIN1 = "iso-8859-1"


class FetchDisallowed(Exception):  # noqa: N818  (the name is fixed by the architecture contract)
    """Raised when robots.txt forbids the requested URL."""

    def __init__(self, url: str) -> None:
        """Build the error.

        Args:
            url: The URL that robots.txt refuses.
        """
        super().__init__(f"robots.txt disallows {url}")
        self.url = url


class FetchError(Exception):
    """Raised when a URL could not be retrieved after every retry."""

    def __init__(self, url: str, reason: str) -> None:
        """Build the error.

        Args:
            url: The URL that failed.
            reason: Human-readable cause, e.g. a status code or exception text.
        """
        super().__init__(f"{url}: {reason}")
        self.url = url
        self.reason = reason


@dataclass(slots=True, frozen=True)
class FetchResult:
    """One successfully retrieved page."""

    url: str
    final_url: str
    status: int
    text: str
    from_cache: bool
    elapsed_s: float


@dataclass(slots=True, frozen=True)
class CacheEntry:
    """A previously stored response body and its validators."""

    body: str
    etag: str | None
    last_modified: str | None


def _sleep(seconds: float) -> None:
    """Block for ``seconds``.

    Every wait in this module goes through here, so a test can watch what the
    crawler asked for without waiting for it.

    Args:
        seconds: How long to wait; anything at or below zero returns at once.
    """
    if seconds > 0:
        time.sleep(seconds)


def origin_of(url: str) -> str:
    """Return the scheme-plus-host prefix of a URL.

    Args:
        url: Any absolute URL.

    Returns:
        Something like ``https://www.example.sk``.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def host_of(url: str) -> str:
    """Return the host a URL points at.

    Args:
        url: Any absolute URL.

    Returns:
        The network location, or an empty string for a relative URL.
    """
    return urlsplit(url).netloc


def robots_token(user_agent: str) -> str:
    """Return the product token a robots.txt ``User-agent`` line is matched on.

    Args:
        user_agent: The full User-Agent string.

    Returns:
        The first product token, e.g. ``"coffee-aggregator"``.
    """
    return user_agent.split("/", maxsplit=1)[0].split(" ", maxsplit=1)[0]


def retry_after_seconds(header: str | None) -> float | None:
    """Read a ``Retry-After`` header, in either of its two legal forms.

    Args:
        header: The header value, when the server sent one.

    Returns:
        The delay in seconds, or None when the header is absent or unreadable.
    """
    if not header:
        return None
    raw = header.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _atomic_write(path: Path, payload: str) -> None:
    """Write a file so a concurrent reader never sees it half written.

    Args:
        path: The final destination.
        payload: The text to store.
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(payload, "utf-8")
    temporary.replace(path)


class DiskCache:
    """Conditional-GET cache: one body file plus one metadata file per URL.

    Bodies and validators are keyed by the URL the server finally served, and a
    URL that redirects gets an alias entry pointing at it. Without the alias the
    next run would ask the redirecting URL for validators it never stored and
    re-download the page on every crawl.
    """

    def __init__(self, directory: Path) -> None:
        """Create the cache directory if needed.

        Args:
            directory: Where to keep the cached bodies.
        """
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _paths(self, url: str) -> tuple[Path, Path]:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return (self.directory / f"{digest}.body", self.directory / f"{digest}.json")

    def _meta(self, url: str) -> dict[str, str] | None:
        _, meta_path = self._paths(url)
        try:
            loaded = json.loads(meta_path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def resolve(self, url: str) -> str:
        """Follow a stored redirect alias to the URL the body is filed under.

        Args:
            url: The URL the caller asked for.

        Returns:
            The final URL of the previous run, or ``url`` when none is stored.
        """
        meta = self._meta(url)
        if meta is None:
            return url
        alias = meta.get("alias")
        return alias if isinstance(alias, str) and alias and alias != url else url

    def load(self, url: str) -> CacheEntry | None:
        """Read a stored entry, following a redirect alias when there is one.

        Args:
            url: The URL whose body was cached.

        Returns:
            The entry, or None when nothing usable is stored.
        """
        resolved = self.resolve(url)
        body_path, _ = self._paths(resolved)
        meta = self._meta(resolved)
        if meta is None or not body_path.is_file():
            return None
        try:
            body = body_path.read_text("utf-8")
        except OSError:
            logger.debug("unreadable cache entry for %s", resolved)
            return None
        return CacheEntry(body=body, etag=meta.get("etag"), last_modified=meta.get("last_modified"))

    def store(
        self,
        url: str,
        final_url: str,
        body: str,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        """Write an entry, ignoring I/O problems (a cache is never load bearing).

        Args:
            url: The URL that was requested.
            final_url: The URL the last redirect hop actually served.
            body: The decoded response body.
            etag: The ``ETag`` header, when the server sent one.
            last_modified: The ``Last-Modified`` header, when the server sent one.
        """
        body_path, meta_path = self._paths(final_url)
        try:
            # Body first, metadata last, both via a temporary file plus an atomic
            # rename: a reader either sees the previous complete pair or the new
            # one, never a half-written body next to valid metadata.
            _atomic_write(body_path, body)
            _atomic_write(
                meta_path,
                json.dumps({"url": final_url, "etag": etag, "last_modified": last_modified}),
            )
            if final_url != url:
                _, alias_path = self._paths(url)
                _atomic_write(alias_path, json.dumps({"url": url, "alias": final_url}))
        except OSError:
            logger.warning("could not write cache entry for %s", final_url)


class RateLimiter:
    """Per-host pacing of request *starts*, safe to share between threads."""

    def __init__(self, delay_s: float, jitter_s: float) -> None:
        """Build the limiter.

        Args:
            delay_s: Minimum seconds between two request starts to one host.
            jitter_s: Upper bound of the extra uniform delay added to each request.
        """
        self.delay_s = max(0.0, delay_s)
        self.jitter_s = max(0.0, jitter_s)
        self._lock = threading.Lock()
        self._next_start: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}

    def set_host_delay(self, host: str, delay_s: float) -> None:
        """Raise the delay for one host, e.g. because robots.txt asks for it.

        Args:
            host: The host the delay applies to.
            delay_s: The requested delay in seconds.
        """
        with self._lock:
            self._host_delay[host] = max(self._host_delay.get(host, 0.0), delay_s)

    def delay_for(self, host: str) -> float:
        """Return the effective delay for a host.

        Args:
            host: The host to look up.

        Returns:
            The larger of the global delay and any robots.txt crawl-delay.
        """
        with self._lock:
            return max(self.delay_s, self._host_delay.get(host, 0.0))

    def next_start(self, host: str) -> float | None:
        """Return the monotonic time the host's next request may start at.

        Args:
            host: The host to look up.

        Returns:
            The booked start, or None when nothing has been booked yet.
        """
        with self._lock:
            return self._next_start.get(host)

    def defer(self, host: str, delay_s: float, *, since: float | None = None) -> None:
        """Push the host's next slot at least ``delay_s`` past a moment in time.

        This is what makes a robots.txt ``Crawl-delay`` apply to the very first
        content request: the robots.txt fetch itself only booked the configured
        delay, because the longer one was not known until its body was parsed.

        Args:
            host: The host to slow down.
            delay_s: How long after ``since`` the next request may start.
            since: The monotonic moment to count from; defaults to now.
        """
        with self._lock:
            base = time.monotonic() if since is None else since
            booked = self._next_start.get(host, base)
            self._next_start[host] = max(booked, base + delay_s)

    def acquire(self, host: str) -> float:
        """Block until this thread may start a request to ``host``.

        Args:
            host: The host about to be contacted.

        Returns:
            How many seconds the caller was made to wait.
        """
        with self._lock:
            delay = max(self.delay_s, self._host_delay.get(host, 0.0))
            now = time.monotonic()
            earliest = self._next_start.get(host, now)
            start_at = max(now, earliest)
            self._next_start[host] = start_at + delay + random.uniform(0.0, self.jitter_s)  # noqa: S311  (jitter is politeness, not security)
        wait = start_at - time.monotonic()
        if wait <= 0:
            return 0.0
        _sleep(wait)
        return wait


@dataclass(slots=True, frozen=True)
class RobotsEntry:
    """The robots.txt verdict for one origin."""

    parser: urllib.robotparser.RobotFileParser | None
    allow_all: bool


class RobotsCache:
    """Fetches and remembers one robots.txt per origin, failing closed."""

    def __init__(
        self,
        session: requests.Session,
        timeout_s: float,
        limiter: RateLimiter | None = None,
        ua_token: str = ANY_USER_AGENT,
    ) -> None:
        """Build the cache.

        Args:
            session: The session robots.txt is fetched with.
            timeout_s: Per-request timeout.
            limiter: The shared rate limiter, so robots.txt is paced like any
                other request to that host.
            ua_token: Our robots.txt product token, used to read the
                ``Crawl-delay`` that applies to us as soon as it is parsed.
        """
        self._session = session
        self._timeout_s = timeout_s
        self._limiter = limiter
        self._ua_token = ua_token
        self._lock = threading.Lock()
        self._entries: dict[str, RobotsEntry] = {}
        self._origin_locks: dict[str, threading.Lock] = {}

    def _origin_lock(self, origin: str) -> threading.Lock:
        with self._lock:
            lock = self._origin_locks.get(origin)
            if lock is None:
                lock = self._origin_locks[origin] = threading.Lock()
            return lock

    def _entry(self, origin: str) -> RobotsEntry:
        with self._lock:
            cached = self._entries.get(origin)
        if cached is not None:
            return cached
        # One fetch per origin even with a thread pool: the losers of the race
        # wait on the origin lock and then find the entry already cached.
        with self._origin_lock(origin):
            with self._lock:
                cached = self._entries.get(origin)
            if cached is not None:
                return cached
            entry = self._load(origin)
            with self._lock:
                self._entries[origin] = entry
            return entry

    def _load(self, origin: str) -> RobotsEntry:
        """Fetch and parse one origin's robots.txt, redirects walked by hand.

        Args:
            origin: The scheme-plus-host the rules are wanted for.

        Returns:
            The verdict for that origin; anything unreadable fails closed.
        """
        url = f"{origin}/robots.txt"
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            started = self._acquire(current)
            try:
                response = self._session.get(
                    current,
                    timeout=self._timeout_s,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "robots.txt unreachable at %s (%s); disallowing the host", current, exc
                )
                return RobotsEntry(parser=None, allow_all=False)
            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                logger.debug("robots.txt for %s redirects to %s", origin, current)
                continue
            return self._verdict(response, url, host_of(current), started)
        logger.warning(
            "robots.txt at %s redirected more than %d times; disallowing the host",
            url,
            MAX_REDIRECTS,
        )
        return RobotsEntry(parser=None, allow_all=False)

    def _acquire(self, url: str) -> float:
        """Clear one robots.txt hop with the limiter, like any other request.

        Args:
            url: The hop about to be requested.

        Returns:
            The monotonic moment the request is starting at.
        """
        if self._limiter is not None:
            self._limiter.acquire(host_of(url))
        return time.monotonic()

    def _verdict(
        self,
        response: requests.Response,
        url: str,
        host: str,
        started: float,
    ) -> RobotsEntry:
        """Turn the final robots.txt response into a verdict.

        Args:
            response: The last hop's response.
            url: The canonical ``/robots.txt`` URL of the origin.
            host: The host the last hop went to, for the crawl-delay booking.
            started: When that hop started, so a crawl-delay can be applied from
                it rather than from the moment it finished parsing.

        Returns:
            The verdict for the origin.
        """
        if response.status_code >= _SERVER_ERROR_FLOOR or (
            response.status_code in _ROBOTS_CLOSED_STATUSES
        ):
            logger.warning(
                "robots.txt returned %s at %s; disallowing the host",
                response.status_code,
                url,
            )
            return RobotsEntry(parser=None, allow_all=False)
        if response.status_code >= _HTTP_ERROR_FLOOR:
            logger.debug("no robots.txt at %s (%s); allowing all", url, response.status_code)
            return RobotsEntry(parser=None, allow_all=True)
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(url)
        parser.parse(_decode(response).splitlines())
        self._book_crawl_delay(parser, host, started)
        return RobotsEntry(parser=parser, allow_all=False)

    def _book_crawl_delay(
        self,
        parser: urllib.robotparser.RobotFileParser,
        host: str,
        started: float,
    ) -> None:
        """Apply a declared ``Crawl-delay`` to the very next request too.

        Args:
            parser: The freshly parsed rules.
            host: The host they apply to.
            started: When the robots.txt request itself started.
        """
        if self._limiter is None:
            return
        delay = _crawl_delay_of(parser, self._ua_token)
        if delay is None or delay <= self._limiter.delay_for(host):
            return
        logger.info("%s asks for a %.1fs crawl delay; obeying it from now on", host, delay)
        self._limiter.set_host_delay(host, delay)
        self._limiter.defer(host, delay, since=started)

    def can_fetch(self, ua_token: str, url: str) -> bool:
        """Ask robots.txt whether this URL may be requested.

        Args:
            ua_token: The product token of our User-Agent.
            url: The absolute URL we want.

        Returns:
            True when the fetch is allowed.
        """
        entry = self._entry(origin_of(url))
        if entry.parser is None:
            return entry.allow_all
        return entry.parser.can_fetch(ua_token, url)

    def crawl_delay(self, ua_token: str, url: str) -> float | None:
        """Return the crawl-delay robots.txt asks for, when it states one.

        Args:
            ua_token: The product token of our User-Agent.
            url: Any URL on the origin of interest.

        Returns:
            The delay in seconds, or None.
        """
        entry = self._entry(origin_of(url))
        if entry.parser is None:
            return None
        return _crawl_delay_of(entry.parser, ua_token)


def _crawl_delay_of(parser: urllib.robotparser.RobotFileParser, ua_token: str) -> float | None:
    """Read a parsed robots.txt's crawl delay as a float.

    Args:
        parser: The parsed rules.
        ua_token: The product token the rules are read for.

    Returns:
        The delay in seconds, or None when none is declared.
    """
    raw = parser.crawl_delay(ua_token)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):  # pragma: no cover - robotparser returns numbers
        return None


def _decode(response: requests.Response) -> str:
    """Return a response body as text, guessing the charset when none is stated.

    ``requests`` falls back to ISO-8859-1 for any ``text/*`` body whose
    Content-Type names no charset — the rule in RFC 2616, dropped in RFC 7231 —
    and Czech and Slovak shops that omit the charset are common enough that the
    fallback would turn every diacritic into mojibake.

    Args:
        response: The response to read.

    Returns:
        The decoded body.
    """
    declared = response.headers.get("Content-Type", "")
    encoding = (response.encoding or "").lower()
    if "charset=" not in declared.lower() or not encoding or encoding == _LATIN1:
        response.encoding = response.apparent_encoding or "utf-8"
    return response.text


class PoliteFetcher:
    """The only object in the project allowed to make an HTTP request."""

    def __init__(  # noqa: PLR0913, PLR0917  (one knob per politeness requirement)
        self,
        user_agent: str,
        contact: str,
        delay_s: float = 1.0,
        jitter_s: float = 0.5,
        workers: int = 4,
        timeout_s: float = 15.0,
        retries: int = 3,
        cache_dir: Path | None = None,
        ua_token: str | None = None,
    ) -> None:
        """Build the fetcher and its session.

        Args:
            user_agent: Full User-Agent string; its product token is used for robots.
            contact: Contact URL or e-mail, appended to the User-Agent when missing.
            delay_s: Minimum seconds between request starts to the same host.
            jitter_s: Extra uniform random delay on top of ``delay_s``.
            workers: Thread-pool size used by :meth:`fetch_many`.
            timeout_s: Per-request timeout.
            retries: How often a 429/5xx is retried before giving up.
            cache_dir: Optional directory for the conditional-GET cache.
            ua_token: Override for the robots.txt product token, for the rare
                shop whose rules name something else than our first token.
        """
        has_contact = f"+{contact}" in user_agent
        self.user_agent = user_agent if has_contact else f"{user_agent} (+{contact})"
        self.contact = contact
        self.timeout_s = timeout_s
        self.retries = max(0, retries)
        self.workers = max(1, workers)
        self.ua_token = (ua_token or "").strip() or robots_token(self.user_agent)
        logger.info(
            "identifying as %r; robots.txt rules are matched on %r",
            self.user_agent,
            self.ua_token,
        )
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": self.user_agent, "Accept-Language": ACCEPT_LANGUAGE}
        )
        adapter = HTTPAdapter(
            pool_connections=8,
            pool_maxsize=16,
            max_retries=Retry(
                total=self.retries,
                backoff_factor=RETRY_BACKOFF_FACTOR,
                # Connect and read errors only. Status retries happen in get(),
                # which re-enters the limiter; urllib3 would repeat them at once
                # and behind its back. respect_retry_after_header must go too:
                # on its own it makes urllib3 retry 413/429/503 whenever the
                # response carries a Retry-After, empty forcelist or not.
                status=0,
                status_forcelist=(),
                respect_retry_after_header=False,
                allowed_methods=("GET", "HEAD"),
            ),
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.limiter = RateLimiter(delay_s=delay_s, jitter_s=jitter_s)
        self.robots = RobotsCache(
            self.session,
            timeout_s=timeout_s,
            limiter=self.limiter,
            ua_token=self.ua_token,
        )
        self.cache = DiskCache(cache_dir) if cache_dir is not None else None

    def close(self) -> None:
        """Close the underlying session."""
        self.session.close()

    def _conditional_headers(self, entry: CacheEntry | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if entry is None:
            return headers
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        return headers

    def _preflight(self, url: str) -> None:
        """Clear one URL for fetching: robots.txt first, then the host's pacing.

        Every hop of a redirect chain and every retry goes through here, so a
        redirect can never smuggle us onto a path — or a host — that robots.txt
        forbids, and nothing fires faster than the host's delay window allows.

        Args:
            url: The absolute URL about to be requested.

        Raises:
            FetchDisallowed: When robots.txt forbids the URL.
        """
        if not self.robots.can_fetch(self.ua_token, url):
            raise FetchDisallowed(url)
        host = host_of(url)
        crawl_delay = self.robots.crawl_delay(self.ua_token, url)
        if crawl_delay is not None:
            self.limiter.set_host_delay(host, crawl_delay)
        waited = self.limiter.acquire(host)
        logger.debug(
            "GET %s (host delay %.2fs, waited %.2fs)",
            url,
            self.limiter.delay_for(host),
            waited,
        )

    def _request(self, url: str, headers: dict[str, str]) -> requests.Response:
        """Issue one hop after clearing it, never following redirects itself.

        Args:
            url: The absolute URL to request.
            headers: Extra request headers, e.g. the conditional-GET validators.

        Returns:
            The raw response, redirects included.

        Raises:
            FetchDisallowed: When robots.txt forbids the URL.
            FetchError: When the request could not be made at all.
        """
        self._preflight(url)
        try:
            return self.session.get(
                url,
                headers=headers,
                timeout=self.timeout_s,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc

    def get(self, url: str) -> FetchResult:
        """Fetch one URL, obeying robots.txt and the per-host rate limit.

        Args:
            url: The absolute URL to retrieve.

        Returns:
            The retrieved page.

        Raises:
            FetchDisallowed: When robots.txt forbids the URL or any redirect hop.
            FetchError: When the URL could not be retrieved after every retry,
                or when the redirect chain is longer than :data:`MAX_REDIRECTS`.
        """
        resolved = self.cache.resolve(url) if self.cache is not None else url
        entry = self.cache.load(url) if self.cache is not None else None
        validators = self._conditional_headers(entry)
        started = time.monotonic()
        status = 0
        for attempt in range(1, self.retries + 2):
            response, final_url = self._chain(url, resolved, validators)
            status = response.status_code
            if status not in RETRY_STATUSES or attempt > self.retries:
                return self._result(url, final_url, response, entry, time.monotonic() - started)
            self._wait_before_retry(url, response, attempt)
        raise FetchError(url, f"HTTP {status} after {self.retries + 1} attempts")

    def _chain(
        self,
        url: str,
        resolved: str,
        validators: dict[str, str],
    ) -> tuple[requests.Response, str]:
        """Walk one redirect chain by hand, clearing every hop on its own.

        Args:
            url: The URL the caller asked for.
            resolved: The URL the cached validators belong to — the previous
                run's final URL, which may be further down this very chain.
            validators: The conditional-GET headers, sent only to ``resolved``.

        Returns:
            The last response of the chain and the URL it came from.

        Raises:
            FetchError: When the chain is longer than :data:`MAX_REDIRECTS`.
        """
        current = url
        headers = dict(validators) if url == resolved else {}
        for _hop in range(MAX_REDIRECTS + 1):
            response = self._request(current, headers)
            location = response.headers.get("Location")
            if response.status_code in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                headers = dict(validators) if current == resolved else {}
                logger.debug("%s redirects to %s", response.url, current)
                continue
            return response, current
        raise FetchError(url, f"more than {MAX_REDIRECTS} redirects")

    def _wait_before_retry(self, url: str, response: requests.Response, attempt: int) -> None:
        """Wait out a retryable status before asking the same host again.

        Args:
            url: The URL being retried.
            response: The response that asked us to come back later.
            attempt: Which attempt has just failed, counting from one.
        """
        host = host_of(url)
        asked = retry_after_seconds(response.headers.get("Retry-After")) or 0.0
        backoff = RETRY_BACKOFF_FACTOR * 2 ** (attempt - 1)
        wait = max(self.limiter.delay_for(host), asked, backoff)
        logger.warning(
            "%s returned %s; retrying in %.2fs (attempt %d of %d)",
            url,
            response.status_code,
            wait,
            attempt,
            self.retries + 1,
        )
        _sleep(wait)

    def _result(
        self,
        url: str,
        final_url: str,
        response: requests.Response,
        entry: CacheEntry | None,
        elapsed: float,
    ) -> FetchResult:
        """Turn the last hop of a chain into a result, or into a failure.

        Args:
            url: The URL the caller asked for.
            final_url: The URL the last hop actually went to.
            response: The last response of the chain.
            entry: The cached entry the conditional GET was based on, if any.
            elapsed: Seconds spent on the whole chain.

        Returns:
            The retrieved page.

        Raises:
            FetchError: When the final status is an error.
        """
        if response.status_code == _NOT_MODIFIED and entry is not None:
            logger.debug("%s -> 304 in %.2fs (cache=True)", url, elapsed)
            return FetchResult(
                url=url,
                final_url=final_url,
                status=_NOT_MODIFIED,
                text=entry.body,
                from_cache=True,
                elapsed_s=elapsed,
            )
        if response.status_code >= _HTTP_ERROR_FLOOR:
            raise FetchError(url, f"HTTP {response.status_code}")
        body = _decode(response)
        if self.cache is not None:
            self.cache.store(
                url,
                final_url,
                body,
                response.headers.get("ETag"),
                response.headers.get("Last-Modified"),
            )
        logger.debug("%s -> %s in %.2fs (cache=False)", url, response.status_code, elapsed)
        return FetchResult(
            url=url,
            final_url=final_url,
            status=response.status_code,
            text=body,
            from_cache=False,
            elapsed_s=elapsed,
        )

    def _get_or_error(self, url: str) -> FetchResult | FetchError | FetchDisallowed:
        """Fetch one URL, returning every failure instead of raising it.

        Args:
            url: The absolute URL to retrieve.

        Returns:
            The page, or the failure that stopped it.
        """
        try:
            return self.get(url)
        except (FetchError, FetchDisallowed) as exc:
            return exc
        except Exception as exc:  # noqa: BLE001
            # A worker thread must never die with an exception the caller cannot
            # see: a malformed header, a decoding bug in a dependency or an
            # unexpected urllib3 error would otherwise abort a whole batch and
            # take every sibling URL's result with it.
            logger.warning("unexpected failure fetching %s: %r", url, exc)
            return FetchError(url, f"{type(exc).__name__}: {exc}")

    def fetch_many(
        self,
        urls: Iterable[str],
    ) -> list[FetchResult | FetchError | FetchDisallowed]:
        """Fetch many URLs concurrently while keeping the per-host pacing.

        Args:
            urls: The URLs to retrieve.

        Returns:
            One entry per input URL, in the same order; failures are returned
            as the exception object rather than raised.
        """
        ordered: Sequence[str] = list(urls)
        if not ordered:
            return []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(self._get_or_error, ordered))
