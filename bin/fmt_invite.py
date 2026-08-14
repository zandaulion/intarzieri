"""Render a freshly created invite for the terminal. Reads JSON on stdin."""
import json
import sys

d = json.load(sys.stdin)
link = d["url"] or "(set PUBLIC_BASE_URL to render a link)"
days = d["expires_in_days"]

print()
print(f"  link:  {link}")
print(f"  code:  {d['code']}")
print(f"  registers one device, expires in {days} days")
print()
print("  Sharing the link is safe: opening it does not spend the invite,")
print("  so chat-app link previews cannot burn it. Tapping it inside")
print("  WhatsApp is fine too -- it stays re-usable for an hour, so the")
print("  properly installed app can claim it afterwards.")
