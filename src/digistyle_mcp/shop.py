"""Digistyle tools: product search, product detail, categories, brands, filters, deals."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any, Literal

from pydantic import Field

from .http import SITE, ApiError, fetch
from .registry import tool

DISCOVERY = "/discovery/api/digistyle/v1"
SORT = {  # site sort ids (the "sortby" query parameter)
    "relevance": 22,
    "newest": 1,
    "best_selling": 7,
    "most_viewed": 4,
    "price_asc": 20,
    "price_desc": 21,
    "fastest_delivery": 25,
    "buyers_pick": 27,
    "featured": 29,
    "discount": 26,  # biggest discount first; the site uses it on campaign pages (not in its sort menu)
}
Sort = Literal[
    "relevance",
    "newest",
    "best_selling",
    "most_viewed",
    "price_asc",
    "price_desc",
    "fastest_delivery",
    "buyers_pick",
    "featured",
    "discount",
]
ROOTS = ("womens-apparel", "mens-apparel", "kids-apparel")  # top-level categories scanned by ds_deals by default
PER_PAGE = 20


_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _norm(text: str | None) -> str:
    """Match key: no spaces or ZWNJ, Persian letter forms and digits as ASCII, lower case."""
    text = re.sub(r"[\s\u200c]+", "", (text or "")).replace("ي", "ی").replace("ك", "ک")
    return text.translate(_DIGITS).lower()


def _toman(rial: int | float | None) -> int | None:
    """The API prices in Rial; the site shows Toman (Rial / 10)."""
    return None if rial is None else int(rial) // 10


def _stars(rate: float | None) -> float | None:
    """The API rates 0-100; the site shows 0-5 stars."""
    return round(rate / 20, 1) if rate else None  # 0 means not rated yet


def _price(p: dict[str, Any]) -> dict[str, Any]:
    now, was = _toman(p.get("selling_price")), _toman(p.get("rrp_price"))
    out: dict[str, Any] = {"price": now}
    if was and now is not None and was > now:
        out["was"] = was
        out["discount_percent"] = p.get("discount_percent") or round(100 - now * 100 / was)
    if p.get("is_incredible"):
        out["incredible"] = True
    return out


def _variant_names(v: dict[str, Any]) -> tuple[str | None, str | None]:
    """(size, color) of a variant from its themes."""
    size = color = None
    for t in v.get("themes") or []:
        val = t.get("value") or {}
        if val.get("nature") == "color":
            color = val.get("title")
        elif t.get("type") == "sized" or val.get("nature") == "alpha":
            size = val.get("title")
    return size, color


def _card(p: dict[str, Any]) -> dict[str, Any]:
    """One listing product, trimmed."""
    dv = p.get("default_variant") or {}
    live = [v for v in p.get("variants") or [] if v.get("status") == "marketable"]
    sizes = list(dict.fromkeys(s for v in live if (s := _variant_names(v)[0])))
    colors = list(dict.fromkeys(c for v in live if (c := _variant_names(v)[1])))
    rating = p.get("rating") or {}
    return {
        "id": p["id"],
        "title": p.get("title_fa"),
        "brand": (p.get("data_layer") or {}).get("brand"),
        **_price(dv.get("price") or {}),
        "in_stock": p.get("status") == "marketable",
        "sizes_in_stock": sizes,
        "colors": colors or [c.get("title") for c in p.get("colors") or []],
        "rating": _stars(rating.get("rate")),
        "rating_count": rating.get("count"),
        "seller": (dv.get("seller") or {}).get("title"),
        "url": f"{SITE}/product/{p['id']}",
    }


def _listing(d: dict[str, Any], page: int, limit: int) -> dict[str, Any]:
    pager = d.get("pager") or {}
    return {
        "total": pager.get("total_items"),
        "page": pager.get("current_page", page),
        "pages": pager.get("total_pages"),
        "products": [_card(p) for p in (d.get("products") or [])[:limit]],
    }


async def _brands() -> list[dict[str, Any]]:
    body = await fetch("/v1/brands/")
    return [b for group in (body.get("data") or {}).get("brand_groups", {}).values() for b in group]


async def _probe_brand(slug: str) -> dict[str, Any] | None:
    """The brand index misses some brands (Nike); their listing page still answers, so read the brand from it."""
    slug = slug.strip().lower().replace(" ", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        return None
    try:
        d = (await fetch(f"{DISCOVERY}/brands/{slug}/products", {"page": 1, "no_redirect": 1})).get("data") or {}
    except ApiError as e:
        if e.status == 404:
            return None
        raise
    return d.get("brand") or None


async def _search_brand(name: str) -> dict[str, Any] | None:
    """A brand missing from the index (Nike) still shows in the brand filter of a search for its name."""
    key = _norm(name)
    if not key:
        return None
    f = ((await fetch(f"{DISCOVERY}/search", {"q": name.strip(), "page": 1})).get("data") or {}).get("filters") or {}
    for b in (f.get("brands") or {}).get("top_options") or []:
        if key in (_norm(b.get("code")), _norm(b.get("title_en")), _norm(b.get("title_fa"))):
            return b
    return None


async def _brand(name: str) -> dict[str, Any]:
    """Resolve a brand code or Persian/English name to its record."""
    key = _norm(name)
    brands = await _brands()
    for b in brands:
        if key in (_norm(b["code"]), _norm(b.get("title_en")), _norm(b.get("title_fa"))):
            return b
    if probed := await _probe_brand(name) or await _search_brand(name):
        return probed
    near = [b["code"] for b in brands if key in _norm(b["code"]) or key in _norm(b.get("title_fa"))][:8]
    hint = f" Close matches: {', '.join(near)}." if near else ""
    raise ApiError(f"No Digistyle brand matches '{name}'. Use ds_brands to find its code.{hint}")


async def _listing_data(path: str, params: dict[str, Any], category: str | None = None) -> dict[str, Any]:
    try:
        return (await fetch(path, params)).get("data") or {}
    except ApiError as e:
        if e.status == 404 and category:
            raise ApiError(f"Unknown category '{category}'. Use ds_categories to find its code.") from e
        raise


def _index(options: list[dict[str, Any]] | None, label: str) -> dict[str, int]:
    return {_norm(str(o.get(label, ""))): o["id"] for o in options or [] if "id" in o}


async def _resolve(category: str | None, values: list[str] | None, key: str, what: str) -> list[int]:
    """Map size/color names to the category's filter ids (they differ per category)."""
    if not values:
        return []
    if not category:
        raise ApiError(f"Filtering by {what} needs a category (use ds_categories to find a leaf category code).")
    d = await _listing_data(f"{DISCOVERY}/categories/{category}/products", {"page": 1, "use_searchia": 1}, category)
    options = (d.get("filters") or {}).get(key, {}).get("options") or []
    idx = _index(options, "title")
    if not idx:
        raise ApiError(f"Category '{category}' has no {what} filter. Try a more specific (leaf) category.")
    ids = []
    for v in values:
        if _norm(v) not in idx:
            raise ApiError(f"Unknown {what} '{v}' in {category}. Available: {', '.join(o['title'] for o in options)}.")
        ids.append(idx[_norm(v)])
    return ids


