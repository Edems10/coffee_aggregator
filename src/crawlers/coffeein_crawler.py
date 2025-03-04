import json
import re
import chompjs
import unidecode
from collections import defaultdict
import logging
import requests
from bs4 import BeautifulSoup
from src.models.coffe import Coffee
from src.models.metadata import Metadata

from urllib.parse import urljoin


logging.basicConfig(filename="scraper.log", level=logging.ERROR)


class ScraperError(Exception):
    """Custom exception for scraper errors."""

    def __init__(self, url, message):
        self.url = url
        self.status_code = message
        super().__init__(f"Error scraping {url}: {message}")


class CoffeeinCrawler:
    def __init__(
        self,
        base_url: str,
        output: str,
        retries=3,
        timeout=15,
        max_pages=1000,
        ignore_words=None,
    ) -> None:
        self.base_url = base_url
        self.output = output
        self.product_metadata = defaultdict(dict)
        self.ignored_words = ignore_words
        self.retries = retries
        self.timeout = timeout
        self.max_pages = max_pages
        self.base_metadata_url = urljoin(
            base_url, "kategoria/2/cerstvo-prazena-zrnkova-kava/"
        )

    def get_items(self, match: str):
        items_str = match.group(1)
        items_str = items_str.replace("'", '"')
        items_str = re.sub(r",\s*}", "}", items_str)
        items_str = re.sub(r",\s*]", "]", items_str)
        return json.loads(f"[{items_str}]")

    def check_ignored_words(self, string) -> bool:
        if self.ignored_words:
            for ignored_word in self.ignored_words:
                if ignored_word.lower() in string.lower():
                    return True
        return False

    def filter_items(self, items) -> bool:
        for item in items:
            name_unfiltered = item.get("item_name")
            decoded_name = name_unfiltered.encode("utf-8").decode("unicode_escape")

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

            if metadata.id in self.product_metadata:
                print("Found the same product twice. Ending...")
                return False

            product_details = {
                "name": metadata.name,
                "price": metadata.price,
                "link": metadata.link,
            }

            if not self.check_ignored_words(metadata.name):
                self.product_metadata[metadata.id] = product_details

        print(f"Currently found {len(self.product_metadata)} products")
        return True

    def filter_soup(self, soup: BeautifulSoup, url: str) -> bool:
        scripts = soup.find_all("script")
        for script in scripts:
            if "view_item_list" not in script.text:
                continue
            # Use regex to extract the 'items' array
            match = re.search(r'"items":\s*\[(.*?)\]\s*\}', script.text, re.DOTALL)
            if not match:
                # Raise custom error if the match is not found
                error_msg = "Failed to find product data in script tags"
                logging.error(error_msg)
                raise ScraperError(
                    url=url, message="Failed to find product data in script tags"
                )
            try:
                items = self.get_items((match))
                return self.filter_items(items)

            except json.JSONDecodeError as e:
                error_msg = f"JSON decode error: {e}"
                logging.error(error_msg)
                raise ScraperError(url=url, message=f"JSON decode error: {e}") from e

    def find_metadata(self) -> bool:
        page_exists = True
        iterator = 1

        while page_exists and iterator <= self.max_pages:
            url = urljoin(self.base_metadata_url, f"{iterator}/")
            iterator = iterator + 1
            response = requests.get(url, timeout=self.timeout)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, features="html.parser")
                if not self.filter_soup(soup, url):
                    page_exists = False
            else:
                print(
                    f"Failed to retrieve the page. Status code: {response.status_code}"
                )
                page_exists = False
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
                logging.error(
                    f"Failed to retrieve page {detail_url}: {response.status_code}"
                )

        return coffee_details

    def extract_coffee_details(
        self, soup: BeautifulSoup, url: str, item_id: str
    ) -> Coffee:
        try:
            # Find the script containing gtag event data
            script_content = soup.find(
                "script", text=lambda t: t and "gtag('event', 'view_item'" in t
            )

            if script_content:
                # Parse the JavaScript object directly using chompjs
                parsed_data = chompjs.parse_js_objects(script_content.string)[0]

                # Extract the first item from the items array
                product_info = parsed_data["items"][0]

                return Coffee(
                    id=int(item_id),
                    page=url,
                    name=product_info.get("item_name", ""),
                    roast_shade="",
                    package_size="",
                    label_material="",
                    flavor_profile=[],
                    body="",
                    bitterness="",
                    acidity="",
                    sweetness="",
                    region="",
                    farm="",
                    variety=[],
                    processing="",
                    altitude="",
                    reviews=[],
                    review_score=0.0,
                )

        except Exception as e:
            logging.error(f"Error extracting coffee details from {url}: {str(e)}")
            return Coffee(
                id=int(item_id),
                page=url,
                name="N/A",
                roast_shade="",
                package_size="",
                label_material="",
                flavor_profile=[],
                body="",
                bitterness="",
                acidity="",
                sweetness="",
                region="",
                farm="",
                variety=[],
                processing="",
                altitude="",
                reviews=[],
                review_score=0.0,
            )


def main():
    cc = CoffeeinCrawler(
        base_url="https://www.coffeein.sk/",
        timeout=15,
        retries=3,
        ignore_words=["tasting pack"],
        output="coffein_metadata.json",
    )
    cc.find_metadata()
    cc.find_coffee_details()


if __name__ == "__main__":
    main()
