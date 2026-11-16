"""Original fictional recipe fixtures; no external source text or images."""

from uuid import UUID

from home_kitchen.models import Recipe

SYNTHETIC_RECIPE_ID = UUID("d2e70244-4c47-44d1-a890-d787f8da8f16")


def synthetic_recipe() -> Recipe:
    """Return a fresh recipe with explicit source-text mapping and an unknown measure."""
    return Recipe.model_validate(
        {
            "id": str(SYNTHETIC_RECIPE_ID),
            "title": "Cloudpan carrot and pearl barley bowls",
            "servings": 3,
            "source": "manual",
            "ingredients": [
                {
                    "name": "pearl barley",
                    "quantity": "110",
                    "unit": "g",
                    "category": "Grains",
                    "source_indices": [0],
                },
                {
                    "name": "carrot",
                    "quantity": "170",
                    "unit": "g",
                    "category": "Vegetables",
                    "source_indices": [1],
                },
                {
                    "name": "chickpeas",
                    "quantity": "140",
                    "unit": "g",
                    "category": "Legumes",
                    "source_indices": [2],
                },
                {
                    "name": "citrus oil",
                    "quantity": "1",
                    "unit": "tbsp",
                    "category": "Pantry",
                    "source_indices": [3],
                },
                {
                    "name": "salt",
                    "quantity": None,
                    "unit": None,
                    "category": "Seasoning",
                    "source_indices": [4],
                },
            ],
            "instructions": [
                "Rinse the barley and simmer it until tender.",
                "Roast carrot ribbons with chickpeas until their edges curl.",
                "Toss the warm grains with citrus oil and serve with the roasted vegetables.",
            ],
            "source_ingredients": [
                {"index": 0, "text": "110 g pearl barley"},
                {"index": 1, "text": "170 g gulrot (carrot), cut into ribbons"},
                {"index": 2, "text": "140 g chickpeas"},
                {"index": 3, "text": "1 tbsp citrus oil"},
                {"index": 4, "text": "salt, amount not specified"},
            ],
        }
    )
