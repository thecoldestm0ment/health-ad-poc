from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import quote

import requests

from .config import Settings

SERVICE = "I0030"
DOC_URL = "https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I0030"
MESSAGES = {
    "INFO-100": "인증키가 유효하지 않습니다.",
    "INFO-110": "인증키는 유효하지만 I0030 활용신청이 필요합니다.",
    "INFO-120": "I0030 서비스 신청 상태를 확인하세요.",
    "INFO-300": "API 호출 한도를 초과했습니다.",
    "INFO-310": "샘플키 제한입니다. 발급받은 인증키를 사용하세요.",
    "INFO-320": "시간당 API 호출 한도를 초과했습니다.",
    "INFO-400": "API 접근 권한이 없습니다.",
    "INFO-500": "동일 인증키가 사용 중입니다. 잠시 후 다시 실행하세요.",
    "INFO-600": "인증키가 만료되었습니다.",
    "INFO-700": "당일 변경 데이터는 19시 이후 조회하세요.",
    "ERROR-336": "한 번에 최대 1000건까지 요청할 수 있습니다.",
    "ERROR-500": "식품안전나라 서버 오류입니다.",
    "ERROR-503": "식품안전나라 시간대별 호출 한도를 초과했습니다.",
}


class APIError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class SearchResult:
    rows: list[dict]
    total_count: int
    truncated: bool
    pages: list[dict]


def redact(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, dict):
        return {redact(k, secret): redact(v, secret) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    return value


def parse_response(payload):
    if not isinstance(payload, dict):
        raise APIError("INVALID_RESPONSE", "JSON 응답 형식이 올바르지 않습니다.")
    service = payload.get(SERVICE)
    if isinstance(service, dict):
        body = service
    elif "RESULT" in payload:
        body = payload
    else:
        raise APIError("INVALID_RESPONSE", "I0030 응답을 찾을 수 없습니다.")
    result = body.get("RESULT", {})
    if not isinstance(result, dict):
        raise APIError("INVALID_RESPONSE", "RESULT 형식이 올바르지 않습니다.")
    code = str(result.get("CODE", ""))
    if code == "INFO-200":
        return [], 0
    if code != "INFO-000":
        # Do not echo server MESSAGE: it can contain a credential-bearing URL.
        safe_code = code if code in MESSAGES or code.startswith("ERROR-") and code[6:].isdigit() else "API_ERROR"
        raise APIError(safe_code, MESSAGES.get(code, "식품안전나라 API 설정·상태를 확인하세요."))
    rows = body.get("row", [])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise APIError("INVALID_RESPONSE", "row는 객체 목록이어야 합니다.")
    try:
        total = int(body["total_count"])
    except (KeyError, TypeError, ValueError):
        raise APIError("INVALID_RESPONSE", "전체 건수 total_count가 올바르지 않습니다.") from None
    if total < len(rows) or total < 0 or total > 0 and not rows:
        raise APIError("INVALID_RESPONSE", "응답 건수와 row가 일치하지 않습니다.")
    return rows, total


class FoodSafetyClient:
    def __init__(self, settings: Settings, *, use_cache=True, session=None):
        self.settings = settings
        self.use_cache = use_cache
        self.session = session or requests.Session()
        self.requests_made = 0
        self.cache_hits = 0
        self._last_request = None

    def _page(self, filters, start, end):
        # Cache identity contains no key, but separates endpoints and response schema.
        identity = {"schema": 1, "base_url": self.settings.base_url,
                    "service": SERVICE, "filters": filters, "start": start, "end": end}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache_path = self.settings.cache_dir / f"{digest}.json"
        if self.use_cache and cache_path.exists():
            try:
                envelope = json.loads(cache_path.read_text(encoding="utf-8"))
                rows, total = parse_response(envelope["payload"])
                self.cache_hits += 1
                return rows, total, {**envelope, "cache_hit": True}
            except (OSError, ValueError, KeyError, APIError):
                pass  # An invalid cache entry is fetched again.
        if self.requests_made >= self.settings.max_requests:
            raise APIError("REQUEST_BUDGET", "이번 실행의 API 요청 한도에 도달했습니다. 결과를 저장하고 중단합니다.")
        if self._last_request is not None:
            time.sleep(max(0, self.settings.interval - (time.monotonic() - self._last_request)))
        url = f"{self.settings.base_url}/{self.settings.key}/{SERVICE}/json/{start}/{end}"
        if filters:
            url += "/" + "&".join(f"{key}={quote(value, safe='')}" for key, value in sorted(filters.items()))
        self.requests_made += 1
        self._last_request = time.monotonic()
        try:
            response = self.session.get(url, timeout=self.settings.timeout, allow_redirects=False)
        except requests.RequestException:
            raise APIError("NETWORK_ERROR", "API 연결에 실패했습니다. 네트워크·서버 상태를 확인하세요. "
                           "요청 URL은 인증키 보호를 위해 출력하지 않습니다.") from None
        if response.status_code != 200:
            raise APIError(f"HTTP_{response.status_code}", "API 서버가 정상 응답하지 않았습니다. 잠시 후 다시 실행하세요.")
        try:
            # Parse UTF-8 directly; requests' guessed text encoding can corrupt Korean.
            payload = json.loads(response.content.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise APIError("INVALID_JSON", "JSON 대신 다른 응답이 왔습니다. 서버 상태·주소를 확인하세요.") from None
        rows, total = parse_response(payload)
        envelope = {"request": identity, "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "payload": redact(payload, self.settings.key)}
        if self.use_cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(cache_path)
        return redact(rows, self.settings.key), total, {**envelope, "cache_hit": False}

    def search(self, *, name="", report_no="", manufacturer="", max_pages=None):
        if not any((name, report_no, manufacturer)):
            raise ValueError("전체 DB 다운로드 대신 검색 조건을 하나 이상 입력하세요.")
        filters = {k: v for k, v in {"PRDLST_NM": name, "PRDLST_REPORT_NO": report_no,
                                    "BSSH_NM": manufacturer}.items() if v}
        return self._collect(filters, max_pages or self.settings.max_pages)

    def check(self):
        # Always use the network: a cache must never masquerade as key validation.
        old = self.use_cache
        self.use_cache = False
        try:
            return self._collect({}, 1, page_size=1)
        finally:
            self.use_cache = old

    def _collect(self, filters, max_pages, page_size=None):
        size = page_size or self.settings.page_size
        rows, pages = [], []
        total = None
        for index in range(max_pages):
            page_rows, page_total, envelope = self._page(filters, index * size + 1, (index + 1) * size)
            if total is not None and page_total != total:
                raise APIError("DATA_CHANGED", "페이지 조회 중 전체 건수가 변경되었습니다. --refresh로 다시 조회하세요.")
            total = page_total
            pages.append(envelope)
            rows.extend(page_rows)
            if len(rows) >= total:
                break
            if len(page_rows) != size:
                raise APIError("INCOMPLETE_PAGE", "응답 페이지가 예상보다 짧습니다. --refresh로 다시 조회하세요.")
        unique = []
        seen = set()
        for row in rows:
            signature = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if signature not in seen:
                unique.append(row)
                seen.add(signature)
        # Duplicate records across pages can hide candidates; do not auto-confirm.
        return SearchResult(unique, total or 0, len(rows) < (total or 0) or len(unique) < len(rows), pages)
