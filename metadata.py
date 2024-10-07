from dataclasses import dataclass

@dataclass
class Metadata:
    id: str
    name: str
    price: float

    def __init__(self, id: str, name: str, price: float) -> None:
        self.id = id
        self.name = name
        self.price = price