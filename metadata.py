from dataclasses import dataclass


@dataclass
class Metadata:
    """
    Represents metadata information.

    Attributes:
        id (str): Unique identifier.
        link (str): Link to the associated resource.
        name (str): Name of the resource.
        price (float): Price of the resource.
    """
    id: str
    link: str
    name: str
    price: float

    def __init__(self, id: str, link: str, name: str, price: float) -> None:
        self.id = id
        self.link = link
        self.name = name
        self.price = price
