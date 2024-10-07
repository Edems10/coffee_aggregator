import json
import re
import unidecode
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

from metadata import Metadata

class ScraperError(Exception):
    """Custom exception for scraper errors."""
    pass

class CoffeeinCrawler():
    def __init__(self,base_url:str, ignore_words = None) -> None:
        self.base_url = base_url
        self.product_metadata = defaultdict(dict)
        self.ignored_words = ignore_words

    def get_items(self, match:str):
        items_str = match.group(1)
        items_str = items_str.replace("'", '"') 
        items_str = re.sub(r',\s*}', '}', items_str)  # Remove trailing commas
        items_str = re.sub(r',\s*]', ']', items_str)  # Remove trailing commas
        return json.loads(f'[{items_str}]')  # Wrap in [] to make it a list
  
    def check_ignored_words(self, string)->bool:
        if self.ignored_words:
            for ignored_word in self.ignored_words:
                if ignored_word.lower() in string.lower():
                    return True
        return False
    
    def filter_items(self,items)->bool:
        for item in items:
            name_unfiltered = item.get('item_name')
            decoded_name = name_unfiltered.encode('utf-8').decode('unicode_escape')
            
            link = unidecode.unidecode(name_unfiltered)
            metadata = Metadata(
                id=item.get('item_id'),
                link=link,
                name=decoded_name,
                price=float(item.get('price')))                           

            if metadata.id in self.product_metadata:
                print("Found the same product twice. Ending...")
                return False
            
            product_details = {
                'name': metadata.name,
                'price': metadata.price,
                'link': metadata.link}
            
            if not self.check_ignored_words(metadata.name):
                self.product_metadata[metadata.id] = product_details

        print(f"Currently found {len(self.product_metadata)} products")
        return True
    
    def filter_soup(self, soup: BeautifulSoup):
        scripts = soup.find_all('script')
        for script in scripts:
            if 'view_item_list' not in script.text:
                continue
            # Use regex to extract the 'items' array
            match = re.search(r'"items":\s*\[(.*?)\]\s*\}', script.text, re.DOTALL)
            if not match:
                # Raise custom error if the match is not found
                raise ScraperError("Failed to find product data in script tags")
            try:
                items = self.get_items((match))
                return self.filter_items(items)
                
            
            except json.JSONDecodeError as e:
                # Raise custom error if JSON decoding fails
                raise ScraperError(f'JSON decode error: {e}') from e
        
    def find_metadata(self, output:str)->bool:
        page_exists = True
        iterator = 1
        
        while(page_exists):
            
            url = f'{self.base_url}{iterator}/'
            iterator = iterator +1
            response = requests.get(url,timeout=60)

            if response.status_code == 200:
                soup = BeautifulSoup(response.text, features="html.parser")
                if not self.filter_soup(soup):
                    page_exists = False    

            else:
                print(f"Failed to retrieve the page. Status code: {response.status_code}")
                page_exists = False

        with open(output, 'w') as json_file:
            json.dump(self.product_metadata, json_file, indent=4)
        return True

def main():
    cc = CoffeeinCrawler(base_url ='https://www.coffeein.sk/kategoria/2/cerstvo-prazena-zrnkova-kava/',ignore_words=["tasting pack"])
    cc.find_metadata('coffein_metadata.json')
      
if __name__ == "__main__":
    main()
    