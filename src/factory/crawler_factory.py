from crawlers.coffeein_crawler import CoffeeinCrawler
from models.page import PageType
from crawlers.crawler_interface import Crawler


class CrawlerFactory:
    @staticmethod
    def create_crawler(crawler_type: str, **kwargs) -> Crawler:
        if crawler_type == PageType.COFFEEIN:
            return CoffeeinCrawler(**kwargs)
        else:
            raise ValueError(f"Unknown crawler type: {crawler_type}")
