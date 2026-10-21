"""READ-ONLY: print the first rows of every product tab (the header and what sits under it) and where the SKU column is - for the moment a sync
refuses to run with "tab 'Sheet1' lacks ['SKU']" (Makstore, 2026-10-20 14:54Z onwards). Prints headers, row numbers and the first few cells of the
first rows (SKU / title cells only, never cost columns). Env: SHEET_NAME, ROWS (default 4), TABS (comma-separated, default: all worksheets)."""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from retry_utils import with_retry

SHEET_NAME = os.getenv("SHEET_NAME") or "Makstore_Full_Feed_Master"
ROWS = int(os.getenv("ROWS") or "4")
TABS = [t.strip() for t in (os.getenv("TABS") or "").split(",") if t.strip()]
SAFE = ("SKU", "Title", "Supplier URL", "Status", "Sync Status", "Stock", "Category", "OPC", "ASIN", "Image URL", "Selling Price (£)")


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = with_retry(lambda: gspread.authorize(creds).open(SHEET_NAME), what="sheet open", max_attempts=3)
    sheets = book.worksheets()
    print(f"{SHEET_NAME!r}: {len(sheets)} worksheet(s): " + ", ".join(f"{w.title} ({w.row_count}x{w.col_count}, frozen rows {w.frozen_row_count})" for w in sheets))
    for ws in sheets:
        if TABS and ws.title not in TABS:
            continue
        values = with_retry(lambda ws=ws: ws.get_values(f"A1:AZ{ROWS}"), what=f"read {ws.title}", max_attempts=3)
        print(f"\n=== {ws.title}: first {len(values)} row(s)")
        for n, row in enumerate(values, start=1):
            cells = [str(c).strip() for c in row]
            if n == 1:
                print(f"  row 1 (header, {len(cells)} cells): " + " | ".join(f"{i + 1}:{c!r}" for i, c in enumerate(cells)))
                hits = [(i + 1, c) for i, c in enumerate(cells) if "sku" in c.lower()]
                print(f"  cells containing 'sku': {hits} | exact 'SKU' present: {'SKU' in cells}")
            else:
                shown = []
                header = [str(c).strip() for c in values[0]] if values else []
                for i, c in enumerate(cells[:40]):
                    name = header[i] if i < len(header) else f"col{i + 1}"
                    if name in SAFE or i < 3:
                        shown.append(f"{name or 'col' + str(i + 1)}={c[:40]!r}")
                print(f"  row {n}: " + " | ".join(shown[:10]))
        print(f"  (row count {ws.row_count}; data rows likely {ws.row_count - 1})")


if __name__ == "__main__":
    main()
