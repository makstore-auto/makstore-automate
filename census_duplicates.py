"""One-off, READ-ONLY (2026-08-17): duplicate census for the account.
The user sees duplicate products on the dashboard for SKUs our API only
ever updated (e.g. 134502521), with no feed import configured. Page every
live listing and report: SKUs appearing on more than one listing, and
groups of listings whose names are near-identical under different SKUs.
Changes nothing."""
import logging
import os
import re
from collections import defaultdict

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    items = []
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        page = body.get("results") if isinstance(body, dict) else body
        if not isinstance(page, list) or not page:
            break
        for it in page:
            it = it or {}
            items.append((str(it.get("sku") or "").strip(),
                          str(it.get("name") or "").strip(),
                          str(it.get("product_listing_id") or ""),
                          str(it.get("opc") or ""),
                          str(it.get("price") or ""),
                          str(it.get("created_at") or "")))
        if len(page) < limit:
            break
        offset += limit
    log.info("live listings: %d", len(items))

    bare = [s.strip() for s in (os.getenv("TWIN_SKUS") or "").split(",") if s.strip()]
    if bare:
        # For each bare SKU, print every listing whose SKU starts with those
        # digits - reveals the decorated original next to the bare duplicate.
        for b in bare:
            hits = [t for t in items if t[0] == b or t[0].startswith(b + "-")]
            for t in hits:
                log.info("TWIN|%s|sku=%s|listing=%s|opc=%s|created=%s|%s",
                         b, t[0], t[2], t[3], t[5], t[1][:55])
            if not hits:
                log.info("TWIN|%s|NO LISTING AT ALL", b)

    by_sku = defaultdict(list)
    for t in items:
        if t[0]:
            by_sku[t[0]].append(t)
    dup_skus = {k: v for k, v in by_sku.items() if len(v) > 1}
    log.info("SKUs on MULTIPLE listings: %d", len(dup_skus))
    for sku, lst in sorted(dup_skus.items()):
        for t in lst:
            log.info("  DUPSKU|%s|listing=%s|opc=%s|price=%s|created=%s|%s",
                     sku, t[2], t[3], t[4], t[5], t[1][:60])

    by_name = defaultdict(list)
    for t in items:
        key = norm(t[1])[:80]
        if key:
            by_name[key].append(t)
    name_dups = {k: v for k, v in by_name.items()
                 if len(v) > 1 and len({t[0] for t in v}) > 1}
    log.info("identical names under DIFFERENT SKUs: %d group(s)", len(name_dups))
    for gi, (_, lst) in enumerate(sorted(name_dups.items()), 1):
        for t in lst:
            log.info("  DUPNAME|g%d|%s|listing=%s|opc=%s|price=%s|created=%s|%s",
                     gi, t[0], t[2], t[3], t[4], t[5], t[1][:70])


if __name__ == "__main__":
    main()
