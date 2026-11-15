"""Bounded, unauthenticated extraction of public Oda product media metadata."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, HttpUrl

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_IMAGES = 12
MAX_JSONLD_NODES = 5_000
MAX_JSONLD_DEPTH = 32
DEFAULT_RETRY_AFTER_SECONDS = 30
USER_AGENT = "HomeKitchenBot/0.1 (+https://github.com/ghjklw/home-kitchen)"
_PRODUCT_PATH = re.compile(r"^/no/products/([1-9][0-9]*)-[a-z0-9]+(?:-[a-z0-9]+)*/?$")
_MEASURE = re.compile(
    r"^[0-9]+(?:[.,][0-9]+)?\s*(?:ml|cl|dl|l|mg|g|kg|oz|lb|lit(?:er|re)s?)$", re.I
)
_ALLOWED_IMAGE_HOSTS = {"images.oda.com"}


class ProductMedia(BaseModel):
    """Public image references copied from the exact Oda product page."""

    url: HttpUrl
    image_url: HttpUrl | None = None
    additional_image_urls: list[HttpUrl] = Field(default_factory=list)
    pack_description: str | None = None
    detail: str
    retry_after_seconds: int | None = Field(default=None, ge=0)


class _JsonLdParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.documents: list[object] = []
        self._script = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.casefold() == "script":
            attributes = {name.casefold(): value for name, value in attrs}
            if (attributes.get("type") or "").split(";", 1)[
                0
            ].strip().casefold() == "application/ld+json":
                self._script = True
                self._parts = []

    def handle_data(self, data):
        if self._script:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag.casefold() == "script" and self._script:
            source = "".join(self._parts).strip()
            if source:
                try:
                    self.documents.append(json.loads(source))
                except (json.JSONDecodeError, ValueError, RecursionError):
                    pass
            self._script = False
            self._parts = []


def _validate_product_url(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value)
        product_match = _PRODUCT_PATH.fullmatch(parsed.path)
    except (TypeError, ValueError):
        product_match = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname != "oda.com"
        or parsed.netloc != "oda.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or product_match is None
    ):
        raise ValueError("Expected a public HTTPS Oda Norway product page URL")
    return value, product_match.group(1)


def _product_objects(value):
    stack = [(value, 0)]
    visited = 0
    while stack and visited < MAX_JSONLD_NODES:
        current, depth = stack.pop()
        visited += 1
        if depth > MAX_JSONLD_DEPTH:
            continue
        if isinstance(current, list):
            stack.extend((child, depth + 1) for child in reversed(current[:MAX_JSONLD_NODES]))
            continue
        if not isinstance(current, dict):
            continue
        kinds = current.get("@type", [])
        if isinstance(kinds, str):
            kinds = [kinds]
        if isinstance(kinds, list) and "Product" in kinds:
            yield current
        children = [child for child in current.values() if isinstance(child, (dict, list))]
        stack.extend((child, depth + 1) for child in reversed(children))


def _image_candidates(value):
    stack = [(value, 0)]
    visited = 0
    while stack and visited < MAX_JSONLD_NODES:
        current, depth = stack.pop()
        visited += 1
        if depth > MAX_JSONLD_DEPTH:
            continue
        if isinstance(current, str):
            yield current
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in reversed(current[:MAX_JSONLD_NODES]))
        elif isinstance(current, dict):
            stack.extend(
                (current[key], depth + 1)
                for key in ("url", "contentUrl")
                if isinstance(current.get(key), str)
            )


def _safe_image_url(value: str) -> HttpUrl | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_IMAGE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not parsed.path.startswith("/")
        ):
            return None
        return HttpUrl(value)
    except (TypeError, ValueError):
        return None


def _pack_description(product: dict) -> str | None:
    """Use only an explicit, unambiguous schema.org size or weight value."""
    size = product.get("size")
    if isinstance(size, str) and _MEASURE.fullmatch(size.strip()):
        return size.strip()[:200]
    weight = product.get("weight")
    if isinstance(weight, str) and _MEASURE.fullmatch(weight.strip()):
        return weight.strip()[:200]
    if isinstance(weight, dict):
        value = weight.get("value")
        unit = weight.get("unitText") or weight.get("unitCode")
        if (
            isinstance(value, (int, float, str))
            and not isinstance(value, bool)
            and isinstance(unit, str)
        ):
            number = str(value).strip()
            unit = unit.strip()
            if (
                number
                and unit
                and len(number) <= 40
                and len(unit) <= 40
                and _MEASURE.fullmatch(f"{number} {unit}")
            ):
                return f"{number} {unit}"
    return None


def _matches_page(product: dict, product_id: str) -> bool:
    for key in ("url", "@id"):
        value = product.get(key)
        if isinstance(value, str):
            try:
                match = _PRODUCT_PATH.fullmatch(urlsplit(value).path)
            except ValueError:
                match = None
            if match is not None and match.group(1) == product_id:
                return True
    for key in ("sku", "productID", "productId"):
        value = product.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            if str(value) == product_id:
                return True
    return False


def _has_product_identifier(product: dict) -> bool:
    return any(
        key in product and product[key] not in (None, "", [])
        for key in ("url", "@id", "sku", "productID", "productId")
    )


def _read_product_metadata(html: str, product_id: str) -> tuple[list[HttpUrl], str | None]:
    parser = _JsonLdParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return [], None
    images: list[HttpUrl] = []
    seen: set[str] = set()
    pack_description = None
    products = [product for doc in parser.documents for product in _product_objects(doc)]
    matches = [product for product in products if _matches_page(product, product_id)]
    if len(matches) == 1:
        selected = matches[0]
    elif not matches and len(products) == 1 and not _has_product_identifier(products[0]):
        # A sole anonymous Product can represent the page entity; an identified
        # mismatch or multiple anonymous products is ambiguous and yields no media.
        selected = products[0]
    else:
        selected = None
    if selected is not None:
        pack_description = _pack_description(selected)
        for candidate in _image_candidates(selected.get("image")):
            safe = _safe_image_url(candidate)
            if safe is not None and str(safe) not in seen:
                seen.add(str(safe))
                images.append(safe)
                if len(images) >= MAX_IMAGES:
                    break
    return images, pack_description


def _retry_after_seconds(value: str | None) -> int:
    if value:
        try:
            return min(max(int(value.strip()), 0), 86_400)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return min(max(int((when - datetime.now(UTC)).total_seconds()), 0), 86_400)
            except (TypeError, ValueError, OverflowError):
                pass
    return DEFAULT_RETRY_AFTER_SECONDS


def _unavailable(url: str, detail: str, retry_after: int | None = None) -> ProductMedia:
    return ProductMedia(url=url, detail=detail, retry_after_seconds=retry_after)


async def fetch_product_media(
    url: str,
    http_client: httpx.AsyncClient | None = None,
) -> ProductMedia:
    """Fetch one public Oda product page without forwarding credentials.

    Redirects, page size, destination host, and product path are checked before
    each request. The helper never fetches image URLs or authenticates to Oda.
    """
    current_url, original_product_id = _validate_product_url(url)
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(12.0),
        follow_redirects=False,
        trust_env=False,
    )
    redirects = 0
    try:
        while True:
            request = client.build_request(
                "GET",
                current_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=12.0,
            )
            # The injected client may carry an unrelated session. Do not let its
            # cookies or auth headers cross into this anonymous public fetch.
            request.headers.pop("authorization", None)
            request.headers.pop("cookie", None)
            try:
                response = await client.send(
                    request,
                    stream=True,
                    auth=None,
                    follow_redirects=False,
                )
            except httpx.HTTPError:
                return _unavailable(current_url, "Oda product media could not be fetched.")

            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirects >= MAX_REDIRECTS:
                        return _unavailable(current_url, "Oda returned an unsupported redirect.")
                    try:
                        redirected_url, product_id = _validate_product_url(
                            urljoin(current_url, location)
                        )
                    except ValueError:
                        return _unavailable(
                            current_url, "Oda redirected outside supported product pages."
                        )
                    if product_id != original_product_id:
                        return _unavailable(current_url, "Oda redirected to a different product.")
                    current_url = redirected_url
                    redirects += 1
                    continue
                if response.status_code == 429 or 500 <= response.status_code <= 599:
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                    return _unavailable(
                        current_url,
                        f"Oda is temporarily unavailable; try again after {retry_after} seconds.",
                        retry_after,
                    )
                if response.status_code != 200:
                    return _unavailable(current_url, "Oda product media is unavailable.")
                if "text/html" not in response.headers.get("content-type", "").casefold():
                    return _unavailable(current_url, "Oda did not return a product page.")
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_PAGE_BYTES:
                            return _unavailable(
                                current_url, "Oda product page exceeds the size limit."
                            )
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        return _unavailable(current_url, "Oda product page exceeds the size limit.")
                html = bytes(body).decode("utf-8", errors="replace")
                images, pack_description = _read_product_metadata(html, original_product_id)
                detail = (
                    "Product images loaded from Oda's public page."
                    if images
                    else "Oda's public page contains no supported product images."
                )
                return ProductMedia(
                    url=current_url,
                    image_url=images[0] if images else None,
                    additional_image_urls=images[1:],
                    pack_description=pack_description,
                    detail=detail,
                )
            except (httpx.HTTPError, UnicodeError):
                return _unavailable(current_url, "Oda product media could not be read.")
            finally:
                await response.aclose()
    finally:
        if owns_client:
            await client.aclose()
