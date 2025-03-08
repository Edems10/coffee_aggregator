from abc import ABC, abstractmethod
from typing import Generator
from bs4 import BeautifulSoup
from models.metadata import Metadata

class Crawler(ABC):
    @abstractmethod
    def find_metadata(
        self, metadata_url_base: str
    ) -> Generator[BeautifulSoup, None, None]:
        """finds metadata for all coffe products"""
        pass

    @abstractmethod
    def find_coffee(self, metadata: list[Metadata]) -> Generator[BeautifulSoup, None, None]:
        """finds specific coffe and all information about it"""
        pass
