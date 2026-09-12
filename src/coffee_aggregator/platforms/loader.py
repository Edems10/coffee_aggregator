from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from coffee_aggregator.sites.base import SiteAdapter

__all__ = ["UnsupportedPlatformError", "build_from_config", "builder_target", "load_config"]


class UnsupportedPlatformError(ValueError):
    """Raised when a shop TOML names a platform nobody implements.

    It subclasses :class:`ValueError` so site discovery can skip one
    forward-looking config file with a warning instead of killing every command.
    """

    def __init__(self, platform: str, path: Path) -> None:
        """Build the error.

        Args:
            platform: The platform name found in the file.
            path: The configuration file that named it.
        """
        super().__init__(f"{path}: unsupported platform {platform!r}")
        self.platform = platform
        self.path = path


#: Where a platform's builder lives by convention: ``platforms/<name>.py`` must
#: expose ``build(config, path)``. A platform is added by dropping in a module —
#: no registration, no edit here.
BUILDER_TEMPLATE = "coffee_aggregator.platforms.{name}:build"

#: Overrides for the rare platform whose module cannot be named after it.
_PLATFORM_BUILDERS: dict[str, str] = {}


def builder_target(platform: str) -> str:
    """Return the ``module:function`` reference a platform name resolves to.

    Args:
        platform: The lower-cased platform name from a shop TOML.

    Returns:
        The dotted reference, from the override map when one exists.
    """
    return _PLATFORM_BUILDERS.get(platform, BUILDER_TEMPLATE.format(name=platform))


def _builder(target: str) -> Callable[[dict[str, Any], Path], SiteAdapter]:
    """Import one platform module and return its ``build`` function.

    Args:
        target: A ``module:function`` reference from :data:`_PLATFORM_BUILDERS`.

    Returns:
        The platform's builder.
    """
    module_name, _, function_name = target.partition(":")
    module = importlib.import_module(module_name)
    return cast(
        "Callable[[dict[str, Any], Path], SiteAdapter]",
        getattr(module, function_name),
    )


def load_config(path: Path) -> dict[str, Any]:
    """Read one shop TOML file.

    Args:
        path: Path to the file.

    Returns:
        The parsed mapping.
    """
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def build_from_config(path: Path) -> SiteAdapter:
    """Build a site adapter from a shop TOML file.

    Args:
        path: Path to the file; it must contain a ``platform`` key.

    Returns:
        The configured adapter.

    Raises:
        UnsupportedPlatformError: When ``platform`` names an unknown platform.
    """
    config = load_config(path)
    platform = str(config.get("platform", "")).strip().lower()
    if not platform.isidentifier():
        raise UnsupportedPlatformError(platform, Path(path))
    try:
        build = _builder(builder_target(platform))
    except (ImportError, AttributeError) as exc:
        raise UnsupportedPlatformError(platform, Path(path)) from exc
    return build(config, Path(path))
