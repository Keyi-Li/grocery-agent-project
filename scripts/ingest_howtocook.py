"""One-time ingestion script: adds recipes from Anduin2017/HowToCook
(github.com/Anduin2017/HowToCook, Unlicense) — real Chinese home-cooking
recipes, to fix the gap that scripts/ingest_recipes.py's corpus
(recipe_nlg_lite) has essentially zero Chinese cuisine.

Unlike ingest_recipes.py, this ADDS to the recipes table rather than
replacing it — run ingest_recipes.py first if starting from scratch.
Safe to re-run on its own (skips nothing special, but re-running just
re-adds the same recipes as new rows; not idempotent by itself — only
run once per corpus reset).

Maps HowToCook's consistent per-dish Markdown template onto the same
schema recipe_nlg_lite uses, so retrieval code treats every recipe the
same regardless of origin:

    # {name}的做法              -> name
    ## 必备原料和工具            -> clean ingredient list (embedded, like `ner`)
    ## 计算                     -> ingredients (display, quantified)
    ## 操作                     -> steps (display)

Run once:

    python scripts/ingest_howtocook.py
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

from dotenv import load_dotenv

from grocery_agent.dataclass import Recipe
from grocery_agent.db import get_connection
from grocery_agent.embeddings import embed_batch
from grocery_agent.repositories import RecipeRepository

load_dotenv()

_REPO_URL = "https://github.com/Anduin2017/HowToCook.git"
BATCH_SIZE = 64
# Same convention as ingest_recipes.py's EMBED_MAX_INGREDIENTS — keeps
# both sources embedded the same way so they sit comparably in the
# same vector space.
EMBED_MAX_INGREDIENTS = 3

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _clone_repo(dest_dir: str) -> str:
    subprocess.run(
        ["git", "clone", "--depth", "1", _REPO_URL, dest_dir],
        check=True,
        capture_output=True,
    )
    return os.path.join(dest_dir, "dishes")


def _find_dish_files(dishes_dir: str) -> list[str]:
    paths = []
    for root, _dirs, files in os.walk(dishes_dir):
        if os.path.normpath(root).startswith(os.path.normpath(os.path.join(dishes_dir, "template"))):
            continue  # the contributor template, not a real recipe
        for f in files:
            if f.endswith(".md"):
                paths.append(os.path.join(root, f))
    return paths


def _extract_section(text: str, heading: str) -> str:
    """Returns the raw text between `## {heading}` and the next `##`
    heading (or end of file), empty string if not found."""
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1).strip() == heading:
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            return text[start:end].strip()
    return ""


def _bullet_items(section_text: str) -> list[str]:
    """Extracts only bullet-list lines from a section — some sections
    (e.g. 计算) have explanatory prose before the actual list, which
    this skips rather than treating as an ingredient."""
    items = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped[:1] in ("-", "*", "+"):
            item = stripped[1:].strip()
            if item:
                items.append(item)
    return items


def _parse_dish(path: str) -> Recipe | None:
    with open(path, encoding="utf-8") as f:
        text = f.read()

    title_match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    if not title_match:
        return None
    name = title_match.group(1).strip()
    if name.endswith("的做法"):
        name = name[: -len("的做法")].strip()
    if not name:
        return None

    clean_ingredients = _bullet_items(_extract_section(text, "必备原料和工具"))
    steps = _extract_section(text, "操作")
    ingredients_display = ", ".join(_bullet_items(_extract_section(text, "计算")))
    if not ingredients_display:
        ingredients_display = ", ".join(clean_ingredients)  # fallback if 计算 is missing/differently named

    if not (clean_ingredients and steps and ingredients_display):
        return None  # not enough to be useful — skip rather than guess

    embed_text = f"{name}. Ingredients: {', '.join(clean_ingredients[:EMBED_MAX_INGREDIENTS])}"
    return Recipe(name=name, ingredients=ingredients_display, steps=steps), embed_text


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        print("cloning HowToCook...")
        dishes_dir = _clone_repo(tmp_dir)
        dish_files = _find_dish_files(dishes_dir)
        print(f"found {len(dish_files)} dish files")

        conn = get_connection()
        repo = RecipeRepository(conn)

        batch_recipes: list[Recipe] = []
        batch_texts: list[str] = []
        total = 0
        skipped = 0

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

        for path in dish_files:
            parsed = _parse_dish(path)
            if parsed is None:
                skipped += 1
                continue
            recipe, embed_text = parsed
            batch_recipes.append(recipe)
            batch_texts.append(embed_text)
            if len(batch_recipes) >= BATCH_SIZE:
                flush()

        flush()
        conn.close()
        print(f"done — ingested {total} recipes, skipped {skipped} unparseable files")


if __name__ == "__main__":
    main()
