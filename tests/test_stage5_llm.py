"""Tests for Stage 5 LLM parsing layer (grocery_agent.llm).

These call the real Claude API (ANTHROPIC_API_KEY from .env) — no
mocking, since this stage's whole job is the model's behavior.
"""

from grocery_agent.llm import parse_utterance


def test_add_item_with_full_details():
    result = parse_utterance("I bought apples: 3 units, best before August 31")

    assert result.tool_name == "add_item"
    assert result.arguments["name"] == "apple"
    assert result.arguments["quantity"] == 3
    assert result.arguments["expiry_date"]


def test_consume_item():
    result = parse_utterance("I ate one apple")

    assert result.tool_name == "consume_item"
    assert result.arguments["name"] == "apple"
    assert result.arguments["quantity"] == 1


def test_add_to_shopping_list():
    result = parse_utterance("Add eggs to my list")

    assert result.tool_name == "add_to_shopping_list"
    assert result.arguments["name"] == "egg"


def test_query_stock_specific_item():
    result = parse_utterance("How much milk do I have?")

    assert result.tool_name == "query_stock"
    assert result.arguments.get("name") == "milk"


def test_query_stock_everything():
    result = parse_utterance("What do I have?")

    assert result.tool_name == "query_stock"
    assert not result.arguments.get("name")


def test_query_shopping_list():
    result = parse_utterance("What's on my shopping list?")

    assert result.tool_name == "query_shopping_list"


def test_query_expiring_soon():
    result = parse_utterance("What's expiring soon?")

    assert result.tool_name == "query_expiring_soon"


def test_ambiguous_consume_asks_for_clarification():
    result = parse_utterance("consume apple")

    assert result.needs_clarification
    assert result.clarification


def test_multilingual_item_name_normalizes_to_canonical_english():
    result = parse_utterance("我吃了一个苹果")  # "I ate one apple"

    assert result.tool_name == "consume_item"
    assert result.arguments["name"] == "apple"
    assert result.arguments["quantity"] == 1
