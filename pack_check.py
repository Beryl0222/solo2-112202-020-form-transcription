"""读取并校验业务资料包的物理格式和已声明的编号关联。"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parent / "materials"


def read_tables(directory: Path = DEFAULT_DATA) -> dict:
    contract = json.loads((directory / "tables.json").read_text(encoding="utf-8"))
    tables = {}
    for name, columns in contract["columns"].items():
        with (directory / name).open(encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source, delimiter="\t")
            if reader.fieldnames != columns:
                raise ValueError(f"{name} 的列名与资料格式不一致")
            rows = list(reader)
        if not rows:
            raise ValueError(f"{name} 为空")
        seen = set()
        for index, row in enumerate(rows, start=2):
            if None in row or None in row.values():
                raise ValueError(f"{name} 第 {index} 行列数不符")
            if not row["id"] or row["id"] in seen:
                raise ValueError(f"{name} 第 {index} 行的 id 为空或重复")
            seen.add(row["id"])
        tables[name] = rows

    for name, columns in contract["compound_keys"]:
        keys = [tuple(row[column] for column in columns) for row in tables[name]]
        if len(set(keys)) != len(keys) or any(not all(key) for key in keys):
            raise ValueError(f"{name} 的复合键为空或重复")

    for source, field, target in contract["references"]:
        valid_ids = {row["id"] for row in tables[target]}
        for row in tables[source]:
            value = row[field]
            if value and not set(value.split("|")) <= valid_ids:
                raise ValueError(f"{source} 的 {row['id']} 中 {field} 引用未知记录")
    return tables


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT_DATA, help="业务资料包路径")
    args = parser.parse_args()
    for filename, rows in read_tables(args.directory).items():
        print(f"{filename}  共 {len(rows)} 条")
