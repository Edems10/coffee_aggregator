from factory.crawler_factory import CrawlerFactory
from models.page import PageType
from assets.constants import COFFEIN_MAIN_COFFE_PAGE
from factory.processor_factory import ProcessorFactory

def main():

    crawler = CrawlerFactory.create_crawler(PageType.COFFEEIN)
    processor = ProcessorFactory.create_processor(PageType.COFFEEIN, ignored_coffes = ["tasting pack"])
    
    metadata_set = set()
    for metadata_soup in crawler.find_metadata(COFFEIN_MAIN_COFFE_PAGE):
        metadata_batch = processor.process_metadata(metadata_soup)
        metadata_set.update(metadata_batch)

if __name__ == "__main__":
    main()
