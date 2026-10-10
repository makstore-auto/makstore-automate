"""READ-ONLY (2026-10-10): which SKUs of an explicit list sit in OnBuy orders - and are those orders still open?

Built for a list-based delete (Makstore, 930 SKUs): a listing must not be deleted under an order that still has to be dispatched. Pages GET /v2/orders
(newest first) for every status in STATUSES - an empty entry means OnBuy's own default (awaiting_dispatch), "all" means every status - back to DAYS days,
and prints one HIT line per order line whose SKU is on the list (zero-prefix spellings of a barcode count as the same SKU). Order id, status, dates,
product SKU and quantity only - never buyer details. Writes orders_hits.csv and orders_open_skus.txt (the SKUs of orders that are not closed) for the workflow
to upload; nothing in the Sheet, Supabase or OnBuy.

Env: LIST_FILE (one SKU per line, # comments), DAYS (default 30), STATUSES (default ",all"), MAX_PAGES (default 40).
"""
import csv
import os
import time
from datetime import datetime, timedelta, timezone

from onbuy_client import BASE_URL, OnBuyClient

LIST_FILE = os.environ.get("LIST_FILE", "")
DAYS = int(os.getenv("DAYS") or "30")
MAX_PAGES = int(os.getenv("MAX_PAGES") or "40")
STATUSES = (os.getenv("STATUSES") if os.getenv("STATUSES") is not None else ",all").split(",")
CLOSED = {"cancelled", "canceled", "refunded", "complete", "completed", "dispatched", "delivered", "closed"}


def read_list(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return list(dict.fromkeys(out))


def walk(node):
    """Every dict nested anywhere inside node."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def order_time(o):
    for k in ("date", "created", "created_at", "order_date"):
        v = o.get(k)
        if isinstance(v, str) and v[:4].isdigit():
            try:
                return datetime.strptime(v[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return None


def norm(sku):
    s = str(sku or "").strip()
    return s.lstrip("0") or s


def is_open(status):
    return str(status or "").strip().lower().replace(" ", "_") not in CLOSED


def hits_in_order(o, wanted_norm):
    """[(sku_as_in_order, quantity)] of the order lines whose SKU is on the list."""
    out = []
    for d in walk(o):
        if d.get("sku") is not None and norm(d.get("sku")) in wanted_norm:
            out.append((str(d.get("sku")).strip(), d.get("quantity")))
    return out


def scan(onbuy, status, cutoff, wanted_norm, sleep=time.sleep):
    """(orders scanned, oldest order time, [hit rows]) for one status filter."""
    scanned, oldest, rows, offset = 0, None, [], 0
    for page in range(MAX_PAGES):
        params = {"site_id": onbuy.site_id, "limit": 100, "offset": offset, "sort[created]": "desc"}
        if status:
            params["filter[status]"] = status
        resp = onbuy._send("GET", f"{BASE_URL}/orders", what="orders page", params=params, timeout=90)
        if resp.status_code == 429:
            print("rate limited - waiting 90s", flush=True)
            sleep(90)
            continue
        if resp.status_code != 200:
            print(f"status {status or '(default)'}: HTTP {resp.status_code} {resp.text[:200]}", flush=True)
            break
        body = resp.json()
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or not results:
            break
        for o in results:
            scanned += 1
            t = order_time(o)
            if t and (oldest is None or t < oldest):
                oldest = t
            if t and t < cutoff:
                continue
            for sku, qty in hits_in_order(o, wanted_norm):
                due = sorted({str(d.get("expected_dispatch_date")) for d in walk(o) if d.get("expected_dispatch_date")})
                rows.append({"status_filter": status or "(default)", "sku": sku, "order_id": o.get("order_id"), "status": o.get("status"),
                             "date": o.get("date"), "dispatch_by": ";".join(due), "quantity": qty})
        print(f"status {status or '(default)'}: page {page + 1}, {scanned} order(s) scanned, oldest {oldest}", flush=True)
        if (oldest and oldest < cutoff) or len(results) < 100:
            break
        offset += 100
        sleep(1.0)
    return scanned, oldest, rows


def main():
    wanted = read_list(LIST_FILE)
    wanted_norm = {norm(s) for s in wanted}
    print(f"list: {len(wanted)} SKU(s)", flush=True)
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=DAYS)
    all_rows = []
    for st in STATUSES:
        scanned, oldest, rows = scan(onbuy, st.strip(), cutoff, wanted_norm)
        print(f"status {st.strip() or '(default)'}: {scanned} order(s) scanned, {len(rows)} order line(s) on the list", flush=True)
        all_rows += rows
    seen, uniq = set(), []
    for r in all_rows:
        k = (r["sku"], r["order_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    for r in uniq:
        flag = "OPEN" if is_open(r["status"]) else "closed"
        print(f"HIT|{flag}|{r['sku']}|order {r['order_id']}|{r['status']}|{r['date']}|dispatch by {r['dispatch_by']}|qty {r['quantity']}", flush=True)
    with open("orders_hits.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sku", "order_id", "status", "date", "dispatch_by", "quantity", "open"])
        for r in uniq:
            w.writerow([r["sku"], r["order_id"], r["status"], r["date"], r["dispatch_by"], r["quantity"], is_open(r["status"])])
    open_skus = sorted({r["sku"] for r in uniq if is_open(r["status"])})
    with open("orders_open_skus.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(open_skus) + ("\n" if open_skus else ""))
    print(f"DONE: {len(uniq)} order line(s) on the list in the last {DAYS} days, {len(open_skus)} SKU(s) in orders that are not closed", flush=True)


if __name__ == "__main__":
    main()
