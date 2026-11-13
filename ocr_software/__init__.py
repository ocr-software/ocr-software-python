"""ocr-software.com: client and command line for the OCR API."""
from .advice import default_pipeline, price_sentence
from .client import Client, OcrError, PagesUnread, split_pages, unread_message

__version__ = "0.1.0"
__all__ = ["Client", "OcrError", "PagesUnread", "split_pages", "unread_message", "default_pipeline", "price_sentence", "__version__"]
