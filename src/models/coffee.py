from dataclasses import dataclass
from typing import Optional


@dataclass
class Origin:
    region: Optional[str] = None
    farm: Optional[str] = None
    altitude: Optional[str] = None
    variety: Optional[str] = None


@dataclass
class Popularity:
    reviews: list[str]
    review_score: float
    buy_count: int


@dataclass
class Species:
    arabica: int
    robusta: int


@dataclass
class Taste:
    body: int
    bitterness: int
    acidity: int
    sweetness: int
    roast_shade: int
    species: Species
    processing: Optional[str] = None
    flavor_profile: Optional[list] = None


@dataclass
class Coffee:
    id: int
    page: str
    name: str
    price: float
    weight: int
    origin: Origin
    taste: Taste
    popularity: Optional[Popularity]
    decaf: Optional[bool] = False
