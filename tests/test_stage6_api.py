"""Tests for the API layer (grocery_agent.api).

The LLM parsing step (already tested against the real model in
test_stage5_llm.py) is monkeypatched here with fixed ParsedCommand
results, and generate_reply (also LLM-backed — see test_stage5_llm.py
for its own live tests) is replaced with a deterministic fake that
just dumps the facts as JSON — the focus here is API plumbing (auth,
dispatch, pending-clarification state, fact resolution), not
re-verifying the model's wording. /utterance auth is overridden via
FastAPI's dependency_overrides. Everything else (the DB, tool
functions, services) is real.
"""

import json

import pytest
from fastapi.testclient import TestClient

import grocery_agent.api as api_module
from grocery_agent.dataclass import Household
from grocery_agent.db import get_connection
from grocery_agent.llm import ParsedCommand, ToolCall
from grocery_agent.repositories import HouseholdRepository


def _fake_generate_reply(context, facts):
    # One line per fact, matching what the real prompt asks the model
    # to do — lets tests assert per-item lines deterministically.
    return "\n".join(json.dumps(f, ensure_ascii=False) for f in facts)


@pytest.fixture
def household(db_conn):
    # The API endpoint opens its own connection per request, so this
    # fixture must commit (db_conn's usual rollback-after-test isolation
    # doesn't apply here) — cleaned up explicitly in the finally block.
    household = Household(name="API Test Household")
    HouseholdRepository(db_conn).create(household)
    db_conn.commit()
    try:
        yield household
    finally:
        cleanup_conn = get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


@pytest.fixture
def household_with_telegram(db_conn):
    household = Household(name="API Telegram Test Household", telegram_chat_id="-100888")
    HouseholdRepository(db_conn).create(household)
    db_conn.commit()
    try:
        yield household
    finally:
        cleanup_conn = get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


@pytest.fixture
def client(household, monkeypatch):
    monkeypatch.setattr(api_module, "generate_reply", _fake_generate_reply)
    api_module.app.dependency_overrides[api_module.verify_api_token] = lambda: household.id
    api_module._pending_clarifications.clear()
    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.clear()
        api_module._pending_clarifications.clear()


def test_verify_api_token_returns_configured_household(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-secret")
    monkeypatch.setenv("API_DEFAULT_HOUSEHOLD_ID", "household-123")

    household_id = api_module.verify_api_token(authorization="Bearer test-secret")

    assert household_id == "household-123"


def test_verify_api_token_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-secret")
    monkeypatch.setenv("API_DEFAULT_HOUSEHOLD_ID", "household-123")

    with pytest.raises(Exception):
        api_module.verify_api_token(authorization="Bearer wrong")


def test_missing_auth_header_returns_401():
    with TestClient(api_module.app) as anon_client:
        response = anon_client.post("/utterance", json={"text": "hi"})
    assert response.status_code == 401


def test_add_item_end_to_end(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "apple", "quantity": 3})]),
    )

    response = client.post("/utterance", json={"text": "I bought 3 apples"})

    assert response.status_code == 200
    assert "3" in response.json()["response"]
    assert "apple" in response.json()["response"]


def test_add_item_with_explicit_purchase_date(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            calls=[
                ToolCall(
                    "add_item",
                    {"name": "rice", "quantity": 1, "purchase_date": "2026-01-01"},
                )
            ]
        ),
    )

    response = client.post("/utterance", json={"text": "I bought rice on Jan 1"})

    assert response.status_code == 200
    assert "rice" in response.json()["response"]


def test_multiple_calls_in_one_utterance_are_all_executed(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            calls=[
                ToolCall("add_item", {"name": "bread", "quantity": 1}),
                ToolCall("add_item", {"name": "cookie", "quantity": 1}),
            ]
        ),
    )

    response = client.post("/utterance", json={"text": "一块面包，一盒饼干"})

    assert response.status_code == 200
    lines = response.json()["response"].splitlines()
    assert len(lines) == 2
    assert any("bread" in line for line in lines)
    assert any("cookie" in line for line in lines)

    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("query_stock", {})]),
    )
    stock = client.post("/utterance", json={"text": "what do I have"}).json()["response"]
    assert "bread" in stock
    assert "cookie" in stock


