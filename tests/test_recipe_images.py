from __future__ import annotations

import json

import httpx
import pytest

from home_kitchen.recipes.images import (
    COMMONS_API,
    MAX_RESPONSE_BYTES,
    WikimediaCommonsImageProvider,
)


def _page(
    *,
    pageid: int = 123,
    mime: str = "image/jpeg",
    image_url: str = "https://upload.wikimedia.org/wikipedia/commons/1/1a/Food.jpg",
    thumb_url: str = "https://upload.wikimedia.org/wikipedia/commons/thumb/1/1a/Food.jpg/640px-Food.jpg",
    source_url: str = "https://commons.wikimedia.org/wiki/File:Food.jpg",
    license_name: str = "CC BY-SA 4.0",
    license_url: str = "https://creativecommons.org/licenses/by-sa/4.0/",
    artist: str = "<b>Example</b> Photographer",
) -> dict:
    return {
        "pageid": pageid,
        "title": "File:Food.jpg",
        "imageinfo": [
            {
                "mime": mime,
                "url": image_url,
                "thumburl": thumb_url,
                "descriptionurl": source_url,
                "extmetadata": {
                    "Artist": {"value": artist},
                    "Credit": {"value": "Kitchen archive"},
                    "LicenseShortName": {"value": license_name},
                    "LicenseUrl": {"value": license_url},
                    "AttributionRequired": {"value": "true"},
                    "Attribution": {"value": "<span>Example Photographer</span>"},
                },
            }
        ],
    }


def _client(
    payload: dict | bytes, *, status: int = 200, capture: list | None = None
) -> httpx.AsyncClient:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    async def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_commons_request_is_bounded_and_uses_fixed_endpoint() -> None:
    captured = []
    async with _client(
        {"query": {"pages": [_page()]}, "continue": {"continue": "||", "gsroffset": 10}},
        capture=captured,
    ) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search(
            "  fish soup  ", limit=4
        )

    request = captured[0]
    assert str(request.url).startswith(COMMONS_API)
    assert request.headers["User-Agent"].startswith("HomeKitchenBot/0.1")
    assert request.url.params["generator"] == "search"
    assert request.url.params["gsrnamespace"] == "6"
    assert request.url.params["gsrlimit"] == "4"
    assert 'filetype:bitmap filemime:"image/jpeg" fish soup' == request.url.params["gsrsearch"]
    assert "extmetadata" in request.url.params["iiprop"]
    assert result.status == "ok"
    assert result.results[0].provider_id == "123"
    assert result.results[0].artist == "Example Photographer"
    assert result.results[0].obligations == [
        "Credit the creator and link the license.",
        "Share adaptations under the same or a compatible license.",
    ]
    assert result.continuation is not None


def test_commons_http_creative_commons_license_url_is_upgraded_safely() -> None:
    candidate = _page(
        license_name="CC BY-SA 3.0",
        license_url="http://creativecommons.org/licenses/by-sa/3.0/",
    )

    parsed = WikimediaCommonsImageProvider._candidate("tomato", candidate)

    assert parsed is not None
    assert parsed.license_url == "https://creativecommons.org/licenses/by-sa/3.0/"


@pytest.mark.asyncio
async def test_continuation_is_opaque_and_bound_to_query() -> None:
    captured = []
    payload = {"query": {"pages": [_page()]}, "continue": {"continue": "||", "gsroffset": 10}}
    async with _client(payload, capture=captured) as client:
        provider = WikimediaCommonsImageProvider(http_client=client)
        first = await provider.search("soup")
        second = await provider.search("soup", continuation=first.continuation)
        wrong_query = await provider.search("bread", continuation=first.continuation)

    assert second.status == "ok"
    assert captured[1].url.params["gsroffset"] == "10"
    assert captured[1].url.params["continue"] == "||"
    assert wrong_query.status == "invalid_continuation"
    assert len(captured) == 2


@pytest.mark.asyncio
async def test_invalid_query_or_result_limit_does_not_call_provider() -> None:
    captured = []
    async with _client({"query": {"pages": []}}, capture=captured) as client:
        provider = WikimediaCommonsImageProvider(http_client=client)
        assert (await provider.search("  ")).status == "unavailable"
        assert (await provider.search("x" * 201)).status == "unavailable"
        assert (await provider.search("x", limit=11)).status == "unavailable"
    assert captured == []


