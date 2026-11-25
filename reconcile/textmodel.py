"""字符与词法层：区分单号字符、规格词、计量缩写、大小写与值域。

关键规则：
- O/o 与 0、I/l 与 1 只是在某些字段里互为候选字形；
- 同一个字符在标题区与表格区解释不同（标题区只作结构词，不进编号值域）；
- 规格词区分大小写（K1 != KI），计量缩写区分 kg / 盒 / 半公斤；
- 形状归一化只用于“哪些号同形、可能互相误读”的聚类，不用于直接改账。
"""
from __future__ import annotations

from .model import FieldKind

# 原始可能字形的互换表（README：形如 0/O 的两字符均为原材料可能字形）
GLYPH_EQUIV = {
    "O": "0", "o": "0", "0": "0",
    "I": "1", "l": "1", "L": "1", "1": "1",
}

# 各字段允许的值域类别
DIGIT_CHARS = set("0123456789")
LETTER_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")

# 计量缩写词表
UNIT_WORDS = {
    "kg": "kg", "公斤": "kg",
    "盒": "盒", "箱": "盒",
    "半公斤": "半公斤",
}

# 规格词模式：字母 + 数字/字母，大小写有意义
SPEC_PREFIX = ("K",)


def shape_normalize(token: str) -> str:
    """把候选字形归一成形状键，用于同形聚类，不代表真实编号。"""
    return "".join(GLYPH_EQUIV.get(ch, ch) for ch in token)


def shape_compatible(a: str, b: str) -> bool:
    """两个字符串是否可能是同形误读（O/0、I/1 层面可互换）。"""
    return shape_normalize(a) == shape_normalize(b)


def interpret(token: str, field: FieldKind, region: str = "cell") -> dict:
    """按字段与区域解释一个词，返回它在该上下文里的合法读法集合。

    标题区（region == "title"）：词只作结构词，不参与编号/数值值域。
    表格区：按字段给值域。
    """
    if region == "title":
        return {"token": token, "role": "structural", "values": (token,)}

    if field is FieldKind.REFERENCE:
        # 单号：一串字符，O/0、I/1 都可能是真实编号字符，逐位展开
        return {
            "token": token,
            "role": "serial",
            "values": tuple(sorted(_expand_glyphs(token))),
        }
    if field is FieldKind.SPEC:
        # 规格词：大小写有意义，K1 与 KI 是两个规格，不做字形展开
        return {"token": token, "role": "spec", "values": (token,)}
    if field is FieldKind.QTY:
        return {"token": token, "role": "measure", "values": (token,)}
    if field is FieldKind.OWNER:
        return {"token": token, "role": "owner", "values": (token,)}
    return {"token": token, "role": "text", "values": (token,)}


def _expand_glyphs(token: str) -> set[str]:
    """对单号逐位展开 O/0、I/1、L/1 候选；其余字符原样保留。

    大写 L 是真实编号字符（如 L0-8），同时与 1 同形，因此该位保留 {L,1}。
    """
    options: list[set[str]] = []
    for ch in token:
        if ch in ("O", "o", "0"):
            options.append({"O", "0"})
        elif ch in ("I", "l", "1"):
            options.append({"I", "1"})
        elif ch == "L":
            options.append({"L", "1"})
        else:
            options.append({ch})
    result = {""}
    for chars in options:
        result = {prefix + nxt for prefix in result for nxt in chars}
    return result


def is_unit_word(word: str) -> bool:
    return word in UNIT_WORDS


def normalize_unit(word: str) -> str:
    return UNIT_WORDS.get(word, word)


def is_spec_word(word: str) -> bool:
    """规格词：前缀字母 + 一位字符（数字或字母），如 K1、KI、L0 类号另作单号处理。"""
    if len(word) < 2:
        return False
    head, tail = word[0], word[1:]
    return head in SPEC_PREFIX and all(c in DIGIT_CHARS or c in LETTER_CHARS for c in tail)


def spec_case_variants(word: str) -> tuple[str, ...]:
    """列出因大小写不同而不同的规格读法（K1/KI），不把它们当成同一个规格。"""
    variants = {word}
    if len(word) >= 2:
        for idx, ch in enumerate(word):
            if ch in ("I", "1"):
                swapped = "1" if ch == "I" else "I"
                variants.add(word[:idx] + swapped + word[idx + 1 :])
    return tuple(sorted(variants))
