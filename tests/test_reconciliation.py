"""端到端业务规则测试：去重、消歧、容量保护、合批、冲回、双计与仓管查询。"""
import unittest
from pathlib import Path

from reconcile.pipeline import run_pipeline
from reconcile.model import PaperType
from reconcile.papers import build_papers, attach_confirmations, extract_confirmations
from reconcile.parsing import parse_scan
from reconcile.units import parse_conventions
from reconcile.registry import load_rules, load_ledger, load_handoffs
from pack_check import read_tables

DATA = Path(__file__).resolve().parent.parent / "materials"


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = run_pipeline(DATA)

    def _match(self, fingerprint):
        return [
            m for m in self.result["matches"] if m.fingerprint == fingerprint
        ]

    def test_forwarded_scan_deduped_by_fingerprint(self):
        # S2/S3 与 S5/S7 各是同一张纸，转发件不能产生第二次入库
        papers = self.result["papers"]
        self.assertEqual(papers["Fr41"].scan_ids, ("S2", "S3"))
        self.assertEqual(papers["Fr44"].scan_ids, ("S5", "S7"))
        self.assertEqual(len(papers), 6)

    def test_issue_readings_converge_to_ledger(self):
        s1 = self._match("Fs81")
        self.assertTrue(any(m.batch.ledger_id == "I1" and m.kg_value == 5.0 for m in s1))
        s4 = self._match("Fs90")
        self.assertTrue(any(m.batch.ledger_id == "I2" and m.kg_value == 25.0 for m in s4))
        s8 = self._match("Fs97")
        self.assertTrue(any(m.batch.ledger_id == "I4" and m.kg_value == 4.0 for m in s8))

    def test_confirmation_locks_cell_without_spreading(self):
        fr41 = self._match("Fr41")
        confirmed = [m for m in fr41 if m.status == "confirmed"]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].batch.ledger_id, "I1")
        self.assertEqual(confirmed[0].kg_value, 2.5)
        # 同形 OI-7 的 I4 没有因为 01-7 的确认而被占用
        used = self.result["engine"].state.used_kg
        self.assertNotIn("I4", used)
        # 确认挂在具体扫描 id 上，Fr44 里同形联号不受影响
        fr44 = self._match("Fr44")
        self.assertTrue(all(m.status == "pending" for m in fr44))

    def test_oversized_misread_does_not_consume_capacity(self):
        # 25kg 的误读在泉路 K1 域没有任何批次能容纳，且不占用 I1/I4 余额
        used = self.result["engine"].state.used_kg
        self.assertEqual(used.get("I1"), 2.5)  # 只有 Fr41 的合法 2.5kg
        self.assertNotIn("I4", used)
        # I1 容量 5kg，剩余 2.5kg 仍可服务未决的合批单
        engine = self.result["engine"]
        i1 = next(b for b in self.result["batches"] if b.ledger_id == "I1")
        self.assertEqual(engine.remaining_capacity(i1), 2.5)

    def test_merged_return_offers_plans_but_consumes_nothing(self):
        fr44 = self._match("Fr44")
        self.assertEqual(len(fr44), 1)
        match = fr44[0]
        self.assertEqual(match.status, "pending")
        plan_names = {p.name for p in match.plans}
        # 两个原批 I1/I4（槽位一二义）与 I3（L0-8），分量必须为正
        self.assertTrue(any("I1" in name and "I3" in name for name in plan_names))
        self.assertTrue(any("I4" in name and "I3" in name for name in plan_names))
        for plan in match.plans:
            self.assertAlmostEqual(sum(a.kg for a in plan.allocations), 3.0)
            self.assertTrue(all(a.kg > 0 for a in plan.allocations))
        # 待确认前不占用任何余额
        used = self.result["engine"].state.used_kg
        self.assertNotIn("I3", used)

    def test_checks_pinpoint_cell_and_changing_options(self):
        checks = {
            (c.fingerprint, c.cell): c for c in self.result["checks"]
        }
        self.assertIn(("Fr41", "规格"), checks)
        self.assertIn(("Fr44", "合批分量"), checks)
        self.assertIn(("Fr44", "联号"), checks)
        for check in checks.values():
            self.assertTrue(check.changes_matching)
            self.assertTrue(check.scan_ids)

    def test_groups_link_same_shape_and_owner_spec(self):
        groups = self.result["groups"]
        main = next(g for g in groups if "Fr44" in g.fingerprints)
        self.assertEqual(
            set(main.fingerprints), {"Fr41", "Fr44", "Fs81", "Fs90", "Fs97"}
        )
        # 账务确认 Fr41 后重算：Fr44 的候选仍合法（I1 剩 2.5kg 可容纳其分量）
        engine = self.result["engine"]
        remaining = engine.revalidate_group(main)
        fr44 = [m for m in remaining if m.fingerprint == "Fr44"]
        self.assertTrue(any(m.feasible for m in fr44))

    def test_revalidation_keeps_issue_corroboration_intact(self):
        # 发料佐证记录发放总量、不占退料余额，整组重算不得把它判成 rejected
        engine = self.result["engine"]
        main = next(g for g in self.result["groups"] if "Fr44" in g.fingerprints)
        remaining = engine.revalidate_group(main)
        issue_matches = [
            m for m in remaining if m.paper_type.value == "发料"
        ]
        self.assertTrue(issue_matches)
        self.assertTrue(all(m.status == "corroborated" for m in issue_matches))
        self.assertTrue(all(m.feasible for m in issue_matches))
        # 退料确认只占用 I1 的 2.5kg，发料登记 I1 的 5kg 总量不被改写
        self.assertEqual(engine.state.used_kg.get("I1"), 2.5)

    def test_reversal_blocked_by_unconfirmed_use_and_shortfall(self):
        self.assertEqual(len(self.result["reversals"]), 1)
        reversal = self.result["reversals"][0]
        self.assertEqual(reversal.booked_kg, 25.0)
        # 2kg 领用待人工核定 -> 不能计入可解释量
        self.assertTrue(any("尚未经领用方确认" in b for b in reversal.blockers))
        self.assertEqual(reversal.supported_kg, 0.5)
        self.assertEqual(reversal.difference_kg, 24.5)
        self.assertTrue(reversal.blocked)
        # 正确归属指向 I1 而非账面的 I2
        self.assertEqual(
            {a.batch.ledger_id for a in reversal.correct_targets}, {"I1"}
        )

    def test_double_count_detection(self):
        findings = self.result["query"].double_counted_pieces()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["paper"], "S2")
        self.assertEqual(findings[0]["booked_ledger"], "I2")

    def test_handoff_query_for_warehouse(self):
        status = self.result["query"].paper_status("S5")
        self.assertIsNotNone(status)
        self.assertEqual(status.fingerprint, "Fr44")
        cells = {c.cell for c in status.pending_checks}
        self.assertEqual(cells, {"合批分量", "联号"})
        group = self.result["query"].group_for("Fr44")
        self.assertIn("Fr41", group.fingerprints)

    def test_historical_voucher_preserved(self):
        from reconcile.physical import historical_reading

        s2 = next(s for s in self.result["scans"] if s.scan_id == "S2")
        # 原凭证按当时阅读结果复核，不被 S6 的后续确认覆盖
        self.assertIn("2.5/25 kg", historical_reading(s2.text))


