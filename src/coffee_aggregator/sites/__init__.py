from __future__ import annotations

from coffee_aggregator.sites.base import ProductRef, SiteAdapter
from coffee_aggregator.sites.loader import (
    CONFIG_DIR,
    all_sites,
    get,
    load_all,
    load_errors,
)
from coffee_aggregator.sites.registry import DuplicateSiteError, UnknownSiteError, register

#: Contract alias: ``sites.all()`` is the name the architecture document uses,
#: ``all_sites()`` the one that does not shadow the builtin.
all = all_sites  # noqa: A001  (the architecture contract names this alias)

__all__ = [
    "CONFIG_DIR",
    "DuplicateSiteError",
    "ProductRef",
    "SiteAdapter",
    "UnknownSiteError",
    "all",
    "all_sites",
    "get",
    "load_all",
    "load_errors",
    "register",
]
