#!/usr/bin/env bash
# Deploy the train tracker: publish the PWA, optionally rebuild the API image.
#   ./deploy.sh          -> web only
#   ./deploy.sh --api    -> also rebuild the container image and restart the unit
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${DEST:-/var/www/trains}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

VERSION="$(find "$ROOT/web" -type f -exec sha256sum {} + | sort -k2 | sha256sum | cut -c1-12)"

sudo mkdir -p "$DEST"
sudo chown "$(id -un):$(id -gn)" "$DEST"
rsync -a --delete "$ROOT/web"/ "$DEST"/
# Stamp every reference, not just the service worker: index.html cache-busts
# app.css and app.js with the same hash.
grep -rl __BUILD_VERSION__ "$DEST" | xargs -r sed -i "s/__BUILD_VERSION__/${VERSION}/g"
sudo restorecon -R "$DEST" 2>/dev/null || true
echo "web deployed ${VERSION} -> ${DEST}"

if [[ "${1:-}" == "--api" ]]; then
  podman build -t localhost/train-api:latest -f "$ROOT/backend/Containerfile" "$ROOT/backend"
  cp "$ROOT/quadlet/train-api.container" "$HOME/.config/containers/systemd/"
  systemctl --user daemon-reload
  systemctl --user restart train-api.service
  echo "api rebuilt and restarted"
fi
