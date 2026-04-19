"""Quick integration test against the live server."""
import urllib.request
import json

base = "http://localhost:8000"

# Get org ID
orgs = json.loads(urllib.request.urlopen(f"{base}/organizations").read())
org_id = orgs[0]["id"] if orgs else "test"
org_name = orgs[0]["name"] if orgs else "unknown"
print(f"Org: {org_name} ({org_id})")


def get(path):
    req = urllib.request.Request(
        f"{base}{path}", headers={"X-Organization-Id": org_id}
    )
    return json.loads(urllib.request.urlopen(req).read())


# Test notification endpoints
notifs = get("/notifications")
print(f"Notifications: {len(notifs)} total")

unread = get("/notifications/unread-count")
print(f"Unread count: {unread}")

# Test variants
variants = get("/variants")
print(f"Variants: {len(variants)} active")

# Test dashboard
dash = get("/dashboard/summary")
print(f"Dashboard: {dash['total_deals']} deals, {dash['pending']} pending")

# Test deals list
deals = get("/deals")
print(f"Deals: {len(deals)} returned")

print("\nAll endpoints OK")
