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

from extract_schema import annotate_table_with_statistics


class FakeCursor:
    def __init__(
        self,
        row_count=100,
        string_group_counts=None,
        frequency_shape_groups=None,
    ):
        self.row_count = row_count
        self.string_group_counts = string_group_counts or {}
        self.frequency_shape_groups = frequency_shape_groups or {}
        self.queries = []
        self._row = None
        self._rows = []

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "SELECT frequency, COUNT(*) AS value_count" in sql:
            matched = None
            for column, groups in self.frequency_shape_groups.items():
                if f"`{column}`" in sql:
                    matched = groups
                    break
            if matched is None:
                matched = []
            self._rows = matched
            self._row = self._rows[0] if self._rows else None
        elif "GROUP BY" in sql and "COUNT(*) AS cnt" in sql:
            matched = None
            for column, counts in self.string_group_counts.items():
                if f"`{column}`" in sql:
                    matched = counts
                    break
            if matched is None:
                raise AssertionError(f"Unexpected SQL: {sql}")
            self._rows = [(count,) for count in matched]
            self._row = self._rows[0] if self._rows else None
        elif sql.startswith("SELECT COUNT(*)"):
            self._row = (self.row_count,)
            self._rows = [self._row]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeExtractor:
    def __init__(
        self,
        ddl,
        columns,
        primary_keys=None,
        foreign_keys=None,
        distinct_counts=None,
        row_count=100,
        histograms=None,
        string_group_counts=None,
        frequency_shape_groups=None,
    ):
        self.ddl = ddl
        self.columns = columns
        self.primary_keys = set(primary_keys or [])
        self.foreign_keys = foreign_keys or {}
        self.distinct_counts = distinct_counts or {}
        self.histograms = histograms or {}
        self.cursor = FakeCursor(
            row_count=row_count,
            string_group_counts=string_group_counts,
            frequency_shape_groups=frequency_shape_groups,
        )

    def get_table_ddl(self, table):
        return self.ddl

    def get_columns(self, table):
        return self.columns

    def get_primary_keys(self, table):
        return self.primary_keys

    def get_foreign_keys(self, table):
        return self.foreign_keys

    def analyze_table(self, table):
        return None

    def get_table_cardinality(self, table):
        return {"row_count": self.cursor.row_count, "columns": {}, "indexes": {}}

    def get_distinct_count(self, table, column):
        return self.distinct_counts[column]

    def get_column_histogram(self, table, column):
        return self.histograms.get(column)


class StatisticsAnnotationTest(unittest.TestCase):
    def test_generated_appendage_takes_precedence_for_non_fk_columns(self):
        ddl = """CREATE TABLE `orders` (
  `order_id` int NOT NULL,
  PRIMARY KEY (`order_id`)
)"""
        extractor = FakeExtractor(
            ddl,
            {"order_id": "int"},
            primary_keys={"order_id"},
            distinct_counts={"order_id": 100},
        )

        annotated = annotate_table_with_statistics(
            extractor,
            "test",
            "orders",
            generated_appendages={"order_id": "mod(rownum-1, 7) + 1"},
        )

        self.assertIn("@order_id := mod(rownum-1, 7) + 1", annotated)

    def test_numeric_missing_histogram_uses_exact_ndv_not_low_range(self):
        ddl = """CREATE TABLE `part` (
  `p_size` int DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"p_size": "int"},
            distinct_counts={"p_size": 50},
            row_count=200,
        )

        annotated = annotate_table_with_statistics(extractor, "test", "part")

        self.assertIn("@p_size := mod(rownum-1, 50) + 1", annotated)
        self.assertNotIn("rand.range(0,5)", annotated)

    def test_low_ndv_integer_preserves_frequency_shape(self):
        ddl = """CREATE TABLE `catalog_sales` (
  `cs_sold_date_sk` int DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"cs_sold_date_sk": "int"},
            distinct_counts={"cs_sold_date_sk": 3},
            row_count=10,
            histograms={
                "cs_sold_date_sk": {
                    "histogram-type": "equi-height",
                    "buckets": [[1, 3, 1.0, 3]],
                },
            },
            frequency_shape_groups={"cs_sold_date_sk": [(2, 1), (3, 1), (5, 1)]},
        )

        annotated = annotate_table_with_statistics(extractor, "test", "catalog_sales")

        self.assertIn("@cs_sold_date_sk := case", annotated)
        self.assertIn("when rownum <= 2 then 1+div(rownum-1,2)", annotated)
        self.assertIn("when rownum <= 5 then 2+div(rownum-3,3)", annotated)
        self.assertIn("when rownum <= 10 then 3+div(rownum-6,5)", annotated)
        self.assertNotIn("case mod(rownum-1", annotated)
        shape_queries = [
            sql for sql, _params in extractor.cursor.queries
            if "SELECT frequency, COUNT(*) AS value_count" in sql
        ]
        self.assertEqual(1, len(shape_queries))
        self.assertNotIn("SELECT `cs_sold_date_sk`", shape_queries[0])

    def test_high_ndv_integer_uses_histogram_without_frequency_shape_query(self):
        ddl = """CREATE TABLE `web_sales` (
  `ws_sold_time_sk` int DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"ws_sold_time_sk": "int"},
            distinct_counts={"ws_sold_time_sk": 85468},
            row_count=100000,
            histograms={
                "ws_sold_time_sk": {
                    "histogram-type": "equi-height",
                    "buckets": [[1, 85468, 1.0, 85468]],
                },
            },
        )

        annotated = annotate_table_with_statistics(extractor, "test", "web_sales")

        self.assertIn("@ws_sold_time_sk := case", annotated)
        self.assertIn("mod((rownum - 0 - 1),85468)+0", annotated)
        shape_queries = [
            sql for sql, _params in extractor.cursor.queries
            if "SELECT frequency, COUNT(*) AS value_count" in sql
        ]
        self.assertEqual([], shape_queries)

    def test_date_missing_histogram_uses_synthetic_base_and_exact_ndv(self):
        ddl = """CREATE TABLE `orders` (
  `o_orderdate` date DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"o_orderdate": "date"},
            distinct_counts={"o_orderdate": 2406},
            row_count=15000,
        )

        annotated = annotate_table_with_statistics(extractor, "test", "orders")

        self.assertIn(
            "@o_orderdate := TIMESTAMP '2000-01-01 00:00:00' + "
            "INTERVAL mod(rownum-1, 2406) DAY",
            annotated,
        )
        self.assertFalse(any("MIN(" in sql for sql, _params in extractor.cursor.queries))

    def test_composite_pk_columns_preserve_marginal_ndv(self):
        ddl = """CREATE TABLE `catalog_sales` (
  `cs_item_sk` int NOT NULL,
  `cs_order_number` int NOT NULL,
  PRIMARY KEY (`cs_item_sk`,`cs_order_number`)
)"""
        extractor = FakeExtractor(
            ddl,
            {"cs_item_sk": "int", "cs_order_number": "int"},
            primary_keys={"cs_item_sk", "cs_order_number"},
            distinct_counts={"cs_item_sk": 102000, "cs_order_number": 1600000},
            row_count=14401261,
        )

        annotated = annotate_table_with_statistics(extractor, "test", "catalog_sales")

        self.assertIn("@cs_item_sk := mod(rownum-1, 102000) + 1", annotated)
        self.assertIn("@cs_order_number := mod(rownum-1, 1600000) + 1", annotated)

    def test_two_column_composite_pk_preserves_frequency_shape_when_unique(self):
        ddl = """CREATE TABLE `store_sales` (
  `ss_item_sk` int NOT NULL,
  `ss_ticket_number` int NOT NULL,
  PRIMARY KEY (`ss_item_sk`,`ss_ticket_number`)
)"""
        extractor = FakeExtractor(
            ddl,
            {"ss_item_sk": "int", "ss_ticket_number": "int"},
            primary_keys={"ss_item_sk", "ss_ticket_number"},
            distinct_counts={"ss_item_sk": 3, "ss_ticket_number": 10},
            row_count=6,
            frequency_shape_groups={"ss_item_sk": [(1, 1), (2, 1), (3, 1)]},
        )

        annotated = annotate_table_with_statistics(extractor, "test", "store_sales")

        self.assertIn("@ss_item_sk := case", annotated)
        self.assertIn("when rownum <= 1 then 1+div(rownum-1,1)", annotated)
        self.assertIn("when rownum <= 3 then 2+div(rownum-2,2)", annotated)
        self.assertIn("when rownum <= 6 then 3+div(rownum-4,3)", annotated)
        self.assertIn("@ss_ticket_number := mod(rownum-1, 10) + 1", annotated)
        shape_queries = [
            sql for sql, _params in extractor.cursor.queries
            if "SELECT frequency, COUNT(*) AS value_count" in sql
        ]
        self.assertEqual(1, len(shape_queries))
        self.assertNotIn("SELECT `ss_item_sk`", shape_queries[0])

    def test_composite_pk_uses_block_stride_when_lcm_is_too_small(self):
        ddl = """CREATE TABLE `inventory` (
  `inv_date_sk` int NOT NULL,
  `inv_item_sk` int NOT NULL,
  `inv_warehouse_sk` int NOT NULL,
  PRIMARY KEY (`inv_date_sk`,`inv_item_sk`,`inv_warehouse_sk`)
)"""
        extractor = FakeExtractor(
            ddl,
            {
                "inv_date_sk": "int",
                "inv_item_sk": "int",
                "inv_warehouse_sk": "int",
            },
            primary_keys={"inv_date_sk", "inv_item_sk", "inv_warehouse_sk"},
            distinct_counts={
                "inv_date_sk": 261,
                "inv_item_sk": 102000,
                "inv_warehouse_sk": 10,
            },
            row_count=133110000,
        )

        annotated = annotate_table_with_statistics(extractor, "test", "inventory")

        self.assertIn(
            "@inv_date_sk := mod(div(mod(rownum-1, 2610), 10), 261) + 1",
            annotated,
        )
        self.assertIn(
            "@inv_warehouse_sk := mod(div(mod(rownum-1, 2610), 1), 10) + 1",
            annotated,
        )
        self.assertIn(
            "@inv_item_sk := mod(div(rownum-1, 2610) + "
            "mod(rownum-1, 2610) * 51000, 102000) + 1",
            annotated,
        )

    def test_low_cardinality_string_uses_exact_ndv_and_frequency_counts(self):
        ddl = """CREATE TABLE `lineitem` (
  `l_shipmode` varchar(10) DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"l_shipmode": "varchar"},
            distinct_counts={"l_shipmode": 3},
            row_count=10,
            string_group_counts={"l_shipmode": [5, 3, 2]},
        )

        annotated = annotate_table_with_statistics(extractor, "test", "lineitem")

        self.assertIn("@l_shipmode := case", annotated)
        self.assertIn("when rownum <= 5 then", annotated)
        self.assertIn("when rownum <= 8 then", annotated)
        self.assertNotIn("rand.regex", annotated)
        group_queries = [
            sql for sql, _params in extractor.cursor.queries
            if "GROUP BY" in sql
        ]
        self.assertEqual(1, len(group_queries))
        self.assertIn("SELECT COUNT(*) AS cnt", group_queries[0])
        self.assertNotIn("SELECT `l_shipmode`", group_queries[0])

    def test_high_cardinality_string_uses_bounded_synthetic_ndv(self):
        ddl = """CREATE TABLE `lineitem` (
  `l_comment` varchar(44) DEFAULT NULL
)"""
        extractor = FakeExtractor(
            ddl,
            {"l_comment": "varchar"},
            distinct_counts={"l_comment": 33614597},
            row_count=59986052,
        )

        annotated = annotate_table_with_statistics(extractor, "test", "lineitem")

        self.assertIn(
            "@l_comment := 'l_comment_' || (mod(rownum-1, 33614597) + 1)",
            annotated,
        )
        self.assertNotIn("rand.regex", annotated)
        self.assertFalse(any("GROUP BY" in sql for sql, _params in extractor.cursor.queries))


if __name__ == "__main__":
    unittest.main()
