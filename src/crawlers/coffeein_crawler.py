import json
import re
from typing import Union
import requests
import unidecode

from collections import defaultdict
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from models.metadata import Metadata
from crawlers.base_crawler import Crawler
from errors.crawler_error import ScraperError


class CoffeeinCrawler(Crawler):
    def __init__(
        self, retries=3, timeout=15, max_pages=1000, ignore_words=None
    ) -> None:
        self.base_url = "https://www.coffeein.sk/"
        self.product_metadata = defaultdict(dict)
        self.retries = retries
        self.timeout = timeout
        self.max_pages = max_pages
        self.ignore_words = ignore_words or ["tasting pack"]

    def find_coffee(self, coffe_url_base):
        pass

    def get_items(self, match: str):
        items_str = match.group(1)
        items_str = items_str.replace("'", '"')
        items_str = re.sub(r",\s*}", "}", items_str)
        items_str = re.sub(r",\s*]", "]", items_str)
        return json.loads(f"[{items_str}]")

    def check_ignore_words(self, string) -> bool:
        if self.ignore_words:
            for ignore_word in self.ignore_words:
                if ignore_word.lower() in string.lower():
                    return True
        return False

    def filter_items(self, items) -> Union[dict, bool]:
        product_metadata = defaultdict(dict)
        for item in items:
            name_unfiltered = item.get("item_name")
            decoded_name = name_unfiltered.encode("utf-8").decode("unicode_escape")

            link = self.create_link(name_unfiltered)
            link = (
                unidecode.unidecode(name_unfiltered)
                .replace(" % ", "-")
                .replace(" - ", "-")
                .replace(" (", "-")
                .replace(") ", "-")
                .replace(",", "")
                .replace(" ", "-")
                .replace("(", "")
                .replace(")", "")
                .lower()
            )
            metadata = Metadata(
                id=item.get("item_id"),
                link=link,
                name=decoded_name,
                price=float(item.get("price")),
            )

            product_details = {
                "name": metadata.name,
                "price": metadata.price,
                "link": metadata.link,
            }

            if not self.check_ignore_words(metadata.name):
                self.product_metadata[metadata.id] = product_details

        print(f"Currently found {len(self.product_metadata)} products")
        return True

    # Look into the view_item_list if there is a better way
    def filter_soup(self, soup: BeautifulSoup, url: str) -> bool:
        scripts = soup.find_all("script")
        for script in scripts:
            if "view_item_list" not in script.text:
                continue

            match = re.search(r'"items":\s*\[(.*?)\]\s*\}', script.text, re.DOTALL)
            if not match:
                raise ScraperError(
                    url=url, message="Failed to find product data in script tags"
                )
            try:
                items = self.get_items((match))
                return self.filter_items(items)

            except json.JSONDecodeError as e:
                raise ScraperError(url=url, message=f"JSON decode error: {e}") from e

    def is_rerouted(self, requested_url: str, response_url: str) -> bool:
        return requested_url != response_url

    def find_metadata(self, metadata_url_base: str) -> dict:
        base_metadata_url = urljoin(self.base_url, metadata_url_base)
        page_iterator = 1

        for page_iterator in range(1, self.max_pages):
            url = urljoin(base_metadata_url, f"{page_iterator}/")
            page_iterator = page_iterator + 1
            response = requests.get(url, timeout=self.timeout)

            if response.status_code == 200:
                if self.is_rerouted(url, response.url):
                    break

                soup_response = BeautifulSoup(response.text, features="html.parser")
                if not self.filter_soup(soup_response, url):
                    break
            else:
                print(
                    f"Failed to retrieve the page. Status code: {response.status_code}"
                )
                break
        with open(self.output, "w") as json_file:
            json.dump(self.product_metadata, json_file, indent=4)
        return True

    def generate_specific_page_url(self, link, item_id):
        return urljoin(self.base_url, f"detail/{link}/{item_id}")

    def find_coffee_details(self, metadata=None):
        if not metadata:
            metadata = self.product_metadata
        if metadata is None:
            raise ValueError("Metadata is not present")

        coffee_details = {}

        for item_id, item_data in metadata.items():
            detail_url = self.generate_specific_page_url(item_id, item_data.get("link"))
            response = requests.get(detail_url, timeout=self.timeout)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, features="html.parser")
                coffee_data = self.extract_coffee_details(soup, detail_url, item_id)
                coffee_details[item_id] = coffee_data
            else:
                print(f"Failed to retrieve page {detail_url}: {response.status_code}")

        return coffee_details
