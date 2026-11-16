"""Automatic recipe language detection using culinary vocabulary and orthographic markers."""

from __future__ import annotations

import re
from typing import Any

# High-frequency and culinary vocabulary per language
_LANGUAGE_VOCABULARY: dict[str, set[str]] = {
    "nb": {
        # Common Bokmål words
        "og",
        "i",
        "med",
        "til",
        "på",
        "for",
        "av",
        "som",
        "en",
        "et",
        "ikke",
        "fra",
        "om",
        "men",
        "slik",
        "dette",
        "etter",
        "når",
        "kan",
        "eller",
        "ved",
        "blir",
        "vært",
        "skal",
        "denne",
        "disse",
        "hvis",
        "under",
        "over",
        "seg",
        "hver",
        "alt",
        "noe",
        # Culinary / Recipe terms
        "skjær",
        "tilsett",
        "stek",
        "steke",
        "kutt",
        "visp",
        "vispes",
        "kok",
        "koke",
        "varm",
        "bland",
        "server",
        "serveres",
        "minutter",
        "spiseskje",
        "spiseskjeer",
        "teskje",
        "teskjeer",
        "hvetemel",
        "smør",
        "løk",
        "hvitløk",
        "rør",
        "hell",
        "dryss",
        "del",
        "ha",
        "kasserolle",
        "stekepanne",
        "stekeovn",
        "ca",
        "fløte",
        "kylling",
        "kjøttdeig",
        "kjøtt",
        "poteter",
        "potet",
        "gulrot",
        "gulrøtter",
        "olje",
        "salt",
        "pepper",
        "sukker",
        "vann",
        "egg",
        "porsjoner",
        "ingredienser",
        "fremgangsmåte",
        "skrell",
        "avkjøl",
        "smak",
        "servering",
        "skiver",
        "terninger",
        "grovt",
        "fint",
    },
    "da": {
        # Common Danish words
        "og",
        "i",
        "med",
        "til",
        "på",
        "for",
        "af",
        "som",
        "en",
        "et",
        "ikke",
        "fra",
        "om",
        "men",
        "dette",
        "efter",
        "når",
        "kan",
        "eller",
        "ved",
        "bliver",
        "været",
        "skal",
        "denne",
        "disse",
        "hvis",
        "under",
        "over",
        "sig",
        "hver",
        "alt",
        "noget",
        # Culinary / Recipe terms
        "skær",
        "tilsæt",
        "steg",
        "stege",
        "pisk",
        "piskes",
        "kog",
        "koge",
        "varm",
        "bland",
        "server",
        "serveres",
        "minutter",
        "spiseskefuld",
        "spiseskeer",
        "teskefuld",
        "hvedemel",
        "smør",
        "løg",
        "hvidløg",
        "rør",
        "hæld",
        "drys",
        "del",
        "gryde",
        "pande",
        "ovn",
        "ca",
        "fløde",
        "kylling",
        "hakket",
        "oksekød",
        "kartofler",
        "kartoffel",
        "gulerod",
        "gulerødder",
        "olie",
        "salt",
        "peber",
        "sukker",
        "vand",
        "æg",
        "portioner",
        "ingredienser",
        "fremgangsmåde",
        "skræl",
        "afkøl",
        "smag",
        "servering",
        "skiver",
        "tern",
        "groft",
    },
    "sv": {
        # Common Swedish words
        "och",
        "i",
        "med",
        "till",
        "på",
        "för",
        "av",
        "som",
        "en",
        "ett",
        "inte",
        "från",
        "om",
        "men",
        "detta",
        "efter",
        "när",
        "kan",
        "eller",
        "vid",
        "blir",
        "varit",
        "ska",
        "denna",
        "dessa",
        "under",
        "över",
        "sig",
        "varje",
        "allt",
        "något",
        # Culinary / Recipe terms
        "skär",
        "tillsätt",
        "stek",
        "steka",
        "vispa",
        "koka",
        "varm",
        "blanda",
        "servera",
        "serveras",
        "minuter",
        "matsked",
        "matskedar",
        "tesked",
        "teskedar",
        "vetemjöl",
        "smör",
        "lök",
        "vitlök",
        "rör",
        "häll",
        "strö",
        "dela",
        "gryta",
        "stekpanna",
        "ugn",
        "cirka",
        "grädde",
        "kyckling",
        "köttfärs",
        "kött",
        "potatis",
        "potatisar",
        "morot",
        "morötter",
        "olja",
        "salt",
        "peppar",
        "socker",
        "vatten",
        "ägg",
        "portioner",
        "ingredienser",
        "tillagning",
        "skala",
        "svalna",
        "gör",
        "smaka",
        "servering",
        "skivor",
        "tärningar",
        "grovt",
    },
    "en": {
        # Common English words
        "the",
        "and",
        "in",
        "with",
        "for",
        "to",
        "of",
        "a",
        "an",
        "not",
        "from",
        "about",
        "but",
        "this",
        "after",
        "when",
        "can",
        "or",
        "by",
        "is",
        "are",
        "be",
        "have",
        "has",
        "all",
        "some",
        "each",
        "into",
        "until",
        "then",
        "over",
        "heat",
        # Culinary / Recipe terms
        "add",
        "mix",
        "cook",
        "bake",
        "fry",
        "stir",
        "chop",
        "dice",
        "slice",
        "whisk",
        "boil",
        "simmer",
        "serve",
        "minutes",
        "tablespoon",
        "tablespoons",
        "teaspoon",
        "teaspoons",
        "cup",
        "cups",
        "flour",
        "butter",
        "onion",
        "onions",
        "garlic",
        "pour",
        "sprinkle",
        "pan",
        "pot",
        "skillet",
        "oven",
        "cream",
        "chicken",
        "beef",
        "potatoes",
        "potato",
        "carrots",
        "carrot",
        "oil",
        "salt",
        "pepper",
        "sugar",
        "water",
        "egg",
        "eggs",
        "servings",
        "ingredients",
        "instructions",
        "method",
        "drain",
        "peel",
        "cool",
        "taste",
        "clove",
        "cloves",
        "pinch",
        "shredded",
    },
    "fr": {
        # Common French words
        "le",
        "la",
        "les",
        "un",
        "une",
        "et",
        "dans",
        "avec",
        "pour",
        "de",
        "du",
        "des",
        "en",
        "par",
        "sur",
        "ce",
        "cette",
        "qui",
        "que",
        "pas",
        "ne",
        "ou",
        "mais",
        # Culinary / Recipe terms
        "ajouter",
        "faire",
        "chauffer",
        "cuire",
        "mélanger",
        "couper",
        "battre",
        "bouillir",
        "mijoter",
        "servir",
        "minutes",
        "cuillère",
        "soupe",
        "café",
        "beurre",
        "oignon",
        "oignons",
        "ail",
        "verser",
        "saupoudrer",
        "poêle",
        "casserole",
        "four",
        "crème",
        "poulet",
        "boeuf",
        "pommes",
        "terre",
        "carottes",
        "huile",
        "sel",
        "poivre",
        "sucre",
        "eau",
        "oeuf",
        "oeufs",
        "portions",
        "ingrédients",
        "instructions",
        "préparation",
        "laisser",
        "porter",
        "pincée",
        "gousses",
        "gousse",
    },
}

