"""仓管交接查询：任一纸单还需查什么、实物当前归哪批、是否被两个领料账本同时计入。"""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Allocation,
    CheckRequest,
    Group,
    Handoff,
    MatchResult,
    Paper,
    Reversal,
)
from .physical import summarize_position


@dataclass(frozen=True)
class PaperStatus:
    fingerprint: str
    scan_ids: tuple[str, ...]
    paper_type: str
    pending_checks: tuple[CheckRequest, ...]
    matches: tuple[MatchResult, ...]
    physical_notes: tuple[str, ...]
    reversal: Reversal | None


class QueryService:
    def __init__(
        self,
        papers: dict[str, Paper],
        matches: list[MatchResult],
        checks: list[CheckRequest],
        groups: list[Group],
        handoffs: list[Handoff],
        reversals: list[Reversal],
        scan_index: dict[str, str],
    ):
        self.papers = papers
        self.matches = matches
        self.checks = checks
        self.groups = groups
        self.handoffs = handoffs
        self.reversals = reversals
        # scan_id -> fingerprint
        self.scan_index = scan_index

    def _fingerprint_for(self, paper_or_scan: str) -> str | None:
        if paper_or_scan in self.papers:
            return paper_or_scan
        return self.scan_index.get(paper_or_scan)

    def paper_status(self, paper_or_scan: str) -> PaperStatus | None:
        fingerprint = self._fingerprint_for(paper_or_scan)
        if fingerprint is None:
            return None
        paper = self.papers[fingerprint]
        checks = tuple(c for c in self.checks if c.fingerprint == fingerprint)
        matches = tuple(m for m in self.matches if m.fingerprint == fingerprint)
        scan_id = paper.scan_ids[0]
        position = summarize_position(scan_id, self.handoffs)
        reversal = next(
            (r for r in self.reversals if r.event.paper_scan_id in paper.scan_ids),
            None,
        )
        return PaperStatus(
            fingerprint=fingerprint,
            scan_ids=paper.scan_ids,
            paper_type=paper.paper_type.value,
            pending_checks=checks,
            matches=matches,
            physical_notes=position.notes,
            reversal=reversal,
        )

    def physical_attribution(self, paper_or_scan: str) -> tuple[Allocation, ...]:
        """实物当前归哪批：取该纸单已确认/候选的归属分配。"""
        fingerprint = self._fingerprint_for(paper_or_scan)
        if fingerprint is None:
            return ()
        allocations: list[Allocation] = []
        for match in self.matches:
            if match.fingerprint != fingerprint:
                continue
            if match.batch is not None and match.kg_value is not None:
                allocations.append(
                    Allocation(match.batch, match.kg_value, match.reason)
                )
            for plan in match.plans:
                allocations.extend(plan.allocations)
        return tuple(allocations)

    def double_counted_pieces(self) -> list[dict]:
        """同一件实物被两个领料账本同时计入的风险点。

        判定：一张退还纸的实物量，被一条已销账记录记到了与对账引擎
        合法归属不同的另一货主/规格账本下。
        """
        findings: list[dict] = []
        for reversal in self.reversals:
            if not reversal.booked_batch:
                continue
            legal_owners = {
                allocation.batch.owner
                for allocation in reversal.correct_targets
            }
            legal_specs = {
                allocation.batch.spec
                for allocation in reversal.correct_targets
            }
            if (
                reversal.booked_batch.owner not in legal_owners
                or reversal.booked_batch.spec not in legal_specs
            ):
                findings.append(
                    {
                        "paper": reversal.event.paper_scan_id,
                        "booked_ledger": reversal.booked_batch.ledger_id,
                        "booked_owner_spec": (
                            reversal.booked_batch.owner,
                            reversal.booked_batch.spec,
                        ),
                        "legal_targets": tuple(
                            (a.batch.ledger_id, a.batch.owner, a.batch.spec, a.kg)
                            for a in reversal.correct_targets
                        ),
                        "issue": "同一实物量被销账到另一货主/规格的领料账本，存在双计风险",
                    }
                )
        return findings

    def group_for(self, paper_or_scan: str) -> Group | None:
        fingerprint = self._fingerprint_for(paper_or_scan)
        for group in self.groups:
            if fingerprint in group.fingerprints:
                return group
        return None
