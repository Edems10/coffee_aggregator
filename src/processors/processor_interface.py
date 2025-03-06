from abc import ABC, abstractmethod
from models.metadata import Metadata
from models.coffee import Coffee
from bs4 import BeautifulSoup


class Processor(ABC):
    @abstractmethod
    def process_metadata(self, metadata_soup: BeautifulSoup) -> Metadata:
        """Process unstrucutured metadata to model Metadata"""

    @abstractmethod
    def process_coffee(self, coffe_soup: BeautifulSoup) -> Coffee:
        """Process unstructured coffe details to model Coffee"""
