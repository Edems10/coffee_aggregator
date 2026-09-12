from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coffee_aggregator.sites.base import SiteAdapter

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[SiteAdapter]] = {}
_INSTANCES: dict[str, SiteAdapter] = {}


class DuplicateSiteError(Exception):
    """Raised when two modules claim the same ``site_id``."""

    def __init__(self, site_id: str) -> None:
        """Build the error.

        Args:
            site_id: The id that is registered twice.
        """
        super().__init__(f"site id {site_id!r} is already registered")
        self.site_id = site_id


class UnknownSiteError(Exception):
    """Raised when a caller asks for a site that nobody registered."""

    def __init__(self, site_id: str, known: list[str]) -> None:
        """Build the error.

        Args:
            site_id: The id that was asked for.
            known: Every id that is registered.
        """
        listing = ", ".join(known) if known else "none"
        super().__init__(f"unknown site {site_id!r}; registered sites: {listing}")
        self.site_id = site_id


def register[AdapterT: type[SiteAdapter]](cls: AdapterT) -> AdapterT:
    """Register a site adapter class under its ``site_id``.

    Args:
        cls: The adapter class to register.

    Returns:
        The class unchanged, so it works as a decorator.

    Raises:
        DuplicateSiteError: When the id is already taken.
    """
    site_id = cls.site_id
    if not site_id:
        message = f"{cls.__name__} must set a non-empty site_id"
        raise ValueError(message)
    if site_id in _REGISTRY:
        raise DuplicateSiteError(site_id)
    _REGISTRY[site_id] = cls
    logger.debug("registered site %s (%s)", site_id, cls.__name__)
    return cls


def register_instance(adapter: SiteAdapter) -> SiteAdapter:
    """Register an already-built adapter, as TOML-configured shops need.

    Args:
        adapter: The adapter instance to publish.

    Returns:
        The adapter unchanged.

    Raises:
        DuplicateSiteError: When the id is already taken.
    """
    if adapter.site_id in _REGISTRY or adapter.site_id in _INSTANCES:
        raise DuplicateSiteError(adapter.site_id)
    _INSTANCES[adapter.site_id] = adapter
    logger.debug("registered configured site %s", adapter.site_id)
    return adapter


def known_ids() -> list[str]:
    """Return every registered site id.

    Returns:
        The ids, sorted alphabetically.
    """
    return sorted(set(_REGISTRY) | set(_INSTANCES))


def instance(site_id: str) -> SiteAdapter:
    """Return the adapter registered under an id, building it once.

    Args:
        site_id: The id to look up.

    Returns:
        The adapter instance.

    Raises:
        UnknownSiteError: When nothing is registered under that id.
    """
    existing = _INSTANCES.get(site_id)
    if existing is not None:
        return existing
    cls = _REGISTRY.get(site_id)
    if cls is None:
        raise UnknownSiteError(site_id, known_ids())
    adapter = cls()
    _INSTANCES[site_id] = adapter
    return adapter


def clear() -> None:
    """Forget every registration (used by the tests)."""
    _REGISTRY.clear()
    _INSTANCES.clear()
