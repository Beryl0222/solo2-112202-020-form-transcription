"""资料包的格式、编号和装载测试。"""
from pathlib import Path
import shutil
import tempfile
import unittest

from pack_check import DEFAULT_DATA, read_tables


class MaterialPackTest(unittest.TestCase):
    def test_reads_all_registered_inputs(self):
        tables = read_tables()
        paths = {path.name for path in DEFAULT_DATA.glob("*.tsv")}
        self.assertEqual(paths, set(tables))
        for rows in tables.values():
            self.assertTrue(rows)

    def test_rejects_duplicate_source_identifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "materials"
            shutil.copytree(DEFAULT_DATA, copy)
            source = sorted(copy.glob("*.tsv"))[0]
            rows = source.read_text(encoding="utf-8").splitlines()
            source.write_text("\n".join([*rows, rows[1]]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "为空或重复"):
                read_tables(copy)


if __name__ == "__main__":
    unittest.main()
