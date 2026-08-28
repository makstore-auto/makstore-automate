"""One-off (2026-08-28, user report): old-product rows were entered WITHOUT
Sync Status "Synced", so the pipeline create-pathed them (phantom duplicate
creations queued) and they now sit "Awaiting OnBuy go-live (created earlier
- listing not yet updatable)". They only need adopting: for every such row
whose SKU IS a live listing right now, clear the phantom bookkeeping (OPC /
OnBuy Product ID - queue ids are logged first for the delete-retry list),
set Sync Status "Synced" + blank Last OnBuy Sync, and let the next sync's
activation pass adopt via a plain by-SKU update. Rows whose SKU is not live
but whose digit-core matches exactly ONE live SKU are re-keyed to it first
(same rules as the fleet zero-SKU repair). Rows with no live match are
REPORTED, never touched - genuinely-new frozen creations keep waiting.
Mirror rows get the same field clears. Live catalog paged twice and
unioned. DRY_RUN default on."""
import json
import os
import time

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import supabase_db
from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")
SHEET_NAME = "Makstore_Full_Feed_Master"
STATUS_PREFIX = "Awaiting OnBuy go-live"


def col_letter(idx0):
    s = ""
    idx0 += 1
    while idx0:
        idx0, rem = divmod(idx0 - 1, 26)
        s = chr(65 + rem) + s
    return s


def digit_core(sku):
    d = "".join(ch for ch in str(sku) if ch.isdigit())
    return d.lstrip("0") or d


def page_live(onbuy):
    out = set()
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
            s = str((it or {}).get("sku") or "").strip()
            if s:
                out.add(s)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return out


def main():
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

    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    live = page_live(onbuy)
    time.sleep(2.0)
    live |= page_live(onbuy)
    print(f"live SKUs (two-pass union): {len(live)}")
    by_core = {}
    for L in live:
        by_core.setdefault(digit_core(L), []).append(L)

    display_use = {}
    for i in range(len(rows)):
        d = display(i + 2)
        if d:
            display_use.setdefault(d, []).append(i + 2)

    updates, mirror_fix, queue_ids = [], [], []
    adopted = rekeyed = frozen = ambiguous = 0
    for i, r in enumerate(rows):
        rownum = i + 2
        status = str(r.get("Sync Status") or "").strip()
        if not status.startswith(STATUS_PREFIX):
            continue
        sku = display(rownum)
        if not sku:
            continue
        qid = str(r.get("OnBuy Product ID") or "").strip()
        target = None
        if sku in live:
            target = sku
        else:
            # Makstore's suffix class: the team drops the trailing suffix
            # number from real dashboard SKUs ("...zzz" vs live
            # "...zzz-315"), which also changes the digits - so match by
            # PREFIX first (live SKU starts with the row's SKU), the
            # ironclad rule for this convention; barcode-core is the
            # fallback for leading-zero style variants.
            cands = [L for L in live if L != sku and L.startswith(sku)]
            if not cands:
                cands = [L for L in by_core.get(digit_core(sku), []) if L != sku]
            if len(cands) == 1 and not display_use.get(cands[0]):
                target = cands[0]
            elif cands:
                print(f"AMBIGUOUS row {rownum} {sku!r}: live variants {cands}")
                ambiguous += 1
                continue
            else:
                frozen += 1
                continue
        if target == sku:
            adopted += 1
            print(f"ADOPT row {rownum} {sku!r} (live) | old qid {qid[:26]!r}")
        else:
            rekeyed += 1
            print(f"REKEY+ADOPT row {rownum} {sku!r} -> {target!r} | old qid {qid[:26]!r}")
            updates.append((f"{col_letter(col['SKU'])}{rownum}", [[target]]))
        if qid:
            queue_ids.append((sku, qid))
        updates.append((f"{col_letter(col['Sync Status'])}{rownum}", [["Synced"]]))
        updates.append((f"{col_letter(col['Last OnBuy Sync'])}{rownum}", [[""]]))
        if "OPC" in col:
            updates.append((f"{col_letter(col['OPC'])}{rownum}", [[""]]))
        if "OnBuy Product ID" in col:
            updates.append((f"{col_letter(col['OnBuy Product ID'])}{rownum}", [[""]]))
        mirror_fix.append(sku)
    print(f"adopt: {adopted} | rekey+adopt: {rekeyed} | left frozen (no live match): {frozen} | ambiguous: {ambiguous}")
    print("erroneous-create queue ids (delete-retry record): " + "; ".join(f"{s}={q}" for s, q in queue_ids[:40]))
    if len(queue_ids) > 40:
        print(f"... plus {len(queue_ids) - 40} more queue id(s)")
    if DRY_RUN:
        print("DRY RUN - nothing written")
        return
    if not updates:
        print("nothing to fix")
        return
    for c in range(0, len(updates), 400):
        chunk = updates[c:c + 400]
        with_retry(lambda ch=chunk: sheet.batch_update(
            [{"range": rg, "values": v} for rg, v in ch], value_input_option="RAW"),
            what=f"adopt writes {c}", max_attempts=3)
        print(f"written {min(c + 400, len(updates))}/{len(updates)}")
    full = {}
    for c in range(0, len(mirror_fix), 100):
        full.update(supabase_db.fetch_full_rows(mirror_fix[c:c + 100]) or {})
    fixed = []
    for sku, row in full.items():
        row["OPC"] = ""
        row["OnBuy Product ID"] = ""
        row["Sync Status"] = "Synced"
        row["Last OnBuy Sync"] = None if row.get("Last OnBuy Sync") is None else ""
        fixed.append(row)
    if fixed:
        supabase_db.upsert_products(fixed)
        print(f"mirror cleared for {len(fixed)} row(s)")
    print(f"DONE: {adopted} adopted, {rekeyed} rekeyed - next sync's activation pass completes the adoption")


if __name__ == "__main__":
    main()
