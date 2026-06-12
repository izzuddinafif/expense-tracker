#!/usr/bin/env python3
"""Helper to search and fix Notion expense entries.

Usage:
  python fix_notion.py search <keyword>
  python fix_notion.py update-account <page_id> <account_name>
  python fix_notion.py update-subcat <page_id> <subcat_name>
  python fix_notion.py update-amount <page_id> <amount>
  python fix_notion.py get <page_id>
  python fix_notion.py list-recent [limit]
"""

import asyncio
import sys
from config import load_config
from db import Database
from notion import NotionClient


async def main() -> None:
    config = load_config()
    db = await Database.connect(config.db_path)

    # Use the first completed user's NotionClient
    users = await db.get_all_users()
    notion = None
    user_cache = None
    for uid in users:
        user = await db.get_user(uid)
        if user and user.is_setup_complete:
            notion = NotionClient.from_user(user)
            user_cache = await notion.load_cache()
            break

    if not notion:
        print("No completed user found in DB.")
        await db.close()
        return

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "search":
        keyword = sys.argv[2] if len(sys.argv) > 2 else ""
        if not keyword:
            print("Usage: python fix_notion.py search <keyword>")
            return
        results = await notion.search_expenses("", keyword, user_cache)
        if not results:
            print(f"No results for '{keyword}'")
            return
        for r in results:
            print(f"  {r['date']} | Rp {r['amount']:>10,.0f} | {r['description'][:50]:<50} | {r.get('subcategory',''):<20} | {r.get('url','')}")

    elif cmd == "update-account":
        page_id = sys.argv[2] if len(sys.argv) > 2 else ""
        acc_name = sys.argv[3] if len(sys.argv) > 3 else ""
        if not page_id or not acc_name:
            print("Usage: python fix_notion.py update-account <page_id> <account_name>")
            return
        url = await notion.update_expense_account(page_id, acc_name, user_cache)
        print(f"✅ Updated → {url}")

    elif cmd == "update-subcat":
        page_id = sys.argv[2] if len(sys.argv) > 2 else ""
        sub_name = sys.argv[3] if len(sys.argv) > 3 else ""
        if not page_id or not sub_name:
            print("Usage: python fix_notion.py update-subcat <page_id> <subcat_name>")
            return
        url = await notion.update_expense_subcategory(page_id, sub_name, user_cache)
        print(f"✅ Updated → {url}")

    elif cmd == "update-amount":
        page_id = sys.argv[2] if len(sys.argv) > 2 else ""
        amount = float(sys.argv[3]) if len(sys.argv) > 3 else 0
        if not page_id or not amount:
            print("Usage: python fix_notion.py update-amount <page_id> <amount>")
            return
        url = await notion.update_expense_amount(page_id, amount)
        print(f"✅ Updated → {url}")

    elif cmd == "get":
        page_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not page_id:
            print("Usage: python fix_notion.py get <page_id>")
            return
        data = await notion._notion_get(f"https://api.notion.com/v1/pages/{page_id}")
        props = data.get("properties", {})
        desc = "".join(t["plain_text"] for t in props.get("Description", {}).get("title", []))
        amount = props.get("Amount", {}).get("number", 0)
        date = props.get("Date of Expense", {}).get("date", {}).get("start", "")
        acc_rel = props.get("Accounts", {}).get("relation", [])
        sub_rel = props.get("Expenses Sub-categories", {}).get("relation", [])
        acc_name = ""
        if acc_rel and user_cache:
            for name, url in user_cache.accounts.items():
                if url.endswith(acc_rel[0]["id"].replace("-", "")):
                    acc_name = name
                    break
        sub_name = ""
        if sub_rel and user_cache:
            for name, url in user_cache.subcategories.items():
                if url.endswith(sub_rel[0]["id"].replace("-", "")):
                    sub_name = name
                    break
        print(f"  Description: {desc}")
        print(f"  Amount: Rp {amount:,.0f}")
        print(f"  Date: {date}")
        print(f"  Account: {acc_name}")
        print(f"  Subcategory: {sub_name}")

    elif cmd == "list-recent":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        owner = ""
        first_user = await db.get_user(next(iter(users), 0)) if users else None
        if first_user:
            owner = first_user.owner_name
        expenses = await notion.fetch_expenses(owner, user_cache)
        for e in expenses[-limit:]:
            print(f"  {e['date']} | Rp {e['amount']:>10,.0f} | {e['description'][:50]:<50} | {e.get('subcategory',''):<20}")

    else:
        print(__doc__)

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
