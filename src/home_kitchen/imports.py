"""Fetch only public supported recipe hosts. Import produces a reviewable draft."""

from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, HttpUrl
from recipe_scrapers import scrape_html
from starlette.concurrency import run_in_threadpool

ALLOWED_HOSTS = {"matprat.no", "www.matprat.no", "marmiton.org", "www.marmiton.org"}
MAX_BYTES = 2_000_000


class ImportedDraft(BaseModel):
    title: str
    servings: int
    ingredients: list[str]
    instructions: list[str]
    source_url: HttpUrl
    image_url: HttpUrl | None = None


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Import supports public HTTPS Matprat and Marmiton recipes")


def parse_recipe(html: str, url: str) -> ImportedDraft:
    scraper = scrape_html(html, org_url=url)
    # Keep source ingredient text for human review: quantities are not guessed.
    import re

    yield_text = scraper.yields()
    match = re.search(r"\d+", yield_text)
    try:
        image = HttpUrl(scraper.image())
        if image.scheme != "https" or image.username or image.password:
            image = None
    except (ValueError, TypeError, AttributeError):
        image = None
    return ImportedDraft(
        title=scraper.title(),
        servings=max(1, int(match.group()) if match else 1),
        ingredients=scraper.ingredients() or scraper.schema.ingredients(),
        instructions=[step.strip() for step in scraper.instructions().splitlines() if step.strip()],
        source_url=HttpUrl(url),
        image_url=image,
    )


async def import_recipe(url: str) -> ImportedDraft:
    validate_url(url)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        for _ in range(4):
            validate_url(url)
            async with client.stream(
                "GET", url, headers={"User-Agent": "HomeKitchen/0.1"}
            ) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                if "text/html" not in response.headers.get("content-type", ""):
                    raise ValueError("Recipe source did not return HTML")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise ValueError("Recipe page exceeds import size limit")
                return await run_in_threadpool(parse_recipe, body.decode("utf-8"), url)
    raise ValueError("Recipe source redirected too many times")
