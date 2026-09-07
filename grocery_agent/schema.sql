-- Stage 3 schema. Mirrors docs/grocery-agent-plan.md Section 3.
-- Applied idempotently (IF NOT EXISTS) via grocery_agent.db.get_direct_connection.

CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    email text NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS households (
    id text PRIMARY KEY,
    name text NOT NULL,
    telegram_chat_id text
);

CREATE TABLE IF NOT EXISTS household_members (
    household_id text NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS items (
    id text PRIMARY KEY,
    name text NOT NULL UNIQUE,
    stale_after_days integer NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS batches (
    id text PRIMARY KEY,
    household_id text NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    item_id text NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    quantity double precision NOT NULL,
    unit text NOT NULL,
    purchase_date date NOT NULL,
    expiry_date date
);

CREATE TABLE IF NOT EXISTS shopping_list_entries (
    id text PRIMARY KEY,
    household_id text NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    item_id text NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_state (
    id text PRIMARY KEY,
    batch_id text NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    reminder_type text NOT NULL,
    last_sent_at timestamptz,
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS action_log (
    id text PRIMARY KEY,
    household_id text NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    user_id text NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
