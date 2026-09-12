from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from coffee_aggregator.models import (
    Coffee,
    Origin,
    Popularity,
    Processing,
    ProcessMethod,
    Review,
    Roast,
    RoastLevel,
    RoastProfile,
    Species,
    Taste,
    Variant,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html() -> Callable[[str, str], str]:
    def _load(site: str, name: str) -> str:
        return (FIXTURE_ROOT / site / name).read_text("utf-8")

    return _load


def make_coffee(site: str = "demo", external_id: str = "1", **overrides: object) -> Coffee:
    """Build a fully populated Coffee so round-trip tests touch every field."""
    coffee = Coffee(
        site=site,
        external_id=external_id,
        url=f"https://example.sk/detail/{external_id}",
        name="Kuba Serrano Superior",
        site_country="SK",
        price=9.99,
        currency="EUR",
        weight_g=200,
        available=True,
        decaf=False,
        origin=Origin(
            country="CU",
            region="Sierra Maestra",
            farm="Finca La Esperanza",
            producer="Ramon",
            washing_station="Central",
            altitude_min_m=1200,
            altitude_max_m=1400,
            altitude_raw="1200 - 1400 m n. m.",
            variety=["Typica", "Bourbon"],
            harvest="2024",
        ),
        processing=Processing(method=ProcessMethod.WASHED, raw="praná"),
        roast=Roast(
            level=RoastLevel.MEDIUM,
            raw="stredné",
            profile=RoastProfile.OMNI,
            roast_date=date(2026, 9, 1),
            best_before=date(2027, 9, 1),
        ),
        species=Species(arabica_pct=100, robusta_pct=0, other=None, is_blend=False),
        taste=Taste(
            body=4,
            bitterness=2,
            acidity=3,
            sweetness=4,
            scale_max=5,
            flavor_notes=["kakao", "karamel"],
            tasting_text="Plné telo.",
            brewing_methods=["espresso", "moka"],
            sca_score=84.5,
        ),
        popularity=Popularity(
            rating=4.8,
            rating_max=5,
            review_count=12,
            reviews=[Review(author="Jana", date=date(2026, 1, 2), rating=5.0, text="super")],
            sold_count=2431,
        ),
        variants=[Variant("1-250", "https://example.sk/detail/1", 250, 11.5, "EUR", True, "250 g")],
        images=["https://example.sk/img/1.jpg"],
        tags=["novinka"],
        categories=["Káva", "Jednodruhová"],
        certifications=["BIO"],
        awards=["Cup of Excellence"],
        specialty_grade=True,
        original_price=12.5,
        description="Popis kávy.",
        origin_text="Kuba, Sierra Maestra",
        raw_attributes={"KRAJINA": "Kuba", "HMOTNOSŤ": "200 g"},
        scraped_at=datetime(2026, 9, 12, 10, 0, tzinfo=UTC),
    )
    for key, value in overrides.items():
        setattr(coffee, key, value)
    return coffee
