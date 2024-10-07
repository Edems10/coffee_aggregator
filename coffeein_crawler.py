import json
import re
import unidecode
from collections import defaultdict
import logging
import requests
from bs4 import BeautifulSoup
from metadata import Metadata

logging.basicConfig(filename="scraper.log", level=logging.ERROR)


class ScraperError(Exception):
    """Custom exception for scraper errors."""

    def __init__(self, url, message):
        self.url = url
        self.status_code = message
        super().__init__(f"Error scraping {url}: {message}")


class CoffeeinCrawler:
    """
    A web scraper class that extracts product data from a specific website.

    Attributes:
        base_url (str): The base URL of the website to scrape.
        output (str): The file path to save the scraped product metadata.
        retries (int): The number of retries for failed HTTP requests. Defaults to 3.
        timeout (int): The timeout for HTTP requests in seconds. Defaults to 15.
        max_pages (int): The maximum number of pages to scrape. Defaults to 1000.
        ignored_words (list): A list of words to ignore in product names. Defaults to None.

    Methods:
        get_items(match: str): Extracts a list of items from a regex match and converts it to a JSON list.
        check_ignored_words(string: str): Checks if a given string contains any of the ignored words.
        filter_items(items: list): Filters scraped items based on existing metadata and ignored words.
        filter_soup(soup: BeautifulSoup, url: str): Extracts product data from a BeautifulSoup object.
        find_metadata(): Finds all metadata from the pages and saves it to the output file.

    Raises:
        ScraperError: If an error occurs during the scraping process.
    """

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

    def get_items(self, match: str):
        items_str = match.group(1)
        items_str = items_str.replace("'", '"')
        items_str = re.sub(r",\s*}", "}", items_str)  # Remove trailing commas
        items_str = re.sub(r",\s*]", "]", items_str)  # Remove trailing commas
        return json.loads(f"[{items_str}]")  # Wrap in [] to make it a list

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
                .replace(" % ","-")
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
            url = f"{self.base_url}{iterator}/"
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
        # TODO add upload to database
        # TODO create comparison between databse and found
        with open(self.output, "w") as json_file:
            json.dump(self.product_metadata, json_file, indent=4)
        return True


def main():
    cc = CoffeeinCrawler(
        base_url="https://www.coffeein.sk/kategoria/2/cerstvo-prazena-zrnkova-kava/",
        timeout=15,
        retries=3,
        ignore_words=["tasting pack"],
        output="coffein_metadata.json",
    )
    cc.find_metadata()


if __name__ == "__main__":
    main()
