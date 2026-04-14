#!/bin/sh
# Rebuild the image and restart via docker compose.
# Usage:  ./restart_container.sh          (incremental rebuild)
#         ./restart_container.sh --no-cache (full clean rebuild)
set -e
WD="$(cd "$(dirname "$0")" && pwd)"
NO_CACHE=${1:-}

# Read CONTAINER_NAME from .env (falls back to the docker-compose default)
if [ -f "${WD}/.env" ]; then
    # shellcheck disable=SC1091
    . "${WD}/.env"
fi
CONTAINER_NAME=${CONTAINER_NAME:-iptv-app}

# Stop and remove any stray container with the same name that compose can't own
docker stop "${CONTAINER_NAME}" 2>/dev/null || true
docker compose -f "${WD}/docker-compose.yml" rm -f 2>/dev/null || true

# shellcheck disable=SC2086
docker compose -f "${WD}/docker-compose.yml" build ${NO_CACHE}
docker compose -f "${WD}/docker-compose.yml" up -d --force-recreate

echo "Done — container restarted via docker compose"
