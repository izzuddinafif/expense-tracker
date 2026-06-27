#!/usr/bin/env python3
"""Quick Notion structure check -- run standalone."""
import json, os, urllib.request

env_path = os.path.join(os.path.dirname(__file__), ".env")
notion_token = ""
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if line.startswith("NOTION_TOKEN="):
            notion_token = line.split("=", 1)[1].strip()
            break

if not notion_token:
    print("ERROR: No NOTION_TOKEN in .env")
    exit(1)

def notion_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + notion_token)
    req.add_header("Notion-Version", "2022-06-28")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def notion_post(url, body):
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + notion_token)
    req.add_header("Notion-Version", "2022-06-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

# Step 1: List all databases
print("=" * 60)
print("NOTION STRUCTURE CHECK")
print("=" * 60)

data = notion_post("https://api.notion.com/v1/search", {
    "filter": {"value": "database", "property": "object"},
    "page_size": 100
})

databases = {}
for db in data.get("results", []):
    title_parts = db.get("title", [])
    title = "".join(t.get("plain_text", "") for t in title_parts).strip()
    if title:
        databases[title] = db["id"]

expected = {
    "Expenses": "expenses_ds",
    "Sub-categories": "subcategories_ds",
    "Accounts": "accounts_ds",
    "Month": "months_ds",
    "Year": "years_ds",
    "Recurring Payment": "recurring_ds",
    "Assets": "assets_ds",
    "Income": "income_ds",
    "Budget": "budget_ds",
    "Categories": "categories_ds",
}

print("\n--- Database Existence ---")
all_ok = True
for title, field in expected.items():
    db_id = databases.get(title)
    if db_id:
        print("  OK " + title + " (" + field + ")")
    else:
        print("  MISSING " + title + " (" + field + ")")
        all_ok = False

print("\nDiscovered titles: " + str(list(databases.keys())))

# Step 2: Check Expenses DB properties
if "Expenses" in databases:
    exp_data = notion_get("https://api.notion.com/v1/databases/" + databases["Expenses"])
    props = exp_data.get("properties", {})
    print("\n--- Expenses DB Properties (" + str(len(props)) + ") ---")
    for pname, pval in sorted(props.items()):
        print("  " + pname + " (" + pval.get("type", "?") + ")")

    merchant_ok = "Merchant" in props and props["Merchant"].get("type") == "rich_text"
    print("\nMerchant field (rich_text): " + ("OK" if merchant_ok else "MISSING"))

# Step 3: Categories count
if "Categories" in databases:
    cat_data = notion_post(
        "https://api.notion.com/v1/databases/" + databases["Categories"] + "/query",
        {"page_size": 100}
    )
    cat_count = len(cat_data.get("results", []))
    cat_status = "OK" if cat_count == 16 else "MISMATCH"
    print("\n--- Categories: " + str(cat_count) + " (expected 16) " + cat_status + " ---")
    for cp in cat_data.get("results", []):
        t = "".join(x.get("plain_text", "") for x in cp["properties"].get("title", {}).get("title", []))
        has_emoji = any(ord(c) > 127 for c in t)
        print("  " + ("OK" if has_emoji else "NO_EMOJI") + " " + t)

# Step 4: Sub-categories count + orphaned check
if "Sub-categories" in databases:
    all_subcats = []
    start_cursor = None
    while True:
        body = {"page_size": 100}
        if start_cursor:
            body["start_cursor"] = start_cursor
        sub_data = notion_post(
            "https://api.notion.com/v1/databases/" + databases["Sub-categories"] + "/query",
            body
        )
        all_subcats.extend(sub_data.get("results", []))
        if not sub_data.get("has_more"):
            break
        start_cursor = sub_data.get("next_cursor")

    sub_status = "OK" if len(all_subcats) == 96 else "MISMATCH"
    print("\n--- Sub-categories: " + str(len(all_subcats)) + " (expected 96) " + sub_status + " ---")

    orphaned = []
    for sc in all_subcats:
        props = sc.get("properties", {})
        cat_rel = props.get("Categories", {}).get("relation", [])
        if not cat_rel:
            t = "".join(x.get("plain_text", "") for x in props.get("title", {}).get("title", []))
            orphaned.append(t)

    if orphaned:
        print("Orphaned subcategories (" + str(len(orphaned)) + "):")
        for o in orphaned:
            print("  - " + o)
    else:
        print("No orphaned subcategories")

print("\n" + "=" * 60)
print("CHECK COMPLETE")
