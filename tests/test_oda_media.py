import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from home_kitchen.oda.media import (
    MAX_PAGE_BYTES,
    USER_AGENT,
    fetch_product_media,
)

PRODUCT_URL = "https://oda.com/no/products/2257-tine-okologisk-lettmelk-12/"
IMAGE_URLS = [f"https://images.oda.com/no/products/milk-{index}.jpg" for index in range(5)]


def page(*, images=None, extra=None):
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "url": PRODUCT_URL,
        "sku": "2257",
    }
    if images is not None:
        product["image"] = images
    if extra:
        product.update(extra)
    return (
        "<!doctype html><html><head><script type='application/ld+json'>"
        + json.dumps(product)
        + "</script></head><body>Product page</body></html>"
    )


@pytest.mark.asyncio
async def test_extracts_only_existing_https_oda_image_references_and_explicit_size():
    html = page(
        images=[
            *IMAGE_URLS,
            "http://images.oda.com/no/products/insecure.jpg",
            "https://images.oda.com.evil.example/no/products/spoof.jpg",
            "https://user:password@images.oda.com/no/products/credential.jpg",
            "https://127.0.0.1/no/products/private.jpg",
        ],
        extra={"size": "1 liter"},
    )

    async def handler(request):
        assert request.url == PRODUCT_URL
        assert request.headers["user-agent"] == USER_AGENT
        assert "bot" in USER_AGENT.casefold()
        assert "home-kitchen" in USER_AGENT.casefold()
        assert request.headers.get("authorization") is None
        assert request.headers.get("cookie") is None
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=html)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-password"),
        cookies={"session": "private-cookie"},
        headers={"Authorization": "Bearer private-token"},
    ) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert str(media.url) == PRODUCT_URL
    assert str(media.image_url) == IMAGE_URLS[0]
    assert [str(image) for image in media.additional_image_urls] == IMAGE_URLS[1:]
    assert media.pack_description == "1 liter"
    assert media.retry_after_seconds is None


@pytest.mark.asyncio
async def test_missing_jsonld_images_and_ambiguous_description_return_no_media():
    html = page(images=None, extra={"description": "Milk in 1 L carton"})

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert media.image_url is None
    assert media.additional_image_urls == []
    assert media.pack_description is None
    assert "no supported product images" in media.detail


@pytest.mark.asyncio
async def test_prefers_product_entity_matching_requested_product_over_related_product():
    document = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Product",
                "url": "https://oda.com/no/products/12-related-item/",
                "image": "https://images.oda.com/no/products/related.jpg",
            },
            {
                "@type": "Product",
                "url": PRODUCT_URL,
                "image": IMAGE_URLS[0],
            },
        ],
    }
    html = "<script type='application/ld+json'>" + json.dumps(document) + "</script>"

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)
    assert str(media.image_url) == IMAGE_URLS[0]


@pytest.mark.asyncio
async def test_does_not_fallback_to_mismatched_or_ambiguous_product_entities():
    document = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Product",
                "url": "https://oda.com/no/products/12-related-item/",
                "image": "https://images.oda.com/no/products/related.jpg",
            },
            {"@type": "Product", "image": "https://images.oda.com/no/products/anonymous.jpg"},
        ],
    }
    html = "<script type='application/ld+json'>" + json.dumps(document) + "</script>"

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)
    assert media.image_url is None
    assert media.additional_image_urls == []


@pytest.mark.asyncio
async def test_single_unidentified_product_is_safe_fallback():
    anonymous = {"@context": "https://schema.org", "@type": "Product", "image": IMAGE_URLS[0]}
    html = "<script type='application/ld+json'>" + json.dumps(anonymous) + "</script>"

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)
    assert str(media.image_url) == IMAGE_URLS[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://oda.com/no/products/2257-milk/",
        "https://evil.example/no/products/2257-milk/",
        "https://oda.com:443/no/products/2257-milk/",
        "https://user@oda.com/no/products/2257-milk/",
        "https://oda.com/no/products/2257-milk/?next=https://evil.example",
        "https://oda.com/no/products/2257-milk/#fragment",
        "https://oda.com/no/api/products/2257-milk/",
        "https://oda.com/no/products/0-milk/",
        "https://oda.com/no/products/not-a-product/",
    ],
)
async def test_rejects_unsafe_or_nonproduct_input_before_request(url):
    def forbidden(_request):
        raise AssertionError("invalid URL must not cause an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="public HTTPS Oda Norway"):
            await fetch_product_media(url, http_client=client)


@pytest.mark.asyncio
async def test_redirect_is_manual_and_rejects_nonoda_target_without_following_it():
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/collect"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert calls == [PRODUCT_URL]
    assert media.image_url is None
    assert "redirected outside" in media.detail


@pytest.mark.asyncio
async def test_redirect_limit_is_enforced_for_same_product():
    calls = []

    async def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": PRODUCT_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert len(calls) == 4
    assert media.image_url is None
    assert "unsupported redirect" in media.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_throttled_and_server_errors_return_retry_guidance_without_retries(status):
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"retry-after": "73"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert calls == 1
    assert media.retry_after_seconds == 73
    assert "73 seconds" in media.detail


@pytest.mark.asyncio
async def test_retry_after_http_date_is_honored():
    target = datetime.now(UTC) + timedelta(seconds=90)

    async def handler(_request):
        return httpx.Response(503, headers={"retry-after": format_datetime(target, usegmt=True)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert 0 <= media.retry_after_seconds <= 90


@pytest.mark.asyncio
async def test_page_size_is_bounded_even_when_content_length_is_missing():
    oversized = b"<html>" + b"x" * (MAX_PAGE_BYTES + 1) + b"</html>"

    async def handler(_request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=oversized)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        media = await fetch_product_media(PRODUCT_URL, http_client=client)

    assert media.image_url is None
    assert "exceeds the size limit" in media.detail
