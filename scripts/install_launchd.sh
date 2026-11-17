#!/bin/bash
# Install (or reinstall) the launchd agent for the current user.
# Substitutes the repo/home paths into the plist template and loads it.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO/launchd/com.alleviate.imessage-attio.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.alleviate.imessage-attio.plist"
LABEL="com.alleviate.imessage-attio"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.imessage-attio"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$PLIST_SRC" > "$PLIST_DST"
chmod +x "$REPO/run.sh"

# Reload cleanly if already installed.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed and loaded $LABEL"
echo "Logs: ~/.imessage-attio/sync.log  (errors: sync.err.log)"
echo "Kick a run now:  launchctl kickstart -k gui/$(id -u)/$LABEL"
