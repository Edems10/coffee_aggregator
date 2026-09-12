from __future__ import annotations

import sys
import types
from typing import TYPE_CHECKING, Any

import pytest

from coffee_aggregator import platforms
from coffee_aggregator.sites.base import ProductRef, SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from coffee_aggregator.http import PoliteFetcher
    from coffee_aggregator.models import Coffee

SHOPTET_TOML = """
platform = "shoptet"
site_id = "myshop"
name = "My Roastery"
country = "CZ"
base_url = "https://myshop.cz"
category_urls = ["https://myshop.cz/zrnkova-kava/"]
"""


class _Configured(SiteAdapter):
    site_id = "myshop"
    name = "My Roastery"
    country = "CZ"
    base_url = "https://myshop.cz"
    kind = "shoptet"

    def discover(
        self,
        fetcher: PoliteFetcher,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        return iter(())

    def parse_product(self, html: str, ref: ProductRef) -> Coffee | None:
        return None


def test_load_config_reads_the_toml(tmp_path: Path) -> None:
    path = tmp_path / "myshop.toml"
    path.write_text(SHOPTET_TOML, encoding="utf-8")
    config = platforms.load_config(path)
    assert config["platform"] == "shoptet"
    assert config["category_urls"] == ["https://myshop.cz/zrnkova-kava/"]


def test_unknown_platform_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "weird.toml"
    path.write_text('platform = "magento"\n', encoding="utf-8")
    with pytest.raises(platforms.UnsupportedPlatformError) as excinfo:
        platforms.build_from_config(path)
    assert "magento" in str(excinfo.value)


def test_missing_platform_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.toml"
    path.write_text('site_id = "x"\n', encoding="utf-8")
    with pytest.raises(platforms.UnsupportedPlatformError):
        platforms.build_from_config(path)


def test_shoptet_is_dispatched_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dispatch is by convention: platforms/<name>.py must expose build()."""
    assert platforms.builder_target("shoptet") == "coffee_aggregator.platforms.shoptet:build"

    seen: list[tuple[dict[str, Any], Path]] = []
    fake = types.ModuleType("coffee_aggregator.platforms.shoptet")

    def build(config: dict[str, Any], path: Path) -> SiteAdapter:
        seen.append((config, path))
        return _Configured()

    fake.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "coffee_aggregator.platforms.shoptet", fake)

    path = tmp_path / "myshop.toml"
    path.write_text(SHOPTET_TOML, encoding="utf-8")
    adapter = platforms.build_from_config(path)

    assert isinstance(adapter, SiteAdapter)
    assert adapter.site_id == "myshop"
    assert seen[0][0]["site_id"] == "myshop"


def test_a_new_platform_needs_no_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping in platforms/<name>.py is all it takes to support a platform."""
    fake = types.ModuleType("coffee_aggregator.platforms.woocommerce")

    def build(config: dict[str, Any], path: Path) -> SiteAdapter:
        return _Configured()

    fake.build = build  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "coffee_aggregator.platforms.woocommerce", fake)

    path = tmp_path / "shop.toml"
    path.write_text(SHOPTET_TOML.replace('"shoptet"', '"woocommerce"'), encoding="utf-8")
    assert platforms.build_from_config(path).site_id == "myshop"


def test_a_platform_without_a_module_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "shop.toml"
    path.write_text('platform = "bigcartel"\n', encoding="utf-8")
    with pytest.raises(platforms.UnsupportedPlatformError) as excinfo:
        platforms.build_from_config(path)
    assert "bigcartel" in str(excinfo.value)


def test_a_platform_name_that_is_not_a_module_name_is_rejected(tmp_path: Path) -> None:
    """A TOML must never be able to point the importer anywhere it likes."""
    path = tmp_path / "shop.toml"
    path.write_text('platform = "../../etc/passwd"\n', encoding="utf-8")
    with pytest.raises(platforms.UnsupportedPlatformError):
        platforms.build_from_config(path)
