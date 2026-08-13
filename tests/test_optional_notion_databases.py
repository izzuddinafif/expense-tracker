import pytest

from models import UserRecord
from notion import DB_NAME_MAP, NotionClient


def test_local_budget_database_is_not_required_for_completed_setup():
    user = UserRecord(
        telegram_id=7,
        owner_name="Afif",
        notion_token="test",
        expenses_ds="expenses",
        subcategories_ds="subcategories",
        accounts_ds="accounts",
        months_ds="months",
        years_ds="years",
        recurring_ds="recurring",
        income_ds="income",
        income_subcategories_ds="subcategories",
        income_months_ds="months",
        income_years_ds="years",
        categories_ds="categories",
        budget_ds=None,
    )

    assert user.is_setup_complete


@pytest.mark.asyncio
async def test_discovery_treats_assets_and_budget_as_optional():
    client = NotionClient("test-token", {})
    databases = [
        {
            "id": f"id-{field}",
            "title": [{"plain_text": title}],
        }
        for field, title in DB_NAME_MAP.items()
        if field not in {"assets_ds", "budget_ds"}
    ]

    async def fake_post(_url, json):
        assert json["filter"]["value"] == "database"
        return {"results": databases, "has_more": False}

    client._notion_post = fake_post
    try:
        found = await client.discover_databases()
    finally:
        await client.aclose()

    assert "assets_ds" not in found
    assert "budget_ds" not in found
    assert found["income_subcategories_ds"] == found["subcategories_ds"]
    assert found["income_months_ds"] == found["months_ds"]
    assert found["income_years_ds"] == found["years_ds"]
