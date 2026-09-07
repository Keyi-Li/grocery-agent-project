"""Tests for Stage 6 API layer (grocery_agent.api).

The LLM parsing step (Stage 5, already tested against the real Claude
API in test_stage5_llm.py) is monkeypatched here with fixed
ParsedCommand results — this stage's job is the API plumbing (auth,
dispatch, pending-clarification state), not re-verifying the model's
parsing quality. Auth is overridden via FastAPI's dependency_overrides
so tests don't need a live Supabase Auth session. Everything else
(the DB, tool functions, services) is real, same as earlier stages.
"""

import pytest
from fastapi.testclient import TestClient

import grocery_agent.api as api_module
from grocery_agent.dataclass import Household, User
from grocery_agent.db import get_connection
from grocery_agent.llm import ParsedCommand
from grocery_agent.repositories import HouseholdRepository, UserRepository


@pytest.fixture
def household_and_user(db_conn):
    # The API endpoint opens its own connection per request, so this
    # fixture must commit (db_conn's usual rollback-after-test isolation
    # doesn't apply here) — cleaned up explicitly in the finally block.
    # households cascade-delete batches/shopping_list_entries/action_log/
    # household_members; items (the shared catalog) are left in place,
    # same as in real use.
    user = User(email="api-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(name="API Test Household", member_ids=[user.id])
    HouseholdRepository(db_conn).create(household)
    db_conn.commit()
    try:
        yield household, user
    finally:
        cleanup_conn = get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


@pytest.fixture
def household_and_user_with_telegram(db_conn):
    user = User(email="api-telegram-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(
        name="API Telegram Test Household",
        member_ids=[user.id],
        telegram_chat_id="-100888",
    )
    HouseholdRepository(db_conn).create(household)
    db_conn.commit()
    try:
        yield household, user
    finally:
        cleanup_conn = get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


@pytest.fixture
def client(household_and_user):
    _household, user = household_and_user

    api_module.app.dependency_overrides[api_module.verify_supabase_token] = (
        lambda: api_module.AuthenticatedUser(id=user.id, email=user.email)
    )
    api_module._pending_clarifications.clear()
    try:
        yield TestClient(api_module.app)
    finally:
        api_module.app.dependency_overrides.clear()
        api_module._pending_clarifications.clear()


def test_static_siri_token_maps_to_configured_user(monkeypatch):
    monkeypatch.setenv("SIRI_SHORTCUT_TOKEN", "test-secret")
    monkeypatch.setenv("SIRI_DEFAULT_USER_ID", "user-123")

    user = api_module.verify_supabase_token(authorization="Bearer test-secret")

    assert user.id == "user-123"


def test_missing_auth_header_returns_401():
    with TestClient(api_module.app) as anon_client:
        response = anon_client.post("/utterance", json={"text": "hi"})
    assert response.status_code == 401


def test_add_item_end_to_end(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name="add_item",
            arguments={"name": "apple", "quantity": 3, "unit": "unit"},
        ),
    )

    response = client.post("/utterance", json={"text": "I bought 3 apples"})

    assert response.status_code == 200
    assert "3" in response.json()["response"]
    assert "apple" in response.json()["response"]


def test_query_stock_after_add(client, monkeypatch):
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name="add_item",
            arguments={"name": "milk", "quantity": 2, "unit": "unit"},
        ),
    )
    client.post("/utterance", json={"text": "I bought 2 milk"})

    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(tool_name="query_stock", arguments={}),
    )
    response = client.post("/utterance", json={"text": "what do I have"})

    assert "milk" in response.json()["response"]


def test_clarification_flow_resolves_on_next_utterance(
    client, household_and_user, monkeypatch
):
    _household, user = household_and_user

    # Seed stock first so the later consume_item has something to deduct.
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name="add_item",
            arguments={"name": "apple", "quantity": 5, "unit": "unit"},
        ),
    )
    client.post("/utterance", json={"text": "I bought 5 apples"})

    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name=None, clarification="How many apples did you eat?"
        ),
    )
    first = client.post("/utterance", json={"text": "I ate an apple"})
    assert first.json()["response"] == "How many apples did you eat?"
    assert user.id in api_module._pending_clarifications

    seen_text = {}

    def fake_parse(text):
        seen_text["value"] = text
        return ParsedCommand(
            tool_name="consume_item", arguments={"name": "apple", "quantity": 1}
        )

    monkeypatch.setattr(api_module, "parse_utterance", fake_parse)
    second = client.post("/utterance", json={"text": "one"})

    assert second.status_code == 200
    assert "1" in second.json()["response"]
    assert "I ate an apple" in seen_text["value"]
    assert "one" in seen_text["value"]
    assert not api_module._pending_clarifications  # cleared after resolving


def test_response_is_also_sent_to_telegram_when_household_has_a_chat_id(
    household_and_user_with_telegram, monkeypatch
):
    _household, user = household_and_user_with_telegram
    api_module.app.dependency_overrides[api_module.verify_supabase_token] = (
        lambda: api_module.AuthenticatedUser(id=user.id, email=user.email)
    )
    api_module._pending_clarifications.clear()
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name="add_item",
            arguments={"name": "bread", "quantity": 1, "unit": "unit"},
        ),
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


def test_telegram_webhook_processes_message_and_replies(
    household_and_user_with_telegram, monkeypatch
):
    household, _user = household_and_user_with_telegram
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )
    monkeypatch.setattr(
        api_module,
        "parse_utterance",
        lambda text: ParsedCommand(
            tool_name="add_item",
            arguments={"name": "tofu", "quantity": 1, "unit": "unit"},
        ),
    )

    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={"message": {"chat": {"id": -100888}, "text": "I bought tofu"}},
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert sent == [("-100888", "Added 1 tofu.")]


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
