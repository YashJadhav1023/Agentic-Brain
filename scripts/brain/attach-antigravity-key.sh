#!/usr/bin/env bash
# Attach a Gemini API key as a second Antigravity capacity pool for the swarm.
#
# The key is read from a silent prompt, so it never appears in shell history, in
# the process list, or in any log. It is written to a 0600 file that only the
# swarm reads. Nothing is printed except a masked confirmation.
#
# Per https://antigravity.google/docs/cli/install#using-a-gemini-api-key an API
# key routes requests directly to the Gemini API, so this pool serves Gemini
# models only and its usage is billed to the key, not to an Antigravity seat.
set -euo pipefail

KEY_FILE="${BRAIN_ANTIGRAVITY_API_KEY_FILE:-$HOME/.config/brain/antigravity-api.key}"
DATA_DIR_NAME="${BRAIN_ANTIGRAVITY_API_DATA_DIR:-antigravity-api}"
GEMINI_ROOT="${BRAIN_GEMINI_DATA_ROOT:-$HOME/.gemini}"
ACCOUNT_DIR="$GEMINI_ROOT/$DATA_DIR_NAME"

printf 'Create the key at https://aistudio.google.com/app/apikey while signed in\n'
printf 'as the SECOND Google account you want the swarm to use.\n\n'
printf 'Paste the Gemini API key (input hidden), then press Enter: '
read -rs API_KEY
printf '\n'

if [[ -z "${API_KEY// }" ]]; then
  printf 'error: no key entered; nothing was written.\n' >&2
  exit 1
fi

mkdir -p "$(dirname "$KEY_FILE")"
chmod 700 "$(dirname "$KEY_FILE")"
# umask so the file is never briefly world-readable between create and chmod.
( umask 077; printf '%s\n' "$API_KEY" > "$KEY_FILE" )
chmod 600 "$KEY_FILE"

# The account's own settings directory. The signed-in account's directory is a
# different one and is never touched.
mkdir -p "$ACCOUNT_DIR"
if [[ -f "$ACCOUNT_DIR/settings.json" ]]; then
  python3 - "$ACCOUNT_DIR/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path))
except Exception:
    data = {}
data["modelProvider"] = "gemini"
json.dump(data, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY
else
  printf '{\n  "modelProvider": "gemini"\n}\n' > "$ACCOUNT_DIR/settings.json"
fi

masked="${API_KEY:0:4}…${API_KEY: -4}"
unset API_KEY
printf '\nAttached.\n'
printf '  key file      : %s (0600, key %s)\n' "$KEY_FILE" "$masked"
printf '  account dir   : %s\n' "$ACCOUNT_DIR"
printf '  model provider: gemini (Gemini models only)\n\n'
printf 'Your signed-in account in %s/%s was not modified.\n' \
  "$GEMINI_ROOT" "${BRAIN_ANTIGRAVITY_OAUTH_DATA_DIR:-antigravity-cli}"
printf 'Verify with: scripts/brain/brain swarm status\n'
