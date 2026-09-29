from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import date

    from coffee_aggregator.models import Coffee, Review, Variant


def _date_value(value: date | None, *, json_safe: bool) -> date | str | None:
    if value is None:
        return None
    return value.isoformat() if json_safe else value


def variant_record(variant: Variant) -> dict[str, Any]:
    """Flatten one packaging option into the jsonb payload it is stored as.

    Args:
        variant: The variant to flatten.

    Returns:
        A flat dictionary safe to store in a jsonb column.
    """
    return {
        "external_id": variant.external_id,
        "url": variant.url,
        "weight_g": variant.weight_g,
        "price": variant.price,
        "currency": variant.currency,
        "available": variant.available,
        "label": variant.label,
        "price_eur": variant.price_eur,
        "price_czk": variant.price_czk,
        "price_per_kg_eur": variant.price_per_kg_eur,
        "price_per_kg_czk": variant.price_per_kg_czk,
    }


def review_record(review: Review) -> dict[str, Any]:
    """Flatten one customer review into the jsonb payload it is stored as.

    Args:
        review: The review to flatten.

    Returns:
        A flat dictionary safe to store in a jsonb column.
    """
    return {
        "author": review.author,
        "date": _date_value(review.date, json_safe=True),
        "rating": review.rating,
        "text": review.text,
    }


def coffee_record(coffee: Coffee, *, json_safe: bool = True) -> dict[str, Any]:
    """Flatten a coffee into one row matching the ``coffee`` table.

    The keys are exactly the schema's data columns, in the order
    :data:`~coffee_aggregator.sinks.postgres.COLUMNS` names them, so a sink can
    feed the dictionary straight into an INSERT without reshaping it.

    Args:
        coffee: The coffee to flatten.
        json_safe: When True (the JSONL sink) dates become ISO strings and enums
            become plain strings; when False (the PostgreSQL sink) the native
            ``date`` objects are kept so psycopg can adapt them.

    Returns:
        A flat dictionary keyed by column name.
    """
    return {
        "site": coffee.site,
        "external_id": coffee.external_id,
        "url": coffee.url,
        "name": coffee.name,
        "site_country": coffee.site_country,
        "roaster": coffee.roaster_name,
        "roaster_key": coffee.roaster_key,
        "price": coffee.price,
        "currency": coffee.currency,
        "weight_g": coffee.weight_g,
        "price_per_kg": coffee.price_per_kg,
        "price_eur": coffee.price_eur,
        "price_czk": coffee.price_czk,
        "price_per_kg_eur": coffee.price_per_kg_eur,
        "price_per_kg_czk": coffee.price_per_kg_czk,
        "fx_rate_eur_czk": coffee.fx_rate_eur_czk,
        "fx_date": _date_value(coffee.fx_date, json_safe=json_safe),
        "available": coffee.available,
        "decaf": coffee.decaf,
        "origin_country": coffee.origin.country,
        "origin_region": coffee.origin.region,
        "origin_farm": coffee.origin.farm,
        "origin_producer": coffee.origin.producer,
        "origin_washing_station": coffee.origin.washing_station,
        "altitude_min_m": coffee.origin.altitude_min_m,
        "altitude_max_m": coffee.origin.altitude_max_m,
        "altitude_raw": coffee.origin.altitude_raw,
        "variety": list(coffee.origin.variety),
        "harvest": coffee.origin.harvest,
        "process_method": str(coffee.processing.method),
        "process_methods": [str(method) for method in coffee.processing.methods],
        "process_raw": coffee.processing.raw,
        "roast_level": str(coffee.roast.level),
        "roast_raw": coffee.roast.raw,
        "roast_profile": str(coffee.roast.profile),
        "roast_date": _date_value(coffee.roast.roast_date, json_safe=json_safe),
        "best_before": _date_value(coffee.roast.best_before, json_safe=json_safe),
        "arabica_pct": coffee.species.arabica_pct,
        "robusta_pct": coffee.species.robusta_pct,
        "is_blend": coffee.species.is_blend,
        "body": coffee.taste.body,
        "bitterness": coffee.taste.bitterness,
        "acidity": coffee.taste.acidity,
        "sweetness": coffee.taste.sweetness,
        "taste_scale_max": coffee.taste.scale_max,
        "flavor_notes": list(coffee.taste.flavor_notes),
        "tasting_text": coffee.taste.tasting_text,
        "brewing_methods": list(coffee.taste.brewing_methods),
        "sca_score": coffee.taste.sca_score,
        "rating": coffee.popularity.rating,
        "rating_max": coffee.popularity.rating_max,
        "review_count": coffee.popularity.review_count,
        "reviews": [review_record(review) for review in coffee.popularity.reviews],
        "sold_count": coffee.popularity.sold_count,
        "variants": [variant_record(variant) for variant in coffee.variants],
        "images": list(coffee.images),
        "tags": list(coffee.tags),
        "categories": list(coffee.categories),
        "certifications": list(coffee.certifications),
        "awards": list(coffee.awards),
        "specialty_grade": coffee.specialty_grade,
        "original_price": coffee.original_price,
        "description": coffee.description,
        "origin_text": coffee.origin_text,
        "raw_attributes": dict(coffee.raw_attributes),
        "scraped_at": coffee.scraped_at.isoformat() if json_safe else coffee.scraped_at,
    }
