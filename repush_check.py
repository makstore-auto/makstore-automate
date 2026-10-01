"""EXISTENCE CHECK by touching (2026-10-01): re-pushes the sheet's CURRENT
price and stock for a list of SKUs (same values the sync would push) and
reports OnBuy's per-item answer. A PUT on a SKU that does not exist is
answered with an error ("SKU does not exist"), so this separates listings
that really are gone from ones a GET /listings sweep merely failed to
return (the sweep is offset-paged and its order shifts while OnBuy
re-indexes after bulk updates). Harmless: values pushed are the sheet's own.

Env: SKUS_FILE (one SKU per line, # comments ok), DRY_RUN (default on).
"""
import json
import logging
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from onbuy_client import OnBuyClient
from revive_zeroed_listings import SHEET_NAME, read_product_tabs, zeroless
from retry_utils import RateLimitError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("repush")
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def main():
    skus = [l.strip() for l in open(os.environ["SKUS_FILE"], encoding="utf-8")
            if l.strip() and not l.startswith("#")]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    sheet = read_product_tabs(book)
    zl = {}
    for s, d in sheet.items():
        zl.setdefault(zeroless(s), d)

    plan, no_row, not_active = [], [], []
    for s in skus:
        d = sheet.get(s) or zl.get(zeroless(s))
        if d is None:
            no_row.append(s)
        elif d["status"] != "ACTIVE" or d["stock"] <= 0 or d["price"] <= 0:
            not_active.append(s)
        else:
            plan.append((s, d["price"], d["stock"]))
    log.info("SKUs: %d | to re-push: %d | no sheet row: %d | sheet inactive/no price: %d",
             len(skus), len(plan), len(no_row), len(not_active))
    if DRY_RUN:
        log.info("DRY RUN - nothing pushed")
        return

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    ok, errors, missing = 0, {}, []
    i = 0
    while i < len(plan):
        chunk = plan[i:i + 500]
        try:
            resp = onbuy.update_listings_by_sku_batch(chunk)
        except RateLimitError:
            log.warning("burst limit - waiting 90s")
            time.sleep(90)
            continue
        answers = {str((it or {}).get("sku") or "").strip(): str((it or {}).get("error") or "").strip()
                   for it in resp or []}
        for s, _p, _st in chunk:
            err = answers.get(s, "no answer")
            if s in answers and not err:
                ok += 1
            else:
                errors[err] = errors.get(err, 0) + 1
                missing.append((s, err))
        i += 500
        time.sleep(2.0)
    log.info("RESULT: %d accepted (listing exists) | %d refused %s", ok, len(missing), errors)
    for s, err in missing[:40]:
        log.info("  REFUSED %s: %s", s, err[:100])


if __name__ == "__main__":
    main()
