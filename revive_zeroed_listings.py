"""RECOVERY (2026-10-01): reverses the 2026-09-30 19:15 UTC mass stock-zeroing
by the deletion reconciler.

What happened: "OnBuy Categories" was moved ahead of "Sheet1", the
reconciler (and the eBay sync / Buy Box Defense) read the FIRST tab by
POSITION, saw almost no eBay-tab SKUs, concluded ~5,000 live listings had
been deleted from the sheet, zeroed their stock (= "inactive" on the OnBuy
dashboard) and queued them on the "Delist Queue" tab for DELETION after a
24-hour grace window. The tab order has been restored; this script brings
the listings back:

1. Reads the product tabs BY NAME ("Sheet1" + "Amazon" - never by
   position) and the Delist Queue.
2. For every queued SKU whose stock was zeroed and which is on a product
   tab with Status ACTIVE, stock > 0 and a selling price: pushes the
   sheet's price + stock to OnBuy (500 per call, the same endpoint the
   reconciler zeroed them with). Rows the sheet says are inactive/out of
   stock stay at 0 - that is the sheet's truth.
3. Verifies with a fresh listings sweep.
4. Clears the pardonable entries (queued SKUs that ARE on a product tab)
   from the Delist Queue in ONE bulk delete - the reconciler's own pardon
   step deletes one row per API call, and 5,000 sequential Sheets writes
   would stall the whole hourly backfill. Entries whose SKU is genuinely
   not on the sheet stay queued.

DRY_RUN (default on) prints the plan and writes the results CSV without
touching OnBuy or the sheet.
"""
import csv
import json
import logging
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import RateLimitError, with_retry

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("revive")

SHEET_NAME = os.getenv("SHEET_NAME") or "Makstore_Full_Feed_Master"
PRODUCT_TABS = ("Sheet1", "Amazon")
QUEUE_TAB = "Delist Queue"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
CHUNK = 500
RESULTS_CSV = "revive_results.csv"


def zeroless(s):
    return s.lstrip("0") or "0"


