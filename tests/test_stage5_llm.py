"""Tests for the LLM parsing layer (grocery_agent.llm).

These call the real OpenRouter API (OPENROUTER_API_KEY from .env) — no
mocking, since the whole point is verifying the model's behavior.

Item names canonicalize to RESPONSE_LANGUAGE (Chinese, per .env), not
English — see grocery_agent.llm._system_prompt.
"""

from grocery_agent.llm import parse_utterance


def test_add_item_with_full_details():
    result = parse_utterance("I bought apples: 3 units, best before August 31")

    assert result.tool_name == "add_item"
    assert result.arguments["name"] == "苹果"
    assert result.arguments["quantity"] == 3
    assert result.arguments["expiry_date"]


def test_consume_item():
    result = parse_utterance("I ate one apple")

    assert result.tool_name == "consume_item"
    assert result.arguments["name"] == "苹果"
    assert result.arguments["quantity"] == 1


def test_add_to_shopping_list():
    result = parse_utterance("Add eggs to my list")

    assert result.tool_name == "add_to_shopping_list"
    assert result.arguments["name"] == "鸡蛋"


def test_query_stock_specific_item():
    result = parse_utterance("How much milk do I have?")

    assert result.tool_name == "query_stock"
    assert result.arguments.get("name") == "牛奶"


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


def test_query_item_details():
    result = parse_utterance("When did I buy the milk?")

    assert result.tool_name == "query_item_details"
    assert result.arguments.get("name") == "牛奶"


def test_ambiguous_consume_asks_for_clarification():
    result = parse_utterance("consume apple")

    assert result.needs_clarification
    assert result.clarification


def test_item_name_canonicalizes_consistently_across_languages():
    english = parse_utterance("I ate one apple")
    chinese = parse_utterance("我吃了一个苹果")  # "I ate one apple"

    assert english.arguments["name"] == chinese.arguments["name"] == "苹果"
    assert english.arguments["quantity"] == chinese.arguments["quantity"] == 1


def test_multiple_items_in_one_utterance_produce_multiple_calls():
    result = parse_utterance("一块面包，一盒饼干")  # a loaf of bread, a pack of cookies

    assert len(result.calls) == 2
    assert all(c.tool_name == "add_item" for c in result.calls)
    names = {c.arguments["name"] for c in result.calls}
    assert names == {"面包", "饼干"}


def test_duplicate_tool_calls_are_deduplicated():
    # Cheap models occasionally repeat the exact same call multiple times
    # in one response (observed: a 2-item request coming back as 4-6
    # duplicated calls) — run several times since it's non-deterministic,
    # and assert the dedup in parse_utterance always collapses it.
    for _ in range(5):
        result = parse_utterance("一块面包，一盒饼干")
        keys = [(c.tool_name, tuple(sorted(c.arguments.items()))) for c in result.calls]
        assert len(keys) == len(set(keys)), f"duplicate calls slipped through: {result.calls}"
