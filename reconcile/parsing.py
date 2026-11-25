"""把各扫描来源的文本解析成带字段角色的候选阅读。

转写方在 candidates 里用 ``A/B`` 表示同一格的两个原始字形。
两种斜杠写法要区分：
- 逐位候选：``O/0I/1-7`` 表示第一位 {O,0}、第二位 {I,1}；
- 整词候选：``OI/01-7`` 表示两个完整读法 OI-7 与 01-7。
解析保留原始拼写，并标注每个词属于标题区还是表格区，
使同一个字符在单据标题和表格里可以有不同解释。
"""
from __future__ import annotations

import re

from .model import FieldKind, PaperType, QtyOption, Reading, Scan, clean_kg
from . import textmodel

# ---- 格名（报告与待查范围引用） ----
CELL_REFERENCE = "联号"
CELL_SPEC = "规格"
CELL_QTY = "数量/单位"
CELL_OWNER = "货主"
CELL_SPLIT = "合批分量"

# 联号/领料号：允许 - 连接，字符含 O/0、I/1、L，斜杠为候选分隔
_REF_TOKEN = r"[A-Za-z0-9]+(?:/[A-Za-z0-9]+)*(?:-[0-9OIolL]+)?"
REF_RE = re.compile(rf"(?:联号|领料号)\s*({_REF_TOKEN}(?:\s*[、和与]\s*{_REF_TOKEN})*)")
# 合并退还里的“合批 A、B” / “合并退回 A 和 B”
MERGE_RE = re.compile(r"(?:合批|合并退回)\s*([^，。]+)")
# 规格词 K1 / KI（斜杠表示候选）
SPEC_RE = re.compile(r"铜扣\s*([A-Za-z0-9/]+)")
# 货主
OWNER_RE = re.compile(r"货主([一-龥]{2,4})|([一-龥]{2,4})(?=铜扣)")
# 数量：阿拉伯数字（可带斜杠候选与小数点）+ 单位
QTY_RE = re.compile(r"(\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)\s*(kg|公斤|盒)")
# 中文数量：二十五公斤、十盒、四公斤；“半公斤”是换算约定不是数量，单独排除
CN_DIGIT = {c: i for i, c in enumerate("零一二三四五六七八九", start=0)}
CN_QTY_RE = re.compile(
    r"([一二三四五六七八九]?十[一二三四五六七八九]?|[一二三四五六七八九])(公斤|盒)"
)
# 盒换算描述：每盒半公斤 / 每盒 0.5 公斤
PER_BOX_RE = re.compile(r"每盒\s*(?:半公斤|([\d.]+)\s*公斤)")
# 比例描述
FRACTION_RE = re.compile(r"今天(一半|[\d.]+)")
# 标题区词（出现在单据开头/说明位置的结构词）
TITLE_WORDS = ("湾内复印件", "单格看不清", "查原纸")

KNOWN_OWNERS = ("泉路", "山社", "湾内")

# 逐位候选：由“单字符/单字符”的同形对拼成，如 O/0、I/1
_POS_PAIR = r"(?:[O0o]/[O0o]|[I1lL]/[I1lL])"
_POSITIONAL_RE = re.compile(rf"^(?:{_POS_PAIR}|[A-Za-z0-9])+$")


def _split_candidates(token: str) -> tuple[str, ...]:
    """把 A/B 整词形式拆成两个候选；没有斜杠时原样返回。"""
    return tuple(part.strip() for part in token.split("/") if part.strip())


def parse_cn_number(token: str) -> float:
    """中文数量：半=0.5，十=10，四=4，二十五=25，十三=13。"""
    if token == "半":
        return 0.5
    if token == "十":
        return 10.0
    if "十" in token:
        tens, _, ones = token.partition("十")
        value = (CN_DIGIT[tens] if tens else 1) * 10
        if ones:
            value += CN_DIGIT[ones]
        return float(value)
    return float(CN_DIGIT.get(token, 0))


def _per_box_kg(text: str) -> tuple[float | None, tuple[int, int]]:
    match = PER_BOX_RE.search(text)
    if not match:
        return None, (-1, -1)
    token = match.group(1)
    value = 0.5 if token is None else float(token)
    return value, match.span()


