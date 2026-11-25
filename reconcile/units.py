"""计量缩写与单位读法。

库管约定（T1）：铜扣 K1 每盒半公斤，必须换算成实际重量核对；
KI 沿用公斤登记。一个词存在两种单位读法时，只有所有证据支持的
读法都通向同样的归属和余额，才允许先销账，否则转最小待查范围。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import LedgerBatch, QtyOption, UnitRule, clean_kg, near
from . import textmodel

PER_BOX_PATTERN = re.compile(r"K([0-9A-Za-z]+)[^。；;]*每盒\s*(?:半公斤|([\d.]+)\s*公斤)")
KG_REGISTER_PATTERN = re.compile(r"(K[0-9A-Za-z]+)[^。；;]*沿用公斤登记")


@dataclass(frozen=True)
class UnitConvention:
    """从库管约定文本中提取的规格级计量规则。"""

    spec: str
    per_box_kg: float | None
    kg_register: bool


def parse_conventions(rules: list[UnitRule]) -> dict[str, UnitConvention]:
    conventions: dict[str, UnitConvention] = {}
    for rule in rules:
        for match in PER_BOX_PATTERN.finditer(rule.text):
            per_box = 0.5 if match.group(2) is None else float(match.group(2))
            spec = f"K{match.group(1)}"
            conventions[spec] = UnitConvention(spec, per_box, False)
        for match in KG_REGISTER_PATTERN.finditer(rule.text):
            spec = match.group(1)
            current = conventions.get(spec)
            conventions[spec] = UnitConvention(
                spec,
                current.per_box_kg if current else None,
                True,
            )
    return conventions


def kg_candidates(option: QtyOption) -> tuple[float, ...]:
    """一个数量候选在换算规则下可能对应的公斤值（去重排序）。

    比例词（“今天一半”）只对非公斤直读的换算结果保留“原值/折半”两支；
    已经是公斤直读且带比例时，按纸单语义取折半值（“共 2.5kg，今天一半”）。
    """
    base: float | None = None
    if option.kg_value is not None:
        base = option.kg_value
    elif option.unit == "盒" and option.per_box_kg is not None:
        base = option.value * option.per_box_kg
    elif option.unit == "kg":
        base = option.value
    if base is None:
        return ()
    if option.fraction is None:
        return (clean_kg(base),)
    if option.unit == "kg":
        return (clean_kg(base * option.fraction),)
    return tuple(sorted({clean_kg(base), clean_kg(base * option.fraction)}))


def ledger_kg(batch: LedgerBatch, conventions: dict[str, UnitConvention]) -> float:
    """登记批的公斤量：盒按库管约定换算，公斤直接使用。"""
    if batch.kg_qty is not None:
        return clean_kg(batch.kg_qty)
    if batch.unit == "kg":
        return clean_kg(batch.qty)
    convention = conventions.get(batch.spec)
    if convention and convention.per_box_kg is not None:
        return clean_kg(batch.qty * convention.per_box_kg)
    if batch.per_box_kg is not None:
        return clean_kg(batch.qty * batch.per_box_kg)
    return clean_kg(batch.qty)


def parse_ledger_batch(row: dict, conventions: dict[str, UnitConvention]) -> LedgerBatch:
    unit_text = row["unit"]
    per_box_match = re.search(r"每盒\s*([\d.]+)\s*kg", unit_text)
    per_box = float(per_box_match.group(1)) if per_box_match else None
    unit = "盒" if "盒" in unit_text else textmodel.normalize_unit(
        re.search(r"kg|公斤", unit_text).group(0) if re.search(r"kg|公斤", unit_text) else unit_text
    )
    qty = float(row["qty"])
    kg_qty: float | None = None
    if unit == "盒":
        convention = conventions.get(row["product"])
        factor = per_box or (convention.per_box_kg if convention else None)
        if factor is not None:
            kg_qty = clean_kg(qty * factor)
    else:
        kg_qty = clean_kg(qty)
    return LedgerBatch(
        ledger_id=row["id"],
        owner=row["owner"],
        reference=row["reference"],
        spec=row["product"],
        qty=qty,
        unit=unit,
        per_box_kg=per_box,
        kg_qty=kg_qty,
    )


@dataclass(frozen=True)
class UnitVerdict:
    """单位读法是否允许先销账的结论。"""

    settle_allowed: bool
    kg_values: tuple[float, ...]
    reason: str


def judge_unit_reading(
    options: tuple[QtyOption, ...] | list[QtyOption],
    target_batches: tuple[LedgerBatch, ...],
    conventions: dict[str, UnitConvention],
    allocations_by_batch: dict[str, float] | None = None,
) -> UnitVerdict:
    """判断纸单数量格的全部读法是否通向同一归属与同一余额。

    一个词存在两种单位/数量读法时，只有现有证据支持的读法都通向同样
    归属和余额才允许先销账；只要有一种读法落不进任何候选余额，或读法
    分散到多个登记，就返回不可销账的裁决，交由最小待查范围处理。
    """
    all_values: set[float] = set()
    unfit: list[float] = []
    fit_batches: set[str] = set()
    for option in options:
        values = kg_candidates(option)
        if not values:
            return UnitVerdict(
                False, (), "该读法缺少可核对的公斤换算，需查原单计量格"
            )
        all_values.update(values)
        capacities = {
            batch.ledger_id: ledger_kg(batch, conventions)
            - (allocations_by_batch or {}).get(batch.ledger_id, 0.0)
            for batch in target_batches
        }
        for value in values:
            hosts = {
                batch_id
                for batch_id, capacity in capacities.items()
                if value <= capacity or near(value, capacity)
            }
            if not hosts:
                unfit.append(value)
            else:
                fit_batches.update(hosts)
    kg_values = tuple(sorted(all_values))
    if unfit:
        return UnitVerdict(
            False,
            kg_values,
            f"读法 {sorted(set(unfit))} 超出候选登记的可退余额，须查原单数量与单位格",
        )
    if len(fit_batches) == 1:
        return UnitVerdict(True, kg_values, f"全部读法都只落在登记 {next(iter(fit_batches))} 的余额内")
    return UnitVerdict(
        False,
        kg_values,
        f"不同读法通向多个登记 {tuple(sorted(fit_batches))}，归属不一致，列入待查",
    )
