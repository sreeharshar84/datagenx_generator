#!/usr/bin/env python3
"""
Export Sakila database metadata to JSON files for offline testing.

Run this once against a live MySQL database:
    python3 tests/sakila/export_metadata.py

Generates:
    tests/sakila/schema.json     - DDL, columns, PKs, FKs
    tests/sakila/histograms.json - Histogram data per column
    tests/sakila/indexes.json    - Index info and cardinality
    tests/sakila/stats.json      - Row counts, distinct counts, min/max
"""

import json
import os
import sys

# Add project root to path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))
sys.path.insert(0, PROJECT_ROOT)

import mysql.connector
from config import HOST, USER, PASSWORD

SCHEMA_NAME = "datagenx_test_src"
OUTPUT_DIR = SCRIPT_DIR


def export_schema(cursor, tables):
    """Export DDL, columns, PKs, FKs."""
    schema_data = {"schema_name": SCHEMA_NAME, "tables": {}}

    for table in tables:
        # Get DDL
        cursor.execute(f"SHOW CREATE TABLE `{SCHEMA_NAME}`.`{table}`")
        row = cursor.fetchone()
        ddl = row[1] if row else ""

        # Get columns
        cursor.execute("""
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, EXTRA
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
        """, (SCHEMA_NAME, table))
        columns = {}
        for col_row in cursor.fetchall():
            columns[col_row[0]] = {
                "type": col_row[1],
                "nullable": col_row[2] == "YES",
                "default": col_row[3],
                "key": col_row[4],
                "extra": col_row[5],
            }

        # Get primary key columns
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
            ORDER BY ORDINAL_POSITION
        """, (SCHEMA_NAME, table))
        primary_key = [r[0] for r in cursor.fetchall()]

        # Get foreign keys
        cursor.execute("""
            SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
              AND REFERENCED_TABLE_NAME IS NOT NULL
        """, (SCHEMA_NAME, table))
        foreign_keys = {}
        for fk_row in cursor.fetchall():
            foreign_keys[fk_row[0]] = {
                "references_table": fk_row[1],
                "references_column": fk_row[2],
            }

        schema_data["tables"][table] = {
            "ddl": ddl,
            "columns": columns,
            "primary_key": primary_key,
            "foreign_keys": foreign_keys,
        }

    return schema_data


def export_histograms(cursor, tables):
    """Export histogram data from COLUMN_STATISTICS."""
    histograms = {"schema_name": SCHEMA_NAME, "tables": {}}

    for table in tables:
        cursor.execute("""
            SELECT COLUMN_NAME, HISTOGRAM
            FROM INFORMATION_SCHEMA.COLUMN_STATISTICS
            WHERE SCHEMA_NAME = %s AND TABLE_NAME = %s
        """, (SCHEMA_NAME, table))

        table_histograms = {}
        for row in cursor.fetchall():
            col_name = row[0]
            hist_json = row[1]
            # hist_json is already a dict in newer mysql.connector versions
            if isinstance(hist_json, str):
                hist_json = json.loads(hist_json)
            table_histograms[col_name] = hist_json

        if table_histograms:
            histograms["tables"][table] = table_histograms

    return histograms


def export_indexes(cursor, tables):
    """Export index information from SHOW INDEX."""
    indexes = {"schema_name": SCHEMA_NAME, "tables": {}}

    for table in tables:
        cursor.execute(f"SHOW INDEX FROM `{SCHEMA_NAME}`.`{table}`")
        rows = cursor.fetchall()

        # SHOW INDEX columns: Table, Non_unique, Key_name, Seq_in_index, Column_name,
        # Collation, Cardinality, Sub_part, Packed, Null, Index_type, Comment, Index_comment, Visible, Expression
        table_indexes = []
        for row in rows:
            table_indexes.append({
                "key_name": row[2],
                "seq_in_index": row[3],
                "column_name": row[4],
                "non_unique": row[1],
                "cardinality": row[6],
                "index_type": row[10] if len(row) > 10 else "BTREE",
            })

        if table_indexes:
            indexes["tables"][table] = table_indexes

    return indexes


def export_stats(cursor, tables, schema_data):
    """Export row counts, distinct counts, min/max values."""
    stats = {"schema_name": SCHEMA_NAME, "tables": {}}

    for table in tables:
        # Row count
        cursor.execute(f"SELECT COUNT(*) FROM `{SCHEMA_NAME}`.`{table}`")
        row_count = cursor.fetchone()[0]

        # Per-column stats
        columns_stats = {}
        for col_name, col_info in schema_data["tables"][table]["columns"].items():
            col_type = col_info["type"].upper()

            # Get distinct count
            cursor.execute(f"SELECT COUNT(DISTINCT `{col_name}`) FROM `{SCHEMA_NAME}`.`{table}`")
            distinct_count = cursor.fetchone()[0]

            # Get min/max for numeric/date columns (skip for text/blob)
            min_val = None
            max_val = None
            if not any(t in col_type for t in ["TEXT", "BLOB", "JSON", "ENUM", "SET"]):
                try:
                    cursor.execute(f"SELECT MIN(`{col_name}`), MAX(`{col_name}`) FROM `{SCHEMA_NAME}`.`{table}`")
                    result = cursor.fetchone()
                    min_val = result[0]
                    max_val = result[1]
                    # Convert non-JSON-serializable types
                    if min_val is not None:
                        min_val = str(min_val) if not isinstance(min_val, (int, float, str, type(None))) else min_val
                    if max_val is not None:
                        max_val = str(max_val) if not isinstance(max_val, (int, float, str, type(None))) else max_val
                except Exception:
                    pass  # Skip if query fails

            columns_stats[col_name] = {
                "distinct_count": distinct_count,
                "min": min_val,
                "max": max_val,
            }

        stats["tables"][table] = {
            "row_count": row_count,
            "columns": columns_stats,
        }

    return stats


def main():
    print(f"Connecting to MySQL at {HOST}...")
    conn = mysql.connector.connect(
        host=HOST, user=USER, password=PASSWORD,
        database=SCHEMA_NAME, autocommit=True
    )
    cursor = conn.cursor()

    # Get table list
    cursor.execute("""
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
    """, (SCHEMA_NAME,))
    tables = [r[0] for r in cursor.fetchall()]
    print(f"Found {len(tables)} tables: {', '.join(tables)}")

    # Ensure histograms exist
    print("Running ANALYZE TABLE to ensure histograms are populated...")
    for table in tables:
        # Get column names for this table
        cursor.execute("""
            SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """, (SCHEMA_NAME, table))
        columns = [r[0] for r in cursor.fetchall()]

        if columns:
            col_list = ", ".join(f"`{c}`" for c in columns)
            try:
                cursor.execute(f"ANALYZE TABLE `{SCHEMA_NAME}`.`{table}` UPDATE HISTOGRAM ON {col_list}")
                cursor.fetchall()
            except Exception as e:
                print(f"  Warning: Could not create histograms for {table}: {e}")

    # Export each category
    print("Exporting schema...")
    schema_data = export_schema(cursor, tables)
    with open(os.path.join(OUTPUT_DIR, "schema.json"), "w") as f:
        json.dump(schema_data, f, indent=2)

    print("Exporting histograms...")
    histograms = export_histograms(cursor, tables)
    with open(os.path.join(OUTPUT_DIR, "histograms.json"), "w") as f:
        json.dump(histograms, f, indent=2)

    print("Exporting indexes...")
    indexes = export_indexes(cursor, tables)
    with open(os.path.join(OUTPUT_DIR, "indexes.json"), "w") as f:
        json.dump(indexes, f, indent=2)

    print("Exporting stats...")
    stats = export_stats(cursor, tables, schema_data)
    with open(os.path.join(OUTPUT_DIR, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2)

    cursor.close()
    conn.close()

    print(f"\nExported to {OUTPUT_DIR}:")
    print("  - schema.json")
    print("  - histograms.json")
    print("  - indexes.json")
    print("  - stats.json")


if __name__ == "__main__":
    main()
