from crawlers.crawler_factory import CrawlerFactory
from models.page import PageType


def main():
    # Create a CoffeeinCrawler using the factory
    crawler = CrawlerFactory.create_crawler(
        PageType.COFFEEIN,
    )

    # Use the crawler
    metadata_list = crawler.find_metadata("kategoria/2/cerstvo-prazena-zrnkova-kava/")

    for metadata in metadata_list:
        coffee_link = crawler.link_coffe_details(metadata)
        coffee = crawler.find_coffee(coffee_link)
        # Process the coffee data as needed


if __name__ == "__main__":
    main()
