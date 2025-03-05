class ScraperError(Exception):
    """Custom exception for scraper errors."""

    def __init__(self, url, message):
        self.url = url
        self.status_code = message
        super().__init__(f"Error scraping {url}: {message}")
