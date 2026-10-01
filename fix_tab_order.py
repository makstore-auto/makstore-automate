"""ONE-OFF: restores "Sheet1" to position 1 (index 0) in the spreadsheet's
tab order. Built 2026-10-01: "OnBuy Categories" ended up ahead of Sheet1
at some point between 2026-09-30 05:25 (last known-good run) and 10:28
(first Buy Box Defense failure) - both generate_xml.py (SHEET_TAB="" ->
spreadsheet.sheet1) and buybox_defense.py (ss.sheet1) read the FIRST tab
by POSITION, not by name, so every run since has opened "OnBuy Categories"
instead of the real product sheet. Its own required-header guard caught
this safely (no rows touched) but the eBay sync + Buy Box Defense have
been unable to run for ~26 hours.

Confirms the current (broken) order before touching anything, and prints
the full before/after order either way.
"""
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = "Makstore_Full_Feed_Master"


def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    book = gspread.authorize(creds).open(SHEET_NAME)
    tabs = book.worksheets()
    order_now = [t.title for t in tabs]
    print("current order:", order_now)

    if order_now[0] != "OnBuy Categories" or "Sheet1" not in order_now:
        raise SystemExit(f"ABORT: expected 'OnBuy Categories' first and 'Sheet1' present, "
                          f"got {order_now} - order has changed again, not touching it blindly")

    sheet1 = next(t for t in tabs if t.title == "Sheet1")
    rest = [t for t in tabs if t.title != "Sheet1"]
    new_order = [sheet1] + rest
    book.reorder_worksheets(new_order)

    confirmed = [t.title for t in book.worksheets()]
    print("new order:", confirmed)
    if confirmed[0] != "Sheet1":
        raise SystemExit(f"WARNING: reorder call completed but order is now {confirmed} - verify manually")
    print("Sheet1 confirmed back in position 1")


if __name__ == "__main__":
    main()
