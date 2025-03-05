from dataclasses import dataclass


@dataclass
class Metadata:
    id: str
    link: str
    name: str
    price: float

    def __eq__(self, other):
        if not isinstance(other, Metadata):
            return NotImplemented
        return self.id == other.id

    def __hash__(self):
        return hash(self.id)
