from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

TESTS_ROOT = Path(__file__).parent
FIXTURE_ROOT = TESTS_ROOT / "fixtures"

#: Mirrors the pre-commit `check-added-large-files --maxkb=300` limit. Captured
#: pages are trimmed to the markup a test asserts on, not dumped whole.
MAX_FIXTURE_BYTES = 300 * 1024

#: Prefixes a platform-driven fixture directory carries; the rest is the site id
#: that the shop config and the parametrised test cases use.
PLATFORM_PREFIXES = ("shoptet_", "woo_")


def _fixture_files() -> list[Path]:
    return sorted(path for path in FIXTURE_ROOT.rglob("*") if path.is_file())


def _fixture_dirs() -> list[Path]:
    return sorted(path for path in FIXTURE_ROOT.iterdir() if path.is_dir())


def _test_module_sources() -> dict[str, str]:
    return {
        path.name: path.read_text("utf-8")
        for path in sorted(TESTS_ROOT.glob("*.py"))
        if path.name != Path(__file__).name
    }


def _fixture_id(path: Path) -> str:
    return str(path.relative_to(FIXTURE_ROOT))


@pytest.mark.parametrize("fixture", _fixture_files(), ids=_fixture_id)
def test_fixture_files_stay_small(fixture: Path) -> None:
    size = fixture.stat().st_size
    assert size <= MAX_FIXTURE_BYTES, (
        f"{fixture.relative_to(FIXTURE_ROOT)} is {size / 1024:.0f} KB, over the "
        f"{MAX_FIXTURE_BYTES // 1024} KB limit; trim the capture to the markup the test asserts on"
    )


@pytest.mark.parametrize("directory", _fixture_dirs(), ids=lambda path: path.name)
def test_every_fixture_directory_has_a_test(directory: Path) -> None:
    names = {directory.name}
    for prefix in PLATFORM_PREFIXES:
        if directory.name.startswith(prefix):
            names.add(directory.name.removeprefix(prefix))

    sources = _test_module_sources()
    referencing = sorted(
        module for module, text in sources.items() if any(name in text for name in names)
    )
    assert referencing, (
        f"tests/fixtures/{directory.name}/ is not read by any test module. Captured pages "
        f"only earn their place when a test asserts on them: add a case naming "
        f"{' or '.join(sorted(names))}, or delete the directory."
    )


# --- the compose file is wired to itself --------------------------------------

COMPOSE = Path(__file__).resolve().parent.parent / "deploy" / "compose.yml"


def compose() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(COMPOSE.read_text("utf-8"))
    return parsed


def test_every_network_a_service_joins_is_declared() -> None:
    """`main` once had a publisher on an `events` network nothing declared, and
    docker refused the whole project.

    Neither pull request was wrong on its own: one added the service and its
    network, the other added a different network to the same block, and the
    hunks were adjacent so git merged both and silently kept one. Green plus
    green made a broken `main`, and only a deploy found it.
    """
    spec = compose()
    declared = set(spec.get("networks") or {})
    for name, service in (spec.get("services") or {}).items():
        for network in service.get("networks") or []:
            assert network == "default" or network in declared, (
                f"service {name!r} joins network {network!r}, which compose does not declare"
            )


def test_every_named_volume_a_service_mounts_is_declared() -> None:
    """The same failure shape, one key over: a mount whose volume nothing declares."""
    spec = compose()
    declared = set(spec.get("volumes") or {})
    for name, service in (spec.get("services") or {}).items():
        for mount in service.get("volumes") or []:
            source = mount.split(":", 1)[0] if isinstance(mount, str) else mount.get("source", "")
            # A bind mount starts with . or /; anything else names a volume.
            if source and not source.startswith((".", "/")):
                assert source in declared, (
                    f"service {name!r} mounts volume {source!r}, which compose does not declare"
                )
