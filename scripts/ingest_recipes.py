"""One-time ingestion script: populates the `recipes` table (the RAG
corpus behind tools.suggest_recipes / grocery_agent.embeddings) from the
recipe_nlg_lite dataset (github.com/m3hrdadfi/recipe-nlg-lite,
huggingface.co/datasets/m3hrdadfi/recipe_nlg_lite) — ~7,198 English
recipes, each with a clean extracted-ingredients field (`ner`) alongside
the raw `ingredients`/`steps` text.

The HuggingFace `datasets` library can't load this one anymore (its
loading-script format was deprecated) — the actual data lives in a
Google Drive-hosted zip that script used to fetch, so this downloads
and parses it directly instead. Needs `gdown`, which is NOT in
requirements.txt (only needed here, never at app runtime):

    pip install gdown

Run once (safe to re-run — replaces the whole table each time, since
this is a shared reference corpus, not user data):

    python scripts/ingest_recipes.py
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile

import gdown
from dotenv import load_dotenv

from grocery_agent.dataclass import Recipe
from grocery_agent.db import get_connection
from grocery_agent.embeddings import embed_batch
from grocery_agent.repositories import RecipeRepository

load_dotenv()

_DATA_URL = "https://drive.google.com/uc?id=1PGH5H_oW7wUvMw_5xaXvbEN7DFll-wDX"
_CSV_NAME = "recipe_nlg_lite/all_data.csv"
BATCH_SIZE = 64


def _download_rows() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "data.zip")
        gdown.download(_DATA_URL, zip_path, quiet=False)
        with zipfile.ZipFile(zip_path) as zf, zf.open(_CSV_NAME) as f:
            reader = csv.DictReader(
                io.TextIOWrapper(f, encoding="utf-8"),
                quotechar='"',
                delimiter="\t",
                quoting=csv.QUOTE_MINIMAL,
            )
            return list(reader)


# Recipes with very few ingredients (e.g. "bagel, onion") embed as a
# vague, generic vector that ends up coincidentally close to a huge
# range of unrelated queries — found in practice, they dominate
# suggest_recipes' vote counts without being genuinely relevant to
# anything. ~4% of the corpus falls below this; worth the loss.
MIN_NER_INGREDIENTS = 4


def _embed_text(name: str, ner: str) -> str:
    # `ner` is the dataset's extracted-ingredient-names field (clean, no
    # quantities/units) — embedded instead of the raw `ingredients` text
    # since the live query text is built from clean canonical item
    # names too (see tools.suggest_recipes); clean-to-clean should
    # retrieve better than clean-to-messy.
    return f"{name}. Ingredients: {ner}"


def main() -> None:
    print("downloading recipe_nlg_lite...")
    rows = _download_rows()
    print(f"downloaded {len(rows)} rows")

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE recipes")
    repo = RecipeRepository(conn)

    batch_recipes: list[Recipe] = []
    batch_texts: list[str] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if not batch_recipes:
            return
        for recipe, embedding in zip(batch_recipes, embed_batch(batch_texts)):
            repo.add(recipe, embedding)
        conn.commit()
        total += len(batch_recipes)
        print(f"ingested {total} recipes so far...")
        batch_recipes.clear()
        batch_texts.clear()

    for row in rows:
        name = row["name"].strip()
        ingredients = row["ingredients"].strip()
        steps = row["steps"].strip()
        ner = row["ner"].strip()
        if not (name and ingredients and steps and ner):
            continue  # every row in this dataset is clean, but don't trust that blindly
        if len([x for x in ner.split(",") if x.strip()]) < MIN_NER_INGREDIENTS:
            continue  # too sparse — see MIN_NER_INGREDIENTS

        batch_recipes.append(Recipe(name=name, ingredients=ingredients, steps=steps))
        batch_texts.append(_embed_text(name, ner))
        if len(batch_recipes) >= BATCH_SIZE:
            flush()

    flush()
    conn.close()
    print(f"done — ingested {total} recipes")


if __name__ == "__main__":
    main()
