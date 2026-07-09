#!/bin/bash
# launchd entrypoint. This script (and the python it invokes) is what must be
# granted Full Disk Access, since it reads ~/Library/Messages/chat.db.
set -euo pipefail
cd "$(dirname "$0")"
exec ./.venv/bin/python -m src.main --once
