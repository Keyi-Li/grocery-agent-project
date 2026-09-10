"""Embedding layer for the recipe RAG pipeline (see
repositories.RecipeRepository, scripts/ingest_recipes.py,
tools.suggest_recipes).

A local model, not an API call — deliberately decoupled from
LLM_BASE_URL/LLM_MODEL (the chat model). Embeddings and generation are
separate concerns: nothing downstream ever sees a vector, only the real
retrieved text (see tools.suggest_recipes) — so the chat model can be
swapped freely without touching this at all. The one hard requirement
is the opposite direction: every embedding ever compared against
another (the whole recipe corpus, and every query) must come from this
same model, since different models produce geometrically unrelated
vector spaces.

Multilingual, not English-only: a household's canonical item names are
in Household.language, not necessarily English (see dataclass.Household)
— the query text built from a household's stock can be in any language,
and still needs to retrieve correctly against the English-only recipe
corpus.
"""

from __future__ import annotations

from fastembed import TextEmbedding

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=MODEL_NAME)
    return _model


def embed(text: str) -> list[float]:
    """Embeds one piece of text — a query (household stock + preference)
    or a recipe being ingested alike, since this model (unlike the E5
    family) has no asymmetric query/passage convention."""
    return next(iter(_get_model().embed([text]))).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch form of embed(), for ingesting many recipes at once —
    meaningfully faster than embedding one at a time."""
    return [vector.tolist() for vector in _get_model().embed(texts)]
