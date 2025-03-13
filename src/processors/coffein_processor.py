from bs4 import BeautifulSoup
import re
from typing import List, Union
from processors.processor_interface import Processor
from errors.crawler_error import ProcessorError
import unidecode
from models.metadata import Metadata
from models.coffee import Coffee, Origin, Popularity, Taste, Species
import json
from models.page import PageType
from assets.constants import (
    DIV,
    SPAN,
    SCRIPT,
    UNKNONW,
    H1,
    DESCRIPTION,
    SWEETNEST_COFFEEIN,
    ACIDITY_COFFEEIN,
    BITTERNESS_COFFEEIN,
    BODY_COFFEIN,
)


class CoffeeinProcessor(Processor):
    ignored_coffees = None

    def __init__(self, ignored_coffees: List[str] = None) -> None:
        super().__init__()
        self.ignored_coffees = ignored_coffees or []

    def process_metadata(self, metadata_soup: BeautifulSoup) -> List[Metadata]:
        page_items = self.get_items(metadata_soup)
        metadata_list = self.get_metadata(page_items)
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

            link = (
                re.sub(r"[ %(),.-]+", "-", unidecode.unidecode(name_unfiltered))
                .strip("-")
                .lower()
            )
            metadata = Metadata(
                int(item.get("item_id")),
                detail_link=link,
                name=decoded_name,
                origin=PageType.COFFEEIN.name,
                price=float(item.get("price")),
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

    def process_coffee(self, coffee_soup: BeautifulSoup) -> Coffee:

        name = coffee_soup.find(H1, itemprop="name").text.strip()
        price = float(coffee_soup.find(SPAN, class_="product_price").get("content"))

        species = self.handle_species(coffee_soup)
        page_id = self.handle_page_id(coffee_soup)
        weight = self.handle_size(name, coffee_soup)
        popularity = self.handle_popularity(coffee_soup)
        decaf = self.handle_decaf(name, coffee_soup)
        if species.arabica == 100 or species.robusta == 100:
            origin = self.handle_origin(coffee_soup)
            taste = self.handle_taste(coffee_soup)
        else:
            return
            origin = self.handle_mixed_origin(coffee_soup)
            taste = self.handle_mixed_taste(coffee_soup)
        # Extract page_id from script tags
        

        # Create and return Coffee object
        return Coffee(
            id=page_id,
            page=PageType.COFFEEIN.name,
            name=name,
            price=price,
            origin=origin,
            taste=taste,
            weight=weight,
            popularity=popularity,
            decaf=decaf,
        )

    def handle_page_id(self, coffee_soup: BeautifulSoup) -> int | None:
        page_id = None
        scripts = coffee_soup.find_all(SCRIPT)
        for script in scripts:
            script_text = script.string if script.string else ""
            if "'item_id': " in script_text:
                match = re.search(r"'item_id': '(\d+)'", script_text)
                if match:
                    page_id = int(match.group(1))
                    break
            elif "setEcommerceView" in script_text:
                match = re.search(r'setEcommerceView\s*,\s*"(\d+)"', script_text)
                if match:
                    page_id = int(match.group(1))
                    break
        return page_id

    def handle_taste(self, coffee_soup: BeautifulSoup) -> Taste:
        taste_ratings = self.handle_taste_ratings(coffee_soup)
        roast_shade = self.handle_roast_shade(coffee_soup)
        flavor_profile = self.handle_flavor_profile(coffee_soup)
        processing = self.handel_processing(coffee_soup)

        return Taste(
            body=taste_ratings[BODY_COFFEIN],
            bitterness=taste_ratings[BITTERNESS_COFFEEIN],
            acidity=taste_ratings[ACIDITY_COFFEEIN],
            sweetness=taste_ratings[SWEETNEST_COFFEEIN],
            roast_shade=roast_shade,
            processing=processing,
            flavor_profile=flavor_profile,
            species=self.handle_species(coffee_soup),
        )

    def handel_processing(self, coffee_soup: BeautifulSoup) -> str | None:
        processing = None
        if match := re.search(r"SPRACOVANIE:\s*(.+)", coffee_soup.text):
            processing = match.group(1).strip()
        return processing

    def handle_flavor_profile(self, coffee_soup: BeautifulSoup) -> list:
        flavor_profile = []
        for div in coffee_soup.find_all(DIV, class_="recommended_preparation"):
            spans = div.find_all(SPAN, recursive=False)
            for span in spans:
                text = span.get_text().strip()
                if text:
                    flavor_profile.append(text)
        return flavor_profile

    def handle_roast_shade(self, coffee_soup: BeautifulSoup) -> str:
        roast_shade = None
        description = coffee_soup.find("p", itemprop=DESCRIPTION)
        if description and (
            match := re.search(r"Odtieň praženia:\s*([^<\n]+)", description.text)
        ):
            roast_shade_text = match.group(1).strip()
            # Extract just the roast shade by splitting at common delimiters
            roast_shade = roast_shade_text.split("Veľkosť")[0].strip()
            roast_shade = roast_shade.split("Metóda")[0].strip()
        return roast_shade

    def handle_taste_ratings(self, coffee_soup: BeautifulSoup) -> dict:
        taste_ratings = {
            "telo": UNKNONW,
            "horkosť": UNKNONW,
            "acidita": UNKNONW,
            "sladkosť": UNKNONW,
        }

        for param in coffee_soup.find_all(DIV, class_="speci_param"):
            param_name = param.find(DIV, class_="speci_param_name")
            if param_name:
                name = param_name.text.strip().lower()
                if name in taste_ratings:
                    points_full = len(param.find_all(SPAN, class_="point_full"))
                    points_total = points_full + len(
                        param.find_all(SPAN, class_="point_empty")
                    )

                    if points_total > 0:
                        percentage = int((points_full / points_total) * 100)
                        taste_ratings[name] = percentage
        return taste_ratings

    def handle_species(self, coffee_soup: BeautifulSoup) -> Species:
        description = coffee_soup.find("p", itemprop="description")

        species = self.handle_species_description(description)
        if species:
            return species

        species = self.handle_species_tags(description)
        if species:
            return species

        return Species(arabica=0, robusta=0)

    def handle_species_description(self, description) -> Species | None:
        arabica_percent = None
        robusta_percent = None
        arabica_match = re.search(
            r"(\d+)\s*%\s*(arabika|arabica)", description.text.lower()
        )
        robusta_match = re.search(r"(\d+)\s*%\s*robusta", description.text.lower())

        if arabica_match:
            arabica_percent = int(arabica_match.group(1))
        if robusta_match:
            robusta_percent = int(robusta_match.group(1))

        if arabica_percent and not robusta_percent:
            robusta_percent = 100 - arabica_percent
        elif robusta_percent and not arabica_percent:
            arabica_percent = 100 - robusta_percent

        return Species(arabica=arabica_percent, robusta=robusta_percent)

    def handle_species_tags(self, description) -> Species | None:
        arabica_percent = None
        robusta_percent = None

        if description:
            strong_tags = description.find_all("strong")
            for strong in strong_tags:
                strong_text = strong.text.strip().lower()
                arabica_match = re.search(r"(\d+)\s*%\s*(arabika|arabica)", strong_text)
                robusta_match = re.search(r"(\d+)\s*%\s*robusta", strong_text)

                if arabica_match:
                    arabica_percent = int(arabica_match.group(1))
                if robusta_match:
                    robusta_percent = int(robusta_match.group(1))
        if arabica_percent and robusta_percent:
            return Species(arabica=arabica_percent, robusta=robusta_percent)
        else:
            return None

    def handle_origin(self, coffee_soup: BeautifulSoup) -> Origin:
        origin_region = None
        origin_farm = None
        origin_altitude = None
        origin_variety = None

        additional_info = coffee_soup.find("div", class_="long_desc_desc")
        if additional_info:
            additional_text = additional_info.text

            origin_match = re.search(r"ODRODA:\s*(.+)", additional_text)
            if origin_match:
                origin_variety = origin_match.group(1).strip()

            region_match = re.search(r"REGIÓN:\s*(.+)", additional_text)
            if region_match:
                origin_region = region_match.group(1).strip()

            farm_match = re.search(r"FARMA:\s*(.+)", additional_text)
            if farm_match:
                origin_farm = farm_match.group(1).strip()

            altitude_match = re.search(r"NADMORSKÁ VÝŠKA:\s*(.+)", additional_text)
            if altitude_match:
                origin_altitude = altitude_match.group(1).strip()

        return Origin(
            region=origin_region,
            farm=origin_farm,
            altitude=origin_altitude,
            variety=origin_variety,
        )

    def extract_weight_in_grams(self, text):
        number_str = re.findall(r"\d+\.?\d*", text)[0]
        number = float(number_str)

        if "kg" in text.lower():
            return int(number * 1000)
        elif "g" in text.lower():
            return int(number)
        else:
            return int(number)

    def handle_size(self, name: str, coffee_soup: BeautifulSoup) -> int:
        if "(1000 g" in name:
            return 1000
        elif "(500 g" in name:
            return 500
        elif "(200 g" in name:
            return 200
        elif "(100 g" in name:
            return 100
        description = coffee_soup.find("p", itemprop=DESCRIPTION)
        if description:
            size_match = re.search(r"Veľkosť balenia:\s*([^<\n]+)", description.text)
            if size_match:
                return self.extract_weight_in_grams(size_match.group(1).strip())

    def handle_popularity(self, coffee_soup: BeautifulSoup) -> Popularity:
        reviews = []
        review_score = 0.0
        buy_count = 0

        review_section = coffee_soup.find(DIV, id="ranks_box")
        if review_section:
            for review_item in review_section.find_all("li", itemprop="review"):
                review_text_div = review_item.find(DIV, class_="rank_right")
                if review_text_div and review_text_div.text.strip():
                    reviews.append(review_text_div.text.strip())

            rating_value = review_section.find("meta", itemprop="ratingValue")
            if rating_value:
                review_score = float(rating_value.get("content", "0"))

        popis_date_data = coffee_soup.find(DIV, class_="popis_date_data")
        if popis_date_data:
            popularity_text = popis_date_data.text
            popularity_match = re.search(
                r"Upražené a vypité:\s*(\d+)x", popularity_text
            )
            if popularity_match:
                buy_count = int(popularity_match.group(1))

        return Popularity(
            reviews=reviews, review_score=review_score, buy_count=buy_count
        )

    def handle_decaf(self, name: str, coffee_soup: BeautifulSoup) -> bool:
        description = coffee_soup.find("p", itemprop=DESCRIPTION)
        is_decaf = False
        if "BEZKOFEINOVÁ" in name.upper() or "BEZKOFEÍNOVÁ" in name.upper():
            is_decaf = True
        else:
            if description and (
                "bezkofeinová" in description.text.lower()
                or "bezkofeínová" in description.text.lower()
            ):
                is_decaf = True

        if (
            is_decaf
            and "bezkofeinová" not in name.lower()
            and "bezkofeínová" not in name.lower()
        ):
            name = f"BEZKOFEÍNOVÁ {name}"

        return is_decaf
