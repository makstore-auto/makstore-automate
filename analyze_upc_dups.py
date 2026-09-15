"""READ-ONLY (2026-09-15): the UPCs tab is the barcode pool the team
draws SKUs from, and it contains duplicated entries that were assumed
unused. For every value duplicated INSIDE the UPCs tab, report where it
is actually used (Sheet1 rows, Amazon rows, or nowhere); independently
report every SKU used on MORE THAN ONE row of the same tab - two rows
under one SKU are one OnBuy listing flip-flopping between two products -
with each row's OnBuy state so "listed live" is decided from data.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = "Makstore_Full_Feed_Master"


def digits(v):
    return str(v or "").replace(",", "").strip()


def sku_rows(tab):
    """SKU -> [(row, sync status, created, opc)] for a data tab."""
    data = tab.get_all_records()
    out = {}
    for idx, row in enumerate(data):
        sku = digits(row.get("SKU"))
        if sku:
            out.setdefault(sku, []).append(
                (idx + 2, str(row.get("Sync Status") or "").strip()[:44],
                 str(row.get("OnBuy Product Created") or "").strip().upper(),
                 str(row.get("OPC") or "").strip().upper()))
    return out


def looks_live(entries):
    """Any row whose state says the SKU reached OnBuy."""
    return any(created == "TRUE" or status.startswith(("Synced", "Pending Approval", "Awaiting"))
               or (opc and opc != "PENDING")
               for _n, status, created, opc in entries)


def main():
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)

    titles = [w.title for w in book.worksheets()]
    print("Tabs:", titles)
    upc_tab = next((w for w in book.worksheets() if "upc" in w.title.lower()), None)
    if upc_tab is None:
        print("NO tab with 'UPC' in its name - nothing to analyze")
        return

    values = [digits(v) for row in upc_tab.get_all_values() for v in row]
    pool = [v for v in values if v.isdigit() and len(v) >= 11]
    counts = {}
    for v in pool:
        counts[v] = counts.get(v, 0) + 1
    dups = {v: c for v, c in counts.items() if c >= 2}
    print(f"\nUPCs tab '{upc_tab.title}': {len(pool)} barcode entries, {len(counts)} distinct, "
          f"{len(dups)} value(s) duplicated inside the pool")

    first = book.sheet1
    ebay = sku_rows(first)
    amazon_tab = next((w for w in book.worksheets() if w.title.strip().lower() == "amazon"), None)
    amazon = sku_rows(amazon_tab) if amazon_tab is not None else {}

    print(f"\n== the duplicated pool values, and where each is USED:")
    unused = 0
    for v in sorted(dups):
        e_rows = [n for n, *_ in ebay.get(v, [])]
        a_rows = [n for n, *_ in amazon.get(v, [])]
        if not e_rows and not a_rows:
            unused += 1
            continue
        print(f"  {v} (x{dups[v]} in pool): Sheet1 rows {e_rows or '-'} | Amazon rows {a_rows or '-'}")
    print(f"  (+ {unused} duplicated pool value(s) not used on any tab)")

    for title, table in (("Sheet1", ebay), (amazon_tab.title if amazon_tab is not None else "Amazon", amazon)):
        multi = {s: e for s, e in table.items() if len(e) > 1}
        print(f"\n== {title}: SKUs on MORE THAN ONE row of this tab: {len(multi)}")
        for s in sorted(multi):
            live = "LIVE-LISTED" if looks_live(multi[s]) else "never reached OnBuy"
            print(f"  {s}: {live}")
            for n, status, created, opc in multi[s]:
                print(f"      row {n}: created={created or '-'} opc={opc or '-'} status={status or '-'}")

    cross = sorted(set(ebay) & set(amazon))
    print(f"\n== SKUs on BOTH Sheet1 and Amazon tab: {len(cross)}")
    for s in cross:
        print(f"  {s}: Sheet1 {[n for n, *_ in ebay[s]]} | Amazon {[n for n, *_ in amazon[s]]}")
    print("\nDone - nothing was written.")


if __name__ == "__main__":
    main()
