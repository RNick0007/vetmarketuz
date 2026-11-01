import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests


LOGGER = logging.getLogger(__name__)
BRAVE_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_IMAGE_SEARCH_URL = "https://api.search.brave.com/res/v1/images/search"
REQUEST_HEADERS = {"Accept": "application/json"}
MANUFACTURER_PATTERN = re.compile(
    r"(?:производитель|изготовитель|виробник|manufacturer|produced by)\s*[:\-]\s*"
    r"([^.;,\n]+)",
    re.IGNORECASE,
)
INSTRUCTION_TERMS = (
    "инструкция",
    "применению",
    "листок-вкладыш",
    "instruction",
    "directions",
)
MEDICINE_TOKEN_PATTERN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
GENERIC_MEDICINE_TOKENS = {
    "ветеринарный",
    "ветеринарная",
    "ветеринарное",
    "животных",
    "лекарственный",
    "препарат",
    "средство",
}


@dataclass(frozen=True)
class MedicineInfo:
    instruction: str | None = None
    photo: str | None = None
    manufacturer: str | None = None
    errors: int = 0


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return urlparse(value).scheme in {"http", "https"} and bool(urlparse(value).netloc)


def _search_results(query: str, api_key: str) -> list[tuple[str, str, str]]:
    response = requests.get(
        BRAVE_WEB_SEARCH_URL,
        params={
            "q": query,
            "count": 10,
            "country": "ALL",
            "search_lang": "ru",
            "safesearch": "moderate",
        },
        headers={**REQUEST_HEADERS, "X-Subscription-Token": api_key},
        timeout=(3, 10),
    )
    response.raise_for_status()
    payload = response.json()
    web_data = payload.get("web") if isinstance(payload, dict) else None
    web_results = web_data.get("results") if isinstance(web_data, dict) else None
    if not isinstance(web_results, list):
        LOGGER.warning("Brave Web Search вернул ответ неожиданного формата.")
        return []

    results: list[tuple[str, str, str]] = []
    for result in web_results:
        if not isinstance(result, dict):
            continue
        url = result.get("url")
        if not _is_http_url(url):
            continue
        results.append(
            (
                str(result.get("title") or ""),
                url,
                str(result.get("description") or ""),
            )
        )
    return results


def _search_image(name: str, api_key: str) -> str | None:
    response = requests.get(
        BRAVE_IMAGE_SEARCH_URL,
        params={
            "q": f"{name} упаковка",
            "count": 10,
            "country": "ALL",
            "search_lang": "ru",
            "safesearch": "strict",
        },
        headers={**REQUEST_HEADERS, "X-Subscription-Token": api_key},
        timeout=(3, 10),
    )
    response.raise_for_status()
    payload = response.json()
    image_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(image_results, list):
        LOGGER.warning("Brave Image Search вернул ответ неожиданного формата.")
        return None

    medicine_tokens = {
        token
        for token in MEDICINE_TOKEN_PATTERN.findall(name.casefold())
        if token not in GENERIC_MEDICINE_TOKENS
    }
    if not medicine_tokens:
        return None

    for result in image_results:
        if not isinstance(result, dict):
            continue
        image_text = " ".join(
            str(result.get(field) or "")
            for field in ("title", "source", "url")
        )
        image_tokens = set(
            MEDICINE_TOKEN_PATTERN.findall(unquote(image_text).casefold())
        )
        if len(medicine_tokens & image_tokens) < min(2, len(medicine_tokens)):
            continue

        properties = result.get("properties")
        thumbnail = result.get("thumbnail")
        candidates = (
            properties.get("url") if isinstance(properties, dict) else None,
            thumbnail.get("src") if isinstance(thumbnail, dict) else None,
            result.get("url"),
        )
        image_url = next((candidate for candidate in candidates if _is_http_url(candidate)), None)
        if image_url is not None:
            return image_url
    return None


def lookup_medicine_info(name: str) -> MedicineInfo:
    api_key = os.getenv("BRAVE_SEARCH_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан BRAVE_SEARCH_API_KEY.")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results_future = executor.submit(
            _search_results,
            f"{name} инструкция",
            api_key,
        )
        image_future = executor.submit(
            _search_image,
            name,
            api_key,
        )

        errors = 0
        try:
            results = results_future.result()
        except (requests.RequestException, ValueError) as error:
            LOGGER.warning(
                "Brave Web Search не выполнил запрос для препарата %r (%s, HTTP %s).",
                name,
                type(error).__name__,
                getattr(getattr(error, "response", None), "status_code", "-"),
            )
            results = []
            errors += 1

        try:
            photo = image_future.result()
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            LOGGER.warning(
                "Brave Image Search не выполнил запрос для препарата %r (%s, HTTP %s).",
                name,
                type(error).__name__,
                getattr(getattr(error, "response", None), "status_code", "-"),
            )
            photo = None
            errors += 1

    instruction = next(
        (
            url
            for title, url, snippet in results
            if any(term in f"{title} {snippet}".casefold() for term in INSTRUCTION_TERMS)
        ),
        None,
    )
    manufacturer = next(
        (
            match.group(1).strip()
            for title, _, snippet in results
            if (match := MANUFACTURER_PATTERN.search(f"{title} {snippet}"))
        ),
        None,
    )
    return MedicineInfo(
        instruction=instruction,
        photo=photo,
        manufacturer=manufacturer,
        errors=errors,
    )
