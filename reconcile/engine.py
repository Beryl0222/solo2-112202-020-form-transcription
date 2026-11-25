"""对账引擎：消歧、匹配、容量保护与有关组复核。

设计原则
--------
1. 单号只能在“同一货主 + 同规格”的原单集合内消歧（T2）。
2. 形状归一化只用于发现同形误读，绝不直接改账。
3. 已确认的合法退还才占用批次余额；错录的大数量不会挤占其他未决单。
4. 复核员确认一张纸的一个格，只对该纸生效，并触发整个有关组重算。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import (
    Allocation,
    AllocationPlan,
    CheckRequest,
    Group,
    LedgerBatch,
    MatchResult,
    Paper,
    clean_kg,
    near,
)
from . import textmodel
from .parsing import CELL_QTY, CELL_REFERENCE, CELL_SPEC, CELL_SPLIT
from .papers import confirmed_value
from .units import judge_unit_reading, kg_candidates, ledger_kg


@dataclass
class EngineState:
    """引擎在一轮核对中累积的可变状态。"""

    # 批次已被“确认合法”退还占用的公斤量
    used_kg: dict[str, float] = field(default_factory=dict)
    matches: list[MatchResult] = field(default_factory=list)
    checks: list[CheckRequest] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)


class ReconciliationEngine:
    def __init__(
        self,
        papers: dict[str, Paper],
        batches: list[LedgerBatch],
        conventions: dict,
    ):
        self.papers = papers
        self.batches = batches
        self.conventions = conventions
        self.state = EngineState()
        self._check_keys: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ #
    # 值域与批次定位
    # ------------------------------------------------------------------ #
    def domain_batches(self, owner: str, spec: str) -> tuple[LedgerBatch, ...]:
        """同货主、同规格的原单集合——单号消歧的唯一合法域。"""
        return tuple(
            batch
            for batch in self.batches
            if batch.owner == owner and batch.spec == spec
        )

    def remaining_capacity(self, batch: LedgerBatch) -> float:
        total = ledger_kg(batch, self.conventions)
        return clean_kg(total - self.state.used_kg.get(batch.ledger_id, 0.0))

    def _consume(self, batch: LedgerBatch, kg: float) -> None:
        self.state.used_kg[batch.ledger_id] = clean_kg(
            self.state.used_kg.get(batch.ledger_id, 0.0) + kg
        )

    # ------------------------------------------------------------------ #
    # 候选解释与“改变对账关系”的判定
    # ------------------------------------------------------------------ #
    def reference_interpretations(
        self, paper: Paper, owner: str, spec: str
    ) -> dict[str, tuple[LedgerBatch, ...]]:
        """每个联号候选在域内能解释成哪些登记批。"""
        refs: set[str] = set()
        for scan in paper.scans:
            refs.update(scan.options.get(CELL_REFERENCE, ()))
        result: dict[str, tuple[LedgerBatch, ...]] = {}
        domain = self.domain_batches(owner, spec)
        for ref in sorted(refs):
            result[ref] = tuple(
                batch
                for batch in domain
                if batch.reference == ref
                or textmodel.shape_compatible(batch.reference, ref)
            )
        return result

    def spec_interpretations(self, paper: Paper) -> tuple[str, ...]:
        specs: set[str] = set()
        for scan in paper.scans:
            specs.update(scan.options.get(CELL_SPEC, ()))
        return tuple(sorted(specs))

    def owner_of(self, paper: Paper) -> str:
        for scan in paper.scans:
            owners = scan.options.get("货主", ())
            if owners:
                return owners[0]
        return ""

    def kg_values(self, paper: Paper) -> tuple[float, ...]:
        confirmed = confirmed_value(paper, CELL_QTY)
        if confirmed:
            match = re.search(r"([\d.]+)\s*kg", confirmed)
            if match:
                return (float(match.group(1)),)
        values: set[float] = set()
        for scan in paper.scans:
            for option in scan.qty_options:
                values.update(kg_candidates(option))
        return tuple(sorted(values))

    def changing_options(self, paper: Paper, owner: str, spec: str) -> dict[str, tuple[str, ...]]:
        """标出哪些阅读候选会改变对账关系（指向不同批次或不同可退余额）。"""
        changing: dict[str, tuple[str, ...]] = {}
        interpretations = self.reference_interpretations(paper, owner, spec)
        target_sets = {
            ref: tuple(sorted(batch.ledger_id for batch in matched))
            for ref, matched in interpretations.items()
            if matched
        }
        if len({targets for targets in target_sets.values()}) > 1:
            changing[CELL_REFERENCE] = tuple(sorted(target_sets))

        specs = self.spec_interpretations(paper)
        if len(specs) > 1 and len(
            {self.domain_batches(owner, candidate) for candidate in specs}
        ) > 1:
            changing[CELL_SPEC] = specs

        kg_values = self.kg_values(paper)
        if len(kg_values) > 1:
            feasible_map = {
                value: tuple(
                    sorted(
                        batch.ledger_id
                        for batch in self.domain_batches(owner, spec)
                        if value <= self.remaining_capacity(batch)
                        or near(value, self.remaining_capacity(batch))
                    )
                )
                for value in kg_values
            }
            if len(set(feasible_map.values())) > 1:
                changing[CELL_QTY] = tuple(str(value) for value in kg_values)
        return changing

    def _add_check(self, check: CheckRequest) -> None:
        key = (check.fingerprint, check.cell)
        if key in self._check_keys:
            return
        self._check_keys.add(key)
        self.state.checks.append(check)

    # ------------------------------------------------------------------ #
    # 主匹配
    # ------------------------------------------------------------------ #
    def run(self) -> EngineState:
        # 带原纸确认的纸先处理，保证确认优先、合法地占用余额
        ordered = sorted(
            self.papers.values(),
            key=lambda paper: (0 if paper.confirmations else 1, paper.fingerprint),
        )
        for paper in ordered:
            if paper.paper_type.value == "发料":
                self._match_issue(paper)
            elif paper.paper_type.value == "退料":
                self._match_return(paper)
        self.state.groups = self.build_groups()
        return self.state

    def _match_issue(self, paper: Paper) -> None:
        owner = self.owner_of(paper)
        kg_values = self.kg_values(paper)
        specs = self.spec_interpretations(paper) or ("",)
        supported_batches: set[str] = set()
        for spec in specs:
            interpretations = self.reference_interpretations(paper, owner, spec)
            # 同一登记批只产出一条佐证记录，候选联号收敛到该批即视为一致
            for ref, batches in interpretations.items():
                for batch in batches:
                    if batch.ledger_id in supported_batches:
                        continue
                    capacity = ledger_kg(batch, self.conventions)
                    if kg_values and not any(near(v, capacity) for v in kg_values):
                        continue
                    supported_batches.add(batch.ledger_id)
                    self.state.matches.append(
                        MatchResult(
                            fingerprint=paper.fingerprint,
                            paper_type=paper.paper_type,
                            scan_ids=paper.scan_ids,
                            owner=owner,
                            spec=spec,
                            reference_reading=batch.reference,
                            batch=batch,
                            kg_value=capacity,
                            feasible=True,
                            status="corroborated",
                            reason=(
                                f"发料阅读（含 O/0、I/1 候选）在 {owner}/{spec} 域内"
                                f"唯一收敛到登记 {batch.ledger_id}，重量 {capacity} kg 一致"
                            ),
                        )
                    )
            self._emit_checks(paper, owner, spec)
        if not supported_batches:
            for spec in specs:
                interpretations = self.reference_interpretations(paper, owner, spec)
                self._unsupported(paper, owner, spec, interpretations, kg_values)

    def _match_return(self, paper: Paper) -> None:
        owner = self.owner_of(paper)
        is_merged = any(scan.merged_slots for scan in paper.scans)
        for spec in self.spec_interpretations(paper) or ("",):
            interpretations = self.reference_interpretations(paper, owner, spec)
            kg_values = self.kg_values(paper)
            if is_merged:
                self._match_merged(paper, owner, spec, kg_values)
            else:
                self._match_single(paper, owner, spec, interpretations, kg_values)

    def _match_single(
        self,
        paper: Paper,
        owner: str,
        spec: str,
        interpretations: dict[str, tuple[LedgerBatch, ...]],
        kg_values: tuple[float, ...],
    ) -> None:
        confirmed_ref = self._confirmed_reference(paper)
        feasible: list[tuple[LedgerBatch, float, str]] = []
        for value in kg_values:
            for ref, batches in interpretations.items():
                for batch in batches:
                    capacity = self.remaining_capacity(batch)
                    if value <= capacity or near(value, capacity):
                        basis = (
                            "原纸格确认" if confirmed_ref == ref else "扫描候选+余额可容纳"
                        )
                        feasible.append((batch, value, basis))

        if confirmed_ref:
            locked = [item for item in feasible if item[0].reference == confirmed_ref]
            if locked:
                batch, value, _ = locked[0]
                self._consume(batch, value)
                self.state.matches.append(
                    MatchResult(
                        fingerprint=paper.fingerprint,
                        paper_type=paper.paper_type,
                        scan_ids=paper.scan_ids,
                        owner=owner,
                        spec=spec,
                        reference_reading=confirmed_ref,
                        batch=batch,
                        kg_value=value,
                        feasible=True,
                        status="confirmed",
                        reason=(
                            f"复核员凭原纸确认联号 {confirmed_ref}、重量 {value} kg；"
                            "确认只对本纸生效，不扩散到同形其他单号"
                        ),
                    )
                )
                self._emit_checks(paper, owner, spec)
                return

        # 单位双读法闸门：只有全部证据支持的读法，在联号指向的批次上
        # 都通向同一归属与同一余额才先销账
        unit_gate = self._unit_reading_gate(paper, owner, spec, interpretations)
        if unit_gate is not None and not unit_gate.settle_allowed:
            self.state.matches.append(
                MatchResult(
                    fingerprint=paper.fingerprint,
                    paper_type=paper.paper_type,
                    scan_ids=paper.scan_ids,
                    owner=owner,
                    spec=spec,
                    reference_reading="/".join(sorted(interpretations)),
                    batch=None,
                    kg_value=kg_values[0] if kg_values else None,
                    feasible=False,
                    status="pending",
                    reason=f"单位读法分歧：{unit_gate.reason}；列为最小待查范围，暂不销账",
                )
            )
            self._add_check(
                CheckRequest(
                    fingerprint=paper.fingerprint,
                    scan_ids=paper.scan_ids,
                    cell=CELL_QTY,
                    question="请查原纸数量/单位格，确认计量缩写与重量",
                    options=tuple(str(v) for v in unit_gate.kg_values),
                    changes_matching=True,
                    reason=unit_gate.reason,
                )
            )
            return

        if feasible:
            batch_ids = {batch.ledger_id for batch, _, _ in feasible}
            if len(batch_ids) == 1:
                batch, value, _ = feasible[0]
                self._consume(batch, value)
                self.state.matches.append(
                    MatchResult(
                        fingerprint=paper.fingerprint,
                        paper_type=paper.paper_type,
                        scan_ids=paper.scan_ids,
                        owner=owner,
                        spec=spec,
                        reference_reading=batch.reference,
                        batch=batch,
                        kg_value=value,
                        feasible=True,
                        status="confirmed",
                        reason=f"全部候选解释都通向登记 {batch.ledger_id}，按 {value} kg 占用其可退余额",
                    )
                )
            else:
                self.state.matches.append(
                    MatchResult(
                        fingerprint=paper.fingerprint,
                        paper_type=paper.paper_type,
                        scan_ids=paper.scan_ids,
                        owner=owner,
                        spec=spec,
                        reference_reading="/".join(sorted(interpretations)),
                        batch=None,
                        kg_value=kg_values[0] if kg_values else None,
                        feasible=True,
                        status="pending",
                        reason=(
                            "候选可落向多个登记 "
                            f"{sorted(batch_ids)}，均未占用余额，等待原纸格确认"
                        ),
                    )
                )
        else:
            self._unsupported(paper, owner, spec, interpretations, kg_values)
        self._emit_checks(paper, owner, spec)

    def _match_merged(
        self,
        paper: Paper,
        owner: str,
        spec: str,
        kg_values: tuple[float, ...],
    ) -> None:
        """合并退还单：按单上实际货主、规格和量分到两个原批，槽位独立消歧。"""
        # 汇总同纸各扫描来源的槽位候选（S5 与 S7 是同纸转发，槽位一致才互相印证）
        slots: list[set[str]] = []
        for scan in paper.scans:
            for index, slot in enumerate(scan.merged_slots):
                while len(slots) <= index:
                    slots.append(set())
                slots[index].update(slot)
        domain = self.domain_batches(owner, spec)
        slot_batches: list[list[LedgerBatch]] = []
        for slot in slots:
            matched = [
                batch
                for batch in domain
                if any(
                    batch.reference == ref
                    or textmodel.shape_compatible(batch.reference, ref)
                    for ref in slot
                )
            ]
            # 同批在槽位内去重
            unique: list[LedgerBatch] = []
            for batch in matched:
                if batch.ledger_id not in {b.ledger_id for b in unique}:
                    unique.append(batch)
            slot_batches.append(unique)

        total = kg_values[0] if kg_values else 0.0
        plans = self._enumerate_plans(slot_batches, total)
        unreadable = any("看不清" in scan.text for scan in paper.scans)

        if len(slot_batches) < 2 or not plans:
            self.state.matches.append(
                MatchResult(
                    fingerprint=paper.fingerprint,
                    paper_type=paper.paper_type,
                    scan_ids=paper.scan_ids,
                    owner=owner,
                    spec=spec,
                    reference_reading=";".join(
                        "/".join(sorted(slot)) for slot in slots
                    ),
                    batch=None,
                    kg_value=total,
                    feasible=False,
                    status="rejected",
                    reason="合批引用在同货主同规格域内找不到两个合法原批，或分量超出余额",
                )
            )
        else:
            self.state.matches.append(
                MatchResult(
                    fingerprint=paper.fingerprint,
                    paper_type=paper.paper_type,
                    scan_ids=paper.scan_ids,
                    owner=owner,
                    spec=spec,
                    reference_reading=";".join(
                        "/".join(sorted(slot)) for slot in slots
                    ),
                    batch=None,
                    kg_value=total,
                    feasible=True,
                    status="pending",
                    reason=(
                        "合批分量格" + ("原纸看不清，" if unreadable else "")
                        + f"存在 {len(plans)} 种合法分法，须查原纸分量格后再占用余额"
                    ),
                    plans=tuple(plans),
                )
            )
        # 最小待查范围：分量格必查；联号槽位若二义也列入
        self._add_check(
            CheckRequest(
                fingerprint=paper.fingerprint,
                scan_ids=paper.scan_ids,
                cell=CELL_SPLIT,
                question="请查原纸合批分量格，确定两个原批各自的退还重量",
                options=(f"合计 {total} kg",),
                changes_matching=True,
                reason="不同分量切分改变两个登记批的余额占用与对账关系",
            )
        )
        ambiguous_slots = [
            tuple(sorted(slot)) for slot in slots if len(slot) > 1
        ]
        if ambiguous_slots:
            self._add_check(
                CheckRequest(
                    fingerprint=paper.fingerprint,
                    scan_ids=paper.scan_ids,
                    cell=CELL_REFERENCE,
                    question="请查原纸联号格，确认合批两个槽位各自的真实联号",
                    options=tuple(opt for slot in ambiguous_slots for opt in slot),
                    changes_matching=True,
                    reason="联号槽位的 O/0、I/1 读法会改变合批归属的原批",
                )
            )

    def _enumerate_plans(
        self, slot_batches: list[list[LedgerBatch]], total: float
    ) -> list[AllocationPlan]:
        """枚举合批单的合法分法：每批分量为正且不超过余额，合计等于总量。"""
        if len(slot_batches) < 2:
            return []
        plans: dict[str, AllocationPlan] = {}
        first, second = slot_batches[0], slot_batches[1]
        for batch_a in first:
            for batch_b in second:
                if batch_a.ledger_id == batch_b.ledger_id:
                    continue
                cap_a = self.remaining_capacity(batch_a)
                cap_b = self.remaining_capacity(batch_b)
                # x 给 A，total-x 给 B，两边都必须为正且在余额内
                lower = max(0.0, clean_kg(total - cap_b))
                upper = min(cap_a, total)
                candidates = sorted(
                    {
                        clean_kg(lower),
                        clean_kg(upper),
                        clean_kg(total / 2),
                    }
                )
                for x in candidates:
                    y = clean_kg(total - x)
                    if x <= 0 or y <= 0:
                        continue
                    if (x > cap_a and not near(x, cap_a)) or (
                        y > cap_b and not near(y, cap_b)
                    ):
                        continue
                    name = (
                        f"{batch_a.reference}({batch_a.ledger_id})={x}kg;"
                        f"{batch_b.reference}({batch_b.ledger_id})={y}kg"
                    )
                    plans[name] = AllocationPlan(
                        name=name,
                        allocations=(
                            Allocation(batch_a, x, "合批候选分量（槽位一）"),
                            Allocation(batch_b, y, "合批候选分量（槽位二）"),
                        ),
                        requires_cell=CELL_SPLIT,
                    )
        return list(plans.values())

    # ------------------------------------------------------------------ #
    # 待查与拒绝
    # ------------------------------------------------------------------ #
    def _confirmed_reference(self, paper: Paper) -> str | None:
        value = confirmed_value(paper, CELL_REFERENCE)
        if not value:
            return None
        match = re.search(r"([0-9A-Za-z-]+)", value)
        return match.group(1) if match else None

    def _unsupported(
        self,
        paper: Paper,
        owner: str,
        spec: str,
        interpretations: dict[str, tuple[LedgerBatch, ...]],
        kg_values: tuple[float, ...],
    ) -> None:
        domain = self.domain_batches(owner, spec)
        oversized = bool(
            domain
            and kg_values
            and all(
                value > self.remaining_capacity(batch)
                for value in kg_values
                for batch in domain
            )
        )
        if oversized:
            # 错录的大数量（漏小数点）直接拒绝，绝不占用其他未决单的合法退还量
            status, reason = (
                "rejected",
                "数量超出同货主同规格域内全部可退余额（疑似漏掉小数点），"
                "拒绝匹配且不占用其他未决单余额",
            )
        else:
            status, reason = (
                "pending",
                f"规格 {spec} 读法在 {owner} 域内无登记支持，需查原纸规格/联号格",
            )
        self.state.matches.append(
            MatchResult(
                fingerprint=paper.fingerprint,
                paper_type=paper.paper_type,
                scan_ids=paper.scan_ids,
                owner=owner,
                spec=spec,
                reference_reading="/".join(sorted(interpretations)),
                batch=None,
                kg_value=max(kg_values) if kg_values else None,
                # 两种情况都没有合法的批次-容量匹配：大数量被拒绝，
                # 无登记支持的待查项同样不具可行性，等待原纸格证据
                feasible=False,
                status=status,
                reason=reason,
            )
        )

    def _unit_reading_gate(
        self,
        paper: Paper,
        owner: str,
        spec: str,
        interpretations: dict[str, tuple[LedgerBatch, ...]],
    ):
        """对纸单上每个数量候选套用单位双读法规则。

        裁决域不是整个货主/规格域，而是联号候选实际指向的批次集合：
        联号已经把目标收窄后，数量读法只需在这些批次上判断是否通向
        同一归属与同一余额。返回 None 表示不设闸（没有候选、纸单已有
        原纸格确认、或联号未指向任何批次）。
        """
        if paper.confirmations:
            return None
        referenced: list[LedgerBatch] = []
        seen: set[str] = set()
        for batches in interpretations.values():
            for batch in batches:
                if batch.ledger_id not in seen:
                    seen.add(batch.ledger_id)
                    referenced.append(batch)
        if not referenced:
            return None
        all_options = [
            option
            for scan in paper.scans
            for option in scan.qty_options
        ]
        if not all_options:
            return None
        return judge_unit_reading(
            all_options,
            tuple(referenced),
            self.conventions,
            self.state.used_kg,
        )

    def _emit_checks(self, paper: Paper, owner: str, spec: str) -> None:
        changing = self.changing_options(paper, owner, spec)
        for cell, options in changing.items():
            self._add_check(
                CheckRequest(
                    fingerprint=paper.fingerprint,
                    scan_ids=paper.scan_ids,
                    cell=cell,
                    question=f"请查原纸单的 {cell} 格",
                    options=tuple(options),
                    changes_matching=True,
                    reason="该格的不同阅读候选指向不同登记或不同可退余额",
                )
            )

    # ------------------------------------------------------------------ #
    # 有关组：同形联号 + 同货主规格，并查集连通
    # ------------------------------------------------------------------ #
    def build_groups(self) -> list[Group]:
        fingerprints = sorted(self.papers)
        parent = {fp: fp for fp in fingerprints}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            root_a, root_b = find(a), find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        ref_shapes: dict[str, set[str]] = {}
        owner_spec: dict[tuple[str, str], set[str]] = {}
        for paper in self.papers.values():
            owner = self.owner_of(paper)
            for spec in self.spec_interpretations(paper) or ("",):
                owner_spec.setdefault((owner, spec), set()).add(paper.fingerprint)
            for scan in paper.scans:
                for ref in scan.options.get(CELL_REFERENCE, ()):
                    ref_shapes.setdefault(textmodel.shape_normalize(ref), set()).add(
                        paper.fingerprint
                    )

        for members in ref_shapes.values():
            members = list(members)
            for other in members[1:]:
                union(members[0], other)
        for members in owner_spec.values():
            members = list(members)
            for other in members[1:]:
                union(members[0], other)

        clusters: dict[str, set[str]] = {}
        for fp in fingerprints:
            clusters.setdefault(find(fp), set()).add(fp)

        groups: list[Group] = []
        for index, members in enumerate(
            sorted(clusters.values(), key=lambda item: sorted(item)[0]), start=1
        ):
            member_papers = [self.papers[fp] for fp in members]
            # 只有复核纸、且没有任何业务纸的孤立簇不构成对账有关组
            if all(
                paper.paper_type.value == "复核" for paper in member_papers
            ):
                continue
            shapes = sorted(
                shape
                for shape, fps in ref_shapes.items()
                if fps & members
            )
            groups.append(
                Group(
                    group_id=f"G{len(groups) + 1}",
                    fingerprints=tuple(sorted(members)),
                    why=(
                        f"同形联号 {shapes} 或同货主/规格相连；"
                        "账务确认其中一对时须整组重新核查合法匹配"
                    ),
                )
            )
        return groups

    def revalidate_group(self, group: Group) -> list[MatchResult]:
        """账务确认其中一对后，重新核查整组还剩哪些合法匹配。

        发料佐证（corroborated）记录的是发放总量，本身不占用退料余额，
        原样保留；只有退料匹配才按确认后的剩余可退容量重新判定。
        """
        remaining: list[MatchResult] = []
        for match in self.state.matches:
            if match.fingerprint not in group.fingerprints:
                continue
            if match.status == "corroborated":
                remaining.append(match)
                continue
            if match.batch is None or match.kg_value is None:
                remaining.append(match)
                continue
            capacity = self.remaining_capacity(match.batch)
            if match.kg_value <= capacity or near(match.kg_value, capacity):
                remaining.append(match)
            else:
                remaining.append(
                    MatchResult(
                        fingerprint=match.fingerprint,
                        paper_type=match.paper_type,
                        scan_ids=match.scan_ids,
                        owner=match.owner,
                        spec=match.spec,
                        reference_reading=match.reference_reading,
                        batch=match.batch,
                        kg_value=match.kg_value,
                        feasible=False,
                        status="rejected",
                        reason="有关组内其他确认占用余额后，该匹配不再合法",
                    )
                )
        return remaining
