"""One-off (2026-09-15): Amazon-tab rows mistakenly given Sheet1 SKUs.

Recomputes the duplicate set LIVE (Amazon-tab SKU also present on the
first tab), prints the sorted list, and - unless DRY_RUN - clears those
rows' SKU and Sync Status cells so the team can assign fresh barcodes.
The rows themselves (link, any fetched data) stay. The cross-tab guard
never let any of these push to OnBuy, so there is nothing to remove
there; clearing the SKU also stops their Supabase upserts from
overwriting the eBay product's mirror row (same-SKU conflict key).
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from generate_xml import col_letter
from retry_utils import with_retry

SHEET_NAME = "Makstore_Full_Feed_Master"
TAB = (os.getenv("SHEET_TAB") or "Amazon").strip()
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)

    first = book.sheet1
    fh = [str(h).strip() for h in first.row_values(1)]
    ebay_skus = {str(v).replace(",", "").strip()
                 for v in first.col_values(fh.index("SKU") + 1)[1:]}
    ebay_skus.discard("")
    print(f"First tab '{first.title}': {len(ebay_skus)} SKUs")

    tab = book.worksheet(TAB)
    headers = [str(h).strip() for h in tab.row_values(1)]
    col_map = {h: i + 1 for i, h in enumerate(headers) if h}
    data = tab.get_all_records()

    dups = []
    for idx, row in enumerate(data):
        sku = str(row.get("SKU") or "").replace(",", "").strip()
        if sku and sku in ebay_skus:
            dups.append((idx + 2, sku, str(row.get("Title") or "")[:40]))
    print(f"\nTab '{TAB}': {len(dups)} row(s) carry a Sheet1 SKU:")
    for n, sku, title in dups:
        print(f"  row {n}: {sku}  {title}")

    if DRY_RUN:
        print("\nDRY RUN - nothing cleared.")
        return
    if not dups:
        print("Nothing to clear - done.")
        return
    updates = []
    for n, _sku, _t in dups:
        updates.append({"range": f"{col_letter(col_map['SKU'])}{n}", "values": [[""]]})
        if "Sync Status" in col_map:
            updates.append({"range": f"{col_letter(col_map['Sync Status'])}{n}", "values": [[""]]})
    tab.batch_update(updates)
    print(f"\nCLEARED SKU + Sync Status on {len(dups)} row(s) - the rows stay; "
          "give each a fresh unique barcode and the next sync lists them properly.")


if __name__ == "__main__":
    main()
