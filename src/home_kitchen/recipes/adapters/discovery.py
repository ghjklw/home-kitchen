"""Concrete public-web recipe discovery adapters (Marmiton, Matprat)."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import HttpUrl

from home_kitchen.recipes.discovery import (
    MAX_RESULTS,
    DiscoveryContext,
    ProviderSearch,
    RecipeDiscoveryItem,
)

MARMITON_SEARCH = "https://www.marmiton.org/recettes/recherche.aspx"
MARMITON_INDEX = "https://www.marmiton.org/recettes/"
MATPRAT_INDEX = "https://www.matprat.no/oppskrifter/"
MAX_RESPONSE_BYTES = 1_000_000
MAX_HTML_NODES = 20_000
USER_AGENT = "HomeKitchenBot/0.1 (+https://github.com/ghjklw/home-kitchen)"
_MARMITON_HOSTS = {"marmiton.org", "www.marmiton.org"}
_MARMITON_RECIPE_PATH = re.compile(r"^/recettes/recette_[a-z0-9_.~-]+_[0-9]+\.aspx$")
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _Anchor:
    def __init__(self, href: str):
        self.href = href
        self.text: list[str] = []
        self.image_url: str | None = None
        self.image_alt: str | None = None


class _MarmitonParser(HTMLParser):
    """Extract only visible links to canonical recipe paths; never execute page code."""

    def __init__(self, result_limit: int):
        super().__init__(convert_charrefs=True)
        self.result_limit = result_limit
        self.node_count = 0
        self.truncated = False
        self._anchor: _Anchor | None = None
        self._anchors: list[_Anchor] = []
        self._elements: list[tuple[str, set[str]]] = []
        self.search_list_found = False

    def handle_starttag(self, tag, attrs):
        self.node_count += 1
        if self.node_count > MAX_HTML_NODES:
            self.truncated = True
            return
        attributes = dict(attrs)
        tag = tag.casefold()
        classes: set[str] = set((attributes.get("class") or "").split())
        if tag not in _VOID_TAGS:
            self._elements.append((tag, classes))
        if tag == "ul" and "search-list" in classes:
            self.search_list_found = True
        in_search_item = False
        in_search_list = False
        if tag == "a":
            for element, element_classes in self._elements:
                if element == "ul" and "search-list" in element_classes:
                    in_search_list = True
                elif in_search_list and element == "li" and "search-list__item" in element_classes:
                    in_search_item = True
        if (
            tag == "a"
            and self._anchor is None
            and in_search_item
            and "card-content__title" in classes
        ):
            self._anchor = _Anchor(attributes.get("href", ""))
        elif tag.casefold() == "img" and self._anchor is not None:
            self._anchor.image_url = (
                attributes.get("src")
                or attributes.get("data-src")
                or _first_srcset(attributes.get("srcset"))
            )
            self._anchor.image_alt = attributes.get("alt")

    def handle_endtag(self, tag):
        self.node_count += 1
        if self.node_count > MAX_HTML_NODES:
            self.truncated = True
            return
        if tag.casefold() == "a" and self._anchor is not None:
            self._anchors.append(self._anchor)
            self._anchor = None
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag.casefold():
                del self._elements[index:]
                break

    def handle_data(self, data):
        if self._anchor is not None and sum(map(len, self._anchor.text)) < 1000:
            self._anchor.text.append(data[:1000])

    def results(self, page_url: str) -> list[RecipeDiscoveryItem]:
        merged: dict[str, dict[str, str | None]] = {}
        for anchor in self._anchors:
            recipe_url = _marmiton_recipe_url(anchor.href, page_url)
            if recipe_url is None:
                continue
            url = str(recipe_url)
            existing = merged.get(url)
            if existing is None and len(merged) >= self.result_limit:
                continue
            title = " ".join("".join(anchor.text).split()) or " ".join(
                (anchor.image_alt or "").split()
            )
            photo = _marmiton_image_url(anchor.image_url, page_url)
            if existing is None:
                merged[url] = {"title": title, "image": str(photo) if photo else None}
            else:
                if title:
                    existing["title"] = title
                if photo:
                    existing["image"] = str(photo)
        return [
            RecipeDiscoveryItem(
                source="marmiton",
                title=values["title"] or "Recipe",
                url=HttpUrl(url),
                image_url=HttpUrl(values["image"]) if values["image"] else None,
                source_attribution="Marmiton",
                importable=True,
            )
            for url, values in merged.items()
            if values["title"]
        ]


def _first_srcset(srcset: str | None) -> str | None:
    if not srcset:
        return None
    return srcset.split(",", 1)[0].strip().split(" ", 1)[0] or None


def _safe_url(raw: str | None, hosts: set[str], base: str) -> HttpUrl | None:
    if not raw:
        return None
    absolute = urljoin(base, raw.strip())
    try:
        parsed = urlsplit(absolute)
        port = parsed.port
        candidate = HttpUrl(absolute)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        return None
    return candidate


def _marmiton_recipe_url(raw: str, base: str) -> HttpUrl | None:
    candidate = _safe_url(raw, _MARMITON_HOSTS, base)
    if candidate is None:
        return None
    parsed = urlsplit(str(candidate))
    if parsed.query or parsed.fragment or not _MARMITON_RECIPE_PATH.fullmatch(parsed.path):
        return None
    return candidate


def _marmiton_image_url(raw: str | None, base: str) -> HttpUrl | None:
    return _safe_url(raw, {"assets.afcdn.com"}, base)


def _search_page_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _MARMITON_HOSTS
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path != "/recettes/recherche.aspx"
        or parsed.fragment
    ):
        return None
    query_parts = parsed.query.split("&") if parsed.query else []
    if len(query_parts) != 1 or not query_parts[0].startswith("aqt="):
        return None
    return url


class MarmitonDiscoveryProvider:
    source: Literal["marmiton"] = "marmiton"
    timeout_seconds = 10.0

    def __init__(self, *, http_client: httpx.AsyncClient | None = None):
        self.http_client = http_client

    async def _fetch(self, query: str) -> tuple[bytes, str]:
        if self.http_client is None:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
            ) as client:
                return await self._fetch_with_client(client, query)
        return await self._fetch_with_client(self.http_client, query)

    async def _fetch_with_client(self, client: httpx.AsyncClient, query: str) -> tuple[bytes, str]:
        url = MARMITON_SEARCH
        params: dict[str, str] | None = {"aqt": query}
        for redirect_count in range(4):
            async with client.stream("GET", url, params=params, follow_redirects=False) as response:
                params = None
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count == 3:
                        raise ValueError("Too many search redirects")
                    location = response.headers.get("location")
                    destination = _search_page_url(urljoin(str(response.url), location or ""))
                    if destination is None:
                        raise ValueError("Search redirected outside the supported first page")
                    url = destination
                    continue
                if response.status_code != 200:
                    raise httpx.HTTPStatusError(
                        "Marmiton search was unavailable",
                        request=response.request,
                        response=response,
                    )
                content_type = response.headers.get("content-type", "").casefold()
                if "text/html" not in content_type:
                    raise ValueError("Marmiton search did not return HTML")
                if int(response.headers.get("content-length", "0") or 0) > MAX_RESPONSE_BYTES:
                    raise ValueError("Marmiton search response exceeded the size limit")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("Marmiton search response exceeded the size limit")
                    body.extend(chunk)
                return bytes(body), str(response.url)
        raise ValueError("Marmiton search was unavailable")

    async def search(
        self,
        query: str,
        limit: int,
        actor_id: str | None = None,
        *,
        context: DiscoveryContext | None = None,
    ) -> ProviderSearch:
        del actor_id, context
        body, page_url = await self._fetch(query)
        parser = _MarmitonParser(min(limit, MAX_RESULTS))
        parser.feed(body.decode("utf-8", errors="replace"))
        if not parser.search_list_found or parser.truncated:
            return ProviderSearch(
                status="unavailable",
                detail="Marmiton search results could not be identified reliably.",
                index_url=HttpUrl(MARMITON_INDEX),
            )
        results = parser.results(page_url)
        return ProviderSearch(
            results=results,
            detail="Results use Marmiton’s search ranking; check relevance.",
        )


class MatpratDiscoveryProvider:
    source: Literal["matprat"] = "matprat"
    timeout_seconds = 0.1

    async def search(
        self,
        query: str,
        limit: int,
        actor_id: str | None = None,
        *,
        context: DiscoveryContext | None = None,
    ) -> ProviderSearch:
        del query, limit, actor_id, context
        return ProviderSearch(
            status="unsupported",
            detail="Matprat does not expose a supported public keyword search adapter.",
            index_url=HttpUrl(MATPRAT_INDEX),
        )
