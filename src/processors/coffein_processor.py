from bs4 import BeautifulSoup
import re
from typing import List, Union
from processors.processor_interface import Processor
from errors.crawler_error import ProcessorError
import unidecode
from models.metadata import Metadata
from models.coffee import Coffee
import json
from models.page import PageType


class CoffeeinProcessor(Processor):
    ignored_coffees = None

    def __init__(self, ignored_coffees: List[str] = None) -> None:
        super().__init__()
        self.ignored_coffees = ignored_coffees or []

    def process_metadata(self, metadata_soup: BeautifulSoup) -> List[Metadata]:
        items = self.get_items(metadata_soup)
        metadata_list = self.get_metadata(items)
        return metadata_list

    def get_items(self, soup: BeautifulSoup) -> List[dict]:
        scripts = soup.find_all("script")
        for script in scripts:
            if "view_item_list" not in script.text:
                continue
            metadata_regex = re.search(
                r'"items":\s*\[(.*?)\]\s*\}', script.text, re.DOTALL
            )
            if not metadata_regex:
                raise ProcessorError("Failed to find product data in script tags")
            try:
                return self.filter_regex((metadata_regex))
            except json.JSONDecodeError as e:
                raise ProcessorError(f"Failed to parse JSON: {str(e)}")

    def filter_regex(self, metadata_regex: re.Match) -> List[dict]:
        metadata = metadata_regex.group(1)
        metadata = metadata.replace("'", '"')
        metadata = re.sub(r",\s*}", "}", metadata)
        return json.loads(f"[{metadata}]")

    def get_metadata(self, items: List[dict]) -> Union[List[Metadata], None]:
        metadata_list = []
        for item in items:
            name_unfiltered = item.get("item_name")
            decoded_name = name_unfiltered.encode("utf-8").decode("unicode_escape")

            link = re.sub(r'[ %(),.-]+', '-', unidecode.unidecode(name_unfiltered)).strip('-').lower()
            metadata = Metadata(int(item.get("item_id")),
                detail_link=link,
                name=decoded_name,
                origin=PageType.COFFEEIN.name,
                price=float(item.get("price"))
            )

            if metadata not in metadata_list and not self.is_ignored_coffee(
                metadata.name
            ):
                metadata_list.append(metadata)

        return metadata_list

    def is_ignored_coffee(self, coffe_name: str) -> bool:
        for ignore_coffe in self.ignored_coffees:
            if ignore_coffe.lower() in coffe_name.lower():
                return True
        return False

    def process_coffee(self, coffe_soup) -> Coffee:
        return super().process_coffee(coffe_soup)
