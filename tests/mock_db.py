"""
Mock database module for unit tests.

Loads pre-exported JSON fixtures and provides a mock cursor
that returns appropriate data based on query patterns.

Usage:
    from tests.mock_db import MockDatabase

    db = MockDatabase()
    conn = db.connect()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM `schema`.`table`")
    result = cursor.fetchone()
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FIXTURES_DIR = Path(__file__).parent / "sakila"


class MockCursor:
    """Mock MySQL cursor that returns data from JSON fixtures."""

    def __init__(self, db: "MockDatabase"):
        self.db = db
        self._results: List[Tuple] = []
        self._index = 0
        self.lastrowid = 0
        self.rowcount = 0
        self.description = None

    def execute(self, query: str, params: tuple = None):
        """Route query to appropriate handler."""
        # Handle parameterized queries
        if params:
            query_resolved = query
            for p in params:
                query_resolved = query_resolved.replace("%s", repr(p), 1)
        else:
            query_resolved = query

        query_upper = query_resolved.upper().strip()

        if "COLUMN_STATISTICS" in query_upper:
            self._results = self._handle_column_statistics(query_resolved)
        elif "SHOW INDEX" in query_upper:
            self._results = self._handle_show_index(query_resolved)
        elif "COUNT(DISTINCT" in query_upper:
            self._results = self._handle_count_distinct(query_resolved)
        elif re.search(r"COUNT\s*\(\s*\*\s*\)", query_upper):
            self._results = self._handle_count_star(query_resolved)
        elif "MIN(" in query_upper or "MAX(" in query_upper:
            self._results = self._handle_min_max(query_resolved)
        elif "INFORMATION_SCHEMA.TABLES" in query_upper:
            self._results = self._handle_tables_query(query_resolved)
        elif "INFORMATION_SCHEMA.COLUMNS" in query_upper:
            self._results = self._handle_columns_query(query_resolved)
        elif "KEY_COLUMN_USAGE" in query_upper:
            self._results = self._handle_key_column_usage(query_resolved)
        elif "TABLE_CONSTRAINTS" in query_upper:
            self._results = self._handle_constraints_query(query_resolved)
        elif "SHOW CREATE TABLE" in query_upper:
            self._results = self._handle_show_create_table(query_resolved)
        elif query_upper.startswith("SELECT"):
            self._results = self._handle_generic_select(query_resolved)
        elif query_upper.startswith(("SET", "USE", "DROP", "CREATE", "INSERT", "ANALYZE", "TRUNCATE", "LOAD")):
            # DDL/DML - no results
            self._results = []
        else:
            self._results = []

        self._index = 0
        self.rowcount = len(self._results)

    def fetchone(self) -> Optional[Tuple]:
        if self._index < len(self._results):
            row = self._results[self._index]
            self._index += 1
            return row
        return None

    def fetchall(self) -> List[Tuple]:
        results = self._results[self._index:]
        self._index = len(self._results)
        return results

    def fetchmany(self, size: int = 1) -> List[Tuple]:
        results = self._results[self._index:self._index + size]
        self._index += len(results)
        return results

    def close(self):
        pass

    def _extract_table_name(self, query: str) -> Optional[str]:
        """Extract table name from query."""
        patterns = [
            r"FROM\s+`?\w+`?\.`?(\w+)`?",
            r"TABLE_NAME\s*=\s*['\"](\w+)['\"]",
            r"SHOW\s+INDEX\s+FROM\s+`?\w+`?\.`?(\w+)`?",
            r"SHOW\s+INDEX\s+FROM\s+`?(\w+)`?",
            r"SHOW\s+CREATE\s+TABLE\s+`?\w+`?\.`?(\w+)`?",
            r"SHOW\s+CREATE\s+TABLE\s+`?(\w+)`?",
        ]
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _extract_column_name(self, query: str) -> Optional[str]:
        """Extract column name from COUNT(DISTINCT col) or MIN/MAX."""
        patterns = [
            r"COUNT\s*\(\s*DISTINCT\s+`?(\w+)`?\s*\)",
            r"(?:MIN|MAX)\s*\(\s*`?(\w+)`?\s*\)",
        ]
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _handle_column_statistics(self, query: str) -> List[Tuple]:
        """Return histogram data."""
        results = []
        table_match = re.search(r"TABLE_NAME\s*=\s*['\"](\w+)['\"]", query, re.IGNORECASE)
        tables = [table_match.group(1)] if table_match else self.db.histograms.get("tables", {}).keys()

        for table_name in tables:
            if table_name not in self.db.histograms.get("tables", {}):
                continue
            for col_name, hist_data in self.db.histograms["tables"][table_name].items():
                results.append((
                    self.db.schema_name,
                    table_name,
                    col_name,
                    json.dumps(hist_data),
                ))
        return results

    def _handle_show_index(self, query: str) -> List[Tuple]:
        """Return index information."""
        table_name = self._extract_table_name(query)
        if not table_name or table_name not in self.db.indexes.get("tables", {}):
            return []

        results = []
        for idx in self.db.indexes["tables"][table_name]:
            # Match SHOW INDEX output format
            results.append((
                table_name,              # Table
                idx.get("non_unique", 0),  # Non_unique
                idx["key_name"],         # Key_name
                idx["seq_in_index"],     # Seq_in_index
                idx["column_name"],      # Column_name
                "A",                     # Collation
                idx.get("cardinality", 0),  # Cardinality
                None,                    # Sub_part
                None,                    # Packed
                "",                      # Null
                idx.get("index_type", "BTREE"),  # Index_type
                "",                      # Comment
                "",                      # Index_comment
                "YES",                   # Visible
                None,                    # Expression
            ))
        return results

    def _handle_count_distinct(self, query: str) -> List[Tuple]:
        """Handle COUNT(DISTINCT col) queries."""
        table_name = self._extract_table_name(query)
        col_name = self._extract_column_name(query)

        if not table_name or not col_name:
            return [(0,)]

        stats = self.db.stats.get("tables", {}).get(table_name, {})
        col_stats = stats.get("columns", {}).get(col_name, {})
        return [(col_stats.get("distinct_count", 0),)]

    def _handle_count_star(self, query: str) -> List[Tuple]:
        """Handle COUNT(*) queries."""
        table_name = self._extract_table_name(query)
        if not table_name:
            return [(0,)]

        stats = self.db.stats.get("tables", {}).get(table_name, {})
        return [(stats.get("row_count", 0),)]

    def _handle_min_max(self, query: str) -> List[Tuple]:
        """Handle MIN/MAX queries."""
        table_name = self._extract_table_name(query)
        col_name = self._extract_column_name(query)

        if not table_name or not col_name:
            return [(None, None)]

        stats = self.db.stats.get("tables", {}).get(table_name, {})
        col_stats = stats.get("columns", {}).get(col_name, {})

        min_val = col_stats.get("min")
        max_val = col_stats.get("max")

        # Check if both MIN and MAX in query
        query_upper = query.upper()
        if "MIN(" in query_upper and "MAX(" in query_upper:
            return [(min_val, max_val)]
        elif "MIN(" in query_upper:
            return [(min_val,)]
        else:
            return [(max_val,)]

    def _handle_tables_query(self, query: str) -> List[Tuple]:
        """Handle INFORMATION_SCHEMA.TABLES queries."""
        results = []
        for table_name in self.db.schema.get("tables", {}):
            row_count = self.db.stats.get("tables", {}).get(table_name, {}).get("row_count", 0)
            results.append((table_name, row_count))
        return results

    def _handle_columns_query(self, query: str) -> List[Tuple]:
        """Handle INFORMATION_SCHEMA.COLUMNS queries."""
        table_match = re.search(r"TABLE_NAME\s*=\s*['\"](\w+)['\"]", query, re.IGNORECASE)
        tables = [table_match.group(1)] if table_match else self.db.schema.get("tables", {}).keys()

        results = []
        for table_name in tables:
            table_info = self.db.schema.get("tables", {}).get(table_name, {})
            for i, (col_name, col_info) in enumerate(table_info.get("columns", {}).items()):
                results.append((
                    table_name,
                    col_name,
                    i + 1,  # ORDINAL_POSITION
                    col_info.get("type", "VARCHAR(255)"),
                    "YES" if col_info.get("nullable", True) else "NO",
                    col_info.get("default"),
                    col_info.get("key", ""),
                    col_info.get("extra", ""),
                ))
        return results

    def _handle_key_column_usage(self, query: str) -> List[Tuple]:
        """Handle KEY_COLUMN_USAGE queries for FK info."""
        results = []

        # Check if looking for PKs or FKs
        if "CONSTRAINT_NAME = 'PRIMARY'" in query.upper():
            # PK query
            table_match = re.search(r"TABLE_NAME\s*=\s*['\"](\w+)['\"]", query, re.IGNORECASE)
            tables = [table_match.group(1)] if table_match else self.db.schema.get("tables", {}).keys()

            for table_name in tables:
                table_info = self.db.schema.get("tables", {}).get(table_name, {})
                for i, pk_col in enumerate(table_info.get("primary_key", [])):
                    results.append((pk_col, i + 1))  # COLUMN_NAME, ORDINAL_POSITION
        else:
            # FK query
            for table_name, table_info in self.db.schema.get("tables", {}).items():
                for col_name, fk_info in table_info.get("foreign_keys", {}).items():
                    results.append((
                        table_name,
                        col_name,
                        fk_info["references_table"],
                        fk_info["references_column"],
                    ))
        return results

    def _handle_constraints_query(self, query: str) -> List[Tuple]:
        """Handle TABLE_CONSTRAINTS queries."""
        results = []
        for table_name, table_info in self.db.schema.get("tables", {}).items():
            if table_info.get("primary_key"):
                results.append((table_name, "PRIMARY", "PRIMARY KEY"))
            for col_name in table_info.get("foreign_keys", {}):
                results.append((table_name, f"fk_{col_name}", "FOREIGN KEY"))
        return results

    def _handle_show_create_table(self, query: str) -> List[Tuple]:
        """Handle SHOW CREATE TABLE queries."""
        table_name = self._extract_table_name(query)
        if not table_name:
            return []

        table_info = self.db.schema.get("tables", {}).get(table_name, {})
        ddl = table_info.get("ddl", f"CREATE TABLE `{table_name}` ()")
        return [(table_name, ddl)]

    def _handle_generic_select(self, query: str) -> List[Tuple]:
        """Handle generic SELECT queries - return sample PK values."""
        table_name = self._extract_table_name(query)
        if not table_name:
            return []

        # For sampling queries, return PK range
        table_info = self.db.schema.get("tables", {}).get(table_name, {})
        pk_cols = table_info.get("primary_key", [])
        if not pk_cols:
            return []

        pk_col = pk_cols[0]
        stats = self.db.stats.get("tables", {}).get(table_name, {})
        col_stats = stats.get("columns", {}).get(pk_col, {})

        min_val = col_stats.get("min", 1)
        max_val = col_stats.get("max", 100)

        # Return sample values
        if isinstance(min_val, (int, float)) and isinstance(max_val, (int, float)):
            return [(i,) for i in range(int(min_val), min(int(max_val) + 1, int(min_val) + 100))]
        return [(min_val,)]


class MockConnection:
    """Mock MySQL connection."""

    def __init__(self, db: "MockDatabase"):
        self.db = db
        self.autocommit = True

    def cursor(self, **kwargs) -> MockCursor:
        return MockCursor(self.db)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class MockDatabase:
    """Loads JSON fixtures and provides mock connections."""

    def __init__(self, fixtures_dir: Path = FIXTURES_DIR):
        self.fixtures_dir = fixtures_dir
        self.schema_name = "datagenx_test_src"

        # Load fixtures
        self.schema = self._load_json("schema.json")
        self.histograms = self._load_json("histograms.json")
        self.indexes = self._load_json("indexes.json")
        self.stats = self._load_json("stats.json")

        if self.schema:
            self.schema_name = self.schema.get("schema_name", self.schema_name)

    def _load_json(self, filename: str) -> Dict[str, Any]:
        """Load JSON file, return empty dict if not found."""
        path = self.fixtures_dir / filename
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {}

    def connect(self) -> MockConnection:
        return MockConnection(self)


# Drop-in replacement for mysql.connector.connect
_mock_db = None

def connect(host=None, user=None, password=None, database=None, **kwargs) -> MockConnection:
    """Drop-in replacement for mysql.connector.connect."""
    global _mock_db
    if _mock_db is None:
        _mock_db = MockDatabase()
    return _mock_db.connect()


# Exception classes to match mysql.connector
class Error(Exception):
    pass

class DatabaseError(Error):
    pass

class InterfaceError(Error):
    pass