class ConstructionTest(unittest.TestCase):
    def test_confirmation_targets_scan_not_shape(self):
        tables = read_tables(DATA)
        rules = load_rules(tables["transfer_limits.tsv"])
        conventions = parse_conventions(rules)
        fallback = conventions["K1"].per_box_kg
        scans = [parse_scan(row, fallback) for row in tables["scans.tsv"]]
        confirmations = extract_confirmations(
            [s for s in scans if s.paper_type == PaperType.REVIEW]
        )
        self.assertTrue(confirmations)
        for conf in confirmations:
            self.assertEqual(conf.target_scan_id, "S2")
        papers = attach_confirmations(build_papers(scans), confirmations)
        # Fr44 上不允许出现来自 S6 的确认
        self.assertEqual(papers["Fr44"].confirmations, ())
        self.assertTrue(papers["Fr41"].confirmations)

    def test_handoff_pending_marks_unconfirmed(self):
        tables = read_tables(DATA)
        handoffs = load_handoffs(tables["stock_handoffs.tsv"])
        sh4 = next(h for h in handoffs if h.handoff_id == "SH4")
        self.assertFalse(sh4.confirmed)
        self.assertEqual(sh4.used_kg, 2.0)
        sh2 = next(h for h in handoffs if h.handoff_id == "SH2")
        self.assertTrue(sh2.confirmed)

    def test_ledger_box_to_kg_conversion(self):
        tables = read_tables(DATA)
        rules = load_rules(tables["transfer_limits.tsv"])
        batches = load_ledger(tables["issue_ledger.tsv"], rules)
        i1 = next(b for b in batches if b.ledger_id == "I1")
        self.assertEqual(i1.kg_qty, 5.0)
        i2 = next(b for b in batches if b.ledger_id == "I2")
        self.assertEqual(i2.kg_qty, 25.0)


if __name__ == "__main__":
    unittest.main()
