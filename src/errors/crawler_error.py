class ProcessorError(Exception):
    """Custom exception for processor errors."""

    def __init__(self, message):
        super().__init__(f"Error processing: {message}")
