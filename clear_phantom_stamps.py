"""One-off (2026-08-25): the 2026-08-20 Makstore adoption run minted 10
phantom duplicate creations because the pasted SKUs were wrong; the team has
now corrected the SKU cells. Clear the phantom bookkeeping those rows carry
(OPC / OnBuy Product ID / Last OnBuy Sync) so the corrected SKUs adopt the
real listings cleanly (Synced + blank sync -> activation/update path), and
purge the stale mirror rows keyed by the OLD wrong SKUs. DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "Makstore_Full_Feed_Master"
CORRECTED = {s.strip() for s in (os.getenv("CORRECTED_SKUS") or "").split(",") if s.strip()}
OLD_SKUS = [s.strip() for s in (os.getenv("OLD_SKUS") or "").split(",") if s.strip()]


def col_letter(idx):
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def main():
    if not CORRECTED:
        raise SystemExit("CORRECTED_SKUS empty - refusing")
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)
    col = {h.strip(): i for i, h in enumerate(headers)}
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    clear_cols = [c for c in ("OPC", "OnBuy Product ID", "Last OnBuy Sync") if c in col]
    updates, found = [], []
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if sku not in CORRECTED:
            continue
        rownum = i + 2
        found.append(sku)
        print(f"row {rownum} SKU {sku} | status {str(r.get('Sync Status') or '')[:30]!r} | OPC {str(r.get('OPC') or '')!r} | qid {str(r.get('OnBuy Product ID') or '')[:26]!r}")
        for c in clear_cols:
            updates.append({"range": f"{col_letter(col[c])}{rownum}", "values": [[""]]})
    missing = CORRECTED - set(found)
    if missing:
        print(f"WARNING - corrected SKUs not found in sheet (check the cells): {sorted(missing)}")
    print(f"rows found: {len(found)} | cells to clear: {len(updates)} | old mirror SKUs to purge: {len(OLD_SKUS)}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if updates:
        with_retry(lambda: sheet.batch_update([dict(u) for u in updates]), what="clear write", max_attempts=3)
    # mirror: clear phantom fields on the corrected SKUs' rows
    full = supabase_db.fetch_full_rows(found) or {}
    fixed = []
    for sku, row in full.items():
        row["OPC"] = ""
        row["OnBuy Product ID"] = ""
        row["Last OnBuy Sync"] = None if row.get("Last OnBuy Sync") is None else ""
        fixed.append(row)
    if fixed:
        supabase_db.upsert_products(fixed)
        print(f"mirror cleared for {len(fixed)} row(s)")
    if OLD_SKUS:
        supabase_db.delete_products(OLD_SKUS)
        print(f"mirror rows deleted for {len(OLD_SKUS)} old SKU(s)")
    print("done")


if __name__ == "__main__":
    main()