def to_float(v):
    try:
        return float(str(v).replace("£", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def read_product_tabs(book):
    """{sku: {price, stock, status, sync, tab}} from the product tabs, read
    by NAME. SKU text is the displayed text (leading zeros survive)."""
    out = {}
    for name in PRODUCT_TABS:
        ws = book.worksheet(name)
        values = with_retry(ws.get_all_values, what=f"read {name}", max_attempts=3)
        headers = [str(h).strip() for h in values[0]] if values else []
        for need in ("SKU", "Selling Price (£)", "Stock", "Status"):
            if need not in headers:
                raise SystemExit(f"ABORT: tab {name!r} has no {need!r} column - headers {headers[:8]}")
        ix = {h: headers.index(h) for h in ("SKU", "Selling Price (£)", "Stock", "Status")}
        ix_sync = headers.index("Sync Status") if "Sync Status" in headers else None
        n = 0
        for r in values[1:]:
            def c(i):
                return str(r[i]).strip() if i is not None and i < len(r) else ""
            sku = c(ix["SKU"]).replace(",", "")
            if not sku:
                continue
            n += 1
            out[sku] = {"price": to_float(c(ix["Selling Price (£)"])),
                        "stock": int(to_float(c(ix["Stock"]))),
                        "status": c(ix["Status"]).upper(),
                        "sync": c(ix_sync),
                        "tab": name}
        log.info("tab %s: %d rows with a SKU", name, n)
        if name == "Sheet1" and n < 1000:
            raise SystemExit(f"ABORT: Sheet1 has only {n} SKU rows - not trusting this read")
    return out


def read_queue(book):
    tab = book.worksheet(QUEUE_TAB)
    rows = with_retry(tab.get_all_values, what="read queue", max_attempts=3)
    entries = []
    for idx, r in enumerate(rows[1:], start=2):
        r = list(r) + [""] * (5 - len(r))
        sku = str(r[0]).strip()
        if sku:
            entries.append({"row": idx, "sku": sku, "first": r[1], "zeroed": r[2],
                            "deleted": r[3], "note": r[4]})
    return tab, entries


def sweep_listings(onbuy):
    """{sku: (price, stock)} - a fresh full sweep (verification only)."""
    out, offset, limit = {}, 0, 100
    while True:
        def _page(off=offset):
            for _try in range(6):
                r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                                params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                                timeout=60)
                if r.status_code in (429, 500, 502, 503) and _try < 5:
                    log.info("listings page HTTP %s - waiting 60s", r.status_code)
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            sku = str(it.get("sku") or "").strip()
            if sku:
                out[sku] = (to_float(it.get("price")), int(to_float(it.get("stock"))))
        if len(items) < limit:
            break
        offset += limit
    return out


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    log.info("tab order now: %s", [t.title for t in book.worksheets()])

    sheet = read_product_tabs(book)
    sheet_zl = {}
    for sku, d in sheet.items():
        sheet_zl.setdefault(zeroless(sku), (sku, d))
    queue_tab, entries = read_queue(book)
    log.info("delist queue: %d entries (%d with stock zeroed, %d already deleted)",
             len(entries), sum(1 for e in entries if e["zeroed"]), sum(1 for e in entries if e["deleted"]))

    plan, skipped = [], {"not_on_sheet": [], "sheet_inactive": [], "no_price": []}
    pardonable_rows = []
    failed_flagged = 0
    for e in entries:
        hit = sheet.get(e["sku"])
        if hit is None:
            z = sheet_zl.get(zeroless(e["sku"]))
            hit = z[1] if z else None
        if hit is None:
            skipped["not_on_sheet"].append(e["sku"])
            continue
        if not e["deleted"]:
            pardonable_rows.append(e["row"])
        if not e["zeroed"]:
            continue
        if hit["status"] != "ACTIVE" or hit["stock"] <= 0:
            skipped["sheet_inactive"].append(e["sku"])
            continue
        if hit["price"] <= 0:
            skipped["no_price"].append(e["sku"])
            continue
        if hit["sync"].startswith("Failed"):
            failed_flagged += 1
        plan.append((e["sku"], hit["price"], hit["stock"], hit["tab"]))

    by_tab = {}
    for _s, _p, _st, t in plan:
        by_tab[t] = by_tab.get(t, 0) + 1
    log.info("PLAN: restore %d listings %s | skip: %d not on sheet, %d sheet says inactive/OOS, %d no price"
             " | %d of the restores carry a 'Failed:' Sync Status | %d queue rows pardonable",
             len(plan), by_tab, len(skipped["not_on_sheet"]), len(skipped["sheet_inactive"]),
             len(skipped["no_price"]), failed_flagged, len(pardonable_rows))
    for s in plan[:8]:
        log.info("  sample: %s -> price %.2f stock %d (%s)", *s)

    results = []  # (sku, outcome)
    if DRY_RUN:
        log.info("DRY RUN - OnBuy and the sheet were not touched")
        for s, p, st, t in plan:
            results.append((s, "planned", p, st, t))
    else:
        onbuy = OnBuyClient()
        if not onbuy.authenticate():
            raise SystemExit("OnBuy auth failed")
        ok = bad = 0
        errors = {}
        i = 0
        while i < len(plan):
            chunk = plan[i:i + CHUNK]
            try:
                resp = onbuy.update_listings_by_sku_batch([(s, p, st) for s, p, st, _t in chunk])
            except RateLimitError:
                log.warning("burst limit at %d/%d - waiting 90s", i, len(plan))
                time.sleep(90)
                continue
            errs = {str((it or {}).get("sku") or "").strip(): str((it or {}).get("error") or "").strip()
                    for it in resp or []}
            for s, p, st, t in chunk:
                err = errs.get(s, "no answer")
                if s in errs and not err:
                    ok += 1
                    results.append((s, "restored", p, st, t))
                else:
                    bad += 1
                    errors[err] = errors.get(err, 0) + 1
                    results.append((s, f"failed: {err}", p, st, t))
            log.info("restored %d / %d so far (%d failed)", i + len(chunk), len(plan), bad)
            i += CHUNK
            time.sleep(2.0)
        log.info("RESTORE DONE: %d restored, %d failed %s", ok, bad, errors)

        # Verification: a fresh sweep, then count what is still at stock 0.
        time.sleep(20)
        try:
            live = sweep_listings(onbuy)
            planned = {s for s, *_ in plan}
            zero_now = sum(1 for s in planned if s in live and live[s][1] <= 0)
            total_zero = sum(1 for _p, st in live.values() if st <= 0)
            log.info("VERIFY: %d live listings | %d of the restore set still at stock 0 | %d live listings at stock 0 overall",
                     len(live), zero_now, total_zero)
        except Exception as exc:  # noqa: BLE001 - verification must not mask the result
            log.warning("verification sweep failed: %s", str(exc)[:200])

        # Bulk-clear the pardonable queue rows in ONE request.
        if bad == 0 or ok > 0:
            rows = sorted(set(pardonable_rows), reverse=True)
            ranges, start = [], None
            for r in rows:  # descending; merge contiguous runs
                if start is None:
                    start = end = r
                elif r == end - 1:
                    end = r
                else:
                    ranges.append((end, start))
                    start = end = r
            if start is not None:
                ranges.append((end, start))
            reqs = [{"deleteDimension": {"range": {"sheetId": queue_tab.id, "dimension": "ROWS",
                                                   "startIndex": lo - 1, "endIndex": hi}}}
                    for lo, hi in ranges]  # already descending
            if reqs:
                with_retry(lambda: queue_tab.spreadsheet.batch_update({"requests": reqs}),
                           what="bulk clear queue", max_attempts=3)
            _t, left = read_queue(book)
            log.info("QUEUE CLEARED: removed %d pardonable rows in %d range(s); %d entries remain (not on the sheet)",
                     len(rows), len(reqs), len(left))

    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sku", "outcome", "price", "stock", "tab"])
        for r in results:
            w.writerow(r)
        for s in skipped["sheet_inactive"]:
            w.writerow([s, "skipped: sheet says inactive/out of stock", "", "", ""])
        for s in skipped["no_price"]:
            w.writerow([s, "skipped: no selling price on the sheet", "", "", ""])
        for s in skipped["not_on_sheet"]:
            w.writerow([s, "skipped: not on a product tab (stays queued)", "", "", ""])
    log.info("wrote %s", RESULTS_CSV)


if __name__ == "__main__":
    main()
