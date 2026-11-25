"""纸单聚合：同纸指纹去重，转发件不产生第二次入库；原纸格确认只对本纸生效。"""
from __future__ import annotations

import re
from collections import defaultdict

from .model import CellConfirmation, Paper, Scan
from .parsing import CELL_QTY, CELL_REFERENCE


def build_papers(scans: list[Scan]) -> dict[str, Paper]:
    """按 fingerprint 聚合扫描。同纸的多个来源只算一张纸单。"""
    grouped: dict[str, list[Scan]] = defaultdict(list)
    for scan in scans:
        grouped[scan.fingerprint].append(scan)

    papers: dict[str, Paper] = {}
    for fingerprint, members in grouped.items():
        paper_type = members[0].paper_type
        papers[fingerprint] = Paper(
            fingerprint=fingerprint,
            paper_type=paper_type,
            scan_ids=tuple(scan.scan_id for scan in members),
            scans=tuple(members),
        )
    return papers


_REVIEW_TARGET_RE = re.compile(r"查原纸\s*([A-Za-z0-9]+)")
_FIELD_RE = re.compile(r"(联号字段|重量栏|规格栏|货主栏)(?:是|为)?\s*([^，。]+)")
_FIELD_CELL = {
    "联号字段": CELL_REFERENCE,
    "重量栏": CELL_QTY,
    "规格栏": "规格",
    "货主栏": "货主",
}


def extract_confirmations(review_scans: list[Scan]) -> list[CellConfirmation]:
    """从复核类扫描（S6 一类）中提取凭原纸单的格确认。"""
    confirmations: list[CellConfirmation] = []
    for scan in review_scans:
        target_match = _REVIEW_TARGET_RE.search(scan.text)
        if not target_match:
            continue
        target = target_match.group(1)
        for field_match in _FIELD_RE.finditer(scan.text):
            cell = _FIELD_CELL.get(field_match.group(1), field_match.group(1))
            confirmations.append(
                CellConfirmation(
                    review_scan_id=scan.scan_id,
                    target_scan_id=target,
                    cell=cell,
                    value=field_match.group(2).strip(),
                    source_text=scan.text,
                )
            )
    return confirmations


def attach_confirmations(
    papers: dict[str, Paper], confirmations: list[CellConfirmation]
) -> dict[str, Paper]:
    """把确认挂到被确认纸单上。确认按 scan_id 定向，绝不扩散到同形其他单号。"""
    by_scan: dict[str, list[CellConfirmation]] = defaultdict(list)
    for confirmation in confirmations:
        by_scan[confirmation.target_scan_id].append(confirmation)

    updated: dict[str, Paper] = {}
    for fingerprint, paper in papers.items():
        attached = tuple(
            conf for scan_id in paper.scan_ids for conf in by_scan.get(scan_id, ())
        )
        updated[fingerprint] = Paper(
            fingerprint=paper.fingerprint,
            paper_type=paper.paper_type,
            scan_ids=paper.scan_ids,
            scans=paper.scans,
            confirmations=attached,
        )
    return updated


def confirmed_value(paper: Paper, cell: str) -> str | None:
    """返回该纸单某格的原纸确认值；没有确认时为 None。"""
    for conf in paper.confirmations:
        if conf.cell == cell:
            return conf.value
    return None
