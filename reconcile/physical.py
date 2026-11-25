"""实物在库、领用回执约束下的销账复核与冲回。

规则（T2 + 现场记录）：
- 中途领取实物须在错账冲回前由领用方确认；
- 库房回执（实称、在库）和下游实际领用量一起约束冲回；
- 销账量超过“在库 + 已确认领用”可解释的部分，列为差额待核，
  不能凭账面余额假定全部实物仍在；
- 原凭证按当时的阅读结果保留复核，不覆盖历史。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import (
    Allocation,
    Handoff,
    LedgerBatch,
    PhysicalEvent,
    Reversal,
    clean_kg,
)
from .registry import parse_booked_detail


@dataclass
class PhysicalPosition:
    """一张纸单对应的实物当前证据汇总。"""

    fingerprint_scan: str
    weighed_kg: float | None
    in_stock_kg: float | None
    used_kg: float | None
    used_confirmed: bool
    notes: tuple[str, ...]


def summarize_position(
    scan_id: str, handoffs: list[Handoff]
) -> PhysicalPosition:
    related = [h for h in handoffs if h.paper_scan_id == scan_id]
    weighed = next((h.weighed_kg for h in related if h.weighed_kg is not None), None)
    # 同一笔领用/留存可能在不同回执中重复描述，取最新一条，不重复累加
    left = next(
        (h.left_kg for h in reversed(related) if h.left_kg is not None), None
    )
    used_events = [h for h in related if h.used_kg is not None]
    used = used_events[-1].used_kg if used_events else None
    # 只要有一条回执仍“待人工核定”，该笔领用就不能用来支持冲回
    confirmed = all(h.confirmed for h in used_events) if used_events else True
    notes = tuple(h.detail for h in related)
    return PhysicalPosition(
        fingerprint_scan=scan_id,
        weighed_kg=weighed,
        in_stock_kg=left,
        used_kg=clean_kg(used) if used is not None else None,
        used_confirmed=confirmed,
        notes=notes,
    )


def find_booked_batch(
    event: PhysicalEvent, batches: list[LedgerBatch]
) -> tuple[LedgerBatch | None, float]:
    owner, spec, qty = parse_booked_detail(event.detail)
    for batch in batches:
        if batch.owner == owner and batch.spec == spec:
            return batch, qty or 0.0
    return None, qty or 0.0


def evaluate_reversal(
    event: PhysicalEvent,
    batches: list[LedgerBatch],
    handoffs: list[Handoff],
    correct_targets: tuple[Allocation, ...],
) -> Reversal:
    """对一条已销账记录给出冲回结论。"""
    booked_batch, booked_kg = find_booked_batch(event, batches)
    position = summarize_position(event.paper_scan_id, handoffs)

    # 可解释量：在库 + 已确认领用；未确认领用不能用来支持冲回
    in_stock = position.in_stock_kg if position.in_stock_kg is not None else 0.0
    confirmed_used = (
        position.used_kg if (position.used_kg is not None and position.used_confirmed) else 0.0
    )
    supported = clean_kg(in_stock + confirmed_used)
    difference = clean_kg(booked_kg - supported)

    blockers: list[str] = []
    if position.used_kg is not None and not position.used_confirmed:
        blockers.append(
            f"领用量 {position.used_kg} kg 尚未经领用方确认（{position.fingerprint_scan} 回执待人工核定）"
        )
    if difference > 0:
        blockers.append(
            f"销账 {booked_kg} kg 超出在库 {in_stock} kg 与已确认领用 {confirmed_used} kg，"
            f"差额 {difference} kg 待核，不得假定实物仍在"
        )

    return Reversal(
        event=event,
        booked_batch=booked_batch,
        booked_kg=booked_kg,
        supported_kg=supported,
        difference_kg=max(difference, 0.0),
        blocked=bool(blockers),
        blockers=tuple(blockers),
        correct_targets=correct_targets,
    )


def historical_reading(scan_text: str) -> str:
    """原凭证按当时阅读结果复核：原样返回，不被后续确认覆盖。"""
    return scan_text
