"""
Lab 11 — Configuration & API Key Setup
"""
import os
import ssl
from dotenv import load_dotenv

# Monkeypatch ssl to avoid SSLError on Windows when importing aiohttp / google.genai
try:
    orig_load_windows_store_certs = ssl.SSLContext._load_windows_store_certs
    def patched_load_windows_store_certs(self, storename, purpose):
        try:
            orig_load_windows_store_certs(self, storename, purpose)
        except Exception:
            pass
    ssl.SSLContext._load_windows_store_certs = patched_load_windows_store_certs
except AttributeError:
    pass



def setup_api_key():
    """Load API keys and map environment variables for OpenAI compatibility."""
    load_dotenv()
    if os.getenv("COMPATIBLE_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.getenv("COMPATIBLE_API_KEY")
    if os.getenv("COMPATIBLE_BASE_URL"):
        os.environ["OPENAI_API_BASE"] = os.getenv("COMPATIBLE_BASE_URL")
        os.environ["OPENAI_BASE_URL"] = os.getenv("COMPATIBLE_BASE_URL")
    # Also keep a dummy key for Google GenAI if ADK internally checks it
    os.environ["GOOGLE_API_KEY"] = os.getenv("COMPATIBLE_API_KEY", "dummy")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
    print("OpenAI-compatible environment variables loaded.")



# Allowed banking topics (used by topic_filter)
ALLOWED_TOPICS = [
    "banking", "account", "transaction", "transfer",
    "loan", "interest", "savings", "credit",
    "deposit", "withdrawal", "balance", "payment",
    "tai khoan", "giao dich", "tiet kiem", "lai suat",
    "chuyen tien", "the tin dung", "so du", "vay",
    "ngan hang", "atm",
]

# Blocked topics (immediate reject)
BLOCKED_TOPICS = [
    "hack", "exploit", "weapon", "drug", "illegal",
    "violence", "gambling", "bomb", "kill", "steal",
]
