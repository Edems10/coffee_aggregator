from abc import ABC, abstractmethod
from typing import Generator, Union
from bs4 import BeautifulSoup


class Crawler(ABC):
    @abstractmethod
    def find_metadata(
        self, metadata_url_base: str
    ) -> Generator[BeautifulSoup, None, None]:
        """finds metadata for all coffe products"""
        pass

    @abstractmethod
    def find_coffee(self, coffe_url_base: str) -> Union[BeautifulSoup, None]:
        """finds specific coffe and all information about it"""
        pass
