from __future__ import annotations

from types import SimpleNamespace

from bot.automation.fair_queue import apply_per_org_cap, fair_reminder_order, interleave_by_org


def _item(org_id: int | None, label: str):
    if org_id is None:
        return SimpleNamespace(org_id=None, label=label)
    return SimpleNamespace(org_id=org_id, label=label)


def _org_id_fn(item) -> int | None:
    return item.org_id


def test_interleave_by_org_round_robin():
    items = [
        _item(1, "a1"),
        _item(1, "a2"),
        _item(1, "a3"),
        _item(2, "b1"),
    ]
    ordered = interleave_by_org(items, org_id_fn=_org_id_fn)
    assert [x.label for x in ordered] == ["a1", "b1", "a2", "a3"]


def test_interleave_puts_missing_org_last():
    items = [_item(None, "x"), _item(1, "a1"), _item(2, "b1")]
    ordered = interleave_by_org(items, org_id_fn=_org_id_fn)
    assert [x.label for x in ordered] == ["a1", "b1", "x"]


def test_apply_per_org_cap():
    items = [_item(1, f"a{i}") for i in range(4)] + [_item(2, "b1")]
    interleaved = interleave_by_org(items, org_id_fn=_org_id_fn)
    capped = apply_per_org_cap(interleaved, org_id_fn=_org_id_fn, max_per_org=2)
    assert len([x for x in capped if x.org_id == 1]) == 2
    assert len([x for x in capped if x.org_id == 2]) == 1


def test_fair_reminder_order_combined():
    items = [_item(1, f"a{i}") for i in range(5)] + [_item(2, "b1")]
    ordered = fair_reminder_order(items, org_id_fn=_org_id_fn, max_per_org=2)
    labels = [x.label for x in ordered]
    assert labels == ["a0", "b1", "a1"]
    assert len([x for x in ordered if x.org_id == 1]) == 2
    assert len([x for x in ordered if x.org_id == 2]) == 1


def test_fair_reminder_order_disabled_when_max_zero():
    items = [_item(1, "a1"), _item(2, "b1")]
    ordered = fair_reminder_order(items, org_id_fn=_org_id_fn, max_per_org=0)
    assert ordered == items
