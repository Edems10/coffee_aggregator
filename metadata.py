from dataclasses import dataclass

@dataclass
class Metadata:
    id: str
    link: str
    name : str
    price: float

    def __init__(self, id: str, link: str, name: str, price: float) -> None:
        self.id = id
        self.link = link
        self.name = name
        self.price = price