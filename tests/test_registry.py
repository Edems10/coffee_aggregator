from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from coffee_aggregator import platforms, sites
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


# --- discovery survives one bad shop, and never lies about having finished ---

_SHOP_TOML = """
platform = "shoptet"
site_id = "{site_id}"
name = "Test Roastery"
country = "CZ"
base_url = "https://{site_id}.example.cz/"
category_urls = ["https://{site_id}.example.cz/kava/"]
currency = "CZK"
"""


def _discovery_ran() -> bool:
    # Read through a call: a direct ``loader._loaded is True`` narrows the type
    # and makes every later line look unreachable to the type checker.
    return loader._loaded


def _configs(tmp_path: Path, *site_ids: str) -> Path:
    configs = tmp_path / "configs"
    configs.mkdir()
    for site_id in site_ids:
        (configs / f"{site_id}.toml").write_text(_SHOP_TOML.format(site_id=site_id), "utf-8")
    return configs


def test_an_unexpected_error_in_one_config_costs_only_that_shop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _configs(tmp_path, "alpha", "beta")
    real = platforms.build_from_config

    def explode(path: Path) -> object:
        if path.name == "alpha.toml":
            message = "a mistyped table"
            raise TypeError(message)
        return real(path)

    monkeypatch.setattr(platforms, "build_from_config", explode)
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=configs, force=True)

    assert any("alpha.toml" in problem and "TypeError" in problem for problem in loader.load_errors)
    assert "beta" in registry.known_ids()


def test_discovery_that_dies_half_way_is_not_marked_as_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(package: object) -> None:
        message = "boom"
        raise RuntimeError(message)

    monkeypatch.setattr(loader, "_import_submodules", explode)
    monkeypatch.setattr(loader, "_loaded", False)

    with pytest.raises(RuntimeError):
        loader.load_all(config_dir=tmp_path / "none", force=True)

    assert _discovery_ran() is False


def test_rebuilding_a_configured_shop_is_not_a_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _configs(tmp_path, "alpha")
    monkeypatch.setattr(loader, "_loaded", False)
    loader.load_all(config_dir=configs, force=True)
    loader.load_all(config_dir=configs, force=True)

    assert loader.load_errors == []
    assert "alpha" in registry.known_ids()


def test_two_configs_claiming_one_id_are_still_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _configs(tmp_path, "alpha")
    (configs / "clash.toml").write_text(_SHOP_TOML.format(site_id="alpha"), "utf-8")
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=configs, force=True)

    assert any("clash.toml" in problem for problem in loader.load_errors)


def test_clearing_the_registry_makes_discovery_run_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _configs(tmp_path, "alpha")
    monkeypatch.setattr(loader, "_loaded", False)
    loader.load_all(config_dir=configs, force=True)
    assert _discovery_ran() is True

    registry.clear()

    # Without this the registry stays empty for the rest of the process: every
    # later call sees the "already loaded" flag and returns from nothing.
    assert _discovery_ran() is False
    assert registry.known_ids() == []
    loader.load_all(config_dir=configs)
    assert "alpha" in registry.known_ids()


def test_a_disabled_shop_is_skipped_without_being_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configs = _configs(tmp_path, "alpha", "beta")
    (configs / "beta.toml").write_text(
        _SHOP_TOML.format(site_id="beta") + '\ndisabled = "its TLS stopped matching"\n',
        "utf-8",
    )
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=configs, force=True)

    assert registry.known_ids() == ["alpha"]
    # Not an error: a shop switched off on purpose must not fail the nightly run
    # and must not show up in `list-sites`' exit code.
    assert loader.load_errors == []


def test_a_disabled_shop_states_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configs = _configs(tmp_path, "alpha")
    (configs / "alpha.toml").write_text(
        _SHOP_TOML.format(site_id="alpha") + "\ndisabled = true\n", "utf-8"
    )
    monkeypatch.setattr(loader, "_loaded", False)

    loader.load_all(config_dir=configs, force=True)

    # `true` switches a shop off too, but the point of the key is the reason, so
    # a bare boolean is recorded as having given none.
    assert registry.known_ids() == []


def test_default_ignore_list_skips_what_has_no_sound_price_per_kg() -> None:
    """Each marker was measured against the live catalogue before being added."""
    adapter = _Fake()
    assert adapter.is_ignored("Kávové předplatné na 6 měsíců") is True
    assert adapter.is_ignored("Urnex Cafiza 2 - 900g") is True
    assert adapter.is_ignored("Dárkový poukaz Pražírna Ignác") is True
    assert adapter.is_ignored("TEST Product") is True


def test_default_ignore_list_keeps_coffee_sold_in_a_gift_box() -> None:
    """ "darkov" was rejected as a marker: a gift set of 2x250 g is coffee."""
    adapter = _Fake()
    assert adapter.is_ignored("BLACK STAR Dárková sada 2x250 g (espreso)") is False
    assert adapter.is_ignored("Pražená káva v černé dárkové plechovce") is False
    assert adapter.is_ignored("Hausbrandt Gourmet Columbus 24 kg") is False


def test_an_ignore_marker_must_be_a_whole_word() -> None:
    """ "cukr" as a bare substring also matched "Káva bez cukru", and an ignored
    product is not hidden but delisted on the next run."""
    adapter = _Fake()
    assert adapter.is_ignored("Káva bez cukru 250 g") is False
    assert adapter.is_ignored("Latest Product: Kolumbie") is False
    assert adapter.is_ignored("Darčeková karta") is False
    assert adapter.is_ignored("Latest Product: Kolumbie") is False