@tool("Search products")
async def ds_search(
    query: Annotated[str | None, Field(description="Search words, Persian works best. Example: 'کفش ورزشی'.")] = None,
    category: Annotated[
        str | None, Field(description="Category code from ds_categories. Example: 'women-tee-shirts'.")
    ] = None,
    brand: Annotated[
        str | None, Field(description="Brand code or name from ds_brands. Example: 'nike' or 'نایکی'.")
    ] = None,
    size: Annotated[
        list[str] | None,
        Field(
            description="Sizes as shown on the site, any of them. Needs a leaf category. Example: ['L', 'XL'], ['42'].",
            max_length=10,
        ),
    ] = None,
    color: Annotated[
        list[str] | None,
        Field(
            description="Color-group names in Persian, any of them. Needs a leaf category. Example: ['مشکی'].",
            max_length=10,
        ),
    ] = None,
    min_price: Annotated[int | None, Field(ge=0, description="Lowest price in Toman. Example: 500000.")] = None,
    max_price: Annotated[int | None, Field(ge=0, description="Highest price in Toman. Example: 2000000.")] = None,
    in_stock_only: Annotated[bool, Field(description="Only products that can be bought now.")] = True,
    on_sale: Annotated[bool, Field(description="Only discounted products (the site's 'دارای تخفیف' switch).")] = False,
    sort: Annotated[Sort, Field(description="Order of results.")] = "relevance",
    page: Annotated[int, Field(ge=1, le=100, description="Result page, 20 products each.")] = 1,
    limit: Annotated[int, Field(ge=1, le=20, description="Products to return from the page.")] = 10,
) -> dict[str, Any]:
    """Search Digistyle fashion products with filters for category, brand, size, color and price (Toman).

    Give a query, a category, a brand, or a mix. Prices are in Toman; `was` and `discount_percent` appear only on
    discounted products. Call ds_product with an id for sizes in stock per color; ds_filters lists the sizes, colors
    and brands a category offers.
    """
    query = (query or "").strip() or None
    if not (query or category or brand):
        raise ApiError("Give a query, a category or a brand.")
    params: dict[str, Any] = {"page": page, "rows": PER_PAGE, "sortby": SORT[sort], "use_searchia": 1}
    if query:
        params["q"] = query
    if min_price is not None:
        params["price[min]"] = min_price * 10
    if max_price is not None:
        params["price[max]"] = max_price * 10
    if in_stock_only:
        params["has_selling_stock"] = 1
    if on_sale:
        params["has_discount"] = "true"
    if size_ids := await _resolve(category, size, "sizes", "size"):
        params["sizes[]"] = size_ids
    if color_ids := await _resolve(category, color, "color_palettes", "color"):
        params["color_palettes[]"] = color_ids
    found = await _brand(brand) if brand else None
    if category:
        path = f"{DISCOVERY}/categories/{category}/products"
        if found:
            params["brands[]"] = [found["id"]]
    elif found:
        path = f"{DISCOVERY}/brands/{found['code']}/products"
        params["no_redirect"] = 1
    else:
        path = f"{DISCOVERY}/search"
    d = await _listing_data(path, params, category)
    out = _listing(d, page, limit)
    if category:
        out["category"] = (d.get("category") or {}).get("title_fa")
    if found:
        out["brand"] = found.get("title_fa")
    return out


