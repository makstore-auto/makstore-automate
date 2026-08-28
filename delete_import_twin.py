"""One-off (2026-08-25): the 2026-08-25 full-catalog import ran while row
116's SKU cell was still empty, so live listing 4855008105181-zzz-17 was
appended as a new row; the team has since pasted that SKU into row 116,
leaving the sheet with a twin pair. Delete the IMPORTED twin only: a row
whose SKU is in TARGET_SKUS, whose Supplier URL is empty, and whose SKU
also exists on another row that HAS a Supplier URL (the real team row).
Twin-row sheets caused real trouble on GTV - do not let pairs linger.
deleteDimension requests apply against live state, so target rows are
sorted descending. DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "Makstore_Full_Feed_Master"
TARGETS = {s.strip() for s in (os.getenv("TARGET_SKUS") or "").split(",") if s.strip()}
# When BOTH rows of a pair carry a Supplier URL the tool refuses to choose;
# DELETE_ROW names the exact row to remove (it must carry a target SKU and
# another row must share it) - for deliberate duplicate-entry resolutions
# (rows 26/97, 2026-08-28).
DELETE_ROW = int(os.getenv("DELETE_ROW") or "0")


def main():
    if not TARGETS:
        raise SystemExit("TARGET_SKUS empty - refusing")
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    by_sku = {}
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if sku in TARGETS:
            by_sku.setdefault(sku, []).append((i + 2, str(r.get("Supplier URL") or "").strip()))
    to_delete = []
    for sku in sorted(TARGETS):
        pair = by_sku.get(sku, [])
        with_url = [rn for rn, u in pair if u]
        without_url = [rn for rn, u in pair if not u]
        print(f"{sku}: rows {[(rn, 'url' if u else 'no-url') for rn, u in pair]}")
        if len(pair) < 2:
            print(f"  SKIP - not a twin pair ({len(pair)} row(s))")
            continue
        if not with_url:
            print("  SKIP - no row carries a Supplier URL; cannot tell which is the team's")
            continue
        if not without_url:
            if DELETE_ROW and DELETE_ROW in [rn for rn, _ in pair]:
                keep = [rn for rn, _ in pair if rn != DELETE_ROW]
                to_delete.append(DELETE_ROW)
                print(f"  DELETE row {DELETE_ROW} (explicit DELETE_ROW); keeping {keep}")
                continue
            print("  SKIP - every row has a Supplier URL; refusing to choose (set DELETE_ROW to pick)")
            continue
        for rn in without_url:
            to_delete.append(rn)
            print(f"  DELETE row {rn} (imported twin, no Supplier URL); keeping {with_url}")
    print(f"rows to delete: {sorted(to_delete)}")
    if DRY_RUN:
        print("DRY RUN - nothing deleted")
        return
    if not to_delete:
        print("nothing to delete")
        return
    requests = [{"deleteDimension": {"range": {
        "sheetId": sheet.id, "dimension": "ROWS",
        "startIndex": rn - 1, "endIndex": rn}}} for rn in sorted(to_delete, reverse=True)]
    with_retry(lambda: sheet.spreadsheet.batch_update({"requests": requests}), what="row delete", max_attempts=3)
    print(f"deleted {len(to_delete)} row(s)")


if __name__ == "__main__":
    main()
