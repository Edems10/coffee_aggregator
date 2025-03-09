from factory.crawler_factory import CrawlerFactory
from models.page import PageType
from factory.processor_factory import ProcessorFactory
from database.supabase_db import SupabaseDB
import pickle


def main():
    crawler = CrawlerFactory.create_crawler(PageType.COFFEEIN)
    processor = ProcessorFactory.create_processor(
        PageType.COFFEEIN, ignored_coffes=["tasting pack"]
    )
    supabase = SupabaseDB()
    metadata_set = set()
    # for metadata_soup in crawler.find_metadata(COFFEIN_MAIN_COFFE_PAGE):
    #     metadata_batch = processor.process_metadata(metadata_soup)
    #     metadata_set.update(metadata_batch)

    # updated_dict = supabase.update_metadata(list(metadata_set))
    # # This should be migrate to old also tracking price would be nice
    # supabase.delete_old_metadata(list(metadata_set))

    # TESTING
    with open("metadata_set.pkl", "rb") as f:
        metadata_set = pickle.load(f)
        print("Loaded metadata from file.")

    for coffee_soup in crawler.find_coffee(list(metadata_set)):
        coffee = processor.process_coffee(coffee_soup)


if __name__ == "__main__":
    main()
