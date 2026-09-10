#!/bin/bash
# Run this from your Mac whenever anything might be down — the LLM
# proxy, the app itself, or either of their tunnels (e.g. after a
# server reboot, or you haven't used the bot in a while). Replaces
# doing any of this by hand.
#
# 1. SSHes into vonneumann.med.yale.edu and makes sure cli-proxy-api,
#    its tunnel, the app, and the app's own tunnel are all running
#    (starts whichever is missing) — see remote_ensure_proxy.sh and
#    remote_ensure_app.sh, both idempotent.
# 2. Writes the current proxy tunnel URL into local .env (used when
#    running the app locally on this Mac for dev/testing — the
#    deployed app itself talks to the proxy over localhost instead,
#    so this step doesn't affect it).
# 3. If the app's own tunnel URL changed since last time: updates it in
#    the server's .env (APP_BASE_URL), restarts the app's tmux session
#    to pick that up, and re-registers it as the Telegram webhook URL
#    (quick tunnels get a new random hostname every restart, so Telegram
#    would otherwise keep POSTing to a dead URL).
#
# Does NOT touch the database in any way — this only starts/restarts
# local processes and rewrites config files/webhook registration.
set -euo pipefail

SERVER="kl825@vonneumann.med.yale.edu"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

echo "Checking LLM proxy on $SERVER..."
PROXY_URL=$(ssh "$SERVER" 'bash -s' < "$SCRIPT_DIR/remote_ensure_proxy.sh")
echo "Proxy reachable at: $PROXY_URL"

python3 - "$ENV_FILE" "$PROXY_URL" <<'EOF'
import re, sys
path, url = sys.argv[1], sys.argv[2]
with open(path) as f:
    text = f.read()
text = re.sub(r'^LLM_BASE_URL=.*$', f'LLM_BASE_URL={url}/v1', text, count=1, flags=re.M)
with open(path, "w") as f:
    f.write(text)
EOF
echo "Updated local .env's LLM_BASE_URL (for local dev use only)"

echo "Checking app on $SERVER..."
APP_URL=$(ssh "$SERVER" 'bash -s' < "$SCRIPT_DIR/remote_ensure_app.sh")
echo "App reachable at: $APP_URL"

CURRENT_APP_BASE_URL=$(ssh "$SERVER" "grep '^APP_BASE_URL=' ~/grocery-agent-project/.env | cut -d= -f2-")

if [ "$CURRENT_APP_BASE_URL" != "$APP_URL" ]; then
    echo "App tunnel URL changed ($CURRENT_APP_BASE_URL -> $APP_URL) — updating and restarting..."
    ssh "$SERVER" "
        sed -i 's#^APP_BASE_URL=.*#APP_BASE_URL=$APP_URL#' ~/grocery-agent-project/.env
        tmux kill-session -t app 2>/dev/null || true
        tmux new-session -d -s app 'cd ~/grocery-agent-project && source .venv/bin/activate && uvicorn grocery_agent.api:app --host 0.0.0.0 --port 8080'
    "
    echo "Restarted app with new APP_BASE_URL"

    # Retried: Telegram occasionally fails a fresh trycloudflare.com
    # hostname with "Failed to resolve host" even though it's already
    # reachable from everywhere else (seen in practice) — a few retries
    # a few seconds apart clears it without any real DNS problem.
    for attempt in 1 2 3 4 5; do
        if python3 "$SCRIPT_DIR/set_telegram_webhook.py" "$APP_URL"; then
            echo "Re-registered Telegram webhook at $APP_URL"
            break
        fi
        echo "Webhook registration attempt $attempt failed, retrying in 6s..."
        sleep 6
    done
else
    echo "App tunnel URL unchanged — nothing else to do"
fi
