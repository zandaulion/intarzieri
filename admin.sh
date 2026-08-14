#!/usr/bin/env bash
# Invite and device management. Talks to the tailnet-only Caddy surface,
# which is what authorises admin -- there is no password, being on the
# tailnet is the credential. Will not work from off the tailnet.
set -euo pipefail

API="${API:-http://<tailnet-address>}"
j() { python3 -m json.tool 2>/dev/null || cat; }

usage() {
  cat <<'USAGE'
usage: ./admin.sh <command>

  invite [label]        create an invite (prints the link and the code)
  invites               list invites
  unvite <id>           revoke an unused invite
  devices               list registered devices
  revoke <id>           lock a device out
  unrevoke <id>         let it back in
  name <id> <label>     label a device

Each invite registers exactly one device. A second phone needs a second
invite. Recipients on iPhone should add the page to the Home Screen first,
then enter the code inside the installed app.
USAGE
}

case "${1:-}" in
  invite)
    label="${2:-}"
    out=$(curl -fsS --max-time 15 -X POST "$API/api/admin/invites" \
          -H 'Content-Type: application/json' -d "{\"label\":\"${label}\"}")
    python3 - "$out" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print()
print("  link:", d["url"] or "(set PUBLIC_BASE_URL to render a link)")
print("  code:", d["code"])
print(f"  expires in {d['expires_in_days']} days, single use")
print()
print("  Share the link. Opening it does NOT spend the invite -- the")
print("  recipient has to tap Activate, so chat-app link previews are safe.")
PY
    ;;
  invites)  curl -fsS --max-time 15 "$API/api/admin/invites" | j ;;
  unvite)   curl -fsS --max-time 15 -X POST "$API/api/admin/invites/${2:?id}/revoke" | j ;;
  devices)  curl -fsS --max-time 15 "$API/api/admin/devices" | j ;;
  revoke)   curl -fsS --max-time 15 -X POST "$API/api/admin/devices/${2:?id}/revoke" \
              -H 'Content-Type: application/json' -d '{"revoked":true}' | j ;;
  unrevoke) curl -fsS --max-time 15 -X POST "$API/api/admin/devices/${2:?id}/revoke" \
              -H 'Content-Type: application/json' -d '{"revoked":false}' | j ;;
  name)     curl -fsS --max-time 15 -X POST "$API/api/admin/devices/${2:?id}/label" \
              -H 'Content-Type: application/json' -d "{\"label\":\"${3:?label}\"}" | j ;;
  *) usage; exit 1 ;;
esac
