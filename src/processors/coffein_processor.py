from bs4 import BeautifulSoup
import re
from typing import List, Union
from models.variety import Variety
from processors.processor_interface import Processor
from errors.crawler_error import ProcessorError
import unidecode
from models.metadata import Metadata
from models.coffee import Coffee, Origin, Review, Taste
import json
from models.page import PageType
from assets.constants import DIV,SPAN,SCRIPT,UNKNONW,H1,ROBUSTA_COFFEEIN,ARABICA_COFFEEIN,DESCRIPTION,COFFEE_ORIGIN,SWEETNEST_COFFEEIN,ACIDITY_COFFEEIN,BITTERNESS_COFFEEIN,BODY_COFFEIN

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

    def process_coffee(self, coffee_soup: BeautifulSoup) -> Coffee:
        """Process unstructured coffee details to model Coffee"""
        # Extract basic information
        name = coffee_soup.find(H1, itemprop='name').text.strip()
        price = float(coffee_soup.find(SPAN, class_='product_price').get('content'))
        ## DECAF, NORMAL, BLEND
        # self.handle_coffe_type()
        #TODO handle MIXED :)
        if self.handle_variety(coffee_soup)== Variety.Mixed:
            pass
        
        # Extract page_id from script tags
        page_id = self.handle_page_id(coffee_soup)
        origin = self.handle_origin(coffee_soup)
        taste = self.handle_taste(coffee_soup)
        weight = self.handle_size(name,coffee_soup)
        review = self.handle_reviews(coffee_soup)
        decaf = self.handle_decaf(name,coffee_soup)
        

        # Create and return Coffee object
        return Coffee(
            id=page_id,
            page=PageType.COFFEEIN.name,
            name=name,
            price=price,
            origin=origin,
            taste=taste,
            weight=weight,
            review=review,
            decaf=decaf
        )

    def handle_page_id(self, coffee_soup:BeautifulSoup)->int|None:
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
        """Extract taste information from coffee product page"""
        
        taste_ratings = {"telo": UNKNONW, "horkosť": UNKNONW, 
                        "acidita": UNKNONW, "sladkosť": UNKNONW}
        
        for param in coffee_soup.find_all(DIV, class_='speci_param'):
            param_name = param.find(DIV, class_='speci_param_name')
            if param_name:
                name = param_name.text.strip().lower()
                if name in taste_ratings:
                    points_full = len(param.find_all(SPAN, class_='point_full'))
                    points_total = points_full + len(param.find_all(SPAN, class_='point_empty'))
                    taste_ratings[name] = f"{points_full}/{points_total}"
        
        roast_shade = UNKNONW
        description = coffee_soup.find('p', itemprop=DESCRIPTION)
        if description and (match := re.search(r'Odtieň praženia:\s*([^<\n]+)', description.text)):
            roast_shade = match.group(1).strip()
        
        #TODO FIX
        flavor_profile = []
        for div in coffee_soup.find_all(DIV, class_='recommended_preparation'):
            flavor_profile.extend([span.text.strip() for span in div.find_all(SPAN) if span.text.strip()])
        
        processing = None
        if match := re.search(r'SPRACOVANIE:\s*(.+)', coffee_soup.text):
            processing = match.group(1).strip()
        
        return Taste(
            body=taste_ratings[BODY_COFFEIN],
            bitterness=taste_ratings[BITTERNESS_COFFEEIN],
            acidity=taste_ratings[ACIDITY_COFFEEIN],
            sweetness=taste_ratings[SWEETNEST_COFFEEIN],
            roast_shade=roast_shade,
            processing=processing,
            flavor_profile=flavor_profile,
            variety=self.handle_variety(coffee_soup)
        )

        
    def handle_variety(self,coffee_soup:BeautifulSoup)->Variety:
        description = coffee_soup.find('p', itemprop=DESCRIPTION)
        if description:
            arabica_match = re.search(r'(\d+)\s*%\s*Arabica', description.text)
            robusta_match = re.search(r'(\d+)\s*%\s*Robusta', description.text)
            #TODO fix
            if arabica_match and robusta_match:
                return Variety.Mixed
            if ARABICA_COFFEEIN in description.text:
                return Variety.Arabica
            if ROBUSTA_COFFEEIN in description.text:
                return Variety.Robusta
        return Variety.Unknown
    
    def handle_origin(self,coffee_soup:BeautifulSoup)->Origin:
        origin_region = ""
        origin_farm = None
        origin_altitude = None
        
        origin_section = coffee_soup.find(DIV, id=COFFEE_ORIGIN)
        if origin_section:
            origin_text = origin_section.text.strip()
            region_match = re.search(r'REGIÓN:\s*(.+)', origin_text)
            if region_match:
                origin_region = region_match.group(1).strip()
            else:
                origin_lines = origin_text.split('\n')
                if origin_lines:
                    origin_region = origin_lines[0].strip()
            
            farm_match = re.search(r'FARMA:\s*(.+)', origin_text)
            if farm_match:
                origin_farm = farm_match.group(1).strip()
            
            altitude_match = re.search(r'NADMORSKÁ VÝŠKA:\s*(.+)', origin_text)
            if altitude_match:
                origin_altitude = altitude_match.group(1).strip()
        
        return Origin(
            region=origin_region,
            farm=origin_farm,
            altitude=origin_altitude
        )
    
    def extract_weight_in_grams(self,text):
        number_str = re.findall(r'\d+\.?\d*', text)[0]
        number = float(number_str)
        
        if "kg" in text.lower():
            return int(number * 1000)
        elif "g" in text.lower():
            return int(number)
        else:
            return int(number) 
    
    def handle_size(self, name:str,coffee_soup:BeautifulSoup)->int:
        if "(1000 g" in name:
            return 1000
        elif "(500 g" in name:
            return 500
        elif "(200 g" in name:
            return 200
        elif "(100 g" in name:
            return 100
        description = coffee_soup.find('p', itemprop=DESCRIPTION)
        if description:
            size_match = re.search(r'Veľkosť balenia:\s*([^<\n]+)', description.text)
            if size_match:
                return self.extract_weight_in_grams(size_match.group(1).strip())
        
    def handle_reviews(self,coffee_soup:BeautifulSoup)->Review:
        reviews = []
        review_score = 0.0
        review_section = coffee_soup.find(DIV, id='ranks_box')
        if review_section:
            for review_item in review_section.find_all('li', itemprop='review'):
                review_text_div = review_item.find(DIV, class_='rank_right')
                if review_text_div and review_text_div.text.strip():
                    reviews.append(review_text_div.text.strip())
            
            rating_value = review_section.find('meta', itemprop='ratingValue')
            if rating_value:
                review_score = float(rating_value.get('content', '0'))
        
        return Review(
            reviews=reviews,
            review_score=review_score
        )
        
    def handle_decaf(self,name:str,coffee_soup:BeautifulSoup)->bool:
        
        description = coffee_soup.find('p', itemprop=DESCRIPTION)
        is_decaf = False
        if "BEZKOFEINOVÁ" in name.upper() or "BEZKOFEÍNOVÁ" in name.upper():
            is_decaf = True
        else:
            # Check in description
            if description and ("bezkofeinová" in description.text.lower() or "bezkofeínová" in description.text.lower()):
                is_decaf = True
        
        # Add decaf information to the name if not already there
        if is_decaf and "bezkofeinová" not in name.lower() and "bezkofeínová" not in name.lower():
            name = f"BEZKOFEÍNOVÁ {name}"
        