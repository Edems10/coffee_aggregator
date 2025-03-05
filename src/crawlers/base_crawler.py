from abc import ABC, abstractmethod
from typing import Union


class Crawler(ABC):
    @abstractmethod
    def find_metadata(self, metadata_url_base: str) -> Union[dict, None]:
        """finds metadata for all coffe products"""
        pass

    @abstractmethod
    def find_coffee(self, coffe_url_base: str) -> Union[dict, None]:
        """finds specific coffe and all information about it"""
        pass
