# Siri Shortcut setup (Stage 7)

This is a manual, on-device step — Shortcuts can't be built from the
command line. Do this once the backend is deployed to Fly.io (you have
a URL like `https://grocery-agent-koi.fly.dev`).

1. Open the **Shortcuts** app on your iPhone → tap **+** to create a
   new shortcut.
2. Add action **Dictate Text**.
3. Add action **Get Contents of URL**:
   - URL: `https://<your-app>.fly.dev/utterance`
   - Method: `POST`
   - Headers:
     - `Authorization`: `Bearer <SIRI_SHORTCUT_TOKEN from your .env>`
     - `Content-Type`: `application/json`
   - Request Body: JSON, one field:
     - `text` → set to the **Dictated Text** variable from step 2
4. Add action **Get Dictionary Value** (Apple's actual name for this —
   not "Get Value for Key"). It reads JSON straight from "Contents of
   URL" without a separate parsing step:
   - Set it to **Get Value for Key**
   - Key: `response`
   - Dictionary: the **Contents of URL** variable from step 3
5. Add action **Speak Text** — input = the value from step 4.
6. Tap the shortcut's name at the top, rename it (e.g. "Grocery"),
   enable **Add to Siri**, and record a phrase.

Say the phrase to Siri, dictate something like "I bought 3 apples,"
and it should speak back the confirmation.

## Notes

- `SIRI_SHORTCUT_TOKEN` is a fixed secret (see `.env`) rather than a
  real Supabase login session, because a plain Shortcut can't run a
  token-refresh flow on its own — see `grocery_agent/api.py`,
  `verify_supabase_token`.
- If Siri mishears or the request fails, step 3 returns an error
  response; add a **Get Dictionary Value** with key `detail` in an
  "If" branch if you want spoken error messages too — not required
  for basic use.
