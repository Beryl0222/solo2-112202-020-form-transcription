"""字符/词法层测试：单号字符、规格词大小写、计量单位、标题与表格区域。"""
import unittest

from reconcile.model import FieldKind
from reconcile import textmodel
from reconcile.parsing import (
    expand_ref_token,
    parse_cn_number,
    parse_qty_options,
    parse_references,
    parse_specs,
    build_readings,
)


class GlyphExpansionTest(unittest.TestCase):
    def test_positional_vs_whole_word_slash(self):
        # O/0I/1-7 是逐位候选：两位各两个字形，共四种展开
        self.assertEqual(
            set(expand_ref_token("O/0I/1-7")),
            {"OI-7", "O1-7", "0I-7", "01-7"},
        )
        # OI/01-7 是两个整词候选，再做字形展开
        self.assertEqual(
            set(expand_ref_token("OI/01-7")),
            {"OI-7", "O1-7", "0I-7", "01-7"},
        )

    def test_l_kept_as_real_character(self):
        # L 是 L0-8 的真实字符，同时与 1 同形；不能被吞掉变成 I
        expanded = set(expand_ref_token("L0-8"))
        self.assertIn("L0-8", expanded)
        self.assertIn("10-8", expanded)
        self.assertNotIn("I0-8", expanded)

    def test_shape_normalize_never_rewrites_serial(self):
        # 形状键只用于聚类，不代表真实编号：O->0、I->1
        self.assertEqual(textmodel.shape_normalize("OI-7"), "01-7")
        self.assertEqual(textmodel.shape_normalize("01-7"), "01-7")
        # OI-7 与 01-7 形状键相同 -> 同形误读候选；与 L0-8 不同形
        self.assertTrue(textmodel.shape_compatible("OI-7", "01-7"))
        self.assertFalse(textmodel.shape_compatible("OI-7", "L0-8"))

    def test_spec_case_is_significant(self):
        # K1 与 KI 是两个规格，规格词不做字形展开
        result = textmodel.interpret("K1", FieldKind.SPEC)["values"]
        self.assertEqual(result, ("K1",))
        self.assertNotEqual(
            textmodel.interpret("K1", FieldKind.SPEC)["values"],
            textmodel.interpret("KI", FieldKind.SPEC)["values"],
        )

    def test_title_region_structural_only(self):
        title = textmodel.interpret("查原纸", FieldKind.TITLE, region="title")
        self.assertEqual(title["role"], "structural")
        cell = textmodel.interpret("01-7", FieldKind.REFERENCE, region="cell")
        self.assertEqual(cell["role"], "serial")
        self.assertIn("01-7", cell["values"])


class QuantityParsingTest(unittest.TestCase):
    def test_chinese_numbers(self):
        self.assertEqual(parse_cn_number("十"), 10.0)
        self.assertEqual(parse_cn_number("二十五"), 25.0)
        self.assertEqual(parse_cn_number("四"), 4.0)
        self.assertEqual(parse_cn_number("半"), 0.5)

    def test_box_conversion_uses_per_box_clause(self):
        text = "十盒，每盒半公斤，货主泉路。"
        options = parse_qty_options(text)
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].unit, "盒")
        self.assertEqual(options[0].kg_value, 5.0)

    def test_half_kg_clause_is_not_a_quantity(self):
        # “每盒半公斤”里的半公斤不能被当成一个 0.5kg 的数量候选
        text = "十盒，每盒半公斤"
        options = parse_qty_options(text)
        self.assertEqual([(o.value, o.unit) for o in options], [(10.0, "盒")])

    def test_fraction_kept_as_metadata_not_silently_applied(self):
        from reconcile.units import kg_candidates

        text = "共 2.5/25 kg，今天一半。"
        options = parse_qty_options(text)
        raws = {o.raw: o for o in options}
        # 25kg 与 2.5kg 都保留为独立候选，没有被静默改小
        self.assertEqual(set(raws), {"2.5", "25"})
        self.assertEqual({o.fraction for o in options}, {0.5})
        kg = set()
        for option in options:
            kg.update(kg_candidates(option))
        # 公斤直读 + 比例词：按纸单语义取折半值，两个候选各自成立
        self.assertEqual(kg, {1.25, 12.5})

    def test_direct_kg_not_multiplied(self):
        options = parse_qty_options("二十五公斤")
        self.assertEqual(options[0].kg_value, 25.0)
        self.assertIsNone(options[0].fraction)


class ReferenceAndSpecParsingTest(unittest.TestCase):
    def test_merged_slots_are_separate(self):
        text = "合并退回 OI/01-7 和 L0-8，一共 3 公斤。"
        _, merged, slots = parse_references(text)
        self.assertEqual(merged, ("OI/01-7", "L0-8"))
        self.assertEqual(len(slots), 2)
        self.assertIn("01-7", slots[0])
        self.assertIn("L0-8", slots[1])

    def test_spec_slash_split(self):
        self.assertEqual(parse_specs("铜扣 KI/K1"), ("KI", "K1"))

    def test_readings_mark_title_and_cell_regions(self):
        text = "泉路铜扣 K1，合批 OI-7、L0-8，合计 3 kg。湾内复印件，单格看不清"
        readings = build_readings(text)
        title_readings = [r for r in readings if r.region == "title"]
        cell_readings = [r for r in readings if r.region == "cell"]
        self.assertTrue(any(r.raw == "湾内复印件" for r in title_readings))
        self.assertTrue(any(r.field == FieldKind.SPEC for r in cell_readings))
        self.assertTrue(any(r.field == FieldKind.QTY for r in cell_readings))
        # 标题区的词只作结构词，不进编号/数值值域
        self.assertTrue(
            all(r.field not in (FieldKind.REFERENCE, FieldKind.QTY) for r in title_readings)
        )
        self.assertTrue(all(r.field == FieldKind.TITLE for r in title_readings))


if __name__ == "__main__":
    unittest.main()