def expand_ref_token(token: str) -> tuple[str, ...]:
    """展开一个联号词的全部候选读法。

    先剥离 ``-后缀``，再判断斜杠是逐位候选还是整词候选，
    最后对每个读法做 O/0、I/1、L/1 的字形展开。
    """
    suffix = ""
    suffix_match = re.search(r"-[0-9A-Za-z]+$", token)
    head = token
    if suffix_match:
        suffix = suffix_match.group(0)
        head = token[: suffix_match.start()]

    if "/" not in head:
        cores = (head,)
    elif _POSITIONAL_RE.match(head):
        # 逐位扫描：X/Y 三个字符作为一个位置的候选
        choices: list[str] = []
        index = 0
        positions: list[set[str]] = []
        while index < len(head):
            if index + 2 < len(head) and head[index + 1] == "/":
                positions.append({head[index], head[index + 2]})
                index += 3
            else:
                positions.append({head[index]})
                index += 1
        cores = {""}
        for chars in positions:
            cores = {prefix + nxt for prefix in cores for nxt in chars}
        cores = tuple(cores)
    else:
        # 整词候选：斜杠两边各自是完整读法
        cores = tuple(_split_candidates(head))

    expanded: set[str] = set()
    for core in cores:
        expanded.update(
            textmodel.interpret(core + suffix, FieldKind.REFERENCE)["values"]
        )
    return tuple(sorted(expanded))


def parse_qty_options(text: str, fallback_per_box_kg: float | None = None) -> tuple[QtyOption, ...]:
    """抽取数量格的全部候选读法，并按库管约定换算成公斤。

    “每盒半公斤”是换算约定而非数量，其文本跨度内的数字不进数量格；
    纸单没写换算时，用库管规则（T1）的每盒公斤量兜底。
    """
    per_box, per_box_span = _per_box_kg(text)
    if per_box is None:
        per_box = fallback_per_box_kg
    fraction_match = FRACTION_RE.search(text)
    fraction = 0.5 if fraction_match and fraction_match.group(1) == "一半" else None
    options: list[QtyOption] = []

    def in_per_box(span: tuple[int, int]) -> bool:
        return per_box_span[0] >= 0 and span[0] >= per_box_span[0] and span[1] <= per_box_span[1]

    for match in QTY_RE.finditer(text):
        if in_per_box(match.span()):
            continue
        raw_token, unit_word = match.group(1), match.group(2)
        unit = textmodel.normalize_unit(unit_word)
        for raw in _split_candidates(raw_token):
            value = float(raw)
            kg_value: float | None = None
            if unit == "kg":
                kg_value = value
            elif unit == "盒" and per_box is not None:
                kg_value = clean_kg(value * per_box)
            # 比例词（今天一半）只作为元数据保留，不直接改写重量：
            # 漏小数点产生的 25kg 与 2.5kg 必须先各自成立，再由证据选择
            options.append(
                QtyOption(
                    raw=raw,
                    value=value,
                    unit=unit,
                    per_box_kg=per_box if unit == "盒" else None,
                    fraction=fraction,
                    kg_value=kg_value,
                )
            )

    for match in CN_QTY_RE.finditer(text):
        if in_per_box(match.span()):
            continue
        value = parse_cn_number(match.group(1))
        unit = textmodel.normalize_unit(match.group(2))
        kg_value = (
            clean_kg(value * per_box)
            if (unit == "盒" and per_box is not None)
            else (value if unit == "kg" else None)
        )
        options.append(
            QtyOption(
                raw=match.group(0),
                value=value,
                unit=unit,
                per_box_kg=per_box if unit == "盒" else None,
                fraction=fraction,
                kg_value=kg_value,
            )
        )
    # 去重（同原始词、同单位、同换算结果只保留一条）
    dedup = {(o.raw, o.unit, o.kg_value, o.fraction): o for o in options}
    return tuple(dedup.values())


def parse_owner(text: str) -> str:
    for match in OWNER_RE.finditer(text):
        candidate = next(group for group in match.groups() if group)
        if candidate in KNOWN_OWNERS:
            return candidate
    for owner in KNOWN_OWNERS:
        if owner in text:
            return owner
    return ""