@pytest.mark.asyncio
async def test_commons_error_envelope_is_unavailable_not_an_empty_search() -> None:
    async with _client({"error": {"code": "ratelimited", "info": "provider detail"}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("tomato")

    assert result.status == "unavailable"
    assert result.results == []
    assert result.detail == "Commons could not complete image search."


@pytest.mark.asyncio
async def test_commons_empty_envelope_remains_a_valid_no_results_search() -> None:
    async with _client({"batchcomplete": ""}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("no matching image")

    assert result.status == "no_usable_results"
    assert result.results == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "candidate",
    [
        _page(mime="image/svg+xml"),
        _page(image_url="http://upload.wikimedia.org/image.jpg"),
        _page(image_url="https://user@upload.wikimedia.org/image.jpg"),
        _page(image_url="https://upload.wikimedia.org:443/image.jpg"),
        _page(thumb_url="https://evil.example/thumb.jpg"),
        _page(source_url="https://evil.example/File:Food.jpg"),
        _page(
            license_name="NonFree", license_url="https://creativecommons.org/licenses/by-nc/4.0/"
        ),
        _page(
            license_name="CC BY-NC 4.0", license_url="https://creativecommons.org/licenses/by/4.0/"
        ),
        _page(
            license_name="CC BY-ND 4.0", license_url="https://creativecommons.org/licenses/by/4.0/"
        ),
        _page(
            license_name="CC BY 4.0", license_url="https://creativecommons.org/licenses/by-nc/4.0/"
        ),
        _page(
            license_name="CC BY-SA 4.0", license_url="https://creativecommons.org/licenses/by/4.0/"
        ),
        # Reject incompatible license families even when the URL points at an
        # otherwise accepted CC BY/CC BY-SA page.
        *[
            _page(license_name=name, license_url=url)
            for name, url in [
                ("CC BY-NC 4.0", "https://creativecommons.org/licenses/by/4.0/"),
                ("CC BY-NC-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
                ("CC BY-NC-ND 4.0", "https://creativecommons.org/licenses/by/4.0/"),
                ("CC BY-ND 4.0", "https://creativecommons.org/licenses/by/4.0/"),
                ("CC BY-SA-NC 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
                ("GFDL", "https://creativecommons.org/licenses/by/4.0/"),
                ("GNU Free Documentation License", "https://commons.wikimedia.org/wiki/GFDL"),
                ("CC BY 4.0", "http://evil.example/licenses/by/4.0/"),
                ("CC BY 4.0", "https://creativecommons.org.evil.example/licenses/by/4.0/"),
                ("CC BY 4.0", "https://creativecommons.org@evil.example/licenses/by/4.0/"),
                ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/?redirect=evil"),
            ]
        ],
        _page(
            license_name="CC BY 4.0 foo", license_url="https://creativecommons.org/licenses/by/4.0/"
        ),
        {
            **_page(license_name="CC BY 4.0", artist=""),
            "imageinfo": [
                {
                    **_page(license_name="CC BY 4.0", artist="")["imageinfo"][0],
                    "extmetadata": {
                        **_page(license_name="CC BY 4.0", artist="")["imageinfo"][0]["extmetadata"],
                        "Credit": {"value": ""},
                        "Attribution": {"value": ""},
                    },
                }
            ],
        },
        {
            **_page(),
            "imageinfo": [
                {
                    **_page()["imageinfo"][0],
                    "extmetadata": {
                        **_page()["imageinfo"][0]["extmetadata"],
                        "Artist": {"value": ""},
                        "Credit": {"value": ""},
                        "Attribution": {"value": ""},
                    },
                }
            ],
        },
    ],
)
async def test_unsupported_or_unsafe_candidates_are_skipped(candidate: dict) -> None:
    async with _client({"query": {"pages": [candidate]}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.status == "no_usable_results"
    assert result.results == []


@pytest.mark.asyncio
async def test_metadata_html_is_plain_text_not_executable_markup() -> None:
    candidate = _page(artist='<script>alert("x")</script><b>Photographer</b>')
    async with _client({"query": {"pages": [candidate]}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.results[0].artist == "Photographer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artist", "credit", "attribution", "expected_required", "expected_artist"),
    [
        ("<b>Ada Photographer</b>", "Archive", "<i>Ada</i>", True, "Ada Photographer"),
        ("", "<span>Archive credit</span>", "", True, None),
        ("", "", "<b>Named attribution</b>", True, None),
    ],
)
async def test_attribution_fields_are_plain_text_and_keep_valid_credit_fallbacks(
    artist: str,
    credit: str,
    attribution: str,
    expected_required: bool,
    expected_artist: str | None,
) -> None:
    candidate = _page(artist=artist)
    meta = candidate["imageinfo"][0]["extmetadata"]
    meta["Credit"] = {"value": credit}
    meta["Attribution"] = {"value": attribution}
    async with _client({"query": {"pages": [candidate]}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    image = result.results[0]
    assert image.attribution_required is expected_required
    assert image.artist == expected_artist
    expected_credit = {
        "Archive": "Archive",
        "<span>Archive credit</span>": "Archive credit",
        "": None,
    }[credit]
    assert image.credit == expected_credit
    expected_attribution = {
        "<i>Ada</i>": "Ada",
        "": None,
        "<b>Named attribution</b>": "Named attribution",
    }[attribution]
    assert image.attribution == expected_attribution


@pytest.mark.asyncio
async def test_cc0_and_cc_by_attribute_rules_are_explicit() -> None:
    cc0 = _page(
        license_name="CC0 1.0", license_url="https://creativecommons.org/publicdomain/zero/1.0/"
    )
    by = _page(license_name="CC BY 4.0", license_url="https://creativecommons.org/licenses/by/4.0/")
    async with _client({"query": {"pages": [cc0, by]}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert len(result.results) == 2
    assert result.results[0].obligations[0].startswith("Public-domain")
    assert result.results[1].attribution_required is True


@pytest.mark.asyncio
async def test_rate_limit_and_http_failure_are_reported() -> None:
    async with _client({}, status=429) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.status == "rate_limited"

    async with _client({}, status=503) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_response_size_and_shape_are_bounded() -> None:
    async with _client(b" " * (MAX_RESPONSE_BYTES + 1)) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.status == "unavailable"

    async with _client({"query": {"pages": {"unexpected": _page()}}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish")
    assert result.status == "unavailable"


@pytest.mark.asyncio
async def test_results_and_pages_are_capped_at_ten() -> None:
    pages = [_page(pageid=index) for index in range(1, 16)]
    async with _client({"query": {"pages": pages}}) as client:
        result = await WikimediaCommonsImageProvider(http_client=client).search("dish", limit=10)
    assert len(result.results) == 10
