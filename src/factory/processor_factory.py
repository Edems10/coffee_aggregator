from typing import List
from models.page import PageType
from processors.base_processor import Processor
from processors.coffein_processor import CoffeeinProcessor


class ProcessorFactory:
    @staticmethod
    def create_processor(crawler_type: str, ignored_coffes: List[str]) -> Processor:
        if crawler_type == PageType.COFFEEIN:
            return CoffeeinProcessor(ignored_coffes)
        else:
            raise ValueError(f"Unknown crawler type: {crawler_type}")
