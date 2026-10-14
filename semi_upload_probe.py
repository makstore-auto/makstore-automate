"""READ-ONLY probe (user 2026-10-14: "Uploaded, run the move now"): has OnBuy created the semi store's uploaded (AliExpress) products yet, and does any of
them still sit off the AliExpress template or carry the upload CSV's own handling time? A handful of GET calls, never a sweep:
  1. the semi sheet's SKUs = the uploaded set (rows ticked "Exported to OnBuy" when any are; every row otherwise)
  2. GET /v2/listings limit=1 -> total_rows, then both ends of the list (offset 0 and offset total-TAIL): new listings appear at one end - the semi SKUs
     found at either end are checked
  3. SAMPLE random semi SKUs looked up one by one (filter[sku])
Prints what it saw and writes found=true|false, live=<semi listings seen>, total_rows=<n> to GITHUB_OUTPUT when that is set.
Env: SEMI_SHEET_NAME (Makstore_Feed_Master), TEMPLATE_ALI (43190), SAMPLE (5), TAIL (100), GOOGLE_CREDENTIALS + the OnBuy keys."""
import json
import os
import random

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import listing_sweep
from delivery_templates import own_handling
from onbuy_client import OnBuyClient
from retry_utils import with_retry

SEMI_SHEET_NAME = (os.getenv("SEMI_SHEET_NAME") or "Makstore_Feed_Master").strip()
TEMPLATE_ALI = (os.getenv("TEMPLATE_ALI") or "43190").strip()
SAMPLE = int(os.getenv("SAMPLE") or "5")
TAIL = int(os.getenv("TAIL") or "100")


def exported_skus(values):
    """The semi sheet's uploaded SKUs: rows ticked 'Exported to OnBuy' when any are ticked, every SKU otherwise (sheet order kept)."""
    if not values:
        return []
    h = [str(x).strip() for x in values[0]]
    if "SKU" not in h:
        raise ValueError("semi sheet has no SKU column")
    i_s = h.index("SKU")
    i_e = h.index("Exported to OnBuy") if "Exported to OnBuy" in h else None
    all_skus, ticked, seen = [], [], set()
    for r in values[1:]:
        sku = str(r[i_s]).replace(",", "").strip() if i_s < len(r) else ""
        if not sku or sku in seen:
            continue
        seen.add(sku)
        all_skus.append(sku)
        if i_e is not None and i_e < len(r) and str(r[i_e]).strip().upper() in ("TRUE", "YES", "1", "DONE"):
            ticked.append(sku)
    return ticked or all_skus


def needs_move(listing, template_ali=TEMPLATE_ALI):
    """True when this (semi-sourced) listing is off the AliExpress template or carries its own handling time."""
    it = listing or {}
    return str(it.get("delivery_template_id") or "").strip() != str(template_ali) or own_handling(it) != 0


def hits(items, semi, template_ali=TEMPLATE_ALI):
    """-> (semi listings among items, those of them that need the move) as SKU lists, in order, without repeats."""
    seen, need = [], []
    for it in items or []:
        sku = str((it or {}).get("sku") or "").strip()
        if sku in semi and sku not in seen:
            seen.append(sku)
            if needs_move(it, template_ali):
                need.append(sku)
    return seen, need


def newest_end(head, tail):
    """'head' | 'tail' | '?': which end of the listing order carries the newest created_at."""
    def newest(items):
        return max((str((it or {}).get("created_at") or "") for it in items or []), default="")
    a, b = newest(head), newest(tail)
    if not a and not b:
        return "?"
    return "head" if a > b else "tail"


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SEMI_SHEET_NAME), what="semi sheet open", max_attempts=3)
    values = with_retry(book.sheet1.get_all_values, what="read semi sheet", max_attempts=3)
    skus = exported_skus(values)
    semi = set(skus)
    print(f"semi sheet {SEMI_SHEET_NAME!r}: {len(values) - 1 if values else 0} rows, {len(skus)} uploaded SKUs to watch | AliExpress template {TEMPLATE_ALI}")
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    _one, body = listing_sweep.fetch_page(onbuy, 0, 1)
    total = int(((body or {}).get("metadata") or {}).get("total_rows") or 0)
    head, _ = listing_sweep.fetch_page(onbuy, 0, TAIL)
    tail, _ = listing_sweep.fetch_page(onbuy, max(0, total - TAIL), TAIL) if total > TAIL else ([], None)
    end = newest_end(head, tail)
    seen_h, need_h = hits(head, semi)
    seen_t, need_t = hits(tail, semi)
    print(f"listings on OnBuy: total_rows {total} | newest listings sit at the {end} of the list | semi listings among the first {len(head)}: {len(seen_h)} "
          f"({len(need_h)} need the move), among the last {len(tail)}: {len(seen_t)} ({len(need_t)} need the move)")
    sample = random.sample(skus, min(SAMPLE, len(skus))) if skus else []
    found_s, need_s = [], []
    for sku in sample:
        it = onbuy.get_listing(sku)
        if it:
            found_s.append(sku)
            if needs_move(it):
                need_s.append(sku)
            print(f"  {sku}: listing exists | template {it.get('delivery_template_id')} handling {it.get('handling_time')} created {str(it.get('created_at') or '')[:16]}"
                  + (" -> needs the move" if needs_move(it) else " -> already right"))
        else:
            print(f"  {sku}: no listing yet")
    live = len(set(seen_h) | set(seen_t) | set(found_s))
    need = len(set(need_h) | set(need_t) | set(need_s))
    found = need > 0
    print(f"RESULT: {live} uploaded listing(s) seen, {need} of them off the AliExpress template or with their own handling time -> "
          + ("RUN THE MOVE" if found else "nothing to move" + ("" if live else " (OnBuy has not created the uploaded products yet)")))
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"found={'true' if found else 'false'}\nlive={live}\ntotal_rows={total}\n")


if __name__ == "__main__":
    main()
