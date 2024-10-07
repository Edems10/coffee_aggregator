import json
import re
import unidecode
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

from metadata import Metadata


def find_metadata(output:str,filter = None)->bool:
    page_exists = True
    iterator = 1 
    product_data = defaultdict(dict)
    
    while(page_exists):
        
        url = f'https://www.coffeein.sk/kategoria/2/cerstvo-prazena-zrnkova-kava/{iterator}/'
        iterator = iterator +1
        response = requests.get(url)

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, features="html.parser")
            scripts = soup.find_all('script')
                       
            for script in scripts:
                if 'view_item_list' in script.text:
                    # Use regex to extract the 'items' array
                    match = re.search(r'"items":\s*\[(.*?)\]\s*\}', script.text, re.DOTALL)
                    if match:
                        items_str = match.group(1)
                        
                        items_str = items_str.replace("'", '"') 
                        items_str = re.sub(r',\s*}', '}', items_str)  # Remove trailing commas
                        items_str = re.sub(r',\s*]', ']', items_str)  # Remove trailing commas
                        try:
                            items = json.loads(f'[{items_str}]')  # Wrap in [] to make it a list
                            for item in items:
                                name_unfiltered = item.get('item_name')
                                decoded_name = name_unfiltered.encode('utf-8').decode('unicode_escape')
                                #TODO : fix the link
                                link = unidecode.unidecode(name_unfiltered)
                                metadata = Metadata(
                                    id=item.get('item_id'),
                                    link=link,
                                    name = decoded_name,
                                    price=float(item.get('price'))
                                )
                                
                                if metadata.id in product_data:
                                    page_exists = False
                                    print("Found the same product twice Ending")
                                    break
                                product_details = {
                                    'name': metadata.name,
                                    'price': metadata.price,
                                    'link': metadata.link
                                }
                                if filter:
                                    if filter.lower() in metadata.link.lower():
                                        pass
                                    else:
                                        product_data[metadata.id] = product_details 
                                else:
                                    product_data[metadata.id] = product_details
                            print(f"Currently found {len(product_data)}")
                        except json.JSONDecodeError as e:
                            print(f"JSON decode error: {e}")
                    break
            else:
                print("Product data script not found.")

        else:
            print(f"Failed to retrieve the page. Status code: {response.status_code}")
            page_exists = False

    with open(output, 'w') as json_file:
        json.dump(product_data, json_file, indent=4)
    return True

def main():
    find_metadata('coffein_metadata.json',filter="tasting pack")

        
if __name__ == "__main__":
    main()