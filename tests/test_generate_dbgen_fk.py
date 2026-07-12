import sys
import types
import unittest


mysql_pkg = types.ModuleType("mysql")
connector_mod = types.ModuleType("mysql.connector")


class Error(Exception):
    pass


connector_mod.Error = Error
connector_mod.connect = lambda **kwargs: None
mysql_pkg.connector = connector_mod
sys.modules.setdefault("mysql", mysql_pkg)
sys.modules.setdefault("mysql.connector", connector_mod)

from datagenx.generation.GenerateDbgen import build_single_fk_expression


class FakeCursor:
    def __init__(self, actual_distinct, source_rows, total_rows, null_rows, zero_rows, groups):
        self.actual_distinct = actual_distinct
        self.source_rows = source_rows
        self.total_rows = total_rows
        self.null_rows = null_rows
        self.zero_rows = zero_rows
        self.groups = groups
        self.queries = []
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        normalized = " ".join(sql.split())
        if "SELECT COUNT(*), MIN(`id`)" in normalized:
            self._row = (5000, 1)
            self._rows = [self._row]
        elif "SELECT COUNT(DISTINCT `fk`), COUNT(*)" in normalized and "`src`.`child`" in normalized:
            self._row = (self.actual_distinct, self.source_rows)
            self._rows = [self._row]
        elif "SUM(`fk` IS NULL)" in normalized:
            self._row = (self.total_rows, self.null_rows, self.zero_rows)
            self._rows = [self._row]
        elif "SELECT COUNT(DISTINCT `id`), MIN(`id`), MAX(`id`)" in normalized:
            self._row = (5000, 1, 5000)
            self._rows = [self._row]
        elif "SELECT frequency, COUNT(*) AS value_count" in normalized:
            self._rows = self.groups
            self._row = self._rows[0] if self._rows else None
        elif "SELECT COUNT(*) AS cnt" in normalized:
            # Low-cardinality exact path.
            self._rows = [(5,), (3,), (2,)]
            self._row = self._rows[0]
        elif "SELECT `id` FROM `tgt`.`parent`" in normalized:
            self._rows = [(1,), (2,), (3,)]
            self._row = self._rows[0]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class SingleFkGenerationTests(unittest.TestCase):
    def test_high_ndv_fk_preserves_frequency_shape_without_source_literals(self):
        cursor = FakeCursor(
            actual_distinct=1200,
            source_rows=2402,
            total_rows=2404,
            null_rows=0,
            zero_rows=2,
            groups=[(3, 2), (2, 1198)],
        )

        expression, description = build_single_fk_expression(
            cursor,
            "src",
            "tgt",
            "child",
            "fk",
            "parent",
            "id",
        )

        self.assertIn("frequency-shape FK (1200 distinct, 2 frequency groups)", description)
        self.assertIn("when rownum <= 2 then 0", expression)
        self.assertIn("when rownum <= 8 then 1+div(rownum-3,3)", expression)
        self.assertIn("when rownum <= 2404 then 3+div(rownum-9,2)", expression)
        self.assertNotIn("mod(rownum-1, 1200)", expression)

        source_value_queries = [
            sql for sql in cursor.queries
            if "SELECT `fk`" in sql
        ]
        self.assertEqual([], source_value_queries)

    def test_low_cardinality_fk_exact_path_does_not_select_source_literals(self):
        cursor = FakeCursor(
            actual_distinct=3,
            source_rows=10,
            total_rows=10,
            null_rows=0,
            zero_rows=0,
            groups=[],
        )

        expression, description = build_single_fk_expression(
            cursor,
            "src",
            "tgt",
            "child",
            "fk",
            "parent",
            "id",
        )

        self.assertIn("exact low-cardinality FK frequencies", description)
        self.assertIn("when rownum <= 5 then 1", expression)
        self.assertIn("when rownum <= 8 then 2", expression)
        self.assertIn("when rownum <= 10 then 3", expression)

        source_value_queries = [
            sql for sql in cursor.queries
            if "SELECT `fk`" in sql
        ]
        self.assertEqual([], source_value_queries)


if __name__ == "__main__":
    unittest.main()
