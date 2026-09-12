from __future__ import annotations

import importlib
import logging
import pkgutil
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from coffee_aggregator.sites.registry import (
    DuplicateSiteError,
    instance,
    known_ids,
    register_instance,
)

if TYPE_CHECKING:
    from types import ModuleType

    from coffee_aggregator.sites.base import SiteAdapter

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent / "configs"

_loaded = False

#: Everything discovery could not load, as one human-readable line each. A
#: mistyped TOML or a module that fails to import must not kill every command,
#: but it must not disappear either: ``list-sites`` prints these and exits 2.
load_errors: list[str] = []

__all__ = ["CONFIG_DIR", "all_sites", "get", "load_all", "load_errors"]


def _import_submodules(package: ModuleType) -> None:
    prefix = f"{package.__name__}."
    paths = list(getattr(package, "__path__", []))
    for module_info in pkgutil.walk_packages(paths, prefix):
        if module_info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        try:
            importlib.import_module(module_info.name)
        except Exception as exc:  # noqa: BLE001
            # A duplicate site id, a bad regex or a malformed default map raises
            # at import time; that must cost us one adapter, not every command.
            _record(f"{module_info.name}: {type(exc).__name__}: {exc}")


def _load_configs(config_dir: Path) -> None:
    from coffee_aggregator import platforms  # noqa: PLC0415  (avoids an import cycle)

    if not config_dir.is_dir():
        return
    for path in sorted(config_dir.glob("*.toml")):
        try:
            adapter = platforms.build_from_config(path)
        except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError) as exc:
            _record(f"{path}: {type(exc).__name__}: {exc}")
            continue
        try:
            register_instance(adapter)
        except DuplicateSiteError as exc:
            _record(f"{path}: {exc}")


def _record(problem: str) -> None:
    """Remember one discovery failure and log it.

    Args:
        problem: What could not be loaded, and why.
    """
    logger.warning("site discovery: %s", problem)
    if problem not in load_errors:
        load_errors.append(problem)


def load_all(config_dir: Path | None = None, *, force: bool = False) -> None:
    """Import every bespoke adapter module and build every configured shop.

    Args:
        config_dir: Where the shop TOML files live; defaults to ``sites/configs``.
        force: Re-run the discovery even when it already ran.
    """
    global _loaded  # noqa: PLW0603  (module-level "did we import yet" flag)
    if _loaded and not force:
        return
    load_errors.clear()
    import coffee_aggregator.platforms as platforms_pkg  # noqa: PLC0415  (discovery is lazy)
    import coffee_aggregator.sites as sites_pkg  # noqa: PLC0415  (discovery is lazy)

    try:
        _import_submodules(sites_pkg)
        _import_submodules(platforms_pkg)
        _load_configs(config_dir or CONFIG_DIR)
    finally:
        # Set only once the work is over, so a failure cannot leave a
        # half-populated registry that every later call returns immediately.
        _loaded = True


def get(site_id: str) -> SiteAdapter:
    """Return one registered shop adapter.

    Args:
        site_id: The registry id, e.g. ``"coffeein"``.

    Returns:
        The adapter instance.
    """
    load_all()
    return instance(site_id)


def all_sites() -> list[SiteAdapter]:
    """Return every registered shop adapter.

    Returns:
        The adapters, sorted by ``site_id``.
    """
    load_all()
    return [instance(site_id) for site_id in known_ids()]
