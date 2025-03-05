from abc import ABC, abstractmethod
from models.metadata import Metadata
from models.coffee import Coffee
from models.page import PageType


class Processor(ABC):
    @abstractmethod
    def process_metadata(self, metadata_dict: dict) -> Metadata:
        """Process unstrucutured metadata to model Metadata"""

    @abstractmethod
    def process_coffee(self, coffe_dict: dict) -> Coffee:
        """Process unstructured coffe details to model Coffee"""


class ProcessorFactory:
    @staticmethod
    def create_crawler(crawler_type: str, **kwargs) -> Processor:
        if crawler_type == PageType.COFFEEIN:
            return Processor(**kwargs)
        else:
            raise ValueError(f"Unknown crawler type: {crawler_type}")
