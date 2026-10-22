"""READ-ONLY (2026-10-22): does any OnBuy API read expose the CATEGORY of the product a listing sits on?
Why: OnBuy charged 15% commission on two Amazon-tab 3D printers (orders T6QR55D / T6Q6FJQ) whose sheet category (Printers ->
Consumer Electronics tier) says 7%: the listing sits on OnBuy's own catalogue product, whose category decides the commission, and
GET /v2/listings does not return that category. For each SKU this reads the listing (opc, product codes, product_url) and then tries
the product reads the docs hint at (GET /v2/products/{opc}, GET /v2/products with filter[opc] / filter[product_codes] / filter[query])
and prints, per call: the HTTP status, the top-level keys, the first result's keys and EVERY key whose name contains category / tier /
commission with its value. These endpoints carry no buyer data. Env: SKUS (comma-separated)."""
import json
import os
import re
import time

from onbuy_client import BASE_URL, OnBuyClient

SKUS = [s.strip() for s in (os.getenv("SKUS") or "").split(",") if s.strip()]
CAT = re.compile(r"categor|tier|commission", re.I)


def walk(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                yield from walk(v, path)
            else:
                yield path, v
    elif isinstance(node, list):
        for i, v in enumerate(node[:3]):
            yield from walk(v, f"{prefix}[{i}]")


def show(label, resp):
    print(f"\n-- {label}: HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print("   non-JSON body:", resp.text[:200].replace("\n", " "))
        return None
    if isinstance(body, dict):
        print("   top-level keys:", sorted(body.keys()))
        results = body.get("results")
        if isinstance(results, list):
            print(f"   results: {len(results)}")
            if results and isinstance(results[0], dict):
                print("   first result keys:", sorted(results[0].keys()))
        elif resp.status_code != 200:
            print("   body:", json.dumps(body)[:300])
    elif isinstance(body, list):
        print(f"   list of {len(body)}; first keys: {sorted(body[0].keys()) if body and isinstance(body[0], dict) else body[:1]}")
    hits = [(path, v) for path, v in walk(body) if CAT.search(path)]
    for path, v in hits[:30]:
        print(f"   {path} = {v!r}"[:200])
    if not hits and resp.status_code == 200:
        print("   (no category / tier / commission key anywhere in the response)")
    return body


def main():
    client = OnBuyClient()
    if not client.authenticate():
        raise SystemExit("OnBuy auth failed")
    site = client.site_id
    for sku in SKUS:
        print(f"\n===== SKU {sku}")
        resp = client._send("GET", f"{BASE_URL}/listings", what="listing", params={"site_id": site, "filter[sku]": sku, "limit": 5}, timeout=60)
        body = show("GET /v2/listings?filter[sku]", resp) or {}
        opc, codes, name = "", [], ""
        for it in (body.get("results") or []) if isinstance(body, dict) else []:
            if str(it.get("sku")).strip() == sku:
                opc = it.get("opc") or it.get("product_encoded_id") or ""
                codes = [c for c in (it.get("product_codes") or []) if c]
                name = it.get("name") or ""
        print(f"   opc {opc!r} | product codes {codes} | name {name[:70]!r}")
        code = codes[0] if codes else ""
        attempts = [
            ("GET /v2/products/{opc}", f"{BASE_URL}/products/{opc}", {"site_id": site}),
            ("GET /v2/products?filter[opc]", f"{BASE_URL}/products", {"site_id": site, "filter[opc]": opc, "limit": 5}),
            ("GET /v2/products?opc=", f"{BASE_URL}/products", {"site_id": site, "opc": opc, "limit": 5}),
            ("GET /v2/products?filter[product_codes]", f"{BASE_URL}/products", {"site_id": site, "filter[product_codes]": code, "limit": 5}),
            ("GET /v2/products?filter[product_code]", f"{BASE_URL}/products", {"site_id": site, "filter[product_code]": code, "limit": 5}),
            ("GET /v2/products?filter[query]=<code>", f"{BASE_URL}/products", {"site_id": site, "filter[query]": code, "limit": 5}),
            ("GET /v2/products?filter[field]=product_codes&filter[query]=<code>", f"{BASE_URL}/products",
             {"site_id": site, "filter[field]": "product_codes", "filter[query]": code, "limit": 5}),
            ("GET /v2/products?filter[query]=<name>", f"{BASE_URL}/products", {"site_id": site, "filter[query]": name[:60], "limit": 5}),
            ("GET /v2/products?filter[sku]", f"{BASE_URL}/products", {"site_id": site, "filter[sku]": sku, "limit": 5}),
            ("GET /v2/listings?filter[sku]&fields=category", f"{BASE_URL}/listings", {"site_id": site, "filter[sku]": sku, "fields": "category_id,category", "limit": 5}),
        ]
        for label, url, params in attempts:
            if not opc and "{opc}" in label:
                continue
            resp = client._send("GET", url, what=label, params=params, timeout=60)
            if resp.status_code == 429:
                print(f"-- {label}: rate limited, waiting 60 s")
                time.sleep(60)
                resp = client._send("GET", url, what=label, params=params, timeout=60)
            show(label, resp)
            time.sleep(1.5)


if __name__ == "__main__":
    main()
