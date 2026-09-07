"""Tests for Stage 8 sandboxed code-execution fallback.

The real Modal sandbox mechanics are tested directly (test_run_in_sandbox_*)
since that's cheap and fast; the full network round-trip from inside a
Modal container back to a locally-running test server isn't practical
to test automatically, so sandbox.run_in_sandbox is monkeypatched for
the dispatch-integration tests — the /internal/sandbox/* endpoints it
would call are tested directly via TestClient instead, which is the
part that actually touches the DB.
"""

import pytest
from fastapi.testclient import TestClient

import grocery_agent.api as api_module
import grocery_agent.sandbox as sandbox_module
from grocery_agent.dataclass import Household, User
from grocery_agent.db import get_connection
from grocery_agent.llm import ParsedCommand, ToolCall
from grocery_agent.repositories import (
    ActionLogRepository,
    HouseholdRepository,
    UserRepository,
)
from grocery_agent.sandbox import (
    generate_code,
    issue_sandbox_token,
    resolve_sandbox_token,
    revoke_sandbox_token,
    run_in_sandbox,
    validate_generated_code,
)


# --- validate_generated_code ------------------------------------------


def test_validate_accepts_clean_code():
    validate_generated_code('def run(ctx):\n    return "ok"')


@pytest.mark.parametrize(
    "code",
    [
        'x = 1\nreturn "no run function"',
        'def run(ctx):\n    import os\n    return "bad"',
        'def run(ctx):\n    open("/etc/passwd")\n    return "bad"',
        'def run(ctx):\n    exec("1+1")\n    return "bad"',
        'def run(ctx):\n    __import__("os")\n    return "bad"',
    ],
)
def test_validate_rejects_bad_code(code):
    with pytest.raises(ValueError):
        validate_generated_code(code)


# --- sandbox tokens ------------------------------------------------------


def test_sandbox_token_round_trip():
    token = issue_sandbox_token("household-1", "user-1")
    scope = resolve_sandbox_token(token)
    assert scope.household_id == "household-1"
    assert scope.user_id == "user-1"

    revoke_sandbox_token(token)
    assert resolve_sandbox_token(token) is None


def test_resolve_unknown_token_returns_none():
    assert resolve_sandbox_token("not-a-real-token") is None


# --- run_in_sandbox (real Modal, no network calls) ------------------------


def test_run_in_sandbox_executes_real_modal_container():
    result = run_in_sandbox(
        'def run(ctx):\n    return "hello from sandbox"',
        base_url="http://unused.invalid",
        token="unused",
        timeout=60,
    )
    assert result == "hello from sandbox"


# --- generate_code (real LLM) ----------------------------------------------


def test_generate_code_produces_valid_run_function():
    code = generate_code("清空我所有的库存")  # "clear all my stock"
    validate_generated_code(code)  # raises if invalid
    assert "ctx." in code


# --- internal sandbox endpoints (real DB) ----------------------------------


