"""semi_upload_probe.py - the hourly read-only probe behind semi_upload_watch.yml: did OnBuy create the semi store's uploads, and are any off the
AliExpress template or carrying the upload CSV's own handling time?"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import semi_upload_probe as sp


def test_exported_skus_prefers_the_ticked_rows_and_falls_back_to_every_row():
    values = [["SKU", "Supplier URL", "Exported to OnBuy"], ["111", "u", "TRUE"], ["2,22", "u", ""], ["333", "u", "true"], ["", "u", "TRUE"], ["111", "u", "TRUE"]]
    assert sp.exported_skus(values) == ["111", "333"]
    assert sp.exported_skus([["SKU", "Supplier URL", "Exported to OnBuy"], ["111", "u", ""], ["222", "u", ""]]) == ["111", "222"]   # none ticked yet
    assert sp.exported_skus([["SKU", "Supplier URL"], ["111", "u"]]) == ["111"]                                                     # no such column
    assert sp.exported_skus([]) == []


def test_needs_move_means_off_the_ali_template_or_own_handling_time():
    assert sp.needs_move({"delivery_template_id": 22069, "handling_time": 5}, "43190")
    assert sp.needs_move({"delivery_template_id": 43190, "handling_time": "5"}, "43190")
    assert not sp.needs_move({"delivery_template_id": "43190", "handling_time": 0}, "43190")
    assert not sp.needs_move({"delivery_template_id": 43190, "handling_time": None}, "43190")


def test_hits_picks_the_semi_listings_out_of_a_page_without_repeats():
    items = [{"sku": "111", "delivery_template_id": 22069, "handling_time": 5}, {"sku": "999", "delivery_template_id": 22069, "handling_time": 5},
             {"sku": "222", "delivery_template_id": 43190, "handling_time": 0}, {"sku": "111", "delivery_template_id": 22069, "handling_time": 5}, None]
    assert sp.hits(items, {"111", "222", "333"}, "43190") == (["111", "222"], ["111"])
    assert sp.hits([], {"111"}, "43190") == ([], [])


def test_newest_end_reads_created_at():
    head = [{"created_at": "2026-08-01 10:00:00"}, {"created_at": "2026-08-02 10:00:00"}]
    tail = [{"created_at": "2026-10-14 11:40:00"}, {"created_at": "2026-10-13 09:00:00"}]
    assert sp.newest_end(head, tail) == "tail" and sp.newest_end(tail, head) == "head" and sp.newest_end([], []) == "?"


def test_the_watch_workflow_probes_outside_the_api_slot_and_moves_inside_it():
    text = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "semi_upload_watch.yml").read_text(encoding="utf-8")
    assert "python -u semi_upload_probe.py" in text and "python -u delivery_templates.py" in text
    assert text.count("group: onbuy-api") == 1 and text.index("semi_upload_probe.py") < text.index("group: onbuy-api")      # only the move job holds the slot
    assert "needs.probe.outputs.found == 'true' && needs.probe.outputs.busy == 'no'" in text
    assert 'CLEAR_HANDLING: "1"' in text and "TEMPLATE_ALI: ${{ vars.DELIVERY_TEMPLATE_ALI || '43190' }}" in text
    assert "format('{0}', vars.SEMI_UPLOAD_WATCH) != '0'" in text
