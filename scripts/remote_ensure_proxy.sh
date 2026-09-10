#!/bin/bash
# Runs ON vonneumann.med.yale.edu — piped in over SSH by
# scripts/sync_llm_proxy.sh, not meant to be run standalone from
# elsewhere. Idempotent: does nothing to a session that's already
# running, only starts what's missing.
#
# Ensures the cli-proxy-api server and its cloudflared tunnel are both
# up (each in its own tmux session), then prints the current public
# tunnel URL as the ONLY line on stdout — callers capture that line.
set -euo pipefail

cd ~

if ! tmux has-session -t cliproxy 2>/dev/null; then
    tmux new-session -d -s cliproxy './cli-proxy-api --config config.yaml'
    sleep 2
fi

if ! tmux has-session -t tunnel 2>/dev/null; then
    rm -f ~/tunnel.log
    tmux new-session -d -s tunnel './cloudflared tunnel --url http://localhost:8317 > ~/tunnel.log 2>&1'
fi

# Read from the log file, not tmux scrollback — cloudflared prints pages
# of health-check diagnostics right after the URL banner, so by the time
# this runs later the banner line can already have scrolled out of
# tmux's captured history. A file has no such limit.
url=""
for _ in $(seq 1 15); do
    url=$(grep -oE 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' ~/tunnel.log 2>/dev/null | head -1 || true)
    [ -n "$url" ] && break
    sleep 1
done

if [ -z "$url" ]; then
    echo "ERROR: could not find a trycloudflare.com URL in the tunnel session" >&2
    exit 1
fi

echo "$url"
