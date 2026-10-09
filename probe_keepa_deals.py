"""READ-ONLY (2026-10-09): what does Keepa say about DEALS, VOUCHERS and the usual price of an ASIN?

The user sees Amazon deal / voucher discounts end up in our OnBuy prices. choose_offer() takes stats.current[AMAZON] when Amazon
is in stock, else stats.current[NEW] (the lowest new offer). This prints, per ASIN, the price that rule picks next to everything
Keepa knows about discounts: the list price, the Lightning-Deal and Prime-Exclusive prices, the 30/90/180-day averages, the
coupon fields, and the Buy Box. ASINs are public Amazon ids; no SKU, no cost and no margin is printed. Writes nothing.

Env: ASINS (comma-separated), KEEPA_API_KEY.
"""
import json
import os

import keepa_client

ASINS = [a.strip().upper() for a in (os.getenv("ASINS") or "").split(",") if a.strip()]
# Keepa "Price Type indexing" (csv / stats arrays)
NAMES = {0: "AMAZON", 1: "NEW", 4: "LIST", 7: "NEW_FBM", 8: "LIGHTNING_DEAL", 9: "WAREHOUSE", 10: "NEW_FBA", 18: "BUY_BOX_SHIPPING", 30: "PRIME_EXCL"}


def gbp(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "-"
    return "-" if v <= 0 else f"{v / 100:.2f}"


def arr(stats, key):
    a = stats.get(key)
    return a if isinstance(a, list) else []


def at(a, i):
    return a[i] if i < len(a) else None


def main():
    client = keepa_client.KeepaClient.from_env()
    products = client.fetch_products(ASINS)
    print(f"requested {len(ASINS)} | answered {len(products)} | tokens left {client.tokens_left}")
    for asin in ASINS:
        p = products.get(asin)
        if not p:
            print(f"\n{asin}: not known to Keepa")
            continue
        stats = p.get("stats") or {}
        cur = arr(stats, "current")
        chosen = keepa_client.choose_offer(p)
        print(f"\n{asin}: {str(p.get('title') or '')[:70]} | brand {p.get('brand')}")
        print(f"  OUR RULE PICKS: {gbp(chosen[0])} ({chosen[1]}; {chosen[3]}) | availabilityAmazon {p.get('availabilityAmazon')} | productType {p.get('productType')}")
        for label, key in (("current", "current"), ("avg30", "avg30"), ("avg90", "avg90"), ("avg180", "avg180")):
            a = arr(stats, key)
            print(f"  {label:8s} " + "  ".join(f"{NAMES[i]}={gbp(at(a, i))}" for i in (0, 1, 4, 8, 10, 30)))
        print(f"  buyBoxPrice={gbp(stats.get('buyBoxPrice'))} shipping={gbp(stats.get('buyBoxShipping'))} isAmazon={stats.get('buyBoxIsAmazon')} "
              f"isPrimeExcl={stats.get('buyBoxIsPrimeExclusive')} seller={str(stats.get('buyBoxSellerId'))[:14]}")
        print(f"  min(NEW)={gbp((at(arr(stats, 'min'), 1) or [None, None])[1] if isinstance(at(arr(stats, 'min'), 1), list) else None)} "
              f"max(NEW)={gbp((at(arr(stats, 'max'), 1) or [None, None])[1] if isinstance(at(arr(stats, 'max'), 1), list) else None)}")
        print(f"  coupon={json.dumps(p.get('coupon'))} | promotions={json.dumps(p.get('promotions'))[:200]} | "
              f"top-level keys with 'coupon'/'deal'/'promo': {[k for k in p if any(w in k.lower() for w in ('coupon', 'deal', 'promo', 'sale'))]}")
        print(f"  all stats keys: {sorted(stats.keys())[:60]}")


if __name__ == "__main__":
    main()
