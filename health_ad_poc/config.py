from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    key: str
    base_url: str = "https://openapi.foodsafetykorea.go.kr/api"
    timeout: float = 20
    interval: float = 1
    max_requests: int = 80
    page_size: int = 100
    max_pages: int = 3
    cache_dir: Path = ROOT / ".cache" / "i0030"

    @classmethod
    def from_env(cls):
        # OS environment wins; .env is always resolved relative to this project.
        load_dotenv(ROOT / ".env", override=False)
        key = os.getenv("FOOD_API_KEY", "").strip()
        if not key or key.lower() in {"your_api_key", "your_key_here", "sample"}:
            raise ConfigError(".env의 FOOD_API_KEY에 식품안전나라 인증키를 입력하세요.")
        if not key.isascii() or not key.isalnum():
            raise ConfigError("FOOD_API_KEY 형식을 확인하세요. 공백·URL·한글을 넣지 마세요.")
        base = os.getenv("FOOD_API_BASE_URL", cls.base_url).strip().rstrip("/")
        parts = urlsplit(base)
        if (parts.scheme not in {"https", "http"}
                or parts.netloc != "openapi.foodsafetykorea.go.kr"
                or parts.path != "/api" or parts.query or parts.fragment):
            raise ConfigError("FOOD_API_BASE_URL은 식품안전나라 /api 주소여야 합니다.")
        try:
            settings = cls(
                key=key, base_url=base,
                timeout=float(os.getenv("FOOD_API_TIMEOUT", "20")),
                interval=float(os.getenv("FOOD_API_INTERVAL", "1")),
                max_requests=int(os.getenv("FOOD_API_MAX_REQUESTS", "80")),
                page_size=int(os.getenv("FOOD_API_PAGE_SIZE", "100")),
                max_pages=int(os.getenv("FOOD_API_MAX_PAGES", "3")),
            )
        except ValueError:
            raise ConfigError(".env의 시간·횟수 설정은 숫자여야 합니다.") from None
        if not (0 < settings.timeout <= 120 and 0 <= settings.interval <= 60
                and 1 <= settings.max_requests <= 1000
                and 1 <= settings.page_size <= 1000
                and 1 <= settings.max_pages <= 20):
            raise ConfigError("설정 범위: timeout 0초 초과~120초, interval 0~60초, "
                              "requests/page_size 1~1000, max_pages 1~20.")
        return settings