def parse_references(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """返回（全部候选联号展开, 合并单原始槽位词, 每个槽位的候选展开）。"""
    raw_refs: list[str] = []
    merged_slots: list[tuple[str, ...]] = []
    merged_raw: list[str] = []

    merge_match = MERGE_RE.search(text)
    if merge_match:
        for part in re.split(r"[、,，和与]\s*", merge_match.group(1)):
            part = part.strip(" 。 ")
            if not part:
                continue
            merged_raw.append(part)
            slot = expand_ref_token(part)
            merged_slots.append(slot)
            raw_refs.extend(slot)

    for match in REF_RE.finditer(text):
        for part in re.split(r"[、和与]\s*", match.group(1)):
            part = part.strip()
            if not part:
                continue
            for candidate in expand_ref_token(part):
                if candidate not in raw_refs:
                    raw_refs.append(candidate)

    return tuple(dict.fromkeys(raw_refs)), tuple(dict.fromkeys(merged_raw)), tuple(merged_slots)


def parse_specs(text: str) -> tuple[str, ...]:
    specs: list[str] = []
    for match in SPEC_RE.finditer(text):
        specs.extend(_split_candidates(match.group(1)))
    return tuple(dict.fromkeys(specs))


def build_readings(text: str) -> tuple[Reading, ...]:
    """按词在文本中的位置标注标题区/表格区，生成 Reading 列表。

    标题词（如“湾内复印件”“单格看不清”）自身所在跨度才是标题区；
    它们之前的表格格仍按表格区解释，避免标题词污染前面的编号与数量。
    """
    readings: list[Reading] = []
    title_spans: list[tuple[int, int]] = []
    for word in TITLE_WORDS:
        pos = text.find(word)
        if pos >= 0:
            title_spans.append((pos, pos + len(word)))

    def region_of(span_start: int) -> str:
        return "title" if any(start <= span_start < end for start, end in title_spans) else "cell"

    for match in REF_RE.finditer(text):
        readings.append(
            Reading(FieldKind.REFERENCE, CELL_REFERENCE, match.group(1), region_of(match.start()))
        )
    for match in SPEC_RE.finditer(text):
        readings.append(
            Reading(FieldKind.SPEC, CELL_SPEC, match.group(1), region_of(match.start()))
        )
    _, per_box_span = _per_box_kg(text)
    for match in QTY_RE.finditer(text):
        if per_box_span[0] >= 0 and match.start() >= per_box_span[0] and match.end() <= per_box_span[1]:
            continue
        readings.append(
            Reading(FieldKind.QTY, CELL_QTY, match.group(0), region_of(match.start()))
        )
    owner = parse_owner(text)
    if owner:
        readings.append(Reading(FieldKind.OWNER, CELL_OWNER, owner, "cell"))
    # 标题区里出现的同形字符只作结构词，单独标注
    for word in TITLE_WORDS:
        pos = text.find(word)
        if pos >= 0:
            readings.append(Reading(FieldKind.TITLE, "标题", word, "title"))
    return tuple(readings)


def parse_scan(row: dict, fallback_per_box_kg: float | None = None) -> Scan:
    """把 scans.tsv 一行解析成 Scan。"""
    text = row["candidates"]
    paper_type = (
        PaperType(row["paper_type"])
        if row["paper_type"] in {t.value for t in PaperType}
        else PaperType.OTHER
    )
    refs, merged, merged_slots = parse_references(text)
    specs = parse_specs(text)
    owner = parse_owner(text)
    options = {
        CELL_REFERENCE: refs,
        CELL_SPEC: specs,
        CELL_OWNER: (owner,) if owner else (),
    }
    return Scan(
        scan_id=row["id"],
        source=row["source"],
        paper_type=paper_type,
        fingerprint=row["fingerprint"],
        text=text,
        readings=build_readings(text),
        options=options,
        qty_options=parse_qty_options(text, fallback_per_box_kg),
        merged_refs=merged,
        merged_slots=merged_slots,
    )
