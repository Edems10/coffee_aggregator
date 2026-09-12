from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator import sites
from coffee_aggregator.sites import loader, registry
from coffee_aggregator.sites.base import ProductRef, SiteAdapter

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    saved_classes = dict(registry._REGISTRY)
    saved_instances = dict(registry._INSTANCES)
    saved_errors = list(loader.load_errors)
    registry.clear()
    yield
    registry.clear()
    registry._REGISTRY.update(saved_classes)
    registry._INSTANCES.update(saved_instances)
    loader.load_errors[:] = saved_errors


class _Fake(SiteAdapter):
    site_id = "fake"
    name = "Fake Roastery"
    country = "SK"
    base_url = "https://fake.example.sk"

    def discover(
        self,
        fetcher: object,
        *,
        max_pages: int | None = None,
    ) -> Iterator[ProductRef]:
        yield ProductRef(site_id=self.site_id, external_id="1", url=f"{self.base_url}/1")

    def parse_product(self, html: str, ref: ProductRef) -> None:
        return None


def test_register_and_get_returns_a_singleton_instance() -> None:
    registry.register(_Fake)
    assert registry.known_ids() == ["fake"]
    assert registry.instance("fake") is registry.instance("fake")


def test_duplicate_site_id_is_rejected() -> None:
    registry.register(_Fake)
    with pytest.raises(registry.DuplicateSiteError):
        registry.register(_Fake)


def test_unknown_site_id_names_the_known_ones() -> None:
    registry.register(_Fake)
    with pytest.raises(registry.UnknownSiteError) as excinfo:
        registry.instance("nope")
    assert "fake" in str(excinfo.value)


def test_adapter_without_site_id_is_rejected() -> None:
    class Nameless(_Fake):
        site_id = ""

    with pytest.raises(ValueError, match="site_id"):
        registry.register(Nameless)


def test_load_all_imports_site_modules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = textwrap.dedent(
        """
        from __future__ import annotations

        from collections.abc import Iterator

        from coffee_aggregator.sites.base import ProductRef, SiteAdapter
        from coffee_aggregator.sites.registry import register


        @register
        class Discovered(SiteAdapter):
            site_id = "discovered"
            name = "Discovered"
            country = "CZ"
            base_url = "https://discovered.example.cz"

            def discover(
                self,
                fetcher: object,
                *,
                max_pages: int | None = None,
            ) -> Iterator[ProductRef]:
                return iter(())

            def parse_product(self, html: str, ref: ProductRef) -> None:
                return None
        """
    )
    real_sites = sys.modules["coffee_aggregator.sites"]
    extra = tmp_path / "extra_sites"
    extra.mkdir()
    (extra / "discovered_site.py").write_text(module, encoding="utf-8")
    monkeypatch.setattr(real_sites, "__path__", [*real_sites.__path__, str(extra)])
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=tmp_path / "no-configs", force=True)
    assert "discovered" in registry.known_ids()
    assert sites.get("discovered").country == "CZ"
    # not an exact list: walk_packages also (re-)imports the shipped adapters,
    # and whether they register depends on what the rest of the suite imported first
    assert "discovered" in [adapter.site_id for adapter in sites.all_sites()]


def test_default_ignore_list_skips_non_coffee() -> None:
    adapter = _Fake()
    assert adapter.is_ignored("Tasting pack 4x50g") is True
    assert adapter.is_ignored("Cascara 100 g") is True
    assert adapter.is_ignored("Kuba Serrano") is False
    assert adapter.is_ignored(None) is False


def test_list_sites_works_with_zero_registered_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loader, "_loaded", True)
    assert sites.all_sites() == []


def test_a_broken_toml_is_collected_instead_of_silently_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "broken.toml").write_text('platform = "shoptet"\nsite_id = \n', encoding="utf-8")
    (configs / "nameless.toml").write_text(
        'platform = "shoptet"\nname = "x"\ncountry = "CZ"\nbase_url = "https://x/"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=configs, force=True)

    assert len(loader.load_errors) == 2
    assert any("broken.toml" in problem for problem in loader.load_errors)
    assert any("site_id" in problem for problem in loader.load_errors)


def test_load_errors_are_cleared_on_a_clean_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader.load_errors.append("stale")
    monkeypatch.setattr(loader, "_loaded", False)
    loader.load_all(config_dir=tmp_path / "none", force=True)
    assert loader.load_errors == []
