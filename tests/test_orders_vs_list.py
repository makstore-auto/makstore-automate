"""orders_vs_list: which SKUs of an explicit list sit in OnBuy orders, and are those orders still open? (2026-10-10)"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orders_vs_list as ov


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeOnBuy:
    site_id = 2000

    def __init__(self, pages):
        self.pages = pages          # {(status_filter, offset): [orders]}
        self.calls = []

    def _send(self, method, url, *, what, params, timeout):
        key = (params.get("filter[status]", ""), params["offset"])
        self.calls.append(key)
        return Resp(200, {"results": self.pages.get(key, [])})


def order(oid, status, date, *skus):
    return {"order_id": oid, "status": status, "date": date,
            "products": [{"sku": s, "quantity": 1, "expected_dispatch_date": "2026-10-12"} for s in skus]}


def test_a_zero_prefixed_spelling_counts_as_the_same_sku():
    assert ov.norm("0711447224873-hhh-14") == ov.norm("711447224873-hhh-14") == "711447224873-hhh-14"
    assert ov.norm("0000") == "0000"


def test_only_closed_statuses_are_not_open():
    for s in ("awaiting_dispatch", "Awaiting Dispatch", "partially_dispatched", "", None, "on_hold"):
        assert ov.is_open(s)
    for s in ("cancelled", "Cancelled", "refunded", "complete", "dispatched"):
        assert not ov.is_open(s)


def test_the_order_lines_on_the_list_are_found_wherever_they_are_nested():
    o = order("T1", "awaiting_dispatch", "2026-10-09 10:00:00", "111", "999")
    o["nested"] = {"deeper": [{"sku": "0222", "quantity": 2}]}
    assert ov.hits_in_order(o, {"111", "222"}) == [("111", 1), ("0222", 2)]
    assert ov.hits_in_order(o, {"555"}) == []


def test_scan_pages_until_the_orders_get_older_than_the_cutoff():
    old = "2026-08-01 10:00:00"
    page0 = [order(f"T{i}", "awaiting_dispatch", "2026-10-09 10:00:00", "111" if i == 3 else "000") for i in range(100)]
    page1 = [order("OLD", "dispatched", old, "111")]
    onbuy = FakeOnBuy({("", 0): page0, ("", 100): page1})
    scanned, oldest, rows = ov.scan(onbuy, "", datetime(2026, 9, 10), {"111"}, sleep=lambda s: None)
    assert scanned == 101 and oldest == datetime(2026, 8, 1, 10, 0, 0)
    assert [r["order_id"] for r in rows] == ["T3"]                      # the old order is outside the window
    assert rows[0]["dispatch_by"] == "2026-10-12" and onbuy.calls == [("", 0), ("", 100)]


def test_a_status_filter_is_sent_when_given():
    onbuy = FakeOnBuy({("all", 0): [order("T1", "cancelled", "2026-10-09 10:00:00", "111")]})
    _scanned, _oldest, rows = ov.scan(onbuy, "all", datetime(2026, 9, 10), {"111"}, sleep=lambda s: None)
    assert rows[0]["status"] == "cancelled" and onbuy.calls == [("all", 0)]
