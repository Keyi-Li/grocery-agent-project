#!/bin/bash
# Run this from your Mac whenever the Gemini proxy might be down (e.g.
# you haven't used it in a while, or after a server reboot) instead of
# doing the dependency.md recovery steps by hand.
#
# 1. SSHes into vonneumann.med.yale.edu and makes sure cli-proxy-api +
#    cloudflared are both running (starts whichever is missing).
# 2. Writes the current public tunnel URL into local .env.
# 3. Pushes that same URL to the Fly.io app's LLM_BASE_URL secret
#    (a secrets update alone restarts the app — no `fly deploy` needed
#    unless the code itself changed).
set -euo pipefail

SERVER="kl825@vonneumann.med.yale.edu"
FLY_APP="grocery-agent-koi"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
REMOTE_SCRIPT="$SCRIPT_DIR/remote_ensure_proxy.sh"

echo "Checking proxy on $SERVER..."
URL=$(ssh "$SERVER" 'bash -s' < "$REMOTE_SCRIPT")
echo "Proxy reachable at: $URL"

python3 - "$ENV_FILE" "$URL" <<'EOF'
import re, sys
path, url = sys.argv[1], sys.argv[2]
with open(path) as f:
    text = f.read()
text = re.sub(r'^LLM_BASE_URL=.*$', f'LLM_BASE_URL={url}/v1', text, count=1, flags=re.M)
with open(path, "w") as f:
    f.write(text)
EOF
echo "Updated $ENV_FILE"

fly secrets set -a "$FLY_APP" LLM_BASE_URL="$URL/v1"
echo "Updated Fly secret LLM_BASE_URL on $FLY_APP"
