"""One-off (2026-08-15): the user deleted the conflicted (wrong-content)
listings from OnBuy via the dashboard so the pipeline can RECREATE them
with correct information. Deletion alone is not enough: the rows still
carry Synced status + an OPC, which the anti-duplicate guard reads as
"already created" and routes to update-only forever. Clear the OnBuy
state on exactly those rows - Sync Status, OPC, Last OnBuy Sync, OnBuy
Product Created/Listing Active/Product ID, and Last Checked Time (so the
oldest-first batch picks them up immediately) - and the next run creates
them fresh, with the activation pass pushing price/stock right after.
SKUs come from the RESET_SKUS env (default: the store's audited conflict
list). DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = "Makstore_Full_Feed_Master"
# No audited default for this store - the caller must name the SKUs.
DEFAULT_SKUS = ""
SKUS = {s.strip() for s in (os.getenv("RESET_SKUS") or DEFAULT_SKUS).split(",") if s.strip()}
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
CLEAR_COLS = ["Sync Status", "OPC", "Last OnBuy Sync", "OnBuy Product Created",
              "OnBuy Listing Active", "OnBuy Product ID", "Last Checked Time"]


def col_letter(n):
    s = ""
    while n >= 0:
        s = chr(n % 26 + 65) + s
        n = n // 26 - 1
    return s


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {h: i for i, h in enumerate(headers)}
    missing = [c for c in CLEAR_COLS if c not in col_map]
    cols = [c for c in CLEAR_COLS if c in col_map]
    if missing:
        print(f"note: sheet lacks {missing} - clearing {cols}")
    rows = sheet.get_all_records()
    updates, found = [], []
    for i, r in enumerate(rows):
        sku = str(r.get("SKU") or "").strip()
        if sku not in SKUS:
            continue
        found.append(sku)
        rownum = i + 2
        for c in cols:
            updates.append({"range": f"{col_letter(col_map[c])}{rownum}", "values": [[""]]})
        print(f"  reset row {rownum} SKU {sku} (status was: {str(r.get('Sync Status') or '')[:40]!r})")
    absent = SKUS - set(found)
    print(f"rows to reset: {len(found)} of {len(SKUS)} requested" +
          (f" | NOT IN SHEET: {sorted(absent)}" if absent else ""))
    if DRY_RUN:
        print("DRY RUN - nothing cleared")
        return
    CHUNK = 200
    for i in range(0, len(updates), CHUNK):
        sheet.batch_update([dict(u) for u in updates[i:i + CHUNK]], value_input_option="RAW")
    print(f"CLEARED {len(cols)} column(s) on {len(found)} row(s) - next runs re-create them fresh")

    # The anti-duplicate guard reads the Supabase mirror too (existing.get
    # ("OPC")) - clearing only the sheet leaves already_created TRUE and the
    # rows defer forever instead of re-creating (seen live 2026-08-15).
    # Fetch-merge-upsert the same SKUs with their OnBuy state cleared.
    import supabase_db
    full = supabase_db.fetch_full_rows(sorted(found))
    if full:
        for row in full.values():
            row["Sync Status"] = ""
            row["OPC"] = ""
            row["OnBuy Product ID"] = ""
            row["OnBuy Product Created"] = None
            row["OnBuy Listing Active"] = None
            row["Last OnBuy Sync"] = None
        supabase_db.upsert_products(list(full.values()))
        print(f"SUPABASE cleared on {len(full)} row(s)")
    else:
        print("SUPABASE: no rows found for these SKUs - nothing to clear")



if __name__ == "__main__":
    main()
