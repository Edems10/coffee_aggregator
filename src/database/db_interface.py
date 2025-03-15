from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from models.metadata import Metadata


class Database(ABC):
    @abstractmethod
    def delete_metadata(self, id: str) -> None:
        pass

    @abstractmethod
    def update_metadata(
        self, new_metadata_list: List[Metadata]
    ) -> Tuple[List[Dict], List[Dict]]:
        """Updates metadata if they don't exist create them also if
        there are different metadata that are present in database but are not present in
        new_metadata_list delete them"""
