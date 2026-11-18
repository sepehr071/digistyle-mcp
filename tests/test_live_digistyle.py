import pytest

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_search_filters(client):
    args = {"category": "women-tee-shirts", "brand": "esmara", "size": ["L"], "sort": "price_asc", "limit": 5}
    r = (await client.call_tool("ds_search", args)).structured_content
    assert r["total"] > 0 and r["brand"] == "اسمارا"
    prices = [p["price"] for p in r["products"]]
    assert prices[0] <= prices[-1]  # the site sorts by the cheapest variant, so cards can be slightly out of order
    assert all(p["brand"] == "اسمارا" and "L" in p["sizes_in_stock"] for p in r["products"])


async def test_live_search_text_and_price(client):
    r = (
        await client.call_tool("ds_search", {"query": "کفش ورزشی", "max_price": 3000000, "limit": 5})
    ).structured_content
    assert r["products"] and all(p["price"] <= 3000000 for p in r["products"])


async def test_live_product(client):
    s = (await client.call_tool("ds_search", {"category": "women-tee-shirts", "limit": 1})).structured_content
    pid = s["products"][0]["id"]
    r = (await client.call_tool("ds_product", {"product_id": pid})).structured_content
    assert r["id"] == pid and r["title"] and r["price"] and r["variants"] and r["specs"]


async def test_live_categories_brands_filters(client):
    r = (await client.call_tool("ds_categories", {"query": "تی شرت", "max_level": 4})).structured_content
    assert any(c["code"] == "women-tee-shirts" for c in r["categories"])
    r = (await client.call_tool("ds_brands", {"query": "nike"})).structured_content
    assert r["brands"][0]["code"] == "nike"
    r = (await client.call_tool("ds_filters", {"category": "women-tee-shirts"})).structured_content
    assert "L" in r["sizes"] and r["colors"] and r["top_brands"]


async def test_live_deals(client):
    args = {"category": "men-clothing", "min_discount": 20, "limit": 5}
    r = (await client.call_tool("ds_deals", args)).structured_content
    assert r["products"] and all(p["discount_percent"] >= 20 for p in r["products"])


async def test_live_persian_brand_missing_from_index_and_discount_sort(client):
    r = (await client.call_tool("ds_brands", {"query": "نایکی"})).structured_content
    assert r["brands"][0]["code"] == "nike"
    r = (await client.call_tool("ds_deals", {"brand": "نایکی", "pages": 1, "limit": 5})).structured_content
    d = [p["discount_percent"] for p in r["products"]]
    assert d and d == sorted(d, reverse=True) and all(p["brand"] == "نایکی" for p in r["products"])
