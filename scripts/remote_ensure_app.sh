#!/bin/bash
# Runs ON vonneumann.med.yale.edu — piped in over SSH by
# scripts/sync_deployment.sh, not meant to be run standalone from
# elsewhere. Idempotent: does nothing to a session that's already
# running, only starts what's missing. Mirrors remote_ensure_proxy.sh's
# structure exactly, for the app itself instead of the LLM proxy.
#
# Ensures the app (uvicorn) and its own cloudflared tunnel are both up
# (each in its own tmux session), then prints the current public app
# tunnel URL as the ONLY line on stdout — callers capture that line.
#
# Does NOT touch the database — this only starts/restarts local
# processes on this server; nothing here writes to or resets any data.
set -euo pipefail

cd ~/grocery-agent-project

if ! tmux has-session -t app 2>/dev/null; then
    tmux new-session -d -s app \
        'cd ~/grocery-agent-project && source .venv/bin/activate && uvicorn grocery_agent.api:app --host 0.0.0.0 --port 8080'
    sleep 3
fi

if ! tmux has-session -t app_tunnel 2>/dev/null; then
    rm -f ~/app_tunnel.log
    tmux new-session -d -s app_tunnel 'cd ~ && ./cloudflared tunnel --url http://localhost:8080 > ~/app_tunnel.log 2>&1'
fi

# Read from the log file, not tmux scrollback — cloudflared prints pages
# of health-check diagnostics right after the URL banner, so by the time
# this runs later the banner line can already have scrolled out of
# tmux's captured history. A file has no such limit.
url=""
for _ in $(seq 1 15); do
    url=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' ~/app_tunnel.log 2>/dev/null | head -1 || true)
    [ -n "$url" ] && break
    sleep 1
done

if [ -z "$url" ]; then
    echo "ERROR: could not find a trycloudflare.com URL in the app_tunnel session" >&2
    exit 1
fi

echo "$url"
