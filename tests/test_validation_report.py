import sys
import types
import unittest
from types import SimpleNamespace


mysql_pkg = types.ModuleType("mysql")
connector_mod = types.ModuleType("mysql.connector")


class Error(Exception):
    pass


connector_mod.Error = Error
connector_mod.connect = lambda **kwargs: kwargs
mysql_pkg.connector = connector_mod
sys.modules.setdefault("mysql", mysql_pkg)
sys.modules.setdefault("mysql.connector", connector_mod)

try:
    import pandas  # noqa: F401
except ImportError:
    pandas_mod = types.ModuleType("pandas")

    class MiniDataFrame:
        def __init__(self, records=None, columns=None):
            self._records = list(records or [])
            self.columns = columns or []

        @property
        def empty(self):
            return not self._records

        @property
        def iloc(self):
            return self

        def __getitem__(self, index):
            return self._records[index]

    pandas_mod.DataFrame = MiniDataFrame
    sys.modules["pandas"] = pandas_mod

try:
    import plotly.graph_objects  # noqa: F401
    import plotly.subplots  # noqa: F401
except ImportError:
    plotly_pkg = types.ModuleType("plotly")
    graph_objects_mod = types.ModuleType("plotly.graph_objects")
    subplots_mod = types.ModuleType("plotly.subplots")
    subplots_mod.make_subplots = lambda *args, **kwargs: None
    sys.modules.setdefault("plotly", plotly_pkg)
    sys.modules["plotly.graph_objects"] = graph_objects_mod
    sys.modules["plotly.subplots"] = subplots_mod

from datagenx.validation import validation_report


class FakeTiDBStatsCursor:
    def __init__(self):
        self.column_names = []
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            self.column_names = ["COLUMN_NAME", "DATA_TYPE"]
            self._rows = [("o_orderkey", "int")]
        elif sql.startswith("SHOW STATS_HISTOGRAMS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Update_time", "Distinct_count", "Null_count",
                "Avg_col_size", "Correlation",
            ]
            self._rows = [
                ("src", "orders", "", "o_orderkey", 0, None, 4, 0, 8, 1),
            ]
        elif sql.startswith("SHOW STATS_BUCKETS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Bucket_id", "Count", "Repeats",
                "Lower_bound", "Upper_bound", "Ndv",
            ]
            self._rows = [
                ("src", "orders", "", "o_orderkey", 0, 0, 2, 1, "1", "2", 0),
                ("src", "orders", "", "o_orderkey", 0, 1, 4, 1, "3", "4", 0),
            ]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return self._rows


