"""READ-ONLY (2026-10-02): what does eBay say about delivery cost, and what does the
Shipping Cost (£) column hold today?

1. Per product tab: how the Shipping Cost (£) cells are filled (blank / a number /
   zero / free-style text / other text) - counts only.
2. For a random sample of eBay-tab rows (SAMPLE, default 60): one get_item_by_legacy_id
   call each (the sync's own call - same Browse quota unit), reading the item's
   shippingOptions: how many options, which cost types, free vs paid vs unknown, whether
   a buyer location in X-EBAY-C-ENDUSERCTX makes unknown (calculated) costs known, and
   what adding the delivery cost to the cost base would do to the selling price.

Prints aggregates only - never an item id, cost or price of a single product (the
repositories, and so their run logs, are public). Writes nothing anywhere.
"""
import json
import os
import random
import statistics
import time

os.environ.setdefault("FEE_MODE", "category")

import gspread  # noqa: E402
import requests  # noqa: E402
from oauth2client.service_account import ServiceAccountCredentials  # noqa: E402

import fees  # noqa: E402
import pricing  # noqa: E402
import sheet_tabs  # noqa: E402
from generate_xml import (_fetch_item_group_as_item, _is_item_group_error,  # noqa: E402
                          _to_float, get_ebay_token)

SHEET_NAME = os.getenv("SHEET_NAME") or "Makstore_Full_Feed_Master"
SAMPLE = int(os.getenv("SAMPLE") or "60")
CTX = "contextualLocation=country%3DGB%2Czip%3DSW1A1AA"   # a buyer in London, URL-encoded as eBay documents it
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id"


def cell(row, ix, key):
    i = ix.get(key)
    return str(row[i]).strip() if i is not None and i < len(row) else ""


def classify_cell(text):
    t = text.strip().lower()
    if not t:
        return "blank"
    try:
        v = float(t.replace(",", "").replace("£", ""))
    except ValueError:
        return "free-style text" if t in ("free", "free shipping", "n/a", "none", "0.00", "£0") else "other text"
    return "zero" if v == 0 else "a number > 0"


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


def fetch(item_id, token, ctx):
    headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}
    if ctx:
        headers["X-EBAY-C-ENDUSERCTX"] = CTX
    resp = requests.get(ITEM_URL, headers=headers, params={"legacy_item_id": item_id}, timeout=20)
    if resp.status_code == 404:
        return "gone", None
    if resp.status_code == 400 and _is_item_group_error(resp):
        item = _fetch_item_group_as_item(item_id, token)
        return ("group", item) if item else ("gone", None)
    if resp.status_code != 200:
        return f"http {resp.status_code}", None
    return "ok", resp.json()