@tool("Product details")
async def ds_product(
    product_id: Annotated[int, Field(ge=1, description="Product id from ds_search or the /product/<id> site URL.")],
    comments: Annotated[int, Field(ge=0, le=10, description="Customer comments to include.")] = 3,
) -> dict[str, Any]:
    """Get a Digistyle product: price, discount, every size and color with stock, seller, specs, rating, comments.

    Prices are in Toman. Each variant is one size and color; `in_stock` says whether it can be bought now.
    Seller and warranty sit on the variant only when they differ between variants.
    """
    try:
        body = await fetch(f"/v1/products/{product_id}/")
    except ApiError as e:
        if e.status in (400, 404):  # the API answers 400 for an id that does not exist
            raise ApiError(f"Product {product_id} not found.") from e
        raise
    p = (body.get("data") or {}).get("product")
    if not p:
        raise ApiError(f"Product {product_id} not found.")
    variants = []
    for v in p.get("variants") or []:
        size, color = _variant_names(v)
        seller = v.get("seller") or {}
        variants.append(
            {
                "variant_id": v["id"],
                "size": size,
                "color": color,
                **_price(v.get("price") or {}),
                "in_stock": v.get("status") == "marketable",
                "max_per_order": (v.get("price") or {}).get("order_limit"),
                "ships_in_days": v.get("lead_time"),
                "seller": seller.get("title"),
                "official_seller": (seller.get("properties") or {}).get("is_official"),
                "warranty": (v.get("warranty") or {}).get("title_fa"),
            }
        )
    common = {}  # fields identical on every variant move to the top level to keep the answer short
    for key in ("seller", "official_seller", "warranty"):
        values = {v[key] for v in variants}
        if len(variants) > 1 and len(values) == 1:
            common[key] = values.pop()
            for v in variants:
                del v[key]
    rating = p.get("rating") or {}
    price = _price(p.get("price") or {})
    low = _toman((p.get("properties") or {}).get("min_price_in_last_month")) or None  # 0 means unknown
    ov = p.get("comments_overview") or {}
    specs = {
        a["title"]: " ، ".join(x.strip() for x in a.get("values") or [])
        for g in p.get("specifications") or []
        for a in g.get("attributes") or []
    }
    return {
        "id": p["id"],
        "title": p.get("title_fa"),
        "brand": (p.get("brand") or {}).get("title_fa"),
        "brand_code": (p.get("brand") or {}).get("code"),
        "category": (p.get("category") or {}).get("title_fa"),
        "category_code": (p.get("category") or {}).get("code"),
        "in_stock": p.get("status") == "marketable",
        **price,
        "lowest_price_30d": low,
        "rating": _stars(rating.get("rate")),
        "rating_count": rating.get("count"),
        "colors": [c.get("title") for c in p.get("colors") or []],
        "sizes_in_stock": list(dict.fromkeys(v["size"] for v in variants if v["in_stock"] and v["size"])),
        **common,
        "variants": variants,
        "specs": specs,
        "description": ((p.get("review") or {}).get("description") or "").strip(),
        "comments_total": (p.get("comments") or {}).get("count"),
        "comments_summary": ov.get("overview"),
        "pros": ov.get("advantages"),
        "cons": ov.get("disadvantages"),
        "comments": [
            {"rate": c.get("rate"), "date": c.get("created_at"), "text": (c.get("body") or "").strip()[:300]}
            for c in ((p.get("comments") or {}).get("top_comments") or [])[:comments]
        ],
        "image": ((p.get("images") or {}).get("main") or {}).get("url"),
        "url": f"{SITE}/product/{p['id']}",
    }


