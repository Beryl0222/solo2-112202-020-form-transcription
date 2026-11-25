"""登记与实物记录装载：把资料表行转成领域对象。"""
from __future__ import annotations

import re

from .model import Handoff, LedgerBatch, PhysicalEvent, UnitRule
from .units import parse_conventions, parse_ledger_batch


def load_rules(rows: list[dict]) -> list[UnitRule]:
    return [
        UnitRule(rule_id=row["id"], authority=row["authority"], text=row["text"])
        for row in rows
    ]


def load_ledger(rows: list[dict], rules: list[UnitRule]) -> list[LedgerBatch]:
    conventions = parse_conventions(rules)
    return [parse_ledger_batch(row, conventions) for row in rows]


_WEIGH_RE = re.compile(r"实称\s*([\d.]+)\s*kg")
_USED_RE = re.compile(r"签领[^，。]*?([\d.]+)\s*kg")
_MIXED_RE = re.compile(r"([\d.]+)\s*kg\s*已混入")
_LEFT_RE = re.compile(r"库内留\s*([\d.]+)\s*kg")
_PENDING_RE = re.compile(r"待人工核定|待.*确认")
_BOOKED_RE = re.compile(r"冲回\s*([\d.]+)\s*kg")
_BOOKED_REF_RE = re.compile(r"按\s*([一-龥]+)\s*(K[0-9A-Za-z]+)")


def load_physical(rows: list[dict]) -> list[PhysicalEvent]:
    return [
        PhysicalEvent(
            event_id=row["id"],
            paper_scan_id=row["paper"],
            recorded_at=row["recorded_at"],
            detail=row["detail"],
            state=row["state"],
        )
        for row in rows
    ]


def load_handoffs(rows: list[dict]) -> list[Handoff]:
    handoffs: list[Handoff] = []
    for row in rows:
        detail = row["detail"]
        weighed = _WEIGH_RE.search(detail)
        used = _USED_RE.search(detail) or _MIXED_RE.search(detail)
        left = _LEFT_RE.search(detail)
        handoffs.append(
            Handoff(
                handoff_id=row["id"],
                paper_scan_id=row["paper"],
                at=row["at"],
                detail=detail,
                weighed_kg=float(weighed.group(1)) if weighed else None,
                used_kg=float(used.group(1)) if used else None,
                left_kg=float(left.group(1)) if left else None,
                confirmed=not bool(_PENDING_RE.search(detail)),
            )
        )
    return handoffs


def parse_booked_detail(detail: str) -> tuple[str | None, str | None, float | None]:
    """从已销账记录里解析“按 货主 规格 冲回 N kg”。"""
    owner_match = _BOOKED_REF_RE.search(detail)
    qty_match = _BOOKED_RE.search(detail)
    owner = owner_match.group(1) if owner_match else None
    spec = owner_match.group(2) if owner_match else None
    qty = float(qty_match.group(1)) if qty_match else None
    return owner, spec, qty
