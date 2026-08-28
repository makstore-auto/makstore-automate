"""Read-only (2026-08-28): the 86 "Awaiting OnBuy go-live" rows' SKUs match
NOTHING live (no exact, no barcode variant). If they are old products, the
live listings must carry different (dashboard-convention) SKUs - find them
by TITLE: exact normalized title match and, failing that, high-similarity
match against every live listing name. Reports per row so the adoption can
be decided on evidence."""
import difflib
import json
import os
import re
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

SHEET_NAME = "Makstore_Full_Feed_Master"
STATUS_PREFIX = "Awaiting OnBuy go-live"


def norm(t):
    return re.sub(r"\s+", " ", str(t or "").strip()).casefold()


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = [str(h).strip() for h in with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)]
    col = {h: i for i, h in enumerate(headers)}
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    disp = with_retry(lambda: sheet.col_values(col["SKU"] + 1), what="sku display col", max_attempts=3)

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = {}
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off}, timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page {offset}", max_attempts=4).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list) or not items:
            break
        for it in items:
            it = it or {}
            s = str(it.get("sku") or "").strip()
            n = str(it.get("name") or "").strip()
            if s and s not in live:
                live[s] = n
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    print(f"live listings: {len(live)}")
    by_title = {}
    for s, n in live.items():
        by_title.setdefault(norm(n), []).append(s)

    sheet_sku_set = {str(disp[i]).strip() for i in range(1, len(disp)) if str(disp[i]).strip()}
    exact_unique = exact_multi = fuzzy = none = 0
    live_names = list(live.items())
    for i, r in enumerate(rows):
        rownum = i + 2
        status = str(r.get("Sync Status") or "").strip()
        if not status.startswith(STATUS_PREFIX):
            continue
        sku = str(disp[rownum - 1]).strip() if rownum - 1 < len(disp) else ""
        title = str(r.get("Title") or "").strip()
        hits = by_title.get(norm(title), []) if title else []
        hits_free = [h for h in hits if h not in sheet_sku_set]
        if len(hits_free) == 1:
            exact_unique += 1
            print(f"EXACT|{rownum}|{sku}|{hits_free[0]}|{title[:55]}")
        elif len(hits_free) > 1:
            exact_multi += 1
            print(f"MULTI|{rownum}|{sku}|{','.join(hits_free[:4])}|{title[:55]}")
        else:
            best_s, best_ratio = "", 0.0
            tn = norm(title)
            if tn:
                for s, n in live_names:
                    ratio = difflib.SequenceMatcher(None, tn, norm(n)).ratio()
                    if ratio > best_ratio:
                        best_ratio, best_s = ratio, s
            if best_ratio >= 0.75 and best_s not in sheet_sku_set:
                fuzzy += 1
                print(f"FUZZY|{rownum}|{sku}|{best_s}|{best_ratio:.2f}|{title[:45]}|live={live[best_s][:45]}")
            else:
                none += 1
                print(f"NONE|{rownum}|{sku}|{title[:55]}")
    print(f"exact-unique: {exact_unique} | exact-multi: {exact_multi} | fuzzy>=0.75: {fuzzy} | no match: {none}")


if __name__ == "__main__":
    main()
