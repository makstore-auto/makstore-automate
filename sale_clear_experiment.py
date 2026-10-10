"""EXPERIMENT (2026-10-10): which spelling in a listing update makes OnBuy drop a SALE price - and how long does it take?

First live finding (Makstore 0000089208441-zzz-40): five spellings tried one after the other, each read back 4 seconds later, all looked unchanged;
a read 2-3 minutes later showed sale_price / sale_start_date / sale_end_date all null. So OnBuy applies a listing update with a delay and the
4-second read-backs said nothing. This sends ONE update with a different spelling per SKU (PLAN = "sku=variant,sku=variant,..."), then reads every
SKU back at 30 / 60 / 90 / 120 / 180 / 240 / 300 s and prints what each one looks like, so the spelling that works (and the delay) is known.

Touches only the sale fields of the named listings (price / stock are sent back exactly as just read, except in the "min" variants that send no price / stock at all).

Env: PLAN (sku=variant pairs), POLLS (seconds, comma-separated), DRY_RUN (default 1).
"""
import json
import os
import time

from onbuy_client import BASE_URL, OnBuyClient

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
POLLS = [int(x) for x in (os.getenv("POLLS") or "30,60,90,120,180,240,300").split(",") if x.strip()]
PAST = {"sale_start_date": "2026-09-01 00:00:00", "sale_end_date": "2026-09-02 00:00:00"}

# variant key -> (send price/stock too?, function(rec) -> sale fields)
VARIANTS = {
    "ctrl": (True, lambda rec: {}),                                                     # price + stock only: does a plain update touch the sale?
    "null": (True, lambda rec: {"sale_price": None}),
    "null_dates": (True, lambda rec: {"sale_price": None, "sale_start_date": None, "sale_end_date": None}),
    "dates_null": (True, lambda rec: {"sale_start_date": None, "sale_end_date": None}),
    "past": (True, lambda rec: {"sale_price": rec.get("sale_price"), **PAST}),          # sale price kept, window in the past
    "past_min": (False, lambda rec: {"sale_price": rec.get("sale_price"), **PAST}),     # same, no price / stock in the update
    "end_eq_start": (True, lambda rec: {"sale_end_date": rec.get("sale_start_date")}),
    "price_eq": (True, lambda rec: {"sale_price": rec.get("price"), "sale_start_date": rec.get("sale_start_date"), "sale_end_date": rec.get("sale_end_date")}),
}


def read(onbuy, sku):
    r = onbuy._send("GET", f"{BASE_URL}/listings", what=f"get listing {sku}",
                    params={"site_id": onbuy.site_id, "limit": 5, "offset": 0, "filter[sku]": sku}, timeout=60)
    items = (r.json().get("results") if r.status_code == 200 else None) or []
    hit = [i for i in items if str(i.get("sku") or "").strip() == sku]
    return hit[0] if hit else None


def show(rec):
    if rec is None:
        return "not readable"
    return json.dumps({k: rec.get(k) for k in ("price", "stock", "sale_price", "sale_start_date", "sale_end_date", "updated_at")}, default=str)


def main():
    plan = []
    for part in (os.getenv("PLAN") or "").replace("\n", ",").split(","):
        if "=" in part:
            sku, var = part.strip().rsplit("=", 1)
            if sku and var in VARIANTS:
                plan.append((sku.strip(), var.strip()))
            else:
                print(f"ignored plan item {part!r} (variants: {sorted(VARIANTS)})")
    if not plan:
        raise SystemExit("PLAN required, e.g. 123=past,456=null_dates")
    if len(plan) > 12:
        raise SystemExit("at most 12 listings for an experiment")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    before, items = {}, []
    for sku, var in plan:
        rec = read(onbuy, sku)
        before[sku] = rec
        print(f"{sku} [{var}] BEFORE {show(rec)}", flush=True)
        if rec is None:
            continue
        with_ps, make = VARIANTS[var]
        item = {"sku": sku}
        if with_ps:
            item.update({"price": rec.get("price"), "stock": rec.get("stock")})
        item["boost_marketing_commission"] = 0
        item.update(make(rec))
        items.append(item)
        print(f"{sku} [{var}] will send {json.dumps(item, default=str)}", flush=True)
    if DRY_RUN:
        print("DRY RUN - nothing sent")
        return
    payload = {"site_id": onbuy.site_id, "seller_id": onbuy.seller_id, "listings": items}
    t0 = time.time()
    resp = onbuy._send("PUT", f"{BASE_URL}/listings/by-sku", what="sale clear experiment", json=payload, timeout=120)
    print(f"PUT -> HTTP {resp.status_code} {resp.text[:1500]}", flush=True)
    for at in POLLS:
        wait = t0 + at - time.time()
        if wait > 0:
            time.sleep(wait)
        print(f"--- poll at {int(time.time() - t0)} s", flush=True)
        for sku, var in plan:
            if before.get(sku) is None:
                continue
            print(f"{sku} [{var}] {show(read(onbuy, sku))}", flush=True)


if __name__ == "__main__":
    main()
