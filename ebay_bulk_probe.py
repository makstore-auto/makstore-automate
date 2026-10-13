"""READ-ONLY probe (2026-10-13): can this application fetch eBay items in BULK (Browse API getItems, up to 20 ids per call), and what does one call cost against
the daily Browse allowance? Today every sheet row costs one getItemByLegacyId call, so the 4,800-call budget refreshes the eBay tab only every ~27 h on a
7,500-row store. If one bulk call answers 20 items for the price of one, the refresh capacity grows twentyfold.

Prints: the remaining allowance before and after one bulk call and after one single call (= the cost of each), the HTTP status and warnings eBay returns,
the fields a bulk item carries compared with the single-item answer (price, availability, shipping options, description, images, aspects, end date), one line
per item. Env: ITEM_IDS (comma-separated legacy item ids; default = the first N in-stock eBay rows of the sheet), N (10). Writes nothing anywhere."""
import json
import os
import time

import gspread
import requests
from oauth2client.service_account import ServiceAccountCredentials

import generate_xml as gx
import sheet_tabs

BROWSE = "https://api.ebay.com/buy/browse/v1"
SHEET_NAME = os.getenv("SHEET_NAME") or "Makstore_Full_Feed_Master"
N = int(os.getenv("N") or "10")
WANT = [s.strip() for s in (os.getenv("ITEM_IDS") or "").split(",") if s.strip()]


def headers(token):
    return {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"}


def ids_from_sheet(n):
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    ws = sheet_tabs.product_sheet(gspread.authorize(creds).open(SHEET_NAME))
    values = ws.get_all_values()
    h = [str(x).strip() for x in values[0]]
    i_u, i_s = h.index("Supplier URL"), h.index("Stock")
    out = []
    for r in values[1:]:
        url = r[i_u] if i_u < len(r) else ""
        stock = r[i_s] if i_s < len(r) else ""
        if "/itm/" in url and stock.strip() not in ("", "0"):
            out.append(url.split("/itm/", 1)[1].split("?", 1)[0].split("/", 1)[0].strip())
        if len(out) >= n:
            break
    return out


def remaining(token):
    for _ in range(3):
        left = gx.ebay_calls_remaining(token)
        if left is not None:
            return left
        time.sleep(3)
    return None


def describe(item):
    item = item or {}
    est = (item.get("estimatedAvailabilities") or [{}])[0] if item.get("estimatedAvailabilities") else {}
    return (f"legacy {item.get('legacyItemId')} | {str(item.get('title') or '')[:40]!r} | price {(item.get('price') or {}).get('value')} "
            f"{(item.get('price') or {}).get('currency')} | avail {est.get('estimatedAvailabilityStatus')} qty {est.get('estimatedAvailableQuantity')} "
            f"thr {est.get('availabilityThresholdType')}/{est.get('estimatedAvailabilityThreshold')} | shipping options {len(item.get('shippingOptions') or [])} | "
            f"description {len(str(item.get('description') or ''))} chars | image {bool(item.get('image'))} +{len(item.get('additionalImages') or [])} | "
            f"aspects {len(item.get('localizedAspects') or [])} | end {str(item.get('itemEndDate') or '')[:10]} | condition {item.get('condition')}")


def main():
    token = gx.get_ebay_token()
    if not token:
        raise SystemExit("no eBay token")
    ids = WANT or ids_from_sheet(N)
    print(f"items: {len(ids)} -> {ids}")
    r0 = remaining(token)
    print(f"remaining before: {r0}")

    t = time.time()
    resp = requests.get(f"{BROWSE}/item", headers=headers(token), params={"item_ids": ",".join(f"v1|{i}|0" for i in ids)}, timeout=60)
    print(f"BULK getItems({len(ids)}): HTTP {resp.status_code} in {time.time() - t:.1f}s | body keys {list(resp.json().keys()) if resp.headers.get('content-type', '').startswith('application/json') else resp.text[:300]}")
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    items = body.get("items") or []
    for w in (body.get("warnings") or [])[:10]:
        print(f"  warning: {json.dumps(w)[:240]}")
    for e in (body.get("errors") or [])[:10]:
        print(f"  error: {json.dumps(e)[:240]}")
    print(f"  {len(items)} item(s) answered")
    for it in items:
        print("  " + describe(it))
    time.sleep(5)
    r1 = remaining(token)
    print(f"remaining after the bulk call: {r1} (cost {r0 - r1 if r0 is not None and r1 is not None else '?'})")

    t = time.time()
    single = requests.get(f"{BROWSE}/item/get_item_by_legacy_id", headers=headers(token), params={"legacy_item_id": ids[0]}, timeout=60)
    print(f"SINGLE getItemByLegacyId({ids[0]}): HTTP {single.status_code} in {time.time() - t:.1f}s")
    sitem = single.json() if single.status_code == 200 else {}
    if sitem:
        print("  " + describe(sitem))
    time.sleep(5)
    r2 = remaining(token)
    print(f"remaining after the single call: {r2} (cost {r1 - r2 if r1 is not None and r2 is not None else '?'})")

    bitem = next((i for i in items if str(i.get("legacyItemId")) == str(ids[0])), items[0] if items else {})
    if sitem and bitem:
        only_single = sorted(set(sitem) - set(bitem))
        only_bulk = sorted(set(bitem) - set(sitem))
        print(f"fields only in the single answer: {only_single}")
        print(f"fields only in the bulk answer: {only_bulk}")
        for k in ("price", "estimatedAvailabilities", "shippingOptions", "itemEndDate", "condition"):
            print(f"  {k}: same={json.dumps(sitem.get(k), sort_keys=True) == json.dumps(bitem.get(k), sort_keys=True)}")
        print(f"  description: single {len(str(sitem.get('description') or ''))} chars, bulk {len(str(bitem.get('description') or ''))} chars")
    print("VERDICT: " + ("bulk works and costs ONE call per 20 items" if r0 is not None and r1 is not None and resp.status_code == 200 and (r0 - r1) <= 1 and len(items) >= min(len(ids), 2)
                         else "see the lines above (bulk refused, or it costs one call per item, or the allowance could not be read)"))


if __name__ == "__main__":
    main()
