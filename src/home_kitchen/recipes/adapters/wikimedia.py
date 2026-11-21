"""Bounded Wikimedia Commons adapter implementing `ImageSearchProvider`."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

import httpx

from home_kitchen.recipes.images import (
    MAX_CONTINUATION_LENGTH,
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    RecipeImage,
    RecipeImageSearch,
)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
MAX_RESPONSE_BYTES = 1_000_000
MAX_JSON_DEPTH = 20
MAX_JSON_NODES = 20_000
HTTP_TIMEOUT_SECONDS = 10.0
USER_AGENT = "HomeKitchenBot/0.1 (+https://github.com/ghjklw/home-kitchen)"
_ALLOWED_IMAGE_HOSTS = {"upload.wikimedia.org"}
_ALLOWED_SOURCE_HOSTS = {"commons.wikimedia.org"}
_ALLOWED_MIMES = {"image/jpeg"}


class _TextOnly(HTMLParser):
    """Extract plain text from Commons metadata without interpreting its HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "iframe", "object"}:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "iframe", "object"} and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def _plain_text(value: object, *, max_length: int = 500) -> str | None:
    if not isinstance(value, str):
        return None
    parser = _TextOnly()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return None
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return text[:max_length] or None


def _safe_url(value: object, hosts: set[str]) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not parsed.path
        ):
            return None
    except ValueError:
        return None
    return value


def _metadata_value(metadata: Mapping[str, object], key: str) -> object:
    item = metadata.get(key)
    return item.get("value") if isinstance(item, Mapping) else None


def _license(metadata: Mapping[str, object]) -> tuple[str, str, list[str]] | None:
    name = _plain_text(_metadata_value(metadata, "LicenseShortName"), max_length=100)
    raw_url = _metadata_value(metadata, "LicenseUrl")
    # Commons still publishes canonical Creative Commons links over HTTP.
    # Upgrade only exact Creative Commons hosts; all other metadata must pass
    # the existing HTTPS host and license-family validation below.
    if isinstance(raw_url, str):
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            parsed = None
        if parsed and (
            parsed.scheme == "http"
            and parsed.netloc.casefold() in {"creativecommons.org", "www.creativecommons.org"}
            and not parsed.query
            and not parsed.fragment
        ):
            raw_url = parsed._replace(scheme="https").geturl()
    url = _safe_url(
        raw_url,
        {"creativecommons.org", "www.creativecommons.org", "commons.wikimedia.org"},
    )
    if not name or not url:
        return None
    parsed_url = urlsplit(url)
    path = parsed_url.path.rstrip("/") + "/"
    if parsed_url.query or parsed_url.fragment:
        return None
    cc_host = parsed_url.hostname in {"creativecommons.org", "www.creativecommons.org"}

    cc0_name = name.casefold() in {"cc0", "cc0 1.0", "cc0 1.0 universal"}
    if cc0_name:
        if not cc_host or path != "/publicdomain/zero/1.0/":
            return None
        obligations = ["Public-domain dedication; retain source metadata as good practice."]
        return name, url, obligations

    public_domain_name = name.casefold() == "public domain"
    is_commons_pd_url = (
        parsed_url.hostname == "commons.wikimedia.org" and path == "/wiki/Help:Public_domain/"
    )
    is_pd_mark_url = cc_host and path == "/publicdomain/mark/1.0/"
    if public_domain_name:
        if not (is_commons_pd_url or is_pd_mark_url):
            return None
        obligations = ["Public domain; retain source metadata as good practice."]
        return name, url, obligations

    by_sa = re.fullmatch(r"CC BY-SA (1\.0|2\.0|2\.5|3\.0|4\.0)", name, re.IGNORECASE)
    by = re.fullmatch(r"CC BY (1\.0|2\.0|2\.5|3\.0|4\.0)", name, re.IGNORECASE)
    if by_sa:
        if not cc_host or path != f"/licenses/by-sa/{by_sa[1]}/":
            return None
        obligations = [
            "Credit the creator and link the license.",
            "Share adaptations under the same or a compatible license.",
        ]
    elif by:
        if not cc_host or path != f"/licenses/by/{by[1]}/":
            return None
        obligations = [
            "Credit the creator and link the license.",
            "Indicate changes if the image is modified.",
        ]
    else:
        return None
    return name, url, obligations


def _json_is_bounded(value: object) -> bool:
    stack: list[tuple[object, int]] = [(value, 0)]
    count = 0
    while stack:
        current, depth = stack.pop()
        count += 1
        if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return False
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return True


def _continuation_token(query: str, continuation: object) -> str | None:
    if not isinstance(continuation, Mapping):
        return None
    token = continuation.get("continue")
    offset = continuation.get("gsroffset")
    if (
        not isinstance(token, str)
        or len(token) > 100
        or not isinstance(offset, int)
        or not 0 <= offset <= 500
    ):
        return None
    raw = json.dumps(
        {"q": hashlib.sha256(query.encode()).hexdigest(), "offset": offset, "token": token},
        separators=(",", ":"),
    )
    return quote(raw, safe="")


def _parse_continuation(query: str, token: str | None) -> dict[str, object] | None:
    if token is None:
        return {}
    if len(token) > MAX_CONTINUATION_LENGTH:
        return None
    try:
        parsed = json.loads(unquote(token))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"q", "offset", "token"}:
        return None
    if parsed["q"] != hashlib.sha256(query.encode()).hexdigest():
        return None
    if not isinstance(parsed["offset"], int) or not 0 <= parsed["offset"] <= 500:
        return None
    if not isinstance(parsed["token"], str) or len(parsed["token"]) > 100:
        return None
    return {"continue": parsed["token"], "gsroffset": parsed["offset"]}


