"""Tests for Stage 9 receipt-photo parsing (grocery_agent.receipt) and
its Telegram webhook wiring.

parse_receipt itself is tested live (real OpenRouter vision call, no
mocking — same philosophy as Stage 5's text parsing) against a
synthetic fixture receipt. The Telegram webhook tests mock the
network-heavy boundary (downloading the photo + the vision call)
since that's the external cost/latency, not the logic being tested.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient

import grocery_agent.api as api_module
from grocery_agent.dataclass import Household, User
from grocery_agent.db import get_connection
from grocery_agent.llm import ToolCall
from grocery_agent.receipt import parse_receipt
from grocery_agent.repositories import HouseholdRepository, UserRepository
from grocery_agent.tools import query_stock

FIXTURE_RECEIPT = pathlib.Path(__file__).parent / "fixtures" / "test_receipt.jpg"


def test_parse_receipt_extracts_line_items():
    image_bytes = FIXTURE_RECEIPT.read_bytes()

    calls = parse_receipt(image_bytes)

    assert len(calls) == 3
    assert all(c.tool_name == "add_item" for c in calls)
    by_name = {c.arguments["name"]: c.arguments for c in calls}
    assert "牛奶" in by_name
    assert by_name["牛奶"]["quantity"] > 0
    assert "面包" in by_name
    assert "鸡蛋" in by_name
    assert by_name["鸡蛋"]["quantity"] > 0  # "(12ct)" vs "x1" is genuinely ambiguous


@pytest.fixture
def household_and_user_with_telegram(db_conn):
    user = User(email="receipt-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(
        name="Receipt Test Household", member_ids=[user.id], telegram_chat_id="-100777"
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


def test_webhook_photo_message_adds_items_and_replies(
    household_and_user_with_telegram, monkeypatch
):
    household, _user = household_and_user_with_telegram
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")

    monkeypatch.setattr(api_module, "download_telegram_file", lambda file_id: b"fake-bytes")
    monkeypatch.setattr(
        api_module.receipt,
        "parse_receipt",
        lambda image_bytes: [
            ToolCall("add_item", {"name": "牛奶", "quantity": 2, "unit": "l"}),
            ToolCall("add_item", {"name": "面包", "quantity": 1, "unit": "unit"}),
        ],
    )
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )

    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={
                "message": {
                    "chat": {"id": -100777},
                    "photo": [{"file_id": "small"}, {"file_id": "large"}],
                }
            },
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(sent) == 1
    assert "牛奶" in sent[0][1]
    assert "面包" in sent[0][1]

    conn = get_connection()
    try:
        stock = {item["name"]: item["quantity"] for item in query_stock(conn, household.id)}
        assert stock.get("牛奶") == 2
        assert stock.get("面包") == 1
    finally:
        conn.close()


def test_webhook_photo_with_no_items_found_replies_gracefully(
    household_and_user_with_telegram, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setattr(api_module, "download_telegram_file", lambda file_id: b"fake-bytes")
    monkeypatch.setattr(api_module.receipt, "parse_receipt", lambda image_bytes: [])
    sent = []
    monkeypatch.setattr(
        api_module, "send_telegram_message", lambda chat_id, text: sent.append((chat_id, text))
    )

    with TestClient(api_module.app) as client:
        response = client.post(
            "/telegram-webhook",
            json={"message": {"chat": {"id": -100777}, "photo": [{"file_id": "x"}]}},
            headers={"x-telegram-bot-api-secret-token": "test-webhook-secret"},
        )

    assert response.status_code == 200
    assert sent == [("-100777", "没有从图片中识别出任何商品。")]
