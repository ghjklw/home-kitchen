from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image, PngImagePlugin
from pydantic_ai import BinaryContent
from pydantic_ai.models.test import TestModel

from home_kitchen.recipes.models import RecipeContext
from home_kitchen.recipes.photos import (
    MAX_PHOTO_BYTES,
    PhotoExtraction,
    extract_photo,
    photo_agent,
    validate_photo,
)
from home_kitchen.settings import Household, Settings

pytestmark = pytest.mark.mocked_provider


def image_bytes(image_format="PNG", size=(8, 6), **options):
    output = BytesIO()
    Image.new("RGB", size, "red").save(output, format=image_format, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    "image_format,mime",
    [
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
        ("WEBP", "image/webp"),
    ],
)
def test_photo_decodes_supported_formats_and_preserves_dimensions(image_format, mime):
    photo = validate_photo(image_bytes(image_format), mime)
    assert (photo.width, photo.height, photo.mime_type) == (8, 6, mime)
    with Image.open(BytesIO(photo.data)) as decoded:
        decoded.load()
        assert decoded.format == image_format
        assert not {"exif", "icc_profile", "xmp", "comment"}.intersection(decoded.info)
        assert not decoded.getexif()


def test_photo_applies_exif_orientation_and_strips_metadata():
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "private author"
    photo = validate_photo(image_bytes("JPEG", exif=exif), "image/jpeg")
    assert (photo.width, photo.height) == (6, 8)
    with Image.open(BytesIO(photo.data)) as decoded:
        assert not decoded.getexif()
        assert "exif" not in decoded.info
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Private", "sensitive annotation")
    photo = validate_photo(image_bytes(pnginfo=metadata), "image/png")
    with Image.open(BytesIO(photo.data)) as decoded:
        assert "Private" not in decoded.info


@pytest.mark.parametrize(
    "payload,mime",
    [
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (b"not an image", "image/jpeg"),
        (b"", "image/png"),
        (b"a" * (MAX_PHOTO_BYTES + 1), "image/png"),
    ],
)
def test_photo_rejects_unsupported_invalid_and_oversized_inputs(payload, mime):
    with pytest.raises(ValueError):
        validate_photo(payload, mime)


def test_photo_rejects_mime_mismatch_and_truncated_decode():
    with pytest.raises(ValueError, match="MIME"):
        validate_photo(image_bytes(), "image/jpeg")
    with pytest.raises(ValueError):
        validate_photo(image_bytes("JPEG")[:-20], "image/jpeg")
    with pytest.raises(ValueError):
        validate_photo(image_bytes()[:40], "image/png")


@pytest.mark.parametrize("image_format,mime", [("PNG", "image/png"), ("WEBP", "image/webp")])
def test_photo_rejects_animation(image_format, mime):
    output = BytesIO()
    Image.new("RGB", (4, 4), "red").save(
        output,
        format=image_format,
        save_all=True,
        append_images=[Image.new("RGB", (4, 4), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ValueError, match="Animated"):
        validate_photo(output.getvalue(), mime)


def test_photo_fails_closed_for_pixel_limit_and_pillow_bomb_warning(monkeypatch):
    import home_kitchen.recipes.photos as photos

    payload = image_bytes()
    monkeypatch.setattr(photos, "MAX_PHOTO_PIXELS", 47)
    with pytest.raises(ValueError, match="megapixel"):
        validate_photo(payload, "image/png")
    monkeypatch.setattr(photos, "MAX_PHOTO_PIXELS", 20_000_000)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 30)
    with pytest.raises(ValueError, match="unsafe"):
        validate_photo(payload, "image/png")
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 20)
    with pytest.raises(ValueError, match="unsafe"):
        validate_photo(payload, "image/png")


def extraction_output():
    return {
        "draft": {
            "title": "Soup",
            "servings": 2,
            "ingredients": [
                {"name": "salt", "quantity": None, "unit": None, "source_indices": [0]}
            ],
            "instructions": ["Simmer"],
            "source_ingredients": [{"index": 0, "text": "a pinch of salt"}],
            "source_url": "https://model.example/recipe",
            "image_url": "https://model.example/photo.jpg",
        },
        "uncertainties": ["Salt quantity is not numeric"],
    }


async def test_photo_transcribes_with_controlled_typed_agent_and_forces_private_attribution():
    model = TestModel(custom_output_args=extraction_output())
    agent = photo_agent(Settings(local_dev=True), model=model)
    photo = validate_photo(image_bytes(), "image/png")
    result = await extract_photo(photo, RecipeContext(household=Household()), agent=agent)
    assert isinstance(result, PhotoExtraction)
    assert result.draft.ingredients[0].quantity is None
    assert result.draft.ingredients[0].unit is None
    assert result.draft.ingredients[0].source_indices == [0]
    assert result.draft.source_ingredients[0].text == "a pinch of salt"
    assert result.draft.source_url is None
    assert result.draft.image_url is None
    assert result.draft.provenance is None
    assert result.uncertainties == ["Salt quantity is not numeric"]


async def test_photo_sends_binary_content_with_bounded_usage_and_rejects_missing_source_rows():
    photo = validate_photo(image_bytes(), "image/png")
    output = extraction_output()
    output["draft"]["ingredients"][0]["source_indices"] = []
    output["draft"]["source_ingredients"] = []
    agent = AsyncMock()
    agent.run.return_value.output = PhotoExtraction.model_validate(output)
    with pytest.raises(ValueError, match="retain source"):
        await extract_photo(photo, RecipeContext(household=Household()), agent=agent)
    prompt = agent.run.await_args.args[0]
    assert isinstance(prompt[1], BinaryContent)
    assert prompt[1].data == photo.data
    assert prompt[1].media_type == "image/png"
    limits = agent.run.await_args.kwargs["usage_limits"]
    assert limits.request_limit == 2
    assert limits.total_tokens_limit == 12000


def test_photo_agent_uses_only_explicit_application_authentication(monkeypatch):
    model = TestModel()
    calls = []

    def configured(settings):
        calls.append(settings)
        return model

    monkeypatch.setattr("home_kitchen.recipes.photos.anthropic_model", configured)
    settings = Settings(local_dev=True)
    assert photo_agent(settings).model is model
    assert calls == [settings]


async def test_photo_extract_detects_source_language_on_draft():
    french_output = {
        "draft": {
            "title": "Tarte aux pommes maison",
            "servings": 6,
            "ingredients": [
                {"name": "pommes", "quantity": 4, "unit": "item", "source_indices": [0]},
                {"name": "beurre", "quantity": 50, "unit": "g", "source_indices": [1]},
                {"name": "sucre", "quantity": 100, "unit": "g", "source_indices": [2]},
            ],
            "instructions": [
                "Préchauffer le four et mélanger les ingrédients avec de la farine.",
                "Ajouter les pommes et cuire pendant trente minutes.",
            ],
            "source_ingredients": [
                {"index": 0, "text": "4 pommes"},
                {"index": 1, "text": "50 g de beurre"},
                {"index": 2, "text": "100 g de sucre"},
            ],
        },
        "uncertainties": [],
    }
    model = TestModel(custom_output_args=french_output)
    agent = photo_agent(Settings(local_dev=True), model=model)
    photo = validate_photo(image_bytes(), "image/png")
    result = await extract_photo(photo, RecipeContext(household=Household()), agent=agent)
    assert result.draft.source_language == "fr"
