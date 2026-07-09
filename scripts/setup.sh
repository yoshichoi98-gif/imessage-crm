#!/bin/bash
# Turnkey installer for a new Mac (e.g. the CEO's laptop).
# Run from the repo root:  bash scripts/setup.sh
# It sets up the venv, writes the secrets file, creates the Attio object/list
# (idempotent), and installs the background agent. The only manual step it can't
# do for you is granting Full Disk Access (Apple blocks scripting that) — it
# prints the exact instructions at the end.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
ENV_DIR="$HOME/.imessage-attio"
ENV_FILE="$ENV_DIR/env"
PY="$REPO/.venv/bin/python3"

echo "==> 1/5  Creating Python environment + installing dependencies"
python3 -m venv .venv
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt

echo "==> 2/5  Configuration"
mkdir -p "$ENV_DIR"
if [ -f "$ENV_FILE" ]; then
  echo "    $ENV_FILE already exists — leaving it as-is."
else
  read -r -p "    Attio API key: " API_KEY
  read -r -p "    This person's first name (labels their outbound texts, e.g. Saathvik): " SELF
  umask 077
  cat > "$ENV_FILE" <<EOF
ATTIO_API_KEY=$API_KEY
ATTIO_OBJECT_SLUG=text_threads
ATTIO_SYNC_LIST=text_sync
SELF_LABEL=$SELF
EOF
  chmod 600 "$ENV_FILE"
  echo "    wrote $ENV_FILE (locked to your user only)"
fi

echo "==> 3/5  Ensuring the Attio object + attributes exist (idempotent)"
set -a; . "$ENV_FILE"; set +a
"$PY" -m scripts.bootstrap_attio_object | sed 's/^/    /'

echo "==> 4/5  Installing the background agent (runs at login + every 5 min)"
bash scripts/install_launchd.sh | sed 's/^/    /'

echo "==> 5/5  MANUAL STEP — grant Full Disk Access"
REAL_PY="$(/usr/bin/python3 -c 'import os,sys; print(os.path.realpath(sys.executable))' 2>/dev/null || true)"
cat <<EOF

    The sync reads ~/Library/Messages/chat.db, which macOS protects. You must
    grant Full Disk Access ONCE, or every run will fail:

      1. Open  System Settings → Privacy & Security → Full Disk Access
      2. Click the +  (you may need to unlock with Touch ID / password)
      3. Press Cmd+Shift+G and paste this path, then add it:
             $REPO/.venv/bin/python3
      4. Also add (same way):
             $REPO/run.sh
      5. Toggle both ON.

    Then test it:   cd "$REPO" && make sync-now
    Watch it live:  make logs

DONE. Once Full Disk Access is granted, it runs automatically.
EOF