class FakeOverlapCursor:
    def __init__(self):
        self.column_names = []
        self._rows = []
        self._one = None
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        sql_lower = sql.lower()
        if "column_type" in sql_lower and "information_schema.columns" in sql_lower:
            self.column_names = ["COLUMN_NAME", "COLUMN_TYPE"]
            self._rows = [("id", "int"), ("payload", "varchar(10)")]
        elif "information_schema.columns" in sql_lower:
            self.column_names = ["COLUMN_NAME"]
            self._rows = [("id",), ("payload",)]
        elif "information_schema.key_column_usage" in sql_lower:
            self.column_names = ["COLUMN_NAME"]
            self._rows = [("id",)]
        elif "select count(*) from `src`.`orders`" in sql_lower:
            self._one = (10,)
        elif "select count(*) from `tgt`.`orders`" in sql_lower:
            self._one = (10,)
        elif "select min(`id`), max(`id`)" in sql_lower:
            self._one = (1, 10)
        elif "read_from_storage(tiflash" in sql_lower:
            self._one = (0,)
        elif "inl_join(t)" in sql_lower:
            self._one = (0,)
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class ValidationReportTest(unittest.TestCase):
    def test_connect_uses_backend_connection_kwargs(self):
        captured = {}

        def fake_connect(**kwargs):
            captured.update(kwargs)
            return object()

        old_connect = validation_report.mysql.connector.connect
        validation_report.mysql.connector.connect = fake_connect
        try:
            args = SimpleNamespace(
                db_type="tidb",
                host="gateway01.us-west-2.prod.aws.tidbcloud.com",
                user="u",
                password="p",
                database=None,
                source_schema="src",
                port=None,
            )

            validation_report.connect(args)
        finally:
            validation_report.mysql.connector.connect = old_connect

        self.assertEqual(captured["database"], "src")
        self.assertEqual(captured["port"], 4000)
        self.assertTrue(captured["ssl_verify_cert"])
        self.assertTrue(captured["ssl_verify_identity"])

    def test_tidb_histograms_use_statistics_extractor(self):
        args = SimpleNamespace(
            db_type="tidb",
            host="127.0.0.1",
            user="root",
            password="",
            port=4000,
        )
        cursor = FakeTiDBStatsCursor()

        histograms = validation_report.get_histograms(cursor, args, "src", "orders")

        self.assertEqual(set(histograms), {"o_orderkey"})
        self.assertEqual(histograms["o_orderkey"]["histogram-type"], "equi-height")
        self.assertTrue(any(sql.startswith("SHOW STATS_BUCKETS") for sql, _ in cursor.executed))
        self.assertFalse(any("column_statistics" in sql.lower() for sql, _ in cursor.executed))

    def test_skip_overlap_marks_privacy_as_skip(self):
        skipped = validation_report.get_skipped_overlap(["orders"], "large table")

        row = skipped.iloc[0]
        self.assertEqual(row["table"], "orders")
        self.assertEqual(row["status"], "SKIP")
        self.assertIn("large table", row["reason"])

    def test_frequency_shape_diff_compares_distribution_without_values(self):
        self.assertEqual(validation_report.frequency_shape_diff([1, 1, 1], [1, 1, 1]), 0.0)
        self.assertGreater(validation_report.frequency_shape_diff([3, 1], [2, 2]), 0.0)
        self.assertEqual(
            validation_report.frequency_group_shape_diff([(1, 3)], [(1, 3)]),
            0.0,
        )
        self.assertGreater(
            validation_report.frequency_group_shape_diff([(3, 1), (1, 1)], [(2, 2)]),
            0.0,
        )

    def test_histogram_summary_uses_frequency_fallback_for_diverged_low_ndv_histogram(self):
        old_get_histograms = validation_report.get_histograms
        old_get_indexed_columns = validation_report.get_indexed_columns
        old_get_column_types = validation_report.get_column_types
        old_get_frequency_count_groups = validation_report.get_frequency_count_groups

        source_hist = {
            "k": {
                "histogram-type": "equi-height",
                "buckets": [[1, 2, 0.5, 1], [3, 4, 1.0, 1]],
            }
        }
        target_hist = {
            "k": {
                "histogram-type": "equi-height",
                "buckets": [[1, 3, 0.75, 1], [4, 4, 1.0, 1]],
            }
        }

        try:
            validation_report.get_histograms = (
                lambda cursor, args, schema, table: source_hist if schema == "src" else target_hist
            )
            validation_report.get_indexed_columns = lambda cursor, schema, table: {"k"}
            validation_report.get_column_types = lambda cursor, schema, table: {"k": "int"}
            validation_report.get_frequency_count_groups = (
                lambda cursor, schema, table, column: [(10, 2)]
            )

            args = SimpleNamespace(
                db_type="tidb",
                histogram_fallback_max_rows=1000,
                histogram_fallback_max_distinct=10,
                tidb_histogram_fallback_max_distinct=10,
                sampled_histogram_fallback_max_distinct=10,
            )
            row_df = validation_report.pd.DataFrame([
                {"table": "inventory", "source_rows": 20, "target_rows": 20},
            ])
            distinct_df = validation_report.pd.DataFrame([
                {
                    "table": "inventory",
                    "column": "k",
                    "source_distinct": 2,
                    "target_distinct": 2,
                },
            ])

            rows = validation_report.get_histogram_summary(
                None,
                args,
                "src",
                "tgt",
                ["inventory"],
                row_df=row_df,
                distinct_df=distinct_df,
            )
        finally:
            validation_report.get_histograms = old_get_histograms
            validation_report.get_indexed_columns = old_get_indexed_columns
            validation_report.get_column_types = old_get_column_types
            validation_report.get_frequency_count_groups = old_get_frequency_count_groups

        row = rows.iloc[0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["histogram_diff"], 0.0)
        self.assertIn("exact frequency shape fallback", row["reason"])

    def test_histogram_summary_uses_unique_cardinality_fallback_for_missing_histogram(self):
        old_get_histograms = validation_report.get_histograms
        old_get_indexed_columns = validation_report.get_indexed_columns
        old_get_column_types = validation_report.get_column_types
        old_get_frequency_count_groups = validation_report.get_frequency_count_groups

        source_hist = {
            "item_sk": {
                "histogram-type": "equi-height",
                "buckets": [[1, 100, 0.5, 100], [101, 200, 1.0, 100]],
            }
        }

        try:
            validation_report.get_histograms = (
                lambda cursor, args, schema, table: source_hist if schema == "src" else {}
            )
            validation_report.get_indexed_columns = lambda cursor, schema, table: {"item_sk"}
            validation_report.get_column_types = lambda cursor, schema, table: {"item_sk": "int"}
            validation_report.get_frequency_count_groups = (
                lambda cursor, schema, table, column: (_ for _ in ()).throw(AssertionError("not needed"))
            )

            args = SimpleNamespace(
                db_type="tidb",
                histogram_fallback_max_rows=1000,
                histogram_fallback_max_distinct=10,
                tidb_histogram_fallback_max_distinct=10,
                sampled_histogram_fallback_max_distinct=10,
            )
            row_df = validation_report.pd.DataFrame([
                {"table": "item", "source_rows": 200, "target_rows": 200},
            ])
            distinct_df = validation_report.pd.DataFrame([
                {
                    "table": "item",
                    "column": "item_sk",
                    "source_distinct": 200,
                    "target_distinct": 200,
                },
            ])

            rows = validation_report.get_histogram_summary(
                None,
                args,
                "src",
                "tgt",
                ["item"],
                row_df=row_df,
                distinct_df=distinct_df,
            )
        finally:
            validation_report.get_histograms = old_get_histograms
            validation_report.get_indexed_columns = old_get_indexed_columns
            validation_report.get_column_types = old_get_column_types
            validation_report.get_frequency_count_groups = old_get_frequency_count_groups

        row = rows.iloc[0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["histogram_diff"], 0.0)
        self.assertEqual(row["source_histogram_type"], "unique-cardinality")
        self.assertIn("exact unique cardinality fallback", row["reason"])

    def test_tidb_missing_histogram_uses_frequency_fallback_for_large_low_ndv_critical_column(self):
        old_get_histograms = validation_report.get_histograms
        old_get_indexed_columns = validation_report.get_indexed_columns
        old_get_column_types = validation_report.get_column_types
        old_get_frequency_count_groups = validation_report.get_frequency_count_groups

        source_hist = {
            "item_sk": {
                "histogram-type": "equi-height",
                "buckets": [[1, 100, 1.0, 100]],
            }
        }

        try:
            validation_report.get_histograms = (
                lambda cursor, args, schema, table: source_hist if schema == "src" else {}
            )
            validation_report.get_indexed_columns = lambda cursor, schema, table: {"item_sk"}
            validation_report.get_column_types = lambda cursor, schema, table: {"item_sk": "int"}
            validation_report.get_frequency_count_groups = (
                lambda cursor, schema, table, column: [(3, 1), (2, 1), (1, 1)]
            )

            args = SimpleNamespace(
                db_type="tidb",
                histogram_fallback_max_rows=10000,
                histogram_fallback_max_distinct=100000,
                tidb_histogram_fallback_max_distinct=100000,
                sampled_histogram_fallback_max_distinct=1000,
            )
            row_df = validation_report.pd.DataFrame([
                {"table": "store_sales", "source_rows": 14_000_000, "target_rows": 14_000_000},
            ])
            distinct_df = validation_report.pd.DataFrame([
                {
                    "table": "store_sales",
                    "column": "item_sk",
                    "source_distinct": 54_000,
                    "target_distinct": 54_000,
                },
            ])

            rows = validation_report.get_histogram_summary(
                None,
                args,
                "src",
                "tgt",
                ["store_sales"],
                row_df=row_df,
                distinct_df=distinct_df,
            )
        finally:
            validation_report.get_histograms = old_get_histograms
            validation_report.get_indexed_columns = old_get_indexed_columns
            validation_report.get_column_types = old_get_column_types
            validation_report.get_frequency_count_groups = old_get_frequency_count_groups

        row = rows.iloc[0]
        self.assertEqual(row["status"], "PASS")
        self.assertEqual(row["histogram_diff"], 0.0)
        self.assertIn("exact frequency shape fallback", row["reason"])

    def test_tidb_overlap_uses_primary_key_index_nested_loop(self):
        cursor = FakeOverlapCursor()

        rows = validation_report.get_row_overlap(
            cursor,
            "src",
            "tgt",
            ["orders"],
            db_type="tidb",
        )

        overlap_sql = next(sql for sql in cursor.executed if "INL_JOIN(t)" in sql)
        self.assertIn("STRAIGHT_JOIN", overlap_sql)
        self.assertIn("USE INDEX (PRIMARY)", overlap_sql)
        self.assertEqual(
            rows.iloc[0]["reason"],
            "primary-key index nested-loop exact row comparison",
        )

    def test_tidb_overlap_chunks_large_primary_key_tables(self):
        cursor = FakeOverlapCursor()

        rows = validation_report.get_row_overlap(
            cursor,
            "src",
            "tgt",
            ["orders"],
            db_type="tidb",
            overlap_chunk_rows=2,
        )

        chunk_queries = [sql for sql in cursor.executed if "s.`id` >= %s" in sql]
        self.assertEqual(len(chunk_queries), 5)
        self.assertEqual(rows.iloc[0]["reason"], "primary-key chunked exact row comparison")

    def test_tidb_overlap_can_use_tiflash_mpp(self):
        cursor = FakeOverlapCursor()

        rows = validation_report.get_row_overlap(
            cursor,
            "src",
            "tgt",
            ["orders"],
            db_type="tidb",
            tidb_overlap_strategy="mpp",
        )

        overlap_sql = next(sql for sql in cursor.executed if "READ_FROM_STORAGE" in sql)
        self.assertIn("TIFLASH", overlap_sql)
        self.assertEqual(rows.iloc[0]["reason"], "primary-key TiFlash MPP exact row comparison")

    def test_tidb_overlap_can_chunk_tiflash_mpp(self):
        cursor = FakeOverlapCursor()

        rows = validation_report.get_row_overlap(
            cursor,
            "src",
            "tgt",
            ["orders"],
            db_type="tidb",
            overlap_chunk_rows=2,
            tidb_overlap_strategy="mpp",
        )

        chunk_queries = [sql for sql in cursor.executed if "READ_FROM_STORAGE" in sql and "t.`id` >= %s" in sql]
        self.assertEqual(len(chunk_queries), 5)
        self.assertEqual(rows.iloc[0]["reason"], "primary-key TiFlash MPP chunked exact row comparison")


if __name__ == "__main__":
    unittest.main()
