"""单位双读法与容量保护的合成场景测试（不依赖资料包具体行）。"""
import unittest

from reconcile.model import (
    LedgerBatch,
    Paper,
    PaperType,
    QtyOption,
    Scan,
    UnitRule,
)
from reconcile.engine import ReconciliationEngine
from reconcile.parsing import CELL_QTY, CELL_REFERENCE, CELL_SPEC, CELL_OWNER
from reconcile.units import (
    UnitConvention,
    judge_unit_reading,
    parse_conventions,
)


def _batch(ledger_id, owner, spec, reference, kg_qty):
    return LedgerBatch(
        ledger_id=ledger_id,
        owner=owner,
        reference=reference,
        spec=spec,
        qty=kg_qty,
        unit="kg",
        kg_qty=kg_qty,
    )


def _scan(scan_id, fingerprint, refs, specs, owner, qty_options):
    return Scan(
        scan_id=scan_id,
        source="合成来源",
        paper_type=PaperType.RETURN,
        fingerprint=fingerprint,
        text="合成文本",
        options={
            CELL_REFERENCE: refs,
            "规格": specs,
            CELL_OWNER: (owner,),
        },
        qty_options=tuple(qty_options),
    )


class UnitReadingGateTest(unittest.TestCase):
    def setUp(self):
        self.conventions = {"K1": UnitConvention("K1", 0.5, False)}
        self.batches = [
            _batch("B1", "泉路", "K1", "01-7", 5.0),
            _batch("B2", "泉路", "K1", "02-7", 5.0),
        ]

    def test_diverging_unit_readings_block_settlement(self):
        # 域内只有一个候选批时，放得进的读法可销账；超出余额的读法被拦截
        single = (self.batches[0],)
        option_small = QtyOption("2.5", 2.5, "kg", kg_value=2.5)
        option_big = QtyOption("6", 6.0, "kg", kg_value=6.0)
        verdict_small = judge_unit_reading(
            (option_small,), single, self.conventions
        )
        verdict_big = judge_unit_reading(
            (option_big,), single, self.conventions
        )
        self.assertTrue(verdict_small.settle_allowed)
        self.assertFalse(verdict_big.settle_allowed)
        self.assertEqual(verdict_big.kg_values, (6.0,))

    def test_readings_converging_to_same_batch_allowed(self):
        # 域内只剩一个有余量的批：盒换算 2.5kg 与直读 2.5kg 都只落进 B1
        single = (self.batches[0],)
        option_box = QtyOption("5", 5.0, "盒", per_box_kg=0.5, kg_value=2.5)
        option_kg = QtyOption("2.5", 2.5, "kg", kg_value=2.5)
        verdict = judge_unit_reading(
            (option_box, option_kg), single, self.conventions
        )
        self.assertTrue(verdict.settle_allowed, verdict.reason)

    def test_engine_blocks_and_consumes_nothing_on_unit_conflict(self):
        big = QtyOption("9", 9.0, "kg", kg_value=9.0)
        scan = _scan("X1", "FpX", ("01-7",), ("K1",), "泉路", [big])
        paper = Paper(
            fingerprint="FpX",
            paper_type=PaperType.RETURN,
            scan_ids=("X1",),
            scans=(scan,),
        )
        engine = ReconciliationEngine(
            {"FpX": paper}, self.batches, self.conventions
        )
        state = engine.run()
        match = state.matches[0]
        self.assertEqual(match.status, "pending")
        self.assertFalse(match.feasible)
        # 错录的大数量绝不占用任何批次余额
        self.assertEqual(state.used_kg, {})
        cells = {check.cell for check in state.checks}
        self.assertIn(CELL_QTY, cells)

    def test_oversized_qty_never_blocks_other_pending_returns(self):
        # 大数量错录单 + 一张合法小数量未决单：后者仍能正常匹配并占用
        big_scan = _scan(
            "X1", "FpBig", ("01-7",), ("K1",), "泉路",
            [QtyOption("9", 9.0, "kg", kg_value=9.0)],
        )
        good_scan = _scan(
            "X2", "FpGood", ("02-7",), ("K1",), "泉路",
            [QtyOption("3", 3.0, "kg", kg_value=3.0)],
        )
        papers = {
            "FpBig": Paper("FpBig", PaperType.RETURN, ("X1",), (big_scan,)),
            "FpGood": Paper("FpGood", PaperType.RETURN, ("X2",), (good_scan,)),
        }
        engine = ReconciliationEngine(papers, self.batches, self.conventions)
        state = engine.run()
        statuses = {m.fingerprint: m.status for m in state.matches}
        self.assertEqual(statuses["FpBig"], "pending")
        self.assertEqual(statuses["FpGood"], "confirmed")
        # 只有合法单占用 B2 的 3kg，B1 完全不受错录影响
        self.assertEqual(state.used_kg, {"B2": 3.0})


class ConventionParsingTest(unittest.TestCase):
    def test_rules_extract_per_box_and_kg_register(self):
        rules = [
            UnitRule("T1", "库管", "铜扣 K1 每盒半公斤，必须换算后核对实际重量；KI 沿用公斤登记。"),
        ]
        conventions = parse_conventions(rules)
        self.assertEqual(conventions["K1"].per_box_kg, 0.5)
        self.assertTrue(conventions["KI"].kg_register)
        self.assertIsNone(conventions["KI"].per_box_kg)


if __name__ == "__main__":
    unittest.main()
