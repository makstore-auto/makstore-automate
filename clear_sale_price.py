"""Remove the SALE price from OnBuy listings (user 2026-10-10: "the sale must not be displayed - the selling price is the direct price").

Why: 7,777 of Makstore's and 12,415 of Arden's listings carry a sale price (sale_start 2026-10-05, sale_end 2026-10-10, typically 4-5% or ~20% under the normal
price) that our sync never sends - it only sends price / stock / boost. The dashboard shows it as "On Sale: GBP x". This reads each SKU's listing (one filtered
GET), and for the ones that have a sale price sends the SAME price and stock again together with an empty sale_price (and dates), then reads the listing back
to prove the sale is gone. How OnBuy wants "no sale" spelt is not documented to us, so the spellings in VARIANTS are tried in order, per SKU, until the read-back
shows no sale price. Never changes price or stock (the values just read are sent back).

Env: SKUS (comma-separated) or SKU_FILE (one per line), DRY_RUN (default 1 = only report), MAX_SKUS (default 20), VARIANT (a name from VARIANTS to force).
"""
import json
import os
import time

from onbuy_client import BASE_URL, OnBuyClient

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
MAX_SKUS = int(os.getenv("MAX_SKUS") or "20")
FORCE = (os.getenv("VARIANT") or "").strip()
WANT = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
if os.getenv("SKU_FILE"):
    with open(os.environ["SKU_FILE"], encoding="utf-8") as fh:
        WANT += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
WANT = list(dict.fromkeys(WANT))

# how a listing update can say "no sale price": (name, extra fields merged into the listing item)
VARIANTS = [
    ("sale_price null", {"sale_price": None}),
    ("sale_price 0", {"sale_price": 0}),
    ("sale_price empty + dates empty", {"sale_price": "", "sale_start_date": "", "sale_end_date": ""}),
    ("sale_price null + dates null", {"sale_price": None, "sale_start_date": None, "sale_end_date": None}),
    ("sale_price 0 + dates null", {"sale_price": 0, "sale_start_date": None, "sale_end_date": None}),
]


def has_sale(rec):
    v = (rec or {}).get("sale_price")
    try:
        return v is not None and str(v).strip() not in ("", "None", "0", "0.0", "0.00") and float(v) > 0
    except (TypeError, ValueError):
        return False


def read(onbuy, sku):
    r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"get listing {sku}",
                    params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
    items = (r.json().get("results") if r.status_code == 200 else None) or []
    hit = [i for i in items if str(i.get("sku") or "").strip() == sku]
    return hit[0] if hit else None


def show(rec):
    return json.dumps({k: rec.get(k) for k in ("price", "stock", "sale_price", "sale_start_date", "sale_end_date")}, default=str)


def main():
    if not WANT:
        raise SystemExit("SKUS or SKU_FILE required")
    if len(WANT) > MAX_SKUS:
        raise SystemExit(f"{len(WANT)} SKUs asked for, above MAX_SKUS={MAX_SKUS} - refusing (this tool is for a few listings; the bulk run is a separate step)")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    variants = [v for v in VARIANTS if not FORCE or v[0] == FORCE]
    for sku in WANT:
        rec = read(onbuy, sku)
        if rec is None:
            print(f"{sku}: no listing answered")
            continue
        print(f"{sku}: BEFORE {show(rec)}")
        if not has_sale(rec):
            print(f"{sku}: no sale price set - nothing to do")
            continue
        if DRY_RUN:
            print(f"{sku}: DRY RUN - would resend price {rec.get('price')} stock {rec.get('stock')} with the sale cleared")
            continue
        price, stock = rec.get("price"), rec.get("stock")
        done = False
        for name, extra in variants:
            item = {"sku": sku, "price": price, "stock": stock, "boost_marketing_commission": 0}
            item.update(extra)
            payload = {"site_id": onbuy.site_id, "seller_id": onbuy.seller_id, "listings": [item]}
            resp = onbuy._send("PUT", f"{BASE_URL}/listings/by-sku", what=f"clear sale {sku}", json=payload, timeout=60)
            print(f"{sku}: variant '{name}' -> HTTP {resp.status_code} {resp.text[:300]}")
            time.sleep(4)
            after = read(onbuy, sku)
            print(f"{sku}: AFTER  {show(after) if after else 'listing not readable'}")
            if after is not None and not has_sale(after) and str(after.get("price")) == str(price) and after.get("stock") == stock:
                print(f"{sku}: CLEARED with variant '{name}' (price and stock unchanged)")
                done = True
                break
            if after is not None and (str(after.get("price")) != str(price) or after.get("stock") != stock):
                print(f"{sku}: WARNING price/stock changed - stopping on this SKU")
                break
        if not done:
            print(f"{sku}: NOT cleared by any variant")


if __name__ == "__main__":
    main()
