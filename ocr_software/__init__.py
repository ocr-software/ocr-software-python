"""ocr-software.com: client and command line for the OCR API."""
from .client import Client, OcrError, PagesUnread, unread_message

__version__ = "0.1.0"
__all__ = ["Client", "OcrError", "PagesUnread", "unread_message", "__version__"]
