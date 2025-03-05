from typing import Generator
import requests

from collections import defaultdict
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from crawlers.base_crawler import Crawler


class CoffeeinCrawler(Crawler):
    def __init__(self, retries=3, timeout=15, max_pages=1000) -> None:
        self.base_url = "https://www.coffeein.sk/"
        self.product_metadata = defaultdict(dict)
        self.retries = retries
        self.timeout = timeout
        self.max_pages = max_pages

    def find_coffee(self, coffe_url_base):
        pass

    def is_rerouted(self, requested_url: str, response_url: str) -> bool:
        return requested_url != response_url

    def find_metadata(
        self, metadata_url_base: str
    ) -> Generator[BeautifulSoup, None, None]:
        base_metadata_url = urljoin(self.base_url, metadata_url_base)
        page_iterator = 1

        for page_iterator in range(1, self.max_pages):
            url = urljoin(base_metadata_url, f"{page_iterator}/")
            page_iterator = page_iterator + 1
            response = requests.get(url, timeout=self.timeout)

            if response.status_code == 200:
                if self.is_rerouted(url, response.url):
                    break
                yield BeautifulSoup(response.text, features="html.parser")
            else:
                print(f"Failed to retrieve the page.\nStatus: {response.status_code}")
                break

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
