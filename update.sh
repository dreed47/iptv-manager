#!/bin/sh
# Pull latest code from origin and rebuild/restart the container.
# Usage:  ./update.sh            (incremental rebuild after pull)
#         ./update.sh --no-cache (full clean rebuild after pull)
set -e
WD="$(cd "$(dirname "$0")" && pwd)"

echo "Pulling latest code..."
git -C "${WD}" pull origin

echo "Rebuilding and restarting container..."
sh "${WD}/restart_container.sh" "${1:-}"
