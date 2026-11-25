"""命令行报告：把对账结果、待查范围、冲回结论输出给核验/账务/仓管人员。"""
from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import run_pipeline


def _match_line(match) -> str:
    batch = match.batch.ledger_id if match.batch else "（域内无批次）"
    kg = f"{match.kg_value} kg" if match.kg_value is not None else "量待定"
    return (
        f"  [{match.status}] {','.join(match.scan_ids)} 指纹{match.fingerprint} "
        f"{match.owner}/{match.spec} 联号阅读={match.reference_reading} "
        f"-> {batch} {kg}\n      理由：{match.reason}"
    )


def render_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("=== 纸单指纹去重（同纸转发不重复入库）===")
    for paper in result["papers"].values():
        lines.append(
            f"  {paper.fingerprint} [{paper.paper_type.value}] 扫描 {len(paper.scan_ids)} 份: "
            f"{','.join(paper.scan_ids)}"
            + (f"；原纸确认: {[(c.cell, c.value) for c in paper.confirmations]}" if paper.confirmations else "")
        )

    lines.append("\n=== 候选匹配与对账关系 ===")
    for match in result["matches"]:
        lines.append(_match_line(match))
        for plan in match.plans:
            lines.append(
                "      分法候选 " + plan.name + "（需查 " + str(plan.requires_cell) + "）"
            )

    lines.append("\n=== 改变对账关系的阅读候选（应查原单的格）===")
    for check in result["checks"]:
        lines.append(
            f"  {','.join(check.scan_ids)} 指纹{check.fingerprint} 格【{check.cell}】"
            f" 候选={check.options} -> {check.question}（{check.reason}）"
        )

    lines.append("\n=== 有关组（确认一对需整组重算）===")
    for group in result["groups"]:
        lines.append(f"  {group.group_id}: {','.join(group.fingerprints)} — {group.why}")

    lines.append("\n=== 已销账冲回复核 ===")
    for reversal in result["reversals"]:
        booked = reversal.booked_batch.ledger_id if reversal.booked_batch else "未知账本"
        lines.append(
            f"  凭证{reversal.event.event_id}（纸{reversal.event.paper_scan_id}）"
            f"账面冲回 {reversal.booked_kg} kg 到 {booked}；"
            f"在库+已确认领用可解释 {reversal.supported_kg} kg；"
            f"待核差额 {reversal.difference_kg} kg"
        )
        for blocker in reversal.blockers:
            lines.append(f"      阻塞：{blocker}")
        for target in reversal.correct_targets:
            lines.append(
                f"      合法归属 -> {target.batch.ledger_id} "
                f"{target.batch.owner}/{target.batch.spec} {target.kg} kg（{target.basis}）"
            )
        lines.append(f"      原凭证按当时阅读保留：{reversal.event.detail}")

    lines.append("\n=== 双计风险（同一实物被两个领料账本计入）===")
    findings = result["query"].double_counted_pieces()
    if not findings:
        lines.append("  无")
    for finding in findings:
        lines.append(
            f"  纸{finding['paper']} 被销到 {finding['booked_ledger']} "
            f"{finding['booked_owner_spec']}，合法归属 {finding['legal_targets']}：{finding['issue']}"
        )
    return "\n".join(lines)


def render_paper(result: dict, paper_or_scan: str) -> str:
    status = result["query"].paper_status(paper_or_scan)
    if status is None:
        return f"未找到纸单 {paper_or_scan}"
    lines = [
        f"纸单 {status.fingerprint}（扫描 {','.join(status.scan_ids)}，类型 {status.paper_type}）",
        "待查:",
    ]
    if not status.pending_checks:
        lines.append("  无")
    for check in status.pending_checks:
        lines.append(f"  格【{check.cell}】候选={check.options}：{check.question}")
    lines.append("匹配:")
    for match in status.matches:
        lines.append("  " + _match_line(match).strip())
    lines.append("实物回执:")
    for note in status.physical_notes:
        lines.append("  " + note)
    group = result["query"].group_for(paper_or_scan)
    if group:
        lines.append(f"有关组 {group.group_id}: {','.join(group.fingerprints)}")
    attribution = result["query"].physical_attribution(paper_or_scan)
    lines.append("实物当前归属:")
    if not attribution:
        lines.append("  尚无确认归属")
    for allocation in attribution:
        lines.append(
            f"  {allocation.batch.ledger_id} {allocation.batch.owner}/"
            f"{allocation.batch.spec} {allocation.kg} kg"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="退役纸质批单字符纠错与对账")
    parser.add_argument("--directory", type=Path, default=None, help="资料包路径")
    parser.add_argument("--paper", type=str, default=None, help="查询单张纸单（指纹或扫描 id）")
    args = parser.parse_args(argv)
    if args.directory is None:
        from pack_check import DEFAULT_DATA

        args.directory = DEFAULT_DATA
    result = run_pipeline(args.directory)
    if args.paper:
        print(render_paper(result, args.paper))
    else:
        print(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