@pytest.fixture
def household_and_user(db_conn):
    user = User(email="sandbox-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(name="Sandbox Test Household", member_ids=[user.id])
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
def sandbox_client(household_and_user):
    household, user = household_and_user
    token = issue_sandbox_token(household.id, user.id)
    try:
        with TestClient(api_module.app) as client:
            client.headers.update({"Authorization": f"Bearer {token}"})
            yield client
    finally:
        revoke_sandbox_token(token)


def test_sandbox_endpoints_require_valid_token():
    with TestClient(api_module.app) as client:
        response = client.post("/internal/sandbox/get_stock", json={})
    assert response.status_code == 401


def test_sandbox_add_and_get_stock(sandbox_client, household_and_user):
    household, _user = household_and_user

    add_response = sandbox_client.post(
        "/internal/sandbox/add_item", json={"name": "sandbox-apple", "quantity": 3, "unit": "unit"}
    )
    assert add_response.json() == {"ok": True}

    stock_response = sandbox_client.post("/internal/sandbox/get_stock", json={})
    assert {"name": "sandbox-apple", "quantity": 3, "unit": "unit"} in stock_response.json()["items"]


def test_sandbox_consume_item(sandbox_client):
    sandbox_client.post(
        "/internal/sandbox/add_item", json={"name": "sandbox-egg", "quantity": 5, "unit": "unit"}
    )
    consume_response = sandbox_client.post(
        "/internal/sandbox/consume_item", json={"name": "sandbox-egg", "quantity": 2}
    )
    assert consume_response.json() == {"ok": True}

    stock = sandbox_client.post(
        "/internal/sandbox/get_stock", json={"name": "sandbox-egg"}
    ).json()["items"]
    assert stock == [{"name": "sandbox-egg", "quantity": 3, "unit": "unit"}]


def test_sandbox_add_to_shopping_list_and_get_it(sandbox_client):
    sandbox_client.post("/internal/sandbox/add_to_shopping_list", json={"name": "sandbox-bread"})
    items = sandbox_client.post("/internal/sandbox/get_shopping_list", json={}).json()["items"]
    assert items == ["sandbox-bread"]


def test_sandbox_get_expiring_soon_empty(sandbox_client):
    items = sandbox_client.post("/internal/sandbox/get_expiring_soon", json={}).json()["items"]
    assert items == []


# --- _run_custom_action / full dispatch integration ------------------------


def test_run_custom_action_logs_and_returns_sandbox_result(db_conn, monkeypatch):
    user = User(email="custom-action-test@example.com")
    UserRepository(db_conn).create(user)
    household = Household(name="Custom Action Household", member_ids=[user.id])
    HouseholdRepository(db_conn).create(household)
    db_conn.commit()

    try:
        monkeypatch.setattr(
            sandbox_module, "generate_code", lambda description: "def run(ctx):\n    return 'done'"
        )
        monkeypatch.setattr(
            sandbox_module, "run_in_sandbox", lambda code, base_url, token, timeout=60: "库存已清空"
        )

        result = api_module._run_custom_action(db_conn, household.id, user.id, "清空库存")
        db_conn.commit()  # _run_custom_action doesn't commit itself (api.py's
        # caller does, after all calls in an utterance finish) — without this,
        # the uncommitted ActionLog insert's FK lock on `households` blocks
        # the cleanup below (a different connection) from deleting it.

        assert result == "库存已清空"
        logs = ActionLogRepository(db_conn).get_for_household(household.id)
        assert logs[-1]["action"] == "custom_action"
        assert logs[-1]["details"]["result"] == "库存已清空"
    finally:
        cleanup_conn = get_connection()
        try:
            with cleanup_conn.cursor() as cur:
                cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
            cleanup_conn.commit()
        finally:
            cleanup_conn.close()


def test_custom_action_dispatched_from_utterance(monkeypatch):
    from grocery_agent.dataclass import Household, User
    from grocery_agent.repositories import HouseholdRepository, UserRepository

    conn = get_connection()
    user = User(email="dispatch-test@example.com")
    UserRepository(conn).create(user)
    household = Household(name="Dispatch Test Household", member_ids=[user.id])
    HouseholdRepository(conn).create(household)
    conn.commit()

    try:
        api_module.app.dependency_overrides[api_module.verify_supabase_token] = (
            lambda: api_module.AuthenticatedUser(id=user.id, email=user.email)
        )
        api_module._pending_clarifications.clear()
        monkeypatch.setattr(
            api_module,
            "parse_utterance",
            lambda text: ParsedCommand(
                calls=[ToolCall("custom_action", {"description": "清空库存"})]
            ),
        )
        monkeypatch.setattr(sandbox_module, "generate_code", lambda description: "def run(ctx):\n    return 'x'")
        monkeypatch.setattr(
            sandbox_module, "run_in_sandbox", lambda code, base_url, token, timeout=60: "已完成自定义操作"
        )

        with TestClient(api_module.app) as client:
            response = client.post("/utterance", json={"text": "帮我清空库存"})

        assert response.status_code == 200
        assert response.json()["response"] == "已完成自定义操作"
    finally:
        api_module.app.dependency_overrides.clear()
        api_module._pending_clarifications.clear()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM households WHERE id = %s", (household.id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user.id,))
        conn.commit()
        conn.close()
