"""端到端装配：读资料 -> 解析 -> 聚合 -> 对账 -> 冲回 -> 查询服务。"""
from __future__ import annotations

from pathlib import Path

from .engine import ReconciliationEngine
from .model import Allocation, Paper
from .papers import attach_confirmations, build_papers, extract_confirmations
from .parsing import parse_scan
from .physical import evaluate_reversal
from .queries import QueryService
from .registry import (
    load_handoffs,
    load_ledger,
    load_physical,
    load_rules,
)
from .units import parse_conventions


def run_pipeline(directory: Path) -> dict:
    # 延迟导入，避免与仓库根的 pack_check.py 命名冲突
    from pack_check import read_tables

    tables = read_tables(directory)
    rules = load_rules(tables["transfer_limits.tsv"])
    conventions = parse_conventions(rules)
    # 库管约定 T1：K1 每盒半公斤，作为纸单未注明时的盒→kg 兜底换算
    k1_convention = conventions.get("K1")
    fallback_per_box = k1_convention.per_box_kg if k1_convention else None
    scans = [parse_scan(row, fallback_per_box) for row in tables["scans.tsv"]]
    batches = load_ledger(tables["issue_ledger.tsv"], rules)
    physical = load_physical(tables["physical.tsv"])
    handoffs = load_handoffs(tables["stock_handoffs.tsv"])

    papers = build_papers(scans)
    review_scans = [scan for scan in scans if scan.paper_type.value == "复核"]
    confirmations = extract_confirmations(review_scans)
    papers = attach_confirmations(papers, confirmations)

    engine = ReconciliationEngine(papers, batches, conventions)
    state = engine.run()

    # 冲回：对已销账记录，用引擎里该纸的合法归属作为正确目标
    reversals = []
    for event in physical:
        if "已销账" not in event.state:
            continue
        fingerprint = _scan_to_fingerprint(papers, event.paper_scan_id)
        targets = _legal_targets(engine, fingerprint)
        reversals.append(evaluate_reversal(event, batches, handoffs, targets))

    scan_index = {
        scan_id: paper.fingerprint
        for paper in papers.values()
        for scan_id in paper.scan_ids
    }
    query = QueryService(
        papers,
        state.matches,
        state.checks,
        state.groups,
        handoffs,
        reversals,
        scan_index,
    )
    return {
        "scans": scans,
        "papers": papers,
        "confirmations": confirmations,
        "batches": batches,
        "conventions": conventions,
        "matches": state.matches,
        "checks": state.checks,
        "groups": state.groups,
        "physical": physical,
        "handoffs": handoffs,
        "reversals": reversals,
        "query": query,
        "engine": engine,
    }


def _scan_to_fingerprint(papers: dict[str, Paper], scan_id: str) -> str | None:
    for paper in papers.values():
        if scan_id in paper.scan_ids:
            return paper.fingerprint
    return None


def _legal_targets(engine: ReconciliationEngine, fingerprint: str | None) -> tuple[Allocation, ...]:
    if fingerprint is None:
        return ()
    allocations: list[Allocation] = []
    for match in engine.state.matches:
        if match.fingerprint != fingerprint:
            continue
        if match.batch is not None and match.kg_value is not None and match.feasible:
            allocations.append(Allocation(match.batch, match.kg_value, match.reason))
        for plan in match.plans:
            allocations.extend(plan.allocations)
    return tuple(allocations)
