from dataclasses import dataclass
from typing import Optional
from models.page import PageType

@dataclass
class Metadata:
    id: str
    origin:PageType
    name: str
    price: float
    detail_link: str
    image_link: Optional[str] = None

    def __eq__(self, other):
        if not isinstance(other, Metadata):
            return NotImplemented
        return self.id == other.id

    def __hash__(self):
        return hash(self.id)
