#!/usr/bin/env python3
import json, os, urllib.request, re

env_path = os.path.join(os.path.dirname(__file__), ".env")
notion_token = ""
with open(env_path) as f:
    content = f.read()
    # Match NOTION_TOKEN= followed by any non-whitespace
    m = re.search(r'^NOTION_TOKEN=(\S+)', content, re.MULTILINE)
    if m:
        notion_token = m.group(1)

def notion_post(url, body):
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", "Bearer " + notion_token)
    req.add_header("Notion-Version", "2022-06-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

data = notion_post("https://api.notion.com/v1/search", {
    "filter": {"value": "database", "property": "object"},
    "page_size": 100
})
cat_db_id = None
for db in data.get("results", []):
    title = "".join(t.get("plain_text", "") for t in db.get("title", []))
    if title == "Categories":
        cat_db_id = db["id"]
        break

cat_data = notion_post(
    "https://api.notion.com/v1/databases/" + cat_db_id + "/query",
    {"page_size": 100}
)

no_emoji = []
for cp in cat_data.get("results", []):
    name_prop = cp["properties"].get("Name", {})
    title_parts = name_prop.get("title", [])
    full = "".join(t.get("plain_text", "") for t in title_parts)
    has_emoji = any(ord(c) > 127 for c in full)
    status = "OK" if has_emoji else "NO_EMOJI"
    print(status + " " + full)
    if not has_emoji:
        no_emoji.append(full)

print()
if no_emoji:
    print("Missing emoji: " + str(no_emoji))
else:
    print("All 16 categories have emoji")
