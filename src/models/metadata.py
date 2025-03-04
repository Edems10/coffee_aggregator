from dataclasses import dataclass


@dataclass
class Metadata:
    id: str
    link: str
    name: str
    price: float