# Heavily weighted discriminators for differentiating close language pairs
_DISCRIMINATIVE_WORDS: dict[str, dict[str, int]] = {
    "nb": {
        "hvitløk": 4,
        "hvetemel": 4,
        "tilsett": 3,
        "poteter": 3,
        "gulrot": 3,
        "gulrøtter": 3,
        "fløte": 3,
        "stekepanne": 4,
        "stekeovn": 4,
        "fremgangsmåte": 3,
        "spiseskje": 4,
        "kjøttdeig": 4,
        "av": 2,
        "ikke": 1,
    },
    "da": {
        "hvidløg": 4,
        "hvedemel": 4,
        "tilsæt": 4,
        "kartofler": 4,
        "gulerod": 3,
        "gulerødder": 3,
        "spiseskefuld": 4,
        "oksekød": 4,
        "af": 2,
        "pande": 2,
        "skræl": 2,
    },
    "sv": {
        "och": 3,
        "inte": 2,
        "tillsätt": 4,
        "vitlök": 4,
        "vetemjöl": 4,
        "potatis": 4,
        "morot": 3,
        "morötter": 3,
        "grädde": 3,
        "matsked": 4,
        "tillagning": 3,
        "stekpanna": 4,
        "köttfärs": 4,
        "ägg": 2,
    },
    "en": {
        "the": 4,
        "tablespoon": 4,
        "tablespoons": 4,
        "teaspoon": 4,
        "teaspoons": 4,
        "whisk": 3,
        "simmer": 3,
        "skillet": 4,
        "flour": 3,
        "garlic": 3,
        "onions": 3,
        "instructions": 3,
    },
    "fr": {
        "cuillère": 4,
        "mélanger": 4,
        "chauffer": 4,
        "ingrédients": 4,
        "préparation": 4,
        "beurre": 3,
        "boeuf": 4,
        "poêle": 4,
        "ajouter": 3,
    },
}

_WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)


def _extract_text(recipe_data: Any) -> str:
    parts: list[str] = []
    if isinstance(recipe_data, str):
        return recipe_data

    # Check for title
    title = getattr(recipe_data, "title", None)
    if isinstance(title, str):
        parts.append(title)
    elif isinstance(recipe_data, dict) and "title" in recipe_data:
        parts.append(str(recipe_data["title"]))

    # Check for instructions
    instructions = getattr(recipe_data, "instructions", None)
    if instructions is None and isinstance(recipe_data, dict):
        instructions = recipe_data.get("instructions")
    if isinstance(instructions, list):
        for item in instructions:
            if isinstance(item, str):
                parts.append(item)

    # Check for ingredients
    ingredients = getattr(recipe_data, "ingredients", None)
    if ingredients is None and isinstance(recipe_data, dict):
        ingredients = recipe_data.get("ingredients")
    if isinstance(ingredients, list):
        for item in ingredients:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "name"):
                parts.append(str(item.name))
            elif isinstance(item, dict) and "name" in item:
                parts.append(str(item["name"]))

    # Check for source_ingredients
    source_ingredients = getattr(recipe_data, "source_ingredients", None)
    if source_ingredients is None and isinstance(recipe_data, dict):
        source_ingredients = recipe_data.get("source_ingredients")
    if isinstance(source_ingredients, list):
        for item in source_ingredients:
            if hasattr(item, "text"):
                parts.append(str(item.text))
            elif isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))

    return " ".join(parts)


def detect_recipe_language(recipe_data: Any) -> str:
    """Detect recipe language automatically (e.g., 'nb', 'en', 'da', 'sv', 'fr', or 'und').

    Returns 'und' when text is too short, ambiguous, or no language meets the confidence threshold.
    """
    text = _extract_text(recipe_data).lower()
    words = _WORD_PATTERN.findall(text)

    if not words or len(words) < 3:
        return "und"

    scores: dict[str, int] = {lang: 0 for lang in _LANGUAGE_VOCABULARY}

    # Alphabet and character marker bonuses
    has_ae_oe_norwegian = bool(re.search(r"[æø]", text))
    has_ae_oe_swedish = bool(re.search(r"[äö]", text))
    has_french_accents = bool(re.search(r"[éèêàçùôû]", text))

    if has_ae_oe_norwegian:
        scores["nb"] += 3
        scores["da"] += 3
    if has_ae_oe_swedish:
        scores["sv"] += 4
    if has_french_accents:
        scores["fr"] += 4

    word_set = set(words)
    for word in words:
        for lang, vocab in _LANGUAGE_VOCABULARY.items():
            if word in vocab:
                scores[lang] += 1
        for lang, disc in _DISCRIMINATIVE_WORDS.items():
            if word in disc:
                scores[lang] += disc[word]

    # Specific Norwegian vs Danish differentiators
    if "av" in word_set and "af" not in word_set:
        scores["nb"] += 2
    elif "af" in word_set and "av" not in word_set:
        scores["da"] += 3

    if "hvitløk" in word_set:
        scores["nb"] += 4
    if "hvidløg" in word_set:
        scores["da"] += 4
    if "tilsæt" in word_set:
        scores["da"] += 4
    if "tilsett" in word_set:
        scores["nb"] += 3
    if "kartofler" in word_set:
        scores["da"] += 4
    if "poteter" in word_set:
        scores["nb"] += 3

    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_lang, top_score = sorted_scores[0]
    runner_up_lang, runner_up_score = sorted_scores[1]

    # Minimum score required for certainty
    if top_score < 4:
        return "und"

    # If the top two languages are tied or virtually identical without sufficient margin
    if top_score == runner_up_score:
        return "und"

    # When scores are relatively low, require a clear lead
    if top_score < 8 and (top_score - runner_up_score) < 2:
        return "und"

    return top_lang