def test_query_stock_after_add(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "milk", "quantity": 2})]),
    )
    client.post("/utterance", json={"text": "I bought 2 milk"})

    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("query_stock", {})]),
    )
    response = client.post("/utterance", json={"text": "what do I have"})

    assert "milk" in response.json()["response"]


def test_clarification_flow_resolves_on_next_utterance(client, household, monkeypatch):
    # Seed stock first so the later consume_item has something to deduct.
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "apple", "quantity": 5})]),
    )
    client.post("/utterance", json={"text": "I bought 5 apples"})

    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[], clarification="How many apples did you eat?"),
    )
    first = client.post("/utterance", json={"text": "I ate an apple"})
    assert first.json()["response"] == "How many apples did you eat?"
    assert "api-test" in api_module._pending_clarifications

    seen_text = {}

    def fake_parse(text):
        seen_text["value"] = text
        return ParsedCommand(calls=[ToolCall("consume_item", {"name": "apple", "quantity": 1})])

    monkeypatch.setattr(api_module, "parse_utterance", fake_parse)
    second = client.post("/utterance", json={"text": "one"})

    assert second.status_code == 200
    assert "1" in second.json()["response"]
    assert seen_text["value"] == [
        {"role": "user", "content": "I ate an apple"},
        {"role": "assistant", "content": "How many apples did you eat?"},
        {"role": "user", "content": "one"},
    ]
    assert not api_module._pending_clarifications  # cleared after resolving


def test_response_is_also_sent_to_telegram_when_household_has_a_chat_id(
    household_with_telegram, monkeypatch
):
    api_module.app.dependency_overrides[api_module.verify_api_token] = (
        lambda: household_with_telegram.id
    )
    api_module._pending_clarifications.clear()
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )
    monkeypatch.setattr(api_module, "generate_reply", _fake_generate_reply)
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "bread", "quantity": 1})]),
    )

    try:
        with TestClient(api_module.app) as client:
            response = client.post("/utterance", json={"text": "I bought bread"})
    finally:
        api_module.app.dependency_overrides.clear()
        api_module._pending_clarifications.clear()

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0][0] == "-100888"
    assert sent[0][1] == response.json()["response"]


def test_telegram_webhook_processes_message_and_replies(household_with_telegram, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )
    monkeypatch.setattr(api_module, "generate_reply", _fake_generate_reply)
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "tofu", "quantity": 1})]),
    )

    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={
                "message": {
                    "chat": {"id": -100888},
                    "text": "I bought tofu",
                    "from": {"id": 42, "first_name": "Koi"},
                }
            },
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(sent) == 1
    assert sent[0][0] == "-100888"
    assert "tofu" in sent[0][1]


def test_telegram_webhook_attributes_action_to_sender(household_with_telegram, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(api_module, "send_telegram_message", lambda chat_id, text: None)
    monkeypatch.setattr(api_module, "generate_reply", _fake_generate_reply)
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(calls=[ToolCall("add_item", {"name": "tofu", "quantity": 1})]),
    )

    with TestClient(api_module.app) as client:
        client.post(
            "/telegram-webhook",
            json={
                "message": {
                    "chat": {"id": -100888},
                    "text": "I bought tofu",
                    "from": {"id": 42, "first_name": "Koi"},
                }
            },
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )

    from grocery_agent.repositories import ActionLogRepository

    conn = get_connection()
    try:
        logs = ActionLogRepository(conn).get_for_household(household_with_telegram.id)
        assert logs[-1]["user_id"] == "Koi (42)"
    finally:
        conn.close()


def test_telegram_webhook_rejects_wrong_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={"message": {"chat": {"id": -100888}, "text": "hi"}},
            headers={"x-telegram-bot-api-secret-token": "wrong"},
        )
    assert response.status_code == 401


def test_telegram_webhook_ignores_unrecognized_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={"message": {"chat": {"id": -1}, "text": "hi"}},
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
