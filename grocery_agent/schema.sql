-- Applied idempotently (IF NOT EXISTS) via grocery_agent.db.get_direct_connection.
--
-- No `users` table: identity comes directly from Telegram (message.from.id),
-- since Telegram is the only real front end. `households` membership is
-- whoever is in that Telegram group, which Telegram itself already tracks —
-- no parallel membership table needed.

CREATE TABLE IF NOT EXISTS households (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    telegram_chat_id text,
    -- IANA name (e.g. "America/New_York") — what "6pm" means for this
    -- household's daily reminder digest.
    timezone text NOT NULL DEFAULT 'America/New_York'
);

-- The shared canonical catalog — one row per concept
-- ("苹果"), reused across every household. Just a naming lookup: no
-- per-household preference belongs here (see `items.stale_after_days`
-- below for why).
CREATE TABLE IF NOT EXISTS products (
    id uuid PRIMARY KEY,
    name text NOT NULL UNIQUE
);

-- The real, concrete stock: one row per purchase, per household.
-- `stale_after_days` lives here (not on `products`) because it's a
-- per-household preference — two households sharing the same product
-- row must be able to set their own staleness threshold independently.
CREATE TABLE IF NOT EXISTS items (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity double precision NOT NULL,
    purchase_date date NOT NULL,
    expiry_date date,
    stale_after_days integer NOT NULL DEFAULT 5 CHECK (stale_after_days >= 0)
);

CREATE TABLE IF NOT EXISTS shopping_list_entries (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source text NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_state (
    id uuid PRIMARY KEY,
    item_id uuid NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    reminder_type text NOT NULL,
    last_sent_at timestamptz,
    active boolean NOT NULL DEFAULT true
);

-- user_id is a plain label (the Telegram sender's id/name), not a FK —
-- there's no users table to reference.
CREATE TABLE IF NOT EXISTS action_log (
    id uuid PRIMARY KEY,
    household_id uuid NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    user_id text NOT NULL,
    action text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
