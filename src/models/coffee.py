from dataclasses import dataclass
from typing import Optional


@dataclass
class Origin:
    region: str
    farm: Optional[str] = None
    altitude: Optional[str] = None


@dataclass
class Taste:
    body: str
    bitterness: str
    acidity: str
    sweetness: str
    roast_shade: str
    processing: Optional[str] = None
    flavor_profile: Optional[list] = None
    variety: Optional[list] = None


@dataclass
class PackageInformation:
    package_size: str
    label_material: Optional[str] = None


@dataclass
class Review:
    reviews: list[str]
    review_score: float


@dataclass
class Coffee:
    id: int
    page: str
    name: str
    price: float
    origin: Origin
    taste: Taste
    package_info: Optional[PackageInformation]
    review: Optional[Review]
