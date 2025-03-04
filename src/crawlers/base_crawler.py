from abc import ABC, abstractmethod
from typing import List
from models.metadata import Metadata
from models.coffe import Coffee

class Crawler(ABC):
    
    @abstractmethod
    def find_metadata(self)->List[Metadata]:
        """finds metadata for all coffe products"""
        pass
    
    @abstractmethod
    def link_coffe_details(self,metadata:Metadata)->str:
        """builds link for specific coffe products found via metadata search"""
        pass
    
    @abstractmethod
    def find_coffee(self,coffe_link:str)->Coffee:
        """finds specific coffe and all information about it"""
        pass