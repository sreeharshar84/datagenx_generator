import json
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

from lib.schema_extractor import TiDBExtractor, available_extractor_types, connection_kwargs_for


class FakeCursor:
    def __init__(self):
        self.column_names = []
        self._rows = []
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("SHOW STATS_HISTOGRAMS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Update_time", "Distinct_count", "Null_count",
                "Avg_col_size", "Correlation",
            ]
            self._rows = [
                ("test", "orders", "", "o_orderkey", 0, None, 4, 0, 8, 1),
            ]
        elif sql.startswith("SHOW STATS_BUCKETS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Bucket_id", "Count", "Repeats",
                "Lower_bound", "Upper_bound", "Ndv",
            ]
            self._rows = [
                ("test", "orders", "", "o_orderkey", 0, 0, 2, 1, "1", "2", 0),
                ("test", "orders", "", "o_orderkey", 0, 1, 4, 1, "3", "4", 0),
            ]
        elif sql.startswith("SHOW STATS_META"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Update_time",
                "Modify_count", "Row_count", "Last_analyze_time",
            ]
            self._rows = [
                ("test", "orders", "", None, 0, 4, None),
            ]
        elif sql.startswith("EXPLAIN FORMAT"):
            self.column_names = ["json"]
            self._rows = [(json.dumps([{"id": "TableReader_1", "subOperators": []}]),)]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeTopNCursor(FakeCursor):
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("SHOW STATS_HISTOGRAMS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Update_time", "Distinct_count", "Null_count",
                "Avg_col_size", "Correlation",
            ]
            self._rows = [
                ("test", "orders", "", "o_status", 0, None, 2, 0, 8, 1),
            ]
        elif sql.startswith("SHOW STATS_BUCKETS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Bucket_id", "Count", "Repeats",
                "Lower_bound", "Upper_bound", "Ndv",
            ]
            self._rows = []
        elif sql.startswith("SHOW STATS_TOPN"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Value", "Count",
            ]
            self._rows = [
                ("test", "orders", "", "o_status", 0, "SECRET_A", 3),
                ("test", "orders", "", "o_status", 0, "SECRET_B", 1),
            ]
        else:
            super().execute(sql, params)


class FakeNativeTopNCursor(FakeTopNCursor):
    def execute(self, sql, params=None):
        if sql.startswith("SHOW STATS_META"):
            self.executed.append((sql, params))
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Update_time",
                "Modify_count", "Row_count", "Last_analyze_time",
            ]
            self._rows = [
                ("test", "orders", "", None, 0, 10, None),
            ]
            return
        super().execute(sql, params)


class FakePartialStatsCursor(FakeCursor):
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.startswith("SHOW STATS_HISTOGRAMS"):
            self.column_names = [
                "Db_name", "Table_name", "Partition_name", "Column_name",
                "Is_index", "Update_time", "Distinct_count", "Null_count",
                "Avg_col_size", "Correlation",
            ]
            self._rows = [
                ("test", "orders", "", "o_orderkey", 0, None, 4, 0, 8, 1),
            ]
        elif "COUNT(DISTINCT" in sql:
            raise AssertionError("TiDB cardinality should not exact-scan missing stats")
        else:
            super().execute(sql, params)


class TiDBExtractorTest(unittest.TestCase):
    def test_factory_lists_tidb(self):
        self.assertIn("tidb", available_extractor_types())

    def test_cloud_connection_kwargs_enable_ssl_verification(self):
        kwargs = connection_kwargs_for(
            "tidb",
            "gateway01.us-west-2.prod.aws.tidbcloud.com",
            "user",
            "password",
            "test",
        )

        self.assertEqual(kwargs["port"], 4000)
        self.assertTrue(kwargs["ssl_verify_cert"])
        self.assertTrue(kwargs["ssl_verify_identity"])
        self.assertTrue(kwargs["allow_local_infile"])

    def test_local_connection_kwargs_do_not_force_ssl(self):
        kwargs = connection_kwargs_for("tidb", "127.0.0.1", "root", "", "test")

        self.assertEqual(kwargs["port"], 4000)
        self.assertNotIn("ssl_verify_cert", kwargs)
        self.assertNotIn("ssl_verify_identity", kwargs)

    def test_convert_tidb_buckets_to_internal_histogram(self):
        histogram = TiDBExtractor._convert_tidb_buckets(
            [
                {"bucket_id": 0, "count": 2, "lower_bound": "1", "upper_bound": "2"},
                {"bucket_id": 1, "count": 4, "lower_bound": "3", "upper_bound": "4"},
            ],
            distinct_count=4,
        )

        self.assertEqual(histogram["histogram-type"], "equi-height")
        self.assertEqual(histogram["buckets"], [
            [1, 2, 0.5, 2],
            [3, 4, 1.0, 2],
        ])

    def test_stats_and_explain_are_normalized(self):
        extractor = TiDBExtractor("127.0.0.1", "root", "", "test")
        extractor.cursor = FakeCursor()

        self.assertEqual(extractor.get_table_row_count("orders"), 4)
        self.assertEqual(extractor.get_column_cardinalities("orders"), {"o_orderkey": 4})

        histogram = extractor.get_column_histogram("orders", "o_orderkey")
        self.assertEqual(len(histogram["buckets"]), 2)
        self.assertEqual(histogram["buckets"][-1][2], 1.0)

        plan = extractor.get_explain_plan("SELECT * FROM orders")
        self.assertEqual(plan["format"], "tidb_json")
        self.assertEqual(plan["plan"][0]["id"], "TableReader_1")

    def test_topn_fallback_uses_synthetic_singleton_values(self):
        extractor = TiDBExtractor("127.0.0.1", "root", "", "test")
        extractor.cursor = FakeTopNCursor()

        histogram = extractor.get_column_histogram("orders", "o_status")

        self.assertEqual(histogram["histogram-type"], "singleton")
        self.assertEqual(histogram["buckets"], [[1, 0.75], [2, 1.0]])

    def test_native_topn_uses_table_row_probability_mass_without_values(self):
        extractor = TiDBExtractor("127.0.0.1", "root", "", "test")
        extractor.cursor = FakeNativeTopNCursor()

        entries = extractor.get_column_topn("orders", "o_status")

        self.assertEqual([entry.ordinal for entry in entries], [1, 2])
        self.assertEqual([entry.count for entry in entries], [3, 1])
        self.assertEqual([entry.frequency for entry in entries], [0.3, 0.1])
        self.assertFalse(any(
            "SECRET" in str(value)
            for entry in entries
            for value in entry.__dict__.values()
        ))

    def test_column_cardinalities_do_not_exact_scan_missing_tidb_stats(self):
        extractor = TiDBExtractor("127.0.0.1", "root", "", "test")
        extractor.cursor = FakePartialStatsCursor()

        self.assertEqual(extractor.get_column_cardinalities("orders"), {"o_orderkey": 4})
        executed_sql = [sql for sql, _params in extractor.cursor.executed]
        self.assertFalse(any("COUNT(DISTINCT" in sql for sql in executed_sql))


if __name__ == "__main__":
    unittest.main()
