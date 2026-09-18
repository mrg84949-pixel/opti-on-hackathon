from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def interleave_by_org(items: list[T], *, org_id_fn: Callable[[T], int | None]) -> list[T]:
    """Round-robin interleave by org_id (deterministic org order). Items without org go last."""
    buckets: dict[int, deque[T]] = {}
    no_org: list[T] = []
    for item in items:
        org_id = org_id_fn(item)
        if org_id is None:
            no_org.append(item)
            continue
        if org_id not in buckets:
            buckets[org_id] = deque()
        buckets[org_id].append(item)

    result: list[T] = []
    org_ids = sorted(buckets.keys())
    while org_ids:
        next_round: list[int] = []
        for oid in org_ids:
            bucket = buckets[oid]
            if bucket:
                result.append(bucket.popleft())
                if bucket:
                    next_round.append(oid)
        org_ids = next_round
    result.extend(no_org)
    return result


def apply_per_org_cap(
    ordered: list[T],
    *,
    org_id_fn: Callable[[T], int | None],
    max_per_org: int,
) -> list[T]:
    if max_per_org <= 0:
        return list(ordered)
    counts: dict[int, int] = {}
    result: list[T] = []
    for item in ordered:
        org_id = org_id_fn(item)
        if org_id is None:
            result.append(item)
            continue
        if counts.get(org_id, 0) >= max_per_org:
            continue
        counts[org_id] = counts.get(org_id, 0) + 1
        result.append(item)
    return result


def fair_reminder_order(
    items: list[T],
    *,
    org_id_fn: Callable[[T], int | None],
    max_per_org: int,
) -> list[T]:
    if max_per_org <= 0:
        return list(items)
    interleaved = interleave_by_org(items, org_id_fn=org_id_fn)
    return apply_per_org_cap(interleaved, org_id_fn=org_id_fn, max_per_org=max_per_org)
