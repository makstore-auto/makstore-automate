"""One-off (2026-08-27, user-approved SKU repair): re-key rows whose SKU
never existed on OnBuy to the real live SKU, by explicit mapping. Makstore's
two pairs: the team row carries a suffixed SKU stuck "Awaiting OnBuy
go-live" (0734077923817-ahm, 0840814171929-a) while the live listing uses
the bare SKU - the bare SKU's imported row was kept as the only working
handle and becomes redundant once the team row takes over, so it is deleted
(it must be: two rows must never share a SKU). Writes are RAW; Sync Status
"Synced" + blank Last OnBuy Sync primes the activation pass; the stale
mirror key (old SKU) is purged. REKEY format: "old:new,old:new".
DRY_RUN default on."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "Makstore_Full_Feed_Master"
REKEY = [tuple(p.split(":", 1)) for p in (os.getenv("REKEY") or "").split(",") if ":" in p]


def col_letter(idx0):
    s = ""
    idx0 += 1
    while idx0:
        idx0, rem = divmod(idx0 - 1, 26)
        s = chr(65 + rem) + s
    return s


def main():
    if not REKEY:
        raise SystemExit("REKEY empty - refusing")
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    sheet = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME).sheet1, what="sheet open", max_attempts=3)
    headers = [str(h).strip() for h in with_retry(lambda: sheet.row_values(1), what="headers", max_attempts=3)]
    col = {h: i for i, h in enumerate(headers)}
    rows = with_retry(lambda: sheet.get_all_records(), what="sheet read", max_attempts=3)
    disp = with_retry(lambda: sheet.col_values(col["SKU"] + 1), what="sku display col", max_attempts=3)

    def display(rownum):
        return str(disp[rownum - 1]).strip() if rownum - 1 < len(disp) else ""

    by_display = {}
    for i in range(len(rows)):
        d = display(i + 2)
        if d:
            by_display.setdefault(d, []).append(i + 2)

    updates, deletes, mirror_purge = [], [], []
    done = skipped = 0
    for old, new in REKEY:
        old, new = old.strip(), new.strip()
        old_rows = by_display.get(old, [])
        new_rows = by_display.get(new, [])
        if len(old_rows) != 1:
            print(f"SKIP {old}: found on rows {old_rows} (need exactly 1)"); skipped += 1
            continue
        full_rn = old_rows[0]
        r = rows[full_rn - 2]
        twin_rn = None
        if len(new_rows) == 1:
            twin = rows[new_rows[0] - 2]
            if str(twin.get("Supplier URL") or "").strip():
                print(f"SKIP {old}: target {new} row {new_rows[0]} HAS a Supplier URL - not the imported twin"); skipped += 1
                continue
            twin_rn = new_rows[0]
        elif new_rows:
            print(f"SKIP {old}: target {new} on multiple rows {new_rows}"); skipped += 1
            continue
        print(f"REKEY row {full_rn}: {old!r} -> {new!r} [{str(r.get('Sync Status') or '')[:35]}]"
              + (f" | delete twin row {twin_rn}" if twin_rn else " | no twin row"))
        updates.append((f"{col_letter(col['SKU'])}{full_rn}", [[new]]))
        updates.append((f"{col_letter(col['Sync Status'])}{full_rn}", [["Synced"]]))
        updates.append((f"{col_letter(col['Last OnBuy Sync'])}{full_rn}", [[""]]))
        if twin_rn:
            deletes.append(twin_rn)
        mirror_purge.append(old)
        done += 1
    print(f"rekeys: {done} | skipped: {skipped} | twin rows to delete: {len(deletes)}")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if updates:
        with_retry(lambda: sheet.batch_update(
            [{"range": rg, "values": v} for rg, v in updates], value_input_option="RAW"),
            what="rekey writes", max_attempts=3)
    if deletes:
        requests = [{"deleteDimension": {"range": {
            "sheetId": sheet.id, "dimension": "ROWS",
            "startIndex": rn - 1, "endIndex": rn}}} for rn in sorted(deletes, reverse=True)]
        with_retry(lambda: sheet.spreadsheet.batch_update({"requests": requests}), what="twin deletes", max_attempts=3)
    if mirror_purge:
        supabase_db.delete_products(mirror_purge)
    print(f"rekeyed {done} row(s); deleted {len(deletes)} twin row(s); purged {len(mirror_purge)} mirror key(s)")


if __name__ == "__main__":
    main()