class WikimediaCommonsImageProvider:
    """Anonymous, read-only Commons image search; no arbitrary URL fetching."""

    def __init__(self, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    async def search(
        self, query: str, *, limit: int = MAX_RESULTS, continuation: str | None = None
    ) -> RecipeImageSearch:
        clean_query = query.strip() if isinstance(query, str) else ""
        if not clean_query or len(clean_query) > MAX_QUERY_LENGTH or not 1 <= limit <= MAX_RESULTS:
            return RecipeImageSearch(
                status="unavailable",
                detail="Search needs a short query and a result limit from 1 to 10.",
            )
        continuation_params = _parse_continuation(clean_query, continuation)
        if continuation_params is None:
            return RecipeImageSearch(
                status="invalid_continuation",
                detail="The continuation token is invalid for this query.",
            )

        params: dict[str, str | int] = {
            "action": "query",
            "format": "json",
            "formatversion": 2,
            "generator": "search",
            "gsrsearch": f'filetype:bitmap filemime:"image/jpeg" {clean_query}',
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|mime|mediatype|extmetadata",
            "iiurlwidth": 640,
            "iiextmetadatalanguage": "en",
            "iiextmetadatafilter": "Artist|Credit|LicenseShortName|LicenseUrl|AttributionRequired|Attribution",
        }
        for key, value in continuation_params.items():
            if isinstance(value, (str, int)):
                params[key] = value
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS), follow_redirects=False, trust_env=False
        )
        try:
            async with client.stream(
                "GET",
                COMMONS_API,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as response:
                if response.status_code == 429:
                    return RecipeImageSearch(
                        status="rate_limited", detail="Commons rate limit reached; retry later."
                    )
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        return RecipeImageSearch(
                            status="unavailable", detail="Commons returned an oversized response."
                        )
                    chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        except httpx.TimeoutException:
            return RecipeImageSearch(status="unavailable", detail="Commons search timed out.")
        except (httpx.HTTPError, ValueError, UnicodeError):
            return RecipeImageSearch(
                status="unavailable", detail="Commons image search is temporarily unavailable."
            )
        finally:
            if own_client:
                await client.aclose()

        if not _json_is_bounded(payload) or not isinstance(payload, dict):
            return RecipeImageSearch(
                status="unavailable", detail="Commons returned invalid or oversized metadata."
            )
        if "error" in payload:
            return RecipeImageSearch(
                status="unavailable", detail="Commons could not complete image search."
            )
        query_data = payload.get("query")
        pages = query_data.get("pages", []) if isinstance(query_data, Mapping) else []
        if not isinstance(pages, list):
            return RecipeImageSearch(
                status="unavailable", detail="Commons returned an unexpected result format."
            )

        results: list[RecipeImage] = []
        for page in pages[:MAX_RESULTS]:
            candidate = self._candidate(clean_query, page)
            if candidate is not None:
                results.append(candidate)
        next_token = _continuation_token(clean_query, payload.get("continue"))
        return RecipeImageSearch(
            status="ok" if results else "no_usable_results",
            detail=None
            if results
            else "No results had supported image formats and usable license metadata.",
            results=results,
            continuation=next_token,
        )

    @staticmethod
    def _candidate(query: str, page: object) -> RecipeImage | None:
        if not isinstance(page, Mapping):
            return None
        page_id = page.get("pageid")
        title = _plain_text(page.get("title"), max_length=300)
        infos = page.get("imageinfo")
        info = infos[0] if isinstance(infos, list) and infos else None
        if (
            not isinstance(page_id, int)
            or page_id < 1
            or not title
            or not isinstance(info, Mapping)
        ):
            return None
        if info.get("mime") not in _ALLOWED_MIMES:
            return None
        image_url = _safe_url(info.get("url"), _ALLOWED_IMAGE_HOSTS)
        thumb_url = _safe_url(info.get("thumburl"), _ALLOWED_IMAGE_HOSTS)
        source_url = _safe_url(info.get("descriptionurl"), _ALLOWED_SOURCE_HOSTS)
        metadata = info.get("extmetadata")
        if not image_url or not thumb_url or not source_url or not isinstance(metadata, Mapping):
            return None
        license_data = _license(metadata)
        if license_data is None:
            return None
        license_name, license_url, obligations = license_data
        attribution_value = _metadata_value(metadata, "Attribution")
        artist = _plain_text(_metadata_value(metadata, "Artist"))
        credit = _plain_text(_metadata_value(metadata, "Credit"))
        attribution = _plain_text(attribution_value)
        required_value = _plain_text(
            _metadata_value(metadata, "AttributionRequired"), max_length=20
        )
        attribution_required = (required_value or "").casefold() in {"true", "yes", "1"}
        if attribution_required and not (attribution or artist or credit):
            return None
        if "Credit the creator" in obligations[0] and not (artist or attribution or credit):
            return None
        attribution_required = attribution_required or "Credit the creator" in obligations[0]
        return RecipeImage(
            provider_id=str(page_id),
            query=query,
            title=title,
            image_url=image_url,
            thumbnail_url=thumb_url,
            source_page_url=source_url,
            artist=artist,
            credit=credit,
            license_name=license_name,
            license_url=license_url,
            attribution_required=attribution_required,
            attribution=attribution,
            obligations=obligations,
        )