def _flatten(nodes: list[dict[str, Any]], trail: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    rows = []
    for n in nodes or []:
        url = n.get("url")
        code = (url.get("params") or {}).get("category_code") if isinstance(url, dict) else None
        path = (*trail, n.get("title") or "")
        if code and not any(r["code"] == code for r in rows):
            rows.append({"code": code, "title": n.get("title"), "path": " > ".join(path), "level": int(n["level"])})
        rows += _flatten(n.get("children") or [], path)
    return rows


@tool("Categories")
async def ds_categories(
    query: Annotated[
        str | None, Field(description="Filter by words in the category name or path, Persian. Example: 'کفش'.")
    ] = None,
    max_level: Annotated[
        int, Field(ge=1, le=4, description="Deepest menu level: 1 genders, 2 groups, 3 sub-groups, 4 leaf types.")
    ] = 2,
    limit: Annotated[int, Field(ge=1, le=100, description="Rows to return.")] = 40,
) -> dict[str, Any]:
    """List Digistyle categories (from the site menu) with the code to pass to ds_search, ds_filters and ds_deals.

    Start with max_level 2, then search by name to reach leaf categories (level 4), the only ones that have size
    and color filters.
    """
    body = await fetch("/v1/mega-menu/", {"tag": "all"})
    rows = _flatten(body.get("data") or [])
    rows = [r for r in rows if r["level"] <= max_level or query]
    if query:
        rows = [r for r in rows if _norm(query) in _norm(r["path"])]
    return {"total": len(rows), "categories": rows[:limit]}


@tool("Brands")
async def ds_brands(
    query: Annotated[
        str | None, Field(description="Part of the brand name or code, Persian or English. Example: 'nike', 'لویی'.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Brands to return.")] = 30,
) -> dict[str, Any]:
    """List Digistyle brands with their code (for ds_search's brand argument); search by name.

    About 1700 brands exist, so give a query; without one you get the first alphabetical ones. A few brands (Nike)
    are missing from the index: search them by English code ("nike").
    """
    rows = await _brands()
    if query:
        q = _norm(query)
        rows = [
            b for b in rows if q in _norm(b["code"]) or q in _norm(b.get("title_en")) or q in _norm(b.get("title_fa"))
        ]
    if not rows and query and (b := await _probe_brand(query) or await _search_brand(query)):
        rows = [b]
    return {
        "total": len(rows),
        "brands": [
            {"code": b["code"], "name_fa": b.get("title_fa"), "name_en": b.get("title_en"), "id": b["id"]}
            for b in rows[:limit]
        ],
    }


@tool("Category filters")
async def ds_filters(
    category: Annotated[str, Field(description="Category code from ds_categories. Example: 'women-tee-shirts'.")],
    brands: Annotated[int, Field(ge=0, le=100, description="Top brands to list.")] = 15,
) -> dict[str, Any]:
    """Show what a Digistyle category can be filtered by: sizes, colors, top brands, price range, other attributes.

    Use the size and color names in ds_search. Only leaf categories have sizes and colors.
    """
    d = await _listing_data(f"{DISCOVERY}/categories/{category}/products", {"page": 1, "use_searchia": 1}, category)
    f = d.get("filters") or {}
    price = (f.get("price") or {}).get("options") or {}
    groups = [v for k, v in f.items() if k.startswith("attribute_")] + list(
        (f.get("attributes") or {}).get("options") or []
    )
    attrs = {v["title"]: [o.get("title_fa") for o in (v.get("options") or [])][:12] for v in groups if v.get("title")}
    return {
        "category": (d.get("category") or {}).get("title_fa"),
        "leaf": (d.get("category") or {}).get("is_leaf"),
        "total_products": (d.get("pager") or {}).get("total_items"),
        "price_range_toman": {"min": _toman(price.get("min")), "max": _toman(price.get("max"))} if price else None,
        "sizes": [o["title"] for o in (f.get("sizes") or {}).get("options") or []],
        "colors": [o["title"] for o in (f.get("color_palettes") or {}).get("options") or []],
        "top_brands": [
            {"code": b.get("code"), "name": b.get("title_fa")}
            for b in (f.get("brands") or {}).get("top_options") or (f.get("brands") or {}).get("options") or []
        ][:brands],
        "attributes": attrs,
    }


@tool("Discounted products")
async def ds_deals(
    category: Annotated[
        str | None, Field(description="Category code to scan. Default: the women, men and kids root categories.")
    ] = None,
    brand: Annotated[str | None, Field(description="Only this brand (code or name from ds_brands).")] = None,
    promotion: Annotated[
        str | None,
        Field(description="A campaign id or code from a site /promotion/plp_<id>/ link. Example: 'plp_456424735'."),
    ] = None,
    min_discount: Annotated[int, Field(ge=1, le=90, description="Smallest discount percent to keep.")] = 20,
    incredible_only: Annotated[bool, Field(description="Only products flagged as incredible offers.")] = False,
    pages: Annotated[int, Field(ge=1, le=5, description="Listing pages scanned per source, 20 products each.")] = 2,
    limit: Annotated[int, Field(ge=1, le=40, description="Products to return.")] = 15,
) -> dict[str, Any]:
    """Find discounted Digistyle products, biggest discount first, from a category, brand or campaign.

    This reads the site's discounted-only listing sorted by discount size (a campaign link reads that campaign)
    and keeps products at or above min_discount percent. Raise `pages` to go deeper. Prices are in Toman.
    """
    if promotion:
        sources = [f"{DISCOVERY}/promotions/{promotion.removeprefix('plp_')}/products"]
        extra: dict[str, Any] = {}
    elif brand:
        found = await _brand(brand)
        sources = [f"{DISCOVERY}/brands/{found['code']}/products"]
        extra = {"no_redirect": 1}
    else:
        sources = [f"{DISCOVERY}/categories/{c}/products" for c in ([category] if category else ROOTS)]
        extra = {}
    found_cat = bool(category and not promotion)
    if found_cat and brand:
        sources = [f"{DISCOVERY}/categories/{category}/products"]
        extra = {"brands[]": [(await _brand(brand))["id"]]}

    async def scan(path: str, page: int) -> dict[str, Any]:
        params = {
            "page": page,
            "rows": PER_PAGE,
            "sortby": SORT["discount"],
            "has_selling_stock": 1,
            "has_discount": "true",
            "use_searchia": 1,
            **extra,
        }
        return await _listing_data(path, params, category)

    results = await asyncio.gather(*(scan(s, n) for s in sources for n in range(1, pages + 1)))
    seen: set[int] = set()
    cards = []
    scanned = 0
    for d in results:
        for p in d.get("products") or []:
            scanned += 1
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            c = _card(p)
            if c.get("discount_percent", 0) >= min_discount and (c.get("incredible") or not incredible_only):
                cards.append(c)
    cards.sort(key=lambda c: -c["discount_percent"])
    return {"scanned": scanned, "matches": len(cards), "products": cards[:limit]}
