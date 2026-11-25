"""对账领域的核心数据结构。

所有对象都是不可变的值对象；候选阅读、确认、匹配、冲回等状态
由引擎在外层组合，而不是就地修改扫描记录。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FieldKind(str, Enum):
    """一个格在单据中的语义角色。

    同一个字符在不同角色下解释不同：标题区只保留结构词，
    表格区才把 O/0、I/1 当作编号或数值的候选字形。
    """

    TITLE = "title"          # 单据标题、来源说明
    REFERENCE = "reference"  # 联号 / 领料号格
    SPEC = "spec"            # 规格词（如 K1 / KI）
    QTY = "qty"              # 数量与计量缩写
    OWNER = "owner"          # 货主
    NOTE = "note"            # 其他说明文字


class PaperType(str, Enum):
    ISSUE = "发料"
    RETURN = "退料"
    REVIEW = "复核"
    OTHER = "其他"


@dataclass(frozen=True)
class Reading:
    """转写方对一个格给出的一个候选阅读。"""

    field: FieldKind
    cell: str                 # 格名，例如 联号、重量栏
    raw: str                  # 原始候选字形，保留大小写
    region: str = "cell"      # title | cell，决定同形字符的解释域


@dataclass(frozen=True)
class Scan:
    """scans.tsv 的一行：一次扫描来源对一张纸的阅读。"""

    scan_id: str
    source: str
    paper_type: PaperType
    fingerprint: str
    text: str
    readings: tuple[Reading, ...] = field(default_factory=tuple)
    # 从文本中解析出的结构化候选，键见 parsing.CELL_*
    options: dict[str, tuple[str, ...]] = field(default_factory=dict)
    qty_options: tuple["QtyOption", ...] = ()
    merged_refs: tuple[str, ...] = ()
    # 合并退还单每个联号槽位的候选展开（槽位间独立消歧）
    merged_slots: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class QtyOption:
    """数量格的一个候选读法。"""

    raw: str
    value: float
    unit: str                 # 规范化单位 kg | 盒 | 半公斤
    per_box_kg: float | None = None
    fraction: float | None = None  # “今天一半”等比例描述
    kg_value: float | None = None   # 换算成公斤后的重量（盒/半公斤时）


@dataclass(frozen=True)
class CellConfirmation:
    """复核员凭原纸单对某一张纸某一格的确认（S6/P3 一类）。"""

    review_scan_id: str
    target_scan_id: str       # 确认只对该纸单生效，不扩散到同形单号
    cell: str
    value: str
    source_text: str


@dataclass(frozen=True)
class Paper:
    """同一张纸单：由 fingerprint 聚合，转发件不算新纸。"""

    fingerprint: str
    paper_type: PaperType
    scan_ids: tuple[str, ...]
    scans: tuple[Scan, ...]
    confirmations: tuple[CellConfirmation, ...] = ()


@dataclass(frozen=True)
class LedgerBatch:
    """领退双方各自维护的登记（issue_ledger.tsv 一行）。"""

    ledger_id: str
    owner: str
    reference: str
    spec: str
    qty: float
    unit: str
    per_box_kg: float | None = None
    kg_qty: float | None = None


@dataclass(frozen=True)
class UnitRule:
    """transfer_limits.tsv 中库管/账务的计量与消歧约定。"""

    rule_id: str
    authority: str
    text: str


@dataclass(frozen=True)
class PhysicalEvent:
    """physical.tsv 一行：纸单对应的实物动作与销账状态。"""

    event_id: str
    paper_scan_id: str
    recorded_at: str
    detail: str
    state: str


@dataclass(frozen=True)
class Handoff:
    """stock_handoffs.tsv 一行：现场称量与领用回执。"""

    handoff_id: str
    paper_scan_id: str
    at: str
    detail: str
    weighed_kg: float | None = None
    used_kg: float | None = None
    left_kg: float | None = None
    confirmed: bool = True    # 领用方是否已确认


@dataclass(frozen=True)
class CheckRequest:
    """需要查原纸单的最小待查范围。"""

    fingerprint: str
    scan_ids: tuple[str, ...]
    cell: str
    question: str
    options: tuple[str, ...]
    changes_matching: bool
    reason: str


@dataclass(frozen=True)
class Allocation:
    """一条退还量到某个登记批的归属。"""

    batch: LedgerBatch
    kg: float
    basis: str                # 证据说明


@dataclass(frozen=True)
class AllocationPlan:
    """合并退还单在可选解释下的一种完整分法。"""

    name: str
    allocations: tuple[Allocation, ...]
    requires_cell: str | None


@dataclass(frozen=True)
class MatchResult:
    """一张纸单对某一条登记的候选匹配及其合法性。"""

    fingerprint: str
    paper_type: PaperType
    scan_ids: tuple[str, ...]
    owner: str
    spec: str
    reference_reading: str
    batch: LedgerBatch | None
    kg_value: float | None
    feasible: bool
    status: str               # confirmed | pending | rejected | corroborated
    reason: str
    plans: tuple[AllocationPlan, ...] = ()


@dataclass(frozen=True)
class Group:
    """互相引用、同形或同货主规格而相互影响的有关组。"""

    group_id: str
    fingerprints: tuple[str, ...]
    why: str


@dataclass(frozen=True)
class Reversal:
    """已销账记录的冲回结论。"""

    event: PhysicalEvent
    booked_batch: LedgerBatch | None
    booked_kg: float
    supported_kg: float       # 在库 + 已确认领用能解释的量
    difference_kg: float      # 待核差额
    blocked: bool
    blockers: tuple[str, ...]
    correct_targets: tuple[Allocation, ...]


def near(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol


def clean_kg(value: float) -> float:
    """去掉浮点尾数，便于报告与比较。"""
    return round(value + 0.0, 4)
