from dataclasses import dataclass
from typing import Optional
from models.page import PageType


@dataclass
class Metadata:
    page_id: int
    origin: PageType
    name: str
    price: float
    detail_link: str
    image_link: Optional[str] = None

    def __eq__(self, other):
        if not isinstance(other, Metadata):
            return NotImplemented
        return self.page_id == other.page_id

    def __hash__(self):
        return hash(self.page_id)
