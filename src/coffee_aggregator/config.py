from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT_NAME = "coffee-aggregator"
DEFAULT_CONTACT = "https://github.com/coffee-aggregator"
DEFAULT_DELAY_S = 1.0
DEFAULT_WORKERS = 4

USER_AGENT_VARIABLE = "COFFEE_AGG_USER_AGENT"
UA_TOKEN_VARIABLE = "COFFEE_AGG_UA_TOKEN"  # noqa: S105  (an env var name, not a secret)
#: Product tokens that mean the User-Agent is pretending to be a browser.
BROWSER_TOKENS = ("mozilla", "opera", "safari", "chrome")

FX_CACHE_VARIABLE = "COFFEE_AGG_FX_CACHE"

DSN_VARIABLE = "DATABASE_URL"
DSN_HINT = (
    "set DATABASE_URL, e.g. postgresql://coffee:coffee@localhost:5432/coffee "
    "— see docker-compose.yml (or pass --dsn)"
)


class ConfigError(Exception):
    """Raised when a required setting is missing or cannot be parsed."""

    def __init__(self, variable: str, hint: str) -> None:
        """Build the error.

        Args:
            variable: Name of the offending environment variable.
            hint: Actionable advice on how to fix it.
        """
        super().__init__(f"{variable}: {hint}")
        self.variable = variable
        self.hint = hint


def load_dotenv_file(cwd: Path | None = None) -> bool:
    """Load a ``.env`` file from the working directory without overriding real env vars.

    Args:
        cwd: Directory to look in; defaults to the process working directory.

    Returns:
        True when a ``.env`` file was found and loaded.
    """
    env_path = (cwd or Path.cwd()) / ".env"
    if not env_path.is_file():
        return False
    load_dotenv(env_path, override=False)
    return True


def robots_token(user_agent: str) -> str:
    """Return the product token robots.txt rules will be matched on.

    Args:
        user_agent: The full User-Agent string.

    Returns:
        Its first product token, e.g. ``"coffee-aggregator"``.
    """
    return user_agent.split("/", maxsplit=1)[0].split(" ", maxsplit=1)[0]


def check_user_agent(user_agent: str) -> str:
    """Warn when a hand-written User-Agent would make robots.txt match the wrong name.

    ``urllib.robotparser`` matches a shop's ``User-agent:`` lines against our
    *first* product token only. A browser-shaped string therefore claims rules
    written for Firefox, and one that hides ``coffee-aggregator`` further along
    claims none of the rules written for us.

    Args:
        user_agent: The User-Agent string the environment asked for.

    Returns:
        The robots.txt token that string yields.
    """
    token = robots_token(user_agent)
    folded = token.strip().lower()
    if folded in BROWSER_TOKENS:
        logger.warning(
            "%s starts with %r, so robots.txt rules are matched on a browser name; "
            "start it with %r instead, or set %s",
            USER_AGENT_VARIABLE,
            token,
            DEFAULT_USER_AGENT_NAME,
            UA_TOKEN_VARIABLE,
        )
    elif folded != DEFAULT_USER_AGENT_NAME and DEFAULT_USER_AGENT_NAME in user_agent.lower():
        logger.warning(
            "%s mentions %r only after the first token %r, which is what robots.txt "
            "is matched on; set %s to override the token",
            USER_AGENT_VARIABLE,
            DEFAULT_USER_AGENT_NAME,
            token,
            UA_TOKEN_VARIABLE,
        )
    return token


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(name, f"expected a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(name, f"expected an integer, got {raw!r}") from exc


@dataclass(slots=True)
class Settings:
    """Everything the CLI needs that can come from the environment.

    Attributes:
        database_url: PostgreSQL DSN, only needed by the postgres sink and init-db.
        user_agent: Full User-Agent string sent with every request.
        ua_token: Override for the robots.txt product token, when the derived
            one is not what the shops' rules name.
        contact: Contact URL or e-mail advertised in the User-Agent.
        delay_s: Minimum delay between two request starts to one host.
        workers: Size of the fetch thread pool.
        cache_dir: Optional on-disk HTTP cache directory.
        fx_cache: Where the file-backed EUR/CZK rate store lives; unset means
            ``<cache_dir>/fx_rates.json``, or the same file under
            ``~/.cache/coffee-aggregator`` when no cache directory is configured.
    """

    database_url: str | None = None
    user_agent: str = ""
    ua_token: str | None = None
    contact: str = DEFAULT_CONTACT
    delay_s: float = DEFAULT_DELAY_S
    workers: int = DEFAULT_WORKERS
    cache_dir: Path | None = None
    fx_cache: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        """Read every supported variable, validating only what is present.

        A ``.env`` file in the working directory is loaded first; real environment
        variables always win over its contents.

        Returns:
            A populated settings object.

        Raises:
            ConfigError: If a numeric variable cannot be parsed.
        """
        from coffee_aggregator import __version__  # noqa: PLC0415  (avoids an import cycle)

        load_dotenv_file()
        contact = os.environ.get("COFFEE_AGG_CONTACT", DEFAULT_CONTACT).strip() or DEFAULT_CONTACT
        default_ua = f"{DEFAULT_USER_AGENT_NAME}/{__version__} (+{contact})"
        cache_raw = os.environ.get("COFFEE_AGG_CACHE_DIR", "").strip()
        fx_cache_raw = os.environ.get(FX_CACHE_VARIABLE, "").strip()
        configured_ua = os.environ.get(USER_AGENT_VARIABLE, "").strip()
        user_agent = configured_ua or default_ua
        if configured_ua:
            check_user_agent(configured_ua)
        return cls(
            database_url=os.environ.get(DSN_VARIABLE) or None,
            user_agent=user_agent,
            ua_token=os.environ.get(UA_TOKEN_VARIABLE, "").strip() or None,
            contact=contact,
            delay_s=_env_float("COFFEE_AGG_DELAY", DEFAULT_DELAY_S),
            workers=_env_int("COFFEE_AGG_WORKERS", DEFAULT_WORKERS),
            cache_dir=Path(cache_raw) if cache_raw else None,
            fx_cache=Path(fx_cache_raw) if fx_cache_raw else None,
        )

    def fx_cache_path(self) -> Path:
        """Return the file the no-database rate store is kept in.

        Returns:
            ``COFFEE_AGG_FX_CACHE`` when set, otherwise ``fx_rates.json`` inside
            the HTTP cache directory or inside ``~/.cache/coffee-aggregator``.
        """
        from coffee_aggregator.fx import default_cache_path  # noqa: PLC0415  (avoids a cycle)

        return self.fx_cache or default_cache_path(self.cache_dir)

    def require_database_url(self, override: str | None = None) -> str:
        """Return the PostgreSQL DSN, failing loudly when it is unset.

        Args:
            override: DSN passed on the command line, which wins over the env.

        Returns:
            The DSN to connect with.

        Raises:
            ConfigError: If neither the override nor ``DATABASE_URL`` is set.
        """
        dsn = (override or "").strip() or self.database_url
        if not dsn:
            raise ConfigError(DSN_VARIABLE, DSN_HINT)
        return dsn
