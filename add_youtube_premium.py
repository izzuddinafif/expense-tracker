#!/usr/bin/env python3
"""
Add YouTube Premium as a recurring payment to Notion.
Run this inside the Docker container with: docker compose exec bot python3 add_youtube_premium.py
"""
import asyncio
import os
import sys

# Add the app directory to path
sys.path.insert(0, '/app')

from dotenv import load_dotenv
load_dotenv('/app/.env')

import httpx

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
if not NOTION_TOKEN:
    print("ERROR: NOTION_TOKEN not found in .env")
    sys.exit(1)

# We need to discover the Recurring Payment database ID first
headers = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Search for Recurring Payment database
        print("Searching for 'Recurring Payment' database...")
        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json={"filter": {"value": "database", "property": "object"}, "page_size": 100}
        )
        resp.raise_for_status()
        data = resp.json()
        
        recurring_db_id = None
        for db in data.get("results", []):
            title_parts = db.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if title == "Recurring Payment":
                recurring_db_id = db["id"]
                print(f"Found 'Recurring Payment' database: {recurring_db_id}")
                break
        
        if not recurring_db_id:
            print("ERROR: 'Recurring Payment' database not found. Make sure it's shared with your Notion integration.")
            sys.exit(1)
        
        # Fetch existing entries to check if YouTube Premium already exists
        print("\nChecking existing recurring payments...")
        resp = await client.post(
            f"https://api.notion.com/v1/databases/{recurring_db_id}/query",
            headers=headers,
            json={}
        )
        resp.raise_for_status()
        existing = resp.json()
        
        for page in existing.get("results", []):
            props = page.get("properties", {})
            name_prop = props.get("Name", {})
            title_parts = name_prop.get("title", [])
            name = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if "youtube" in name.lower() or "premium" in name.lower():
                print(f"WARNING: Found existing entry: '{name}' (page_id: {page['id']})")
                print("Skipping to avoid duplicate.")
                return
        
        # Get subcategory and account IDs for "Streaming" and "Jago"
        # First, we need to fetch these from their respective databases
        print("\nFetching subcategory 'Streaming'...")
        
        # Search for Sub-categories database
        subcategories_db_id = None
        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json={"filter": {"value": "database", "property": "object"}, "page_size": 100}
        )
        resp.raise_for_status()
        data = resp.json()
        
        for db in data.get("results", []):
            title_parts = db.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if title == "Sub-categories":
                subcategories_db_id = db["id"]
                print(f"Found 'Sub-categories' database: {subcategories_db_id}")
                break
        
        if not subcategories_db_id:
            print("ERROR: 'Sub-categories' database not found")
            sys.exit(1)
        
        # Query for "Streaming" subcategory
        resp = await client.post(
            f"https://api.notion.com/v1/databases/{subcategories_db_id}/query",
            headers=headers,
            json={}
        )
        resp.raise_for_status()
        subcategories = resp.json()
        
        streaming_id = None
        for page in subcategories.get("results", []):
            props = page.get("properties", {})
            name_prop = props.get("Name", {})
            title_parts = name_prop.get("title", [])
            name = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if name == "Streaming":
                streaming_id = page["id"]
                print(f"Found 'Streaming' subcategory: {streaming_id}")
                break
        
        if not streaming_id:
            print("WARNING: 'Streaming' subcategory not found. Using text-only value.")
        
        # Query for "Jago" account
        print("\nFetching account 'Jago'...")
        
        # Search for Accounts database
        accounts_db_id = None
        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json={"filter": {"value": "database", "property": "object"}, "page_size": 100}
        )
        resp.raise_for_status()
        data = resp.json()
        
        for db in data.get("results", []):
            title_parts = db.get("title", [])
            title = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if title == "Accounts":
                accounts_db_id = db["id"]
                print(f"Found 'Accounts' database: {accounts_db_id}")
                break
        
        if not accounts_db_id:
            print("ERROR: 'Accounts' database not found")
            sys.exit(1)
        
        # Query for "Jago" account
        resp = await client.post(
            f"https://api.notion.com/v1/databases/{accounts_db_id}/query",
            headers=headers,
            json={}
        )
        resp.raise_for_status()
        accounts = resp.json()
        
        jago_id = None
        for page in accounts.get("results", []):
            props = page.get("properties", {})
            name_prop = props.get("Name", {})
            title_parts = name_prop.get("title", [])
            name = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if name == "Jago":
                jago_id = page["id"]
                print(f"Found 'Jago' account: {jago_id}")
                break
        
        if not jago_id:
            print("WARNING: 'Jago' account not found. Using text-only value.")
        
        # Create the recurring payment entry
        print("\nCreating 'YouTube Premium (Family)' recurring payment...")
        
        properties = {
            "Name": {
                "title": [{"text": {"content": "YouTube Premium (Family)"}}]
            },
            "Amount": {
                "number": 59900
            },
            "Status": {
                "status": {"name": "Active"}
            }
        }
        
        # Add relation to subcategory if found
        if streaming_id:
            properties["🥡 Sub-categories"] = {
                "relation": [{"id": streaming_id}]
            }
        
        # Add relation to account if found
        if jago_id:
            properties["🧾 Accounts"] = {
                "relation": [{"id": jago_id}]
            }
        
        # Set payment frequency (monthly)
        properties["Payment Frequency"] = {
            "select": {"name": "Monthly"}
        }
        
        resp = await client.post(
            "https://api.notion.com/v1/pages",
            headers=headers,
            json={
                "parent": {"database_id": recurring_db_id},
                "properties": properties
            }
        )
        
        if resp.status_code == 200:
            data = resp.json()
            print(f"✅ Successfully created recurring payment!")
            print(f"   Page ID: {data['id']}")
            print(f"   Name: YouTube Premium (Family)")
            print(f"   Amount: Rp59,900")
            print(f"   Status: Active")
            if streaming_id:
                print(f"   Sub-category: Streaming")
            if jago_id:
                print(f"   Account: Jago")
            print("\nNext time a Jago email with amount Rp59,900 arrives, the bot will auto-fill 'YouTube Premium'!")
        else:
            print(f"ERROR: Failed to create page. Status {resp.status_code}")
            print(resp.text)
            sys.exit(1)

if __name__ == "__main__":
    asyncio.run(main())
