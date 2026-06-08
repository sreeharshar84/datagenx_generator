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

from datagenx.generation.GenerateDbgen import topological_sort
from datagenx.orchestration.MasterRun import discover_tables_and_dependencies


class FakeCursor:
    def __init__(self):
        self._rows = []

    def execute(self, sql, params=None):
        if "FROM INFORMATION_SCHEMA.TABLES" in sql:
            self._rows = [(table,) for table in [
                "catalog_returns",
                "catalog_sales",
                "customer",
                "date_dim",
                "item",
                "store_returns",
                "store_sales",
                "web_returns",
                "web_sales",
            ]]
        elif "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE" in sql:
            # Simulate benchmark source DDL without physical FK metadata.
            self._rows = []
        elif "FROM INFORMATION_SCHEMA.COLUMNS" in sql:
            columns = {
                "catalog_returns": ["cr_item_sk", "cr_order_number"],
                "catalog_sales": ["cs_item_sk", "cs_order_number"],
                "customer": ["c_customer_sk"],
                "date_dim": ["d_date_sk"],
                "item": ["i_item_sk"],
                "store_returns": ["sr_item_sk", "sr_ticket_number"],
                "store_sales": ["ss_item_sk", "ss_ticket_number"],
                "web_returns": ["wr_item_sk", "wr_order_number"],
                "web_sales": ["ws_item_sk", "ws_order_number"],
            }
            self._rows = [
                (table, column)
                for table, table_columns in columns.items()
                for column in table_columns
            ]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchall(self):
        return self._rows


class MasterRunDependencyTests(unittest.TestCase):
    def test_tpcds_fallback_dependencies_include_return_sales_composite_fks(self):
        tables, dependencies = discover_tables_and_dependencies(FakeCursor(), "src")
        sorted_tables = topological_sort(tables, dependencies)

        self.assertIn("catalog_sales", dependencies["catalog_returns"])
        self.assertIn("store_sales", dependencies["store_returns"])
        self.assertIn("web_sales", dependencies["web_returns"])
        self.assertLess(sorted_tables.index("catalog_sales"), sorted_tables.index("catalog_returns"))
        self.assertLess(sorted_tables.index("store_sales"), sorted_tables.index("store_returns"))
        self.assertLess(sorted_tables.index("web_sales"), sorted_tables.index("web_returns"))


if __name__ == "__main__":
    unittest.main()
