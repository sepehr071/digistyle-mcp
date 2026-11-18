import httpx
import pytest
from conftest import fixture

DISC = "/discovery/api/digistyle/v1"
TOOLS = {"ds_search", "ds_product", "ds_categories", "ds_brands", "ds_filters", "ds_deals"}

pytestmark = pytest.mark.anyio


def q(api):
    return dict(api.calls[-1].url.params.multi_items())


async def test_all_tools_are_read_only_and_described(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True, t.name
        assert t.title and t.description.splitlines()[0].endswith("."), t.name
        assert "title" not in str(t.input_schema.get("properties", {}).values()), t.name


async def test_search_category_filters_in_toman(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    api["/v1/brands/"] = fixture("brands.json")
    args = {"category": "women-tee-shirts", "brand": "adidas", "size": ["L"], "min_price": 100000, "max_price": 900000}
    r = (await client.call_tool("ds_search", args)).structured_content
    sent = q(api)
    assert sent["brands[]"] == "1291" and sent["price[min]"] == "1000000" and sent["price[max]"] == "9000000"
    assert sent["sizes[]"] and sent["has_selling_stock"] == "1" and "has_discount" not in sent
    assert r["total"] == 550 and r["pages"] == 28 and r["category"] and len(r["products"]) == 3
    p = r["products"][0]
    rial = fixture("category_listing.json")["data"]["products"][0]["default_variant"]["price"]["selling_price"]
    assert p["price"] * 10 == rial
    assert p["url"].endswith(f"/product/{p['id']}") and 0 <= p["rating"] <= 5 and p["sizes_in_stock"]


async def test_search_on_sale(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    await client.call_tool("ds_search", {"category": "women-tee-shirts", "on_sale": True})
    assert q(api)["has_discount"] == "true"


async def test_search_unknown_size_lists_available(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    r = await client.call_tool("ds_search", {"category": "women-tee-shirts", "size": ["99"]})
    assert r.is_error and "Available" in r.content[0].text


async def test_search_size_needs_category_and_input_required(client, api):
    r = await client.call_tool("ds_search", {"query": "x", "size": ["L"]})
    assert r.is_error and "category" in r.content[0].text
    r = await client.call_tool("ds_search", {})
    assert r.is_error


async def test_search_plain_query_and_brand_routes(client, api):
    api[f"{DISC}/search"] = fixture("search.json")
    args = {"query": "تیشرت", "sort": "price_asc", "in_stock_only": False}
    r = (await client.call_tool("ds_search", args)).structured_content
    sent = q(api)
    assert sent["q"] == "تیشرت" and sent["sortby"] == "20" and "has_selling_stock" not in sent and r["products"]
    api["/v1/brands/"] = fixture("brands.json")
    api[f"{DISC}/brands/adidas/products"] = fixture("category_listing.json")
    r = (await client.call_tool("ds_search", {"brand": "Adidas"})).structured_content
    assert api.calls[-1].url.path.endswith("/brands/adidas/products") and r["brand"]


async def test_brand_not_in_index_is_probed(client, api):
    api["/v1/brands/"] = fixture("brands.json")
    listing = fixture("category_listing.json")
    listing["data"]["brand"] = {"id": 1933, "code": "nike", "title_fa": "نایکی", "title_en": "Nike"}
    api[f"{DISC}/brands/nike/products"] = listing
    r = (await client.call_tool("ds_brands", {"query": "nike"})).structured_content
    assert r["brands"] == [{"code": "nike", "name_fa": "نایکی", "name_en": "Nike", "id": 1933}]


async def test_product_detail(client, api):
    api["/v1/products/18564911/"] = fixture("product.json")
    r = (await client.call_tool("ds_product", {"product_id": 18564911})).structured_content
    assert r["price"] == 799000 and r["was"] == 1198000 and r["discount_percent"] == 33
    assert r["sizes_in_stock"] == ["L", "XL"] and r["colors"] == ["مشکی", "سبز روشن"]
    assert len(r["variants"]) == 3 and r["variants"][0]["size"] == "L" and r["variants"][0]["in_stock"]
    assert r["specs"]["جنس"] and r["brand_code"] == "baynet" and r["rating"] == 4.5 and r["comments"]
    assert r["image"].startswith("https://")


async def test_product_missing(client, api):
    api["/v1/products/1/"] = {"status": 200, "data": {}}
    r = await client.call_tool("ds_product", {"product_id": 1})
    assert r.is_error


async def test_categories_and_brands(client, api):
    api["/v1/mega-menu/"] = fixture("mega_menu.json")
    r = (await client.call_tool("ds_categories", {})).structured_content
    assert r["categories"][0]["code"] and all(c["level"] <= 2 for c in r["categories"])
    r = (await client.call_tool("ds_categories", {"query": "لباس", "max_level": 4})).structured_content
    assert r["total"] >= 1 and all("لباس" in c["path"] for c in r["categories"])
    api["/v1/brands/"] = fixture("brands.json")
    r = (await client.call_tool("ds_brands", {"query": "ADIDAS"})).structured_content
    assert r["brands"][0]["code"] == "adidas"


async def test_filters(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    r = (await client.call_tool("ds_filters", {"category": "women-tee-shirts"})).structured_content
    assert "L" in r["sizes"] and "مشکی" in r["colors"] and r["top_brands"][0]["code"]
    assert r["price_range_toman"]["min"] < r["price_range_toman"]["max"] and r["leaf"] is True


async def test_deals_sorted_by_discount(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    args = {"category": "women-tee-shirts", "min_discount": 1, "pages": 1}
    r = (await client.call_tool("ds_deals", args)).structured_content
    d = [p["discount_percent"] for p in r["products"]]
    assert d == sorted(d, reverse=True) and r["scanned"] == 3 and all(x >= 1 for x in d)
    assert q(api)["sortby"] == "26" and q(api)["has_discount"] == "true" and q(api)["use_searchia"] == "1"
    api[f"{DISC}/promotions/456424735/products"] = fixture("category_listing.json")
    await client.call_tool("ds_deals", {"promotion": "plp_456424735", "pages": 1})
    assert api.calls[-1].url.path.endswith("/promotions/456424735/products")


async def test_http_error_is_actionable(client, api):
    api[f"{DISC}/search"] = lambda request: httpx.Response(403, text="no")
    r = await client.call_tool("ds_search", {"query": "x"})
    assert r.is_error and "DIGISTYLE_MCP_PROXY" in r.content[0].text


async def test_search_discount_sort(client, api):
    api[f"{DISC}/search"] = fixture("search.json")
    await client.call_tool("ds_search", {"query": "x", "sort": "discount"})
    assert q(api)["sortby"] == "26" and q(api)["use_searchia"] == "1"  # the site sends use_searchia=1


async def test_search_blank_query_rejected(client, api):
    r = await client.call_tool("ds_search", {"query": "   "})
    assert r.is_error and not api.calls


async def test_persian_digit_size(client, api):
    api[f"{DISC}/categories/women-tee-shirts/products"] = fixture("category_listing.json")
    listing = fixture("category_listing.json")
    listing["data"]["filters"]["sizes"]["options"].append({"id": 4242, "title": "42"})
    api[f"{DISC}/categories/women-tee-shirts/products"] = listing
    await client.call_tool("ds_search", {"category": "women-tee-shirts", "size": ["۴۲"]})
    assert q(api)["sizes[]"] == "4242"


async def test_persian_brand_missing_from_index_found_by_search(client, api):
    api["/v1/brands/"] = fixture("brands.json")
    hit = {"id": 1933, "code": "nike", "title_fa": "نایکی", "title_en": "Nike"}
    search = fixture("search.json")
    search["data"]["filters"]["brands"] = {"top_options": [hit]}
    api[f"{DISC}/search"] = search
    api[f"{DISC}/brands/nike/products"] = fixture("category_listing.json")
    r = (await client.call_tool("ds_brands", {"query": "نایکی"})).structured_content
    assert r["brands"][0]["code"] == "nike"
    r = (await client.call_tool("ds_search", {"brand": "نایکی"})).structured_content
    assert api.calls[-1].url.path.endswith("/brands/nike/products") and r["total"]


async def test_unknown_ids_have_clear_errors(client, api):
    api["/v1/products/999/"] = lambda request: httpx.Response(400, json={"error": True})
    r = await client.call_tool("ds_product", {"product_id": 999})
    assert r.is_error and "not found" in r.content[0].text
    r = await client.call_tool("ds_filters", {"category": "nope"})
    assert r.is_error and "Unknown category" in r.content[0].text


async def test_product_hoists_common_seller_and_warranty(client, api):
    api["/v1/products/18564911/"] = fixture("product.json")
    r = (await client.call_tool("ds_product", {"product_id": 18564911})).structured_content
    assert r["seller"] and r["warranty"] and "seller" not in r["variants"][0]


async def test_product_unknown_lowest_price_is_dropped(client, api):
    body = fixture("product.json")
    body["data"]["product"]["properties"]["min_price_in_last_month"] = 0
    api["/v1/products/18564911/"] = body
    r = (await client.call_tool("ds_product", {"product_id": 18564911})).structured_content
    assert r["lowest_price_30d"] is None


async def test_filters_searchia_shape(client, api):
    listing = fixture("category_listing.json")
    f = listing["data"]["filters"]
    f["brands"] = {"type": "checkbox", "options": f["brands"].get("top_options") or [{"code": "x", "title_fa": "ایکس"}]}
    f["attributes"] = {"options": [{"title": "جنس", "id": 1, "options": [{"id": 2, "title_fa": "نخ"}]}]}
    api[f"{DISC}/categories/women-tee-shirts/products"] = listing
    r = (await client.call_tool("ds_filters", {"category": "women-tee-shirts"})).structured_content
    assert r["top_brands"] and r["attributes"]["جنس"] == ["نخ"]
    assert q(api)["use_searchia"] == "1"


async def test_unrated_product_has_no_zero_rating(client, api):
    body = fixture("product.json")
    body["data"]["product"]["rating"] = {"rate": 0, "count": 0}
    api["/v1/products/18564911/"] = body
    r = (await client.call_tool("ds_product", {"product_id": 18564911})).structured_content
    assert r["rating"] is None
