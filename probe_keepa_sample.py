"""READ-ONLY (2026-10-10): how often is an Amazon-tab price a DISCOUNT (deal / voucher / sale) and how big is it?

Takes an even sample of the Amazon tab's rows (SAMPLE_N, default 100), fetches them through Keepa (about 1-2 tokens per ASIN) and prints
AGGREGATES only - no SKU, no ASIN, no cost: how many of the sampled products have a Lightning Deal / Prime-Exclusive price, a coupon or
promotion, and how far the price our rule picks sits below the 30 / 90-day average of the same price type and below the list price.
It answers "would pricing from the regular price instead of the discounted one change many rows, and by how much?". Writes nothing.

Env: SAMPLE_N, SHEET_TAB (default Amazon), KEEPA_API_KEY, GOOGLE_CREDENTIALS.
"""
import json
import os
import statistics

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import keepa_client
from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "Makstore_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()
SAMPLE_N = int(os.getenv("SAMPLE_N") or "100")
IDX = {"AMAZON": 0, "NEW": 1, "LIST": 4, "LIGHTNING": 8, "PRIME_EXCL": 30}


def pct(a, b):
    return None if not b else (a / b - 1.0) * 100.0


def band(values, edges):
    out = {}
    lo = None
    for e in edges + [None]:
        n = sum(1 for v in values if (lo is None or v >= lo) and (e is None or v < e))
        out[f"{'' if lo is None else lo}..{'' if e is None else e}"] = n
        lo = e
    return out


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    ws = book.worksheet(TAB)
    values = with_retry(ws.get_all_values, what=f"read {TAB}", max_attempts=3)
    headers = [str(h).strip() for h in values[0]]
    ix = {h: i for i, h in enumerate(headers) if h}
    asins = []
    for r in values[1:]:
        a = keepa_client.parse_asin(r[ix["Supplier URL"]] if "Supplier URL" in ix and ix["Supplier URL"] < len(r) else "")
        if a:
            asins.append(a)
    asins = list(dict.fromkeys(asins))
    step = max(1, len(asins) // SAMPLE_N)
    sample = asins[::step][:SAMPLE_N]
    print(f"{TAB}: {len(asins)} distinct ASINs on the tab | sample {len(sample)}")
    client = keepa_client.KeepaClient.from_env()
    products = client.fetch_products(sample)
    print(f"Keepa answered {len(products)} | tokens left {client.tokens_left}")
    n = deal = prime = coupon = promo = 0
    below30, below90, below_list, picks = [], [], [], 0
    reasons = {}
    for asin in sample:
        p = products.get(asin)
        if not p:
            continue
        price, seller, _avail, reason = keepa_client.choose_offer(p)
        reasons[reason] = reasons.get(reason, 0) + 1
        if price <= 0:
            continue
        n += 1
        stats = p.get("stats") or {}
        cur = stats.get("current") or []
        if len(cur) > IDX["LIGHTNING"] and isinstance(cur[IDX["LIGHTNING"]], int) and cur[IDX["LIGHTNING"]] > 0:
            deal += 1
        if len(cur) > IDX["PRIME_EXCL"] and isinstance(cur[IDX["PRIME_EXCL"]], int) and cur[IDX["PRIME_EXCL"]] > 0:
            prime += 1
        if p.get("coupon"):
            coupon += 1
        if p.get("promotions"):
            promo += 1
        key = IDX["AMAZON"] if reason == "amazon" else IDX["NEW"]
        for arr_name, bucket in (("avg30", below30), ("avg90", below90)):
            arr = stats.get(arr_name) or []
            v = arr[key] if key < len(arr) else None
            if isinstance(v, int) and v > 0:
                bucket.append(pct(price, v))
        lst = cur[IDX["LIST"]] if len(cur) > IDX["LIST"] else None
        if isinstance(lst, int) and lst > 0:
            below_list.append(pct(price, lst))
    print(f"priced products in the sample: {n} | offer the rule picks: {reasons}")
    print(f"Lightning Deal price present: {deal} | Prime-Exclusive price present: {prime} | coupon present: {coupon} | promotions present: {promo}")
    edges = [-50, -30, -20, -10, -5, -2, 2, 5, 10, 20]
    for name, vals in (("price vs 30-day average (%)", below30), ("price vs 90-day average (%)", below90), ("price vs Amazon list price (%)", below_list)):
        if vals:
            print(f"{name}: n={len(vals)} median {statistics.median(vals):+.1f} | share more than 5% below: {sum(1 for v in vals if v < -5) / len(vals):.0%}"
                  f" | more than 10% below: {sum(1 for v in vals if v < -10) / len(vals):.0%} | more than 20% below: {sum(1 for v in vals if v < -20) / len(vals):.0%}")
            print("   histogram:", band(vals, edges))
    # THE RULE IN FORCE (keepa_client.regular_price): max(current, lower of the 30/90-day averages), capped - what the sync now uses as the cost basis
    lifts = []
    for asin in sample:
        p = products.get(asin)
        if not p:
            continue
        price, _seller, _avail, reason = keepa_client.choose_offer(p)
        if price <= 0:
            continue
        usual = keepa_client.regular_price(p, price, reason)
        lifts.append((usual / price - 1.0) * 100.0)
    moved = [x for x in lifts if x > 0.0]
    print(f"regular_price() rule: {len(moved)} of {len(lifts)} sampled prices are lifted to the usual level "
          f"(median lift of those {statistics.median(moved) if moved else 0:.1f}%, p90 {sorted(moved)[int(len(moved) * 0.9)] if moved else 0:.1f}%, max {max(moved) if moved else 0:.1f}%)")
    print("   lift histogram:", band(moved, [1, 2, 5, 10, 20, 30]))


if __name__ == "__main__":
    main()
