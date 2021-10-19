from typing import List

DEFAULT_APP_HOSTNAME: str = "localhost"
DEFAULT_APP_PORT: int = 8000
DEFAULT_ASSETS_DIRECTORY: None = None
DEFAULT_CACHE_DIRECTORY: None = None
DEFAULT_CACHE_PERSIST: bool = True
DEFAULT_CACHE_SIZE_LIMIT: int = 10000000000  # 10GB
DEFAULT_DATASETS_ENABLE_PRIVATE: bool = False
DEFAULT_DATASETS_REVISION: str = "stream-tar"
DEFAULT_EXTRACT_ROWS_LIMIT: int = 100
DEFAULT_LOG_LEVEL: str = "INFO"
DEFAULT_MAX_AGE_LONG_SECONDS: int = 21600  # 6 * 60 * 60 = 6 hours
DEFAULT_MAX_AGE_SHORT_SECONDS: int = 120  # 2 minutes
DEFAULT_WEB_CONCURRENCY: int = 2

DEFAULT_MAX_LOAD_PCT: int = 50
DEFAULT_MAX_SWAP_MEMORY_PCT: int = 60
DEFAULT_MAX_VIRTUAL_MEMORY_PCT: int = 95
DEFAULT_REFRESH_PCT: int = 1

DEFAULT_CONFIG_NAME: str = "default"
DATASETS_BLOCKLIST: List[str] = ["imthanhlv/binhvq_news21_raw"]