def cheapest(item):
    """(state, cost): free / paid / unknown from the item's shippingOptions."""
    options = (item or {}).get("shippingOptions") or []
    if not options:
        return "no options", None
    costs = []
    for opt in options:
        sc = opt.get("shippingCost") or {}
        if sc.get("value") in (None, ""):
            continue
        cur = (sc.get("currency") or "GBP").upper()
        if cur != "GBP":
            return "not GBP", None
        try:
            costs.append(float(sc["value"]))
        except (TypeError, ValueError):
            continue
    if not costs:
        return "unknown (no cost)", None
    low = min(costs)
    return ("free", 0.0) if low <= 0 else ("paid", low)


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    tabs = [sheet_tabs.product_sheet(book)]
    try:
        amz = book.worksheet(sheet_tabs.AMAZON_TAB)
        if amz.title != tabs[0].title:
            tabs.append(amz)
    except gspread.exceptions.WorksheetNotFound:
        pass
    print(f"sheet {SHEET_NAME!r} | top band profit {pricing.TOP_BAND_PROFIT:g}% | fee mode "
          f"{'category' if fees.enabled() else 'flat'}")

    pool = []
    for ws in tabs:
        values = ws.get_all_values()
        header = [str(h).strip() for h in values[0]]
        ix = {h: i for i, h in enumerate(header) if h}
        counts, nrows = {}, 0
        for n, r in enumerate(values[1:], start=2):
            if not cell(r, ix, "SKU"):
                continue
            nrows += 1
            k = classify_cell(cell(r, ix, "Shipping Cost (£)"))
            counts[k] = counts.get(k, 0) + 1
            if (ws.title == tabs[0].title and "/itm/" in cell(r, ix, "Supplier URL")
                    and _to_float(cell(r, ix, "Cost Price (£)")) > 0
                    and _to_float(cell(r, ix, "Stock")) > 0):
                pool.append({"row": n, "url": cell(r, ix, "Supplier URL"), "cost": _to_float(cell(r, ix, "Cost Price (£)")),
                             "have_ship": cell(r, ix, "Shipping Cost (£)"), "category": cell(r, ix, "Category")})
        print(f"\n=== tab {ws.title!r}: {nrows} rows with a SKU | Shipping Cost (£) column present: "
              f"{'Shipping Cost (£)' in ix}")
        for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {v:6d}  {k}")

    if SAMPLE <= 0 or not pool:
        return
    rng = random.Random(20261002)
    sample = rng.sample(pool, min(SAMPLE, len(pool)))
    token = get_ebay_token()
    if not token:
        print("no eBay token - sample skipped")
        return
    print(f"\n=== eBay sample: {len(sample)} in-stock eBay-tab rows (pool {len(pool)}), one call each + one more "
          "with a buyer location for the ones whose cost came back unknown")
    outcome, states_plain, states_ctx = {}, {}, {}
    cost_types, option_types, n_options, paid, deltas, cross_down = {}, {}, [], [], [], 0
    cells_vs_ebay = {"cell blank": 0, "cell = eBay": 0, "cell differs": 0}
    for s in sample:
        m = s["url"].split("/itm/")[1]
        item_id = "".join(ch for ch in m if ch.isdigit())
        try:
            how, item = fetch(item_id, token, ctx=False)
        except requests.RequestException as exc:
            how, item = f"error {type(exc).__name__}", None
        outcome[how] = outcome.get(how, 0) + 1
        time.sleep(0.25)
        if item is None:
            continue
        state, cost = cheapest(item)
        states_plain[state] = states_plain.get(state, 0) + 1
        for opt in item.get("shippingOptions") or []:
            cost_types[opt.get("shippingCostType") or "-"] = cost_types.get(opt.get("shippingCostType") or "-", 0) + 1
            option_types[opt.get("type") or "-"] = option_types.get(opt.get("type") or "-", 0) + 1
        n_options.append(len(item.get("shippingOptions") or []))
        if state.startswith("unknown"):
            try:
                _, item2 = fetch(item_id, token, ctx=True)
            except requests.RequestException:
                item2 = None
            st2, cost2 = cheapest(item2)
            states_ctx[st2] = states_ctx.get(st2, 0) + 1
            if cost is None and cost2 is not None:
                state, cost = st2, cost2
            time.sleep(0.25)
        if cost is not None:
            have = s["have_ship"]
            if not have:
                cells_vs_ebay["cell blank"] += 1
            elif abs(_to_float(have) - cost) < 0.005:
                cells_vs_ebay["cell = eBay"] += 1
            else:
                cells_vs_ebay["cell differs"] += 1
        if state == "paid":
            paid.append(cost)
            rule = fees.rule_for_category_path(s["category"]) if fees.enabled() else None
            c0, c1 = s["cost"], s["cost"] + cost
            p0 = pricing.price_for_profit(c0, pricing.profit_percent(c0), rule=rule) if rule else \
                pricing.price_for_profit(c0, pricing.profit_percent(c0))
            p1 = pricing.price_for_profit(c1, pricing.profit_percent(c1), rule=rule) if rule else \
                pricing.price_for_profit(c1, pricing.profit_percent(c1))
            if p0 > 0:
                deltas.append((p1 / p0 - 1) * 100)
                cross_down += p1 < p0 - 0.005
    print(f"fetch outcome: {outcome}")
    print(f"cheapest delivery option, as the sync's own call returns it: {states_plain}")
    if states_ctx:
        print(f"the unknown ones re-asked with a buyer location (London) in X-EBAY-C-ENDUSERCTX: {states_ctx}")
    print(f"shippingCostType per option: {cost_types} | option type: {option_types} | "
          f"options per item: min {min(n_options) if n_options else 0}, max {max(n_options) if n_options else 0}")
    print(f"the sheet's Shipping Cost cell vs eBay's answer (where eBay's cost is known): {cells_vs_ebay}")
    if paid:
        print(f"paid delivery: {len(paid)} items | fee GBP mean {statistics.mean(paid):.2f}, median "
              f"{statistics.median(paid):.2f}, p90 {pct(paid, 0.9):.2f}, max {max(paid):.2f}")
    if deltas:
        print(f"selling price if that fee joins the cost base (category commission, current profit bands): "
              f"mean {statistics.mean(deltas):+.1f}%, median {statistics.median(deltas):+.1f}%, p90 {pct(deltas, 0.9):+.1f}%, "
              f"max {max(deltas):+.1f}% | rows where the price would FALL (band edge crossed): {cross_down}")


if __name__ == "__main__":
    main()
