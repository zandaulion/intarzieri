"""Render the admin invite list for the terminal. Reads JSON on stdin."""
import json
import sys

data = json.load(sys.stdin)
invites = data.get("invites", [])
if not invites:
    print("  no invites")
    raise SystemExit(0)

for i in invites:
    state = f"used {i['used_at'][:16]}" if i["used_at"] else "pending"
    label = i["label"] or "-"
    flag = " [adoption]" if i.get("adopt_id") else ""
    print(f"  #{i['id']:<3} {label:<18} {state}{flag}")
    # The code is kept only while the invite can still register something;
    # redemption wipes it, so nothing is shown for spent invites.
    if i.get("code"):
        print(f"        code  {i['code']}")
        if i.get("url"):
            print(f"        link  {i['url']}")

print(
    f"\n  invites expire after {data.get('ttl_days')} days; "
    f"re-usable for {data.get('rebind_minutes')} min after first use"
)
