#!/usr/bin/env python3
"""
MasterRun.py — End-to-end data generation and validation.

Orchestrates GenerateDbgen, dbgen binary, and PopulateNewTableAndValidate
for every table in SOURCE_SCHEMA, writing results into TARGET_SCHEMA.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import mysql.connector
from mysql.connector import Error

# Global flags (set by argparse)
VERBOSE = True
COMPARE_HISTOGRAMS = False  # Disabled by default - histogram comparison is unreliable
SKIP_VALIDATION = True
ROWS_OVERRIDE = False
TABLES_FILTER = None  # Optional: comma-separated list of tables to process
COMPOSITE_PK_FREQUENCY_REGISTRY = {}
APPLY_BENCHMARK_FK_DDL = os.environ.get("DATAGENX_APPLY_BENCHMARK_FK_DDL", "1") != "0"

from datagenx.generation.GenerateDbgen import (
    FK_FREQUENCY_SHAPE_MAX_DISTINCT,
    FK_FREQUENCY_SHAPE_MAX_GROUPS,
    annotate_table_with_histogram,
    build_single_fk_expression,
    topological_sort,
)
from extract_schema import annotate_table_with_statistics
from lib.schema_extractor import available_extractor_types, connection_kwargs_for, create_schema_extractor
from datagenx.validation.PopulateNewTableAndValidate import (
    clone_histograms,
    compare_histograms,
    execute_statements,
    load_column_types,
    load_distinct_counts,
    load_histograms,
    load_index_stats,
    load_indexed_columns,
    load_table_stats,
    normalize_ddl,
    pct_diff,
    report_ddl_mismatch,
    report_distinct_counts,
    report_histogram_comparison,
    report_index_stats,
    report_rowcount_mismatch,
    report_table_stats,
)
from datagenx.validation.validation_report import TPCH_FK_FALLBACKS, TPCDS_FK_FALLBACKS

# ----------------------------------------------------------------
# Configuration - imported from central config.py
# ----------------------------------------------------------------
from config import (
    HOST, USER, PASSWORD,
    SOURCE_SCHEMA, TARGET_SCHEMA, DB_TYPE, DB_PORT,
    DBGEN_BINARY, DBGEN_FILES_DIR, DBGEN_TMP_OUT_DIR,
    FILES_COUNT, ROWS_COUNT
)


def _load_histograms_singlestore(cursor, schema, table):
    """Load histograms from SingleStore's ADVANCED_HISTOGRAMS for all columns.

    Returns dict in the same format as MySQL's load_histograms():
      {column_name: {"histogram-type": "equi-height", "buckets": [[lo, hi, cum_freq, num_distinct], ...]}}
    """
    cursor.execute("""
        SELECT COLUMN_NAME, BUCKET_INDEX, RANGE_MIN, RANGE_MAX,
               CARDINALITY, UNIQUE_COUNT
        FROM information_schema.ADVANCED_HISTOGRAMS
        WHERE DATABASE_NAME = %s
          AND TABLE_NAME = %s
          AND BUCKET_INDEX >= 0
        ORDER BY COLUMN_NAME, BUCKET_INDEX
    """, (schema, table))
    rows = cursor.fetchall()

    from collections import defaultdict
    raw = defaultdict(list)
    for col, bucket_idx, range_min, range_max, cardinality, unique_count in rows:
        if range_min is None or range_max is None or cardinality is None:
            continue
        raw[col].append((range_min, range_max, cardinality, unique_count))

    histograms = {}
    for col, buckets in raw.items():
        total_freq = sum(b[2] for b in buckets)
        if total_freq == 0:
            continue
        # Only include columns with numeric range boundaries.
        # String columns have binary-encoded RANGE_MIN/MAX that can't be compared numerically.
        try:
            float(buckets[0][0])
            float(buckets[0][1])
        except (ValueError, TypeError):
            continue
        cum = 0.0
        converted_buckets = []
        for range_min, range_max, cardinality, unique_count in buckets:
            cum += cardinality / total_freq
            converted_buckets.append([
                float(range_min),
                float(range_max),
                round(cum, 5),
                int(unique_count) if unique_count else 1
            ])
        histograms[col] = {"histogram-type": "equi-height", "buckets": converted_buckets}

    return histograms


def _load_histograms_with_extractor(db_type, schema, table):
    """Load histograms through a schema extractor for non-MySQL engines."""
    extractor = create_schema_extractor(db_type, HOST, USER, PASSWORD, schema, DB_PORT)
    if not extractor.connect():
        return {}
    try:
        return extractor.get_table_histograms(table)
    finally:
        extractor.close()


def _find_dbgen_binary():
    """Return the configured dbgen binary path."""
    return os.path.expanduser(DBGEN_BINARY)


def _sql_string_literal(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _histogram_estimated_ndv(histogram):
    if not histogram:
        return None
    buckets = histogram.get("buckets") or []
    hist_type = histogram.get("histogram-type")
    if hist_type == "equi-height":
        total = 0
        for bucket in buckets:
            if len(bucket) >= 4 and bucket[3] is not None:
                try:
                    total += int(bucket[3])
                except (TypeError, ValueError):
                    return None
        return total or None
    if hist_type == "singleton":
        return len(buckets) or None
    return None


def _frequency_shape_groups(cursor, schema, table, column):
    cursor.execute(f"""
        SELECT frequency, COUNT(*) AS value_count
        FROM (
            SELECT COUNT(*) AS frequency
            FROM `{schema}`.`{table}`
            WHERE `{column}` IS NOT NULL
            GROUP BY `{column}`
        ) grouped
        GROUP BY frequency
        ORDER BY frequency
    """)
    return [(int(freq), int(value_count)) for freq, value_count in cursor.fetchall()]


def _frequency_shape_diff_from_groups(source_groups, target_groups):
    source_total = sum(freq * value_count for freq, value_count in source_groups)
    target_total = sum(freq * value_count for freq, value_count in target_groups)
    if source_total <= 0 or target_total <= 0:
        return 1.0

    source_sorted = sorted(source_groups, key=lambda row: row[0], reverse=True)
    target_sorted = sorted(target_groups, key=lambda row: row[0], reverse=True)
    i = j = 0
    source_remaining = source_sorted[0][1] if source_sorted else 0
    target_remaining = target_sorted[0][1] if target_sorted else 0
    distance = 0.0

    while i < len(source_sorted) or j < len(target_sorted):
        source_freq = source_sorted[i][0] if i < len(source_sorted) else 0
        target_freq = target_sorted[j][0] if j < len(target_sorted) else 0
        source_count = source_remaining if i < len(source_sorted) else float("inf")
        target_count = target_remaining if j < len(target_sorted) else float("inf")
        take = min(source_count, target_count)

        source_prob = source_freq / source_total if i < len(source_sorted) else 0.0
        target_prob = target_freq / target_total if j < len(target_sorted) else 0.0
        distance += take * abs(source_prob - target_prob)

        if i < len(source_sorted):
            source_remaining -= take
            if source_remaining == 0:
                i += 1
                if i < len(source_sorted):
                    source_remaining = source_sorted[i][1]
        if j < len(target_sorted):
            target_remaining -= take
            if target_remaining == 0:
                j += 1
                if j < len(target_sorted):
                    target_remaining = target_sorted[j][1]

    return 0.5 * distance


def _unique_cardinality_shape_diff(cursor, table, column):
    counts = []
    for schema in (SOURCE_SCHEMA, TARGET_SCHEMA):
        cursor.execute(f"""
            SELECT COUNT(*) AS rows_total, COUNT(DISTINCT `{column}`) AS distinct_total
            FROM `{schema}`.`{table}`
        """)
        rows_total, distinct_total = cursor.fetchone()
        counts.append((int(rows_total or 0), int(distinct_total or 0)))

    (source_rows, source_distinct), (target_rows, target_distinct) = counts
    if (
        source_rows == target_rows
        and source_rows > 0
        and source_distinct == source_rows
        and target_distinct == target_rows
    ):
        return 0.0
    return None


def _apply_tidb_exact_frequency_histogram_fallback(cursor, table, hist_results, src_hist, tgt_hist):
    """Downgrade TiDB bucket-layout false positives using exact count shapes.

    TiDB may split optimizer histogram buckets differently when the source and
    generated domains use different synthetic value ranges, even if the
    per-value frequency distribution is identical.  For low-NDV columns, compare
    only grouped counts, never source literals.
    """
    if DB_TYPE != "tidb":
        return hist_results

    max_distinct = int(os.environ.get("DATAGENX_HISTOGRAM_EXACT_SHAPE_MAX_DISTINCT", "2000000"))
    adjusted = []
    for col, diff, reason in hist_results:
        if diff < 0.05:
            adjusted.append((col, diff, reason))
            continue

        try:
            unique_diff = _unique_cardinality_shape_diff(cursor, table, col)
        except Exception as e:
            print(f"      Note: exact unique cardinality fallback unavailable for {table}.{col}: {e}")
            unique_diff = None
        if unique_diff is not None:
            adjusted.append((
                col,
                unique_diff,
                f"{reason}; exact unique cardinality diff = {unique_diff:.5f} (TiDB bucket fallback)",
            ))
            continue

        ndv_values = [
            _histogram_estimated_ndv(src_hist.get(col)),
            _histogram_estimated_ndv(tgt_hist.get(col)),
        ]
        ndv_values = [value for value in ndv_values if value is not None]
        if not ndv_values or max(ndv_values) > max_distinct:
            adjusted.append((col, diff, reason))
            continue

        try:
            source_groups = _frequency_shape_groups(cursor, SOURCE_SCHEMA, table, col)
            target_groups = _frequency_shape_groups(cursor, TARGET_SCHEMA, table, col)
            exact_diff = _frequency_shape_diff_from_groups(source_groups, target_groups)
        except Exception as e:
            print(f"      Note: exact frequency fallback unavailable for {table}.{col}: {e}")
            adjusted.append((col, diff, reason))
            continue

        if exact_diff < 0.05:
            adjusted.append((
                col,
                exact_diff,
                f"{reason}; exact frequency shape diff = {exact_diff:.5f} (TiDB bucket fallback)",
            ))
        else:
            adjusted.append((col, diff, reason))

    return adjusted


# ----------------------------------------------------------------
# 1. Setup
# ----------------------------------------------------------------
def discover_tables_and_dependencies(cursor, database):
    """Return (all_tables, dependencies) from INFORMATION_SCHEMA.

    Table names from TABLES and KEY_COLUMN_USAGE can differ in case
    (e.g. 'region' vs 'REGION').  We normalise FK references to match
    the canonical name returned by INFORMATION_SCHEMA.TABLES so the
    topological sort works correctly.
    """
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (database,))
    all_tables = [t[0] for t in cursor.fetchall()]

    # lowercase -> actual name, for resolving case mismatches in FK refs
    canonical = {t.lower(): t for t in all_tables}

    cursor.execute("""
        SELECT TABLE_NAME, REFERENCED_TABLE_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
    """, (database,))

    dependencies = {}
    for table, referenced_table in cursor.fetchall():
        # resolve both sides to canonical casing
        table = canonical.get(table.lower(), table)
        referenced_table = canonical.get(referenced_table.lower(), referenced_table)

        if table not in dependencies:
            dependencies[table] = set()
        if referenced_table and referenced_table != table:
            dependencies[table].add(referenced_table)

    dependencies = add_benchmark_fk_fallback_dependencies(cursor, database, all_tables, dependencies)
    return all_tables, dependencies


def add_benchmark_fk_fallback_dependencies(cursor, database, all_tables, dependencies):
    """Augment discovered dependencies with benchmark FK fallbacks when needed.

    Some benchmark loaders create primary keys and indexes but omit physical
    foreign keys. The generation order still needs those relationships so FK
    appendages can reference already-generated parent tables.
    """
    canonical = {t.lower(): t for t in all_tables}
    normalized = {}
    for table, referenced_tables in (dependencies or {}).items():
        table = canonical.get(table.lower(), table)
        for referenced_table in referenced_tables:
            referenced_table = canonical.get(referenced_table.lower(), referenced_table)
            if referenced_table and referenced_table != table:
                normalized.setdefault(table, set()).add(referenced_table)

    fallback_candidates = []
    table_set = set(all_tables)
    lower_tables = {t.lower() for t in table_set}
    if {"lineitem", "orders", "partsupp"}.issubset(lower_tables):
        fallback_candidates.extend(TPCH_FK_FALLBACKS)
    if {"date_dim", "item", "customer", "store_sales"}.issubset(lower_tables):
        fallback_candidates.extend(TPCDS_FK_FALLBACKS)

    added = 0
    if fallback_candidates:
        cursor.execute("""
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
        """, (database,))
        columns = {}
        for table_name, column_name in cursor.fetchall():
            table_name = canonical.get(table_name.lower(), table_name)
            columns.setdefault(table_name, set()).add(column_name)

        for _name, child_table, parent_table, child_cols, parent_cols in fallback_candidates:
            child_table = canonical.get(child_table.lower(), child_table)
            parent_table = canonical.get(parent_table.lower(), parent_table)
            if child_table not in columns or parent_table not in columns:
                continue
            if not all(col in columns[child_table] for col in child_cols):
                continue
            if not all(col in columns[parent_table] for col in parent_cols):
                continue
            if child_table != parent_table:
                refs = normalized.setdefault(child_table, set())
                before = len(refs)
                refs.add(parent_table)
                if len(refs) != before:
                    added += 1

    if added:
        print(f"Using {added} benchmark FK fallback dependenc{'y' if added == 1 else 'ies'} for table ordering.")

    return {k: sorted(v) for k, v in normalized.items()}


def regenerate_histograms_with_full_sampling(conn, cursor, database):
    """Regenerate all histograms with sampling_rate=1.0 for accurate num_distinct values.

    MySQL samples data when histogram_generation_max_mem_size is exceeded.
    We set it high enough to read all data, ensuring bucket[3] (num_distinct) is accurate.

    Returns the (possibly refreshed) cursor.
    """
    print("Regenerating histograms with full sampling...")

    # Set high memory limit to avoid sampling
    cursor.execute("SET GLOBAL histogram_generation_max_mem_size = 1000000000")  # 1GB

    # Get all columns that currently have histograms
    cursor.execute("""
        SELECT TABLE_NAME, COLUMN_NAME
        FROM information_schema.COLUMN_STATISTICS
        WHERE SCHEMA_NAME = %s
        ORDER BY TABLE_NAME, COLUMN_NAME
    """, (database,))
    columns_with_histograms = cursor.fetchall()

    if not columns_with_histograms:
        print("  No existing histograms found.")
        return cursor

    # Group by table
    table_columns = {}
    for table, column in columns_with_histograms:
        if table not in table_columns:
            table_columns[table] = []
        table_columns[table].append(column)

    # Regenerate histograms for each table
    connection_lost = False
    for table, columns in table_columns.items():
        cols_str = ", ".join(f"`{c}`" for c in columns)
        sql = f"ANALYZE TABLE `{database}`.`{table}` UPDATE HISTOGRAM ON {cols_str} WITH 100 BUCKETS"
        try:
            cursor.execute(sql)
            cursor.fetchall()  # consume results
            print(f"  {table}: OK")
        except Exception as e:
            print(f"  Warning: Failed to regenerate histogram for {table}: {e}")
            if "Lost connection" in str(e) or "2013" in str(e):
                connection_lost = True
            continue

    # Reconnect if connection was lost during histogram regeneration
    if connection_lost:
        print("  Reconnecting after connection loss...")
        cursor = _refresh_cursor(conn, cursor)

    # Verify sampling rates
    try:
        cursor.execute("""
            SELECT TABLE_NAME, MIN(HISTOGRAM->>'$."sampling-rate"') as min_rate
            FROM information_schema.COLUMN_STATISTICS
            WHERE SCHEMA_NAME = %s
            GROUP BY TABLE_NAME
            HAVING min_rate < 1.0
        """, (database,))
        low_sampling = cursor.fetchall()

        if low_sampling:
            print(f"  Warning: {len(low_sampling)} tables still have sampling_rate < 1.0:")
            for table, rate in low_sampling:
                print(f"    {table}: {rate}")
        else:
            print(f"  All {len(table_columns)} tables now have sampling_rate = 1.0")
    except Exception as e:
        print(f"  Warning: Could not verify sampling rates: {e}")
        cursor = _refresh_cursor(conn, cursor)

    return cursor


def prepare_target_schema(cursor, target_schema, tables_to_drop=None):
    """Create target schema and drop existing target tables.

    When a table filter is active, only drop those target tables so referenced
    parent tables remain available for FK creation and expression generation.
    """
    cursor.execute(
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = %s",
        (target_schema,),
    )
    exists = cursor.fetchone() is not None

    if exists:
        cursor.execute("""
            SELECT TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        """, (target_schema,))
        tables = [t[0] for t in cursor.fetchall()]
        if tables_to_drop is not None:
            requested = {table.lower() for table in tables_to_drop}
            tables = [table for table in tables if table.lower() in requested]

        if tables:
            print(f"Dropping {len(tables)} existing table(s) in `{target_schema}`...")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            for t in tables:
                cursor.execute(f"DROP TABLE IF EXISTS `{target_schema}`.`{t}`")
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    else:
        cursor.execute(f"CREATE SCHEMA `{target_schema}`")
        print(f"Created schema `{target_schema}`.")


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _benchmark_fk_script_for_tables(tables):
    table_names = {table.lower() for table in tables}
    if {"lineitem", "orders", "partsupp", "customer", "supplier"}.issubset(table_names):
        return "TPC-H", os.path.join(_project_root(), "scripts", "tpch_fk.sql")
    if {"date_dim", "item", "customer", "store_sales", "catalog_sales", "web_sales"}.issubset(table_names):
        return "TPC-DS", os.path.join(_project_root(), "scripts", "tpcds_fk.sql")
    return None, None


def _strip_sql_comments(statement):
    lines = []
    for line in statement.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _cursor_connection(cursor):
    return getattr(cursor, "_connection", None) or getattr(cursor, "connection", None)


def _is_retriable_connection_error(exc):
    return getattr(exc, "errno", None) in {2006, 2013, 2055}


def _fk_constraint_exists(cursor, target_schema, constraint_name):
    cursor.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = %s
          AND CONSTRAINT_NAME = %s
          AND CONSTRAINT_TYPE = 'FOREIGN KEY'
    """, (target_schema, constraint_name))
    return bool(cursor.fetchone()[0])


def _active_fk_ddl_count(cursor, target_schema, constraint_name):
    cursor.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.PROCESSLIST
        WHERE ID <> CONNECTION_ID()
          AND INFO IS NOT NULL
          AND INFO LIKE %s
          AND INFO LIKE %s
          AND INFO LIKE '%%ALTER TABLE%%'
    """, (f"%`{target_schema}`%", f"%{constraint_name}%"))
    return int(cursor.fetchone()[0] or 0)


def apply_benchmark_fk_script(cursor, target_schema, loaded_tables):
    """Apply benchmark FK constraints after all target tables are loaded.

    Source benchmark loaders may not create physical FK metadata, especially for
    TPC-DS. Generation and validation can use fallback relationships, but adding
    physical constraints to the generated target makes information_schema reflect
    the same relationships. The SQL scripts are intentionally applied only after
    all tables have been loaded.
    """
    if not APPLY_BENCHMARK_FK_DDL:
        print("Skipping benchmark FK script because DATAGENX_APPLY_BENCHMARK_FK_DDL=0.")
        return cursor
    if DB_TYPE not in ("mysql", "tidb"):
        return cursor
    if TABLES_FILTER:
        print("Skipping benchmark FK script for partial table run.")
        return cursor

    benchmark, script_path = _benchmark_fk_script_for_tables(loaded_tables)
    if not benchmark or not script_path or not os.path.exists(script_path):
        return cursor

    cursor.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = %s
          AND CONSTRAINT_TYPE = 'FOREIGN KEY'
    """, (target_schema,))
    existing_fk_count = cursor.fetchone()[0]
    if existing_fk_count:
        print(
            f"{benchmark} FK script for `{target_schema}` found "
            f"{existing_fk_count} existing physical FK constraint(s); "
            "applying missing constraints only."
        )

    with open(script_path) as f:
        sql = f.read()

    statements = [
        _strip_sql_comments(stmt)
        for stmt in sql.split(";")
    ]
    statements = [stmt for stmt in statements if stmt]

    applied = 0
    skipped = 0
    print(f"Applying {benchmark} FK constraints to `{target_schema}` from {script_path}...")

    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    for statement in statements:
        if statement.upper().startswith("SET "):
            cursor.execute(statement)
            continue

        constraint_name = None
        match = re.search(
            r"\bADD\s+CONSTRAINT\s+`?([A-Za-z0-9_]+)`?",
            statement,
            flags=re.IGNORECASE,
        )
        if match:
            constraint_name = match.group(1)
            if _fk_constraint_exists(cursor, target_schema, constraint_name):
                skipped += 1
                continue

        statement = re.sub(
            r"^\s*ALTER\s+TABLE\s+`?([A-Za-z0-9_]+)`?",
            rf"ALTER TABLE `{target_schema}`.`\1`",
            statement,
            count=1,
            flags=re.IGNORECASE,
        )
        statement = re.sub(
            r"\bREFERENCES\s+`?([A-Za-z0-9_]+)`?\s*\(",
            rf"REFERENCES `{target_schema}`.`\1` (",
            statement,
            flags=re.IGNORECASE,
        )
        for attempt in range(1, 4):
            try:
                cursor.execute(statement)
                applied += 1
                break
            except Error as exc:
                if not _is_retriable_connection_error(exc) or attempt == 3:
                    raise

                print(
                    f"      WARN: connection lost while applying FK DDL"
                    f"{f' {constraint_name}' if constraint_name else ''}; "
                    f"checking server-side DDL state before retry ({attempt}/2)"
                )
                conn = _cursor_connection(cursor)
                if conn is None:
                    raise
                for wait_attempt in range(1, 61):
                    cursor = _refresh_cursor(conn, cursor)
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
                    if constraint_name and _fk_constraint_exists(cursor, target_schema, constraint_name):
                        applied += 1
                        break
                    active_ddl = (
                        _active_fk_ddl_count(cursor, target_schema, constraint_name)
                        if constraint_name else 0
                    )
                    if not active_ddl:
                        break
                    if wait_attempt == 60:
                        raise
                    print(
                        f"      FK DDL {constraint_name} still active in TiDB; "
                        f"waiting 30s before retry check"
                    )
                    time.sleep(30)
                else:
                    continue
                if constraint_name and _fk_constraint_exists(cursor, target_schema, constraint_name):
                    break

    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    print(f"      FK constraints applied: {applied}, skipped existing: {skipped}")
    return cursor


# ----------------------------------------------------------------
# 2. Per-table processing
# ----------------------------------------------------------------
def build_fk_appendages(cursor, table, extractor=None):
    """For each FK column in `table`, build dbgen expressions.

    Handles three cases:
    1. Composite PK where all columns are FKs (e.g., PARTSUPP):
       Uses interleaved arithmetic for full coverage of both domains.
    2. Composite FK referencing another table's composite key (e.g., LINEITEM):
       Uses same interleaved formula as referenced table, cycling as needed.
    3. Single-column FKs: Uses rand.range() for uniform distribution.
    """

    def schema_columns(schema):
        cursor.execute("""
            SELECT TABLE_NAME, COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
        """, (schema,))
        columns = {}
        for table_name, column_name in cursor.fetchall():
            columns.setdefault(table_name, set()).add(column_name)
        return columns

    def fallback_fk_rows():
        source_columns = schema_columns(SOURCE_SCHEMA)
        target_columns = schema_columns(TARGET_SCHEMA)
        tables = set(source_columns) | set(target_columns)
        candidates = []
        if {"lineitem", "orders", "partsupp"}.issubset(tables):
            candidates.extend(TPCH_FK_FALLBACKS)
        if {"date_dim", "item", "customer", "store_sales"}.issubset(tables):
            candidates.extend(TPCDS_FK_FALLBACKS)

        rows = []
        seen = set()
        for name, child_table, ref_table, child_cols, ref_cols in candidates:
            if child_table != table:
                continue
            if child_table not in source_columns or ref_table not in source_columns:
                continue
            if ref_table not in target_columns:
                continue
            if not all(col in source_columns[child_table] for col in child_cols):
                continue
            if not all(col in source_columns[ref_table] for col in ref_cols):
                continue
            if not all(col in target_columns[ref_table] for col in ref_cols):
                continue
            if name in seen:
                continue
            seen.add(name)
            for child_col, ref_col in zip(child_cols, ref_cols):
                rows.append((name, child_col, ref_table, ref_col))
        return rows

    # Build canonical name map for target schema (handles case mismatches)
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    """, (TARGET_SCHEMA,))
    tgt_canonical = {t[0].lower(): t[0] for t in cursor.fetchall()}

    # FK columns grouped by constraint name
    cursor.execute("""
        SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
    """, (SOURCE_SCHEMA, table))
    fk_rows = cursor.fetchall()
    if not fk_rows:
        fk_rows = fallback_fk_rows()
        if fk_rows:
            print(f"      Using {len(set(row[0] for row in fk_rows))} fallback FK relationship(s)")
    if not fk_rows:
        return {}

    # Group by constraint name
    from collections import defaultdict
    constraints = defaultdict(list)  # constraint_name -> [(col, ref_table, ref_col), ...]
    for constraint_name, col, ref_table, ref_col in fk_rows:
        constraints[constraint_name].append((col, ref_table, ref_col))

    # PK columns for this table
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
    """, (SOURCE_SCHEMA, table))
    pk_columns = {r[0] for r in cursor.fetchall()}

    # Get source row count for this table
    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    source_row_count = cursor.fetchone()[0]

    # Collect all FK columns that are also PK columns
    all_fk_columns = {col for _, cols in constraints.items() for col, _, _ in cols}
    pk_fk_columns = pk_columns & all_fk_columns

    # Check if ALL PK columns are FKs (composite PK case like PARTSUPP)
    # This can happen with multiple single-column FKs forming the PK
    all_pk_are_fk = (len(pk_columns) > 1 and pk_columns == pk_fk_columns)

    appendages = {}
    source_column_cardinality = None

    def estimated_source_distinct(col):
        """Return source NDV, using engine stats for scaled-down non-MySQL runs."""
        nonlocal source_column_cardinality
        if DB_TYPE == 'tidb' and ROWS_OVERRIDE:
            try:
                if source_column_cardinality is None:
                    cursor.execute(
                        "SHOW STATS_HISTOGRAMS "
                        f"WHERE Db_name = {_sql_string_literal(SOURCE_SCHEMA)} "
                        f"AND Table_name = {_sql_string_literal(table)} "
                        "AND Is_index = 0"
                    )
                    names = [name.lower() for name in cursor.column_names]
                    source_column_cardinality = {}
                    for row in cursor.fetchall():
                        values = dict(zip(names, row))
                        column = values.get("column_name")
                        distinct = values.get("distinct_count")
                        if column and distinct:
                            source_column_cardinality[column] = int(distinct)
                if col in source_column_cardinality:
                    return source_column_cardinality[col]
            except Exception as e:
                print(f"      Warning: Could not read {col} cardinality from {DB_TYPE} stats: {e}")

            fallback = max(1, min(source_row_count, int(ROWS_COUNT)))
            print(
                f"      Warning: {DB_TYPE} stats missing NDV for {table}.{col}; "
                f"using bounded replay estimate {fallback}"
            )
            return fallback

        cursor.execute(
            f"SELECT COUNT(DISTINCT `{col}`) FROM `{SOURCE_SCHEMA}`.`{table}`"
        )
        return cursor.fetchone()[0]

    def build_single_fk_appendage(col, actual_ref, ref_col):
        kwargs = {}
        if DB_TYPE == 'tidb' and ROWS_OVERRIDE:
            kwargs = {
                "source_distinct_override": estimated_source_distinct(col),
                "source_row_count_override": source_row_count,
                "prefer_cycling": True,
            }
        return build_single_fk_expression(
            cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table, col, actual_ref, ref_col, **kwargs
        )

    def synthetic_pk_base(default=1):
        """Use a generated-domain base instead of anchoring to source MIN()."""
        return default

    def synthetic_frequency_case_expression(col):
        """Return deterministic CASE preserving source value frequencies.

        This is useful for non-FK columns in composite primary keys such as
        TPC-H lineitem.l_linenumber. The companion FK PK column cycles through
        parent keys, and this expression assigns line numbers in contiguous
        bands so (orderkey, linenumber) stays unique while the marginal
        frequency shape matches the source. The generated values are synthetic
        ordinals, not the source literals.
        """
        cursor.execute(f"""
            SELECT `{col}`, COUNT(*)
            FROM `{SOURCE_SCHEMA}`.`{table}`
            GROUP BY `{col}`
            ORDER BY `{col}`
        """)
        frequencies = cursor.fetchall()
        if not frequencies:
            return None, None

        cumulative = 0
        case_lines = []
        for ordinal, (_value, count) in enumerate(frequencies, start=1):
            cumulative += count
            case_lines.append(f"when rownum <= {cumulative} then {ordinal}")

        expression = f"""case
    {' '.join(case_lines)}
    else {len(frequencies)}
    end"""
        return expression, len(frequencies)

    def build_grouped_parent_sequence_appendages():
        """Build expressions for parent-FK plus sequence composite PKs.

        Pattern:
            PRIMARY KEY(parent_fk, sequence_col)
            parent_fk references a parent table
            sequence_col is a small integer position within each parent group

        TPC-H lineitem is the canonical example:
            PRIMARY KEY(l_orderkey, l_linenumber)

        Independent cycling preserves uniqueness but makes l_linenumber uniform.
        Grouped generation preserves the source distribution of child rows per
        parent, which naturally preserves the sequence-column histogram.
        """
        if not is_composite_pk or not has_pk_fk_columns or not has_non_fk_pk_columns:
            return None
        if len(pk_fk_columns) != 1 or len(pk_columns - pk_fk_columns) != 1:
            return None

        parent_col = next(iter(pk_fk_columns))
        sequence_col = next(iter(pk_columns - pk_fk_columns))

        # Only apply this to integer-like sequence columns. Other non-FK PK
        # columns can keep the existing generic composite-PK fallback.
        cursor.execute("""
            SELECT DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
        """, (SOURCE_SCHEMA, table, sequence_col))
        row = cursor.fetchone()
        sequence_type = row[0].lower() if row and row[0] else ""
        if sequence_type not in {"tinyint", "smallint", "mediumint", "int", "bigint"}:
            return None

        # Check if sequence_col is truly a low-cardinality sequence column.
        # True sequence columns (like l_linenumber) have few distinct values (~7).
        # High-cardinality ID columns (like ss_ticket_number) should NOT use this logic.
        cursor.execute(
            f"SELECT COUNT(DISTINCT `{sequence_col}`) FROM `{SOURCE_SCHEMA}`.`{table}`"
        )
        sequence_distinct = cursor.fetchone()[0]

        # Threshold: sequence columns have << 0.1% of row count distinct values
        # l_linenumber: 7 distinct / 6M rows = 0.0001%
        # ss_ticket_number: 240K distinct / 2.8M rows = 8.5%
        max_sequence_threshold = max(100, source_row_count // 1000)
        if sequence_distinct > max_sequence_threshold:
            print(
                f"      Grouped PK {parent_col},{sequence_col}: "
                f"SKIPPED ({sequence_col} has {sequence_distinct} distinct values - "
                f"not a sequence column)"
            )
            return None

        parent_fk = None
        for fk_cols in constraints.values():
            if len(fk_cols) == 1 and fk_cols[0][0] == parent_col:
                parent_fk = fk_cols[0]
                break
        if parent_fk is None:
            return None

        _, ref_table, ref_col = parent_fk
        actual_ref = tgt_canonical.get(ref_table.lower())
        if actual_ref is None:
            return None

        cursor.execute(
            f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) "
            f"FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
        )
        parent_count, parent_min = cursor.fetchone()
        parent_count = parent_count or 0
        parent_min = parent_min if parent_min is not None else synthetic_pk_base()
        if parent_count <= 0:
            return None

        cursor.execute(f"""
            SELECT group_size, COUNT(*) AS parent_groups
            FROM (
                SELECT `{parent_col}`, COUNT(*) AS group_size
                FROM `{SOURCE_SCHEMA}`.`{table}`
                WHERE `{parent_col}` IS NOT NULL
                  AND `{sequence_col}` IS NOT NULL
                GROUP BY `{parent_col}`
            ) grouped
            GROUP BY group_size
            ORDER BY group_size
        """)
        group_distribution = [
            (int(group_size), int(parent_groups))
            for group_size, parent_groups in cursor.fetchall()
            if group_size and parent_groups
        ]
        if not group_distribution:
            return None

        total_groups = sum(parent_groups for _, parent_groups in group_distribution)
        total_rows = sum(group_size * parent_groups for group_size, parent_groups in group_distribution)
        if total_rows != source_row_count:
            print(
                f"      Grouped PK {parent_col},{sequence_col}: "
                f"SKIPPED (group rows {total_rows} != source rows {source_row_count})"
            )
            return None
        if total_groups > parent_count:
            print(
                f"      Grouped PK {parent_col},{sequence_col}: "
                f"SKIPPED (needs {total_groups} parent keys, target has {parent_count})"
            )
            return None

        parent_case_lines = []
        sequence_case_lines = []
        cumulative_rows = 0
        parent_offset = 0
        max_group_size = 1

        for group_size, parent_groups in group_distribution:
            max_group_size = max(max_group_size, group_size)
            band_start = cumulative_rows + 1
            band_rows = group_size * parent_groups
            cumulative_rows += band_rows
            parent_start = parent_min + parent_offset
            parent_offset += parent_groups
            local_row = f"rownum-{band_start}"

            parent_expr = f"{parent_start}+div({local_row},{group_size})"
            sequence_expr = f"mod({local_row},{group_size})+{synthetic_pk_base()}"
            parent_case_lines.append(f"when rownum <= {cumulative_rows} then {parent_expr}")
            sequence_case_lines.append(f"when rownum <= {cumulative_rows} then {sequence_expr}")

        parent_fallback = parent_min + max(total_groups - 1, 0)
        sequence_fallback = max_group_size
        result = {
            parent_col: f"""case
    {' '.join(parent_case_lines)}
    else {parent_fallback}
    end""",
            sequence_col: f"""case
    {' '.join(sequence_case_lines)}
    else {sequence_fallback}
    end""",
        }
        print(
            f"      Grouped PK {parent_col},{sequence_col}: "
            f"{total_groups} parent groups, group sizes "
            f"{', '.join(f'{size}x{groups}' for size, groups in group_distribution)}"
        )
        return result

    def build_two_column_fk_pk_frequency_shape_appendages():
        """Preserve FK+PK marginal frequency shape while keeping a 2-column PK unique.

        Pattern:
            PRIMARY KEY(fk_col, partner_col)
            fk_col references a parent table
            partner_col is not an FK

        If no generated FK value needs more rows than the partner column's NDV,
        the partner can cycle within each FK frequency band without duplicating
        the composite key. Source input is only grouped frequency counts, never
        source key literals.
        """
        if not is_composite_pk or len(pk_fk_columns) != 1 or len(non_fk_pk_cols) != 1:
            return None

        fk_col = next(iter(pk_fk_columns))
        partner_col = next(iter(non_fk_pk_cols))

        fk_info = None
        for fk_cols in constraints.values():
            for col, ref_table, ref_col in fk_cols:
                if col == fk_col:
                    fk_info = (ref_table, ref_col)
                    break
            if fk_info:
                break
        if fk_info is None:
            return None

        ref_table, ref_col = fk_info
        actual_ref = tgt_canonical.get(ref_table.lower())
        if actual_ref is None:
            return None

        fk_distinct = estimated_source_distinct(fk_col)
        partner_distinct = estimated_source_distinct(partner_col)
        if not fk_distinct or not partner_distinct:
            return None
        if fk_distinct > FK_FREQUENCY_SHAPE_MAX_DISTINCT:
            return None

        cursor.execute(
            f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`), MAX(`{ref_col}`) "
            f"FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
        )
        target_distinct, target_min, target_max = cursor.fetchone()
        target_distinct = int(target_distinct or 0)
        if target_distinct < fk_distinct or target_min is None or target_max is None:
            return None
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (target_min, target_max)):
            return None
        if target_max - target_min + 1 != target_distinct:
            return None

        valid_fk_predicate = f"`{fk_col}` IS NOT NULL"
        if target_min > 0:
            valid_fk_predicate = f"`{fk_col}` IS NOT NULL AND `{fk_col}` > 0"

        cursor.execute(f"""
            SELECT frequency, COUNT(*) AS value_count
            FROM (
                SELECT COUNT(*) AS frequency
                FROM `{SOURCE_SCHEMA}`.`{table}`
                WHERE {valid_fk_predicate}
                GROUP BY `{fk_col}`
            ) grouped
            GROUP BY frequency
            ORDER BY frequency
        """)
        groups = [
            (int(frequency), int(value_count))
            for frequency, value_count in cursor.fetchall()
            if frequency and value_count
        ]
        if not groups or len(groups) > FK_FREQUENCY_SHAPE_MAX_GROUPS:
            return None
        if max(frequency for frequency, _value_count in groups) > partner_distinct:
            return None

        grouped_distinct = sum(value_count for _frequency, value_count in groups)
        grouped_rows = sum(frequency * value_count for frequency, value_count in groups)
        if grouped_distinct != fk_distinct or grouped_rows != source_row_count:
            return None

        fk_case_lines = []
        cumulative_rows = 0
        distinct_before = 0
        for frequency, value_count in groups:
            band_start = cumulative_rows + 1
            band_rows = frequency * value_count
            cumulative_rows += band_rows
            group_start = target_min + distinct_before
            fk_case_lines.append(
                f"when rownum <= {cumulative_rows} then "
                f"{group_start}+div(rownum-{band_start},{frequency})"
            )
            distinct_before += value_count

        if distinct_before != fk_distinct:
            return None

        result = {
            fk_col: f"""case
    {' '.join(fk_case_lines)}
    else {target_min + fk_distinct - 1}
    end""",
            partner_col: f"mod(rownum-1, {partner_distinct})+{synthetic_pk_base()}",
        }
        COMPOSITE_PK_FREQUENCY_REGISTRY[table.lower()] = {
            "fk_col": fk_col,
            "partner_col": partner_col,
            "target_min": target_min,
            "fk_distinct": fk_distinct,
            "partner_distinct": partner_distinct,
            "groups": groups,
            "expressions": result,
        }
        print(
            f"      FK+PK {fk_col} -> {actual_ref}.{ref_col}: "
            f"frequency-shape PK/FK ({fk_distinct} distinct, "
            f"{len(groups)} frequency groups); {partner_col} cycles {partner_distinct}"
        )
        return result

    def build_frequency_shape_composite_fk_appendages_from_parent(fk_cols, actual_ref):
        """Generate composite FK pairs from a registered synthetic parent PK shape."""
        parent_shape = COMPOSITE_PK_FREQUENCY_REGISTRY.get(actual_ref.lower())
        if not parent_shape or len(fk_cols) != 2:
            return None

        child_by_parent_col = {ref_col: col for col, _ref_table, ref_col in fk_cols}
        parent_fk_col = parent_shape["fk_col"]
        parent_partner_col = parent_shape["partner_col"]
        child_fk_col = child_by_parent_col.get(parent_fk_col)
        child_partner_col = child_by_parent_col.get(parent_partner_col)
        if not child_fk_col or not child_partner_col:
            return None

        child_distinct = estimated_source_distinct(child_fk_col)
        if not child_distinct or child_distinct > FK_FREQUENCY_SHAPE_MAX_DISTINCT:
            return None

        valid_fk_predicate = f"`{child_fk_col}` IS NOT NULL"
        if parent_shape["target_min"] > 0:
            valid_fk_predicate = f"`{child_fk_col}` IS NOT NULL AND `{child_fk_col}` > 0"

        cursor.execute(f"""
            SELECT frequency, COUNT(*) AS value_count
            FROM (
                SELECT COUNT(*) AS frequency
                FROM `{SOURCE_SCHEMA}`.`{table}`
                WHERE {valid_fk_predicate}
                GROUP BY `{child_fk_col}`
            ) grouped
            GROUP BY frequency
            ORDER BY frequency
        """)
        child_groups = [
            (int(frequency), int(value_count))
            for frequency, value_count in cursor.fetchall()
            if frequency and value_count
        ]
        if not child_groups or len(child_groups) > FK_FREQUENCY_SHAPE_MAX_GROUPS:
            return None

        grouped_distinct = sum(value_count for _frequency, value_count in child_groups)
        grouped_rows = sum(frequency * value_count for frequency, value_count in child_groups)
        if grouped_distinct != child_distinct or grouped_rows != source_row_count:
            return None

        parent_segments = []
        value_start = parent_shape["target_min"]
        rows_before = 0
        for parent_frequency, value_count in parent_shape["groups"]:
            parent_segments.append((parent_frequency, value_count, value_start, rows_before))
            value_start += value_count
            rows_before += parent_frequency * value_count

        allocations = []
        segment_index = 0
        used_in_segment = 0
        skipped_values = 0
        for child_frequency, child_value_count in child_groups:
            remaining = child_value_count
            while remaining > 0:
                while segment_index < len(parent_segments):
                    parent_frequency, parent_value_count, _value_start, _rows_before = parent_segments[segment_index]
                    available = parent_value_count - used_in_segment
                    if available > 0 and parent_frequency >= child_frequency:
                        break
                    skipped_values += max(available, 0)
                    segment_index += 1
                    used_in_segment = 0
                if segment_index >= len(parent_segments):
                    return None

                parent_frequency, parent_value_count, value_start, rows_before = parent_segments[segment_index]
                available = parent_value_count - used_in_segment
                take = min(remaining, available)
                allocations.append((
                    child_frequency,
                    take,
                    parent_frequency,
                    value_start + used_in_segment,
                    rows_before + (used_in_segment * parent_frequency),
                ))
                remaining -= take
                used_in_segment += take

        if not allocations:
            return None

        fk_case_lines = []
        partner_case_lines = []
        cumulative_rows = 0
        for child_frequency, value_count, parent_frequency, value_start, parent_rows_before in allocations:
            band_start = cumulative_rows + 1
            band_rows = child_frequency * value_count
            cumulative_rows += band_rows
            local_row = f"rownum-{band_start}"
            child_value_offset = f"div({local_row},{child_frequency})"
            child_occurrence_offset = f"mod({local_row},{child_frequency})"
            spread_parent_offset = (
                f"div({child_occurrence_offset}*{parent_frequency},{child_frequency})"
            )
            parent_row0 = (
                f"{parent_rows_before}+{child_value_offset}*{parent_frequency}+{spread_parent_offset}"
            )
            fk_case_lines.append(
                f"when rownum <= {cumulative_rows} then {value_start}+{child_value_offset}"
            )
            partner_case_lines.append(
                f"when rownum <= {cumulative_rows} then "
                f"mod({parent_row0}, {parent_shape['partner_distinct']})+{synthetic_pk_base()}"
            )

        if cumulative_rows != source_row_count:
            return None

        result = {
            child_fk_col: f"""case
    {' '.join(fk_case_lines)}
    else {allocations[-1][3] + allocations[-1][1] - 1}
    end""",
            child_partner_col: f"""case
    {' '.join(partner_case_lines)}
    else {synthetic_pk_base()}
    end""",
        }
        skipped = f", skipped {skipped_values} parent value(s)" if skipped_values else ""
        print(
            f"      Composite FK {child_fk_col},{child_partner_col} -> "
            f"{actual_ref}.{parent_fk_col},{parent_partner_col}: "
            f"frequency-shape parent tuples ({child_distinct} distinct, "
            f"{len(child_groups)} child frequency groups{skipped})"
        )
        return result

    def build_composite_fk_appendages_from_parent(fk_cols, actual_ref):
        """Generate child composite-FK columns from existing parent key tuples."""
        frequency_shape_appendages = build_frequency_shape_composite_fk_appendages_from_parent(
            fk_cols,
            actual_ref,
        )
        if frequency_shape_appendages:
            return frequency_shape_appendages

        cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{actual_ref}`")
        ref_row_count = cursor.fetchone()[0]
        if not ref_row_count:
            return {}

        col_info = []
        for col, _, ref_col in fk_cols:
            cursor.execute(
                f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) "
                f"FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
            )
            distinct_count, min_val = cursor.fetchone()
            distinct_count = int(distinct_count or 0)
            if distinct_count <= 0:
                return {}
            min_val = min_val if min_val is not None else 0
            source_distinct = estimated_source_distinct(col)
            col_info.append((col, ref_col, source_distinct, distinct_count, min_val))

        col_info.sort(key=lambda x: x[2], reverse=True)
        _, _, largest_source_distinct, largest_parent_distinct, _ = col_info[0]
        parent_span = max(
            1,
            (largest_source_distinct * ref_row_count + largest_parent_distinct - 1)
            // largest_parent_distinct,
        )
        raw_parent_pos = f"div((rownum-1)*{parent_span},{source_row_count})"
        parent_pos = raw_parent_pos

        # If a non-leading child FK has a slightly smaller NDV than the parent
        # key, cap the selected parent positions to keep child-side NDV within
        # the normal 5% validation threshold while still selecting real parent
        # key tuples.
        if len(col_info) == 2:
            _, _, source_distinct, parent_distinct, _ = col_info[1]
            cap = min(parent_distinct, max(source_distinct, (source_distinct * 100) // 95))
            if 0 < cap < parent_distinct:
                parent_mod = f"mod({raw_parent_pos},{parent_distinct})"
                parent_pos = (
                    "case "
                    f"when {parent_mod} >= {cap} then "
                    f"{raw_parent_pos}-({parent_mod}-mod({parent_mod},{cap})) "
                    f"else {raw_parent_pos} "
                    "end"
                )

        generated = {}
        for i, (col, ref_col, source_distinct, parent_distinct, min_val) in enumerate(col_info):
            if i == 0:
                expr = f"div(({parent_pos})*{parent_distinct},{ref_row_count})+{min_val}"
                print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                      f"parent-position span={parent_span}, source={source_distinct} -> {expr}")
            else:
                expr = f"mod({parent_pos}, {parent_distinct})+{min_val}"
                print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                      f"parent-position source={source_distinct}, parent={parent_distinct} -> {expr}")
            generated[col] = expr
        return generated

    for constraint_name, fk_cols in constraints.items():
        if len(fk_cols) < 2:
            continue
        child_cols = {col for col, _, _ in fk_cols}
        if not child_cols.issubset(pk_columns):
            continue
        if any(col in appendages for col in child_cols):
            continue
        ref_table = fk_cols[0][1]
        if any(candidate_ref_table != ref_table for _, candidate_ref_table, _ in fk_cols):
            continue
        actual_ref = tgt_canonical.get(ref_table.lower())
        if actual_ref is None:
            print(f"      Composite FK -> {ref_table}: "
                  f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
            continue
        appendages.update(build_composite_fk_appendages_from_parent(fk_cols, actual_ref))

    if all_pk_are_fk:
        # Composite PK where all columns are FKs (e.g., PARTSUPP, inventory)
        # Collect info for all PK+FK columns across all constraints
        # We need BOTH reference table info (for valid FK values) AND source distinct counts
        pk_fk_info = []  # [(col, ref_table, ref_col, source_distinct, ref_distinct, min_val), ...]
        for constraint_name, fk_cols in constraints.items():
            for col, ref_table, ref_col in fk_cols:
                if col in pk_columns:
                    if col in appendages:
                        continue
                    actual_ref = tgt_canonical.get(ref_table.lower())
                    if actual_ref is None:
                        continue
                    # Get reference table info (for valid FK range)
                    cursor.execute(
                        f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                    )
                    ref_distinct, min_val = cursor.fetchone()
                    min_val = min_val if min_val is not None else 0

                    # Get SOURCE distinct count (actual cardinality we need to match)
                    source_distinct = estimated_source_distinct(col)

                    pk_fk_info.append((col, actual_ref, ref_col, source_distinct, ref_distinct, min_val))

        if len(pk_fk_info) >= 2:
            # N-CYCLING APPROACH for composite FK+PK (any number of columns)
            # See N_CYCLING_COMPOSITE_FK_PK.md for detailed explanation.
            #
            # Problem: Odometer can only give full coverage to ONE dimension.
            # Solution: Largest dimension uses div (grouping), all others use mod (cycling).
            #
            # Pattern:
            #   largest_col = div(rownum-1, rows_per_largest) + min  (grouped)
            #   other_cols  = mod(rownum-1, distinct) + min          (cycling)
            #
            # This guarantees:
            #   - Full coverage of ALL dimensions
            #   - Unique PK combinations (when cycling cols wrap, largest has advanced)

            # Sort by source_distinct DESCENDING (largest first)
            pk_fk_info.sort(key=lambda x: x[3], reverse=True)

            # Calculate rows per largest value using CEILING division
            # This ensures div() never exceeds source_distinct, avoiding FK violations
            # and eliminating need for mod() wrapper (which causes PK collisions)
            largest_source = pk_fk_info[0][3]
            rows_per_largest = max(1, (source_row_count + largest_source - 1) // largest_source)

            for i, (col, ref_table, ref_col, source_distinct, ref_distinct, min_val) in enumerate(pk_fk_info):
                if i == 0:
                    # Largest dimension: grouped via div
                    # With ceiling division, div() stays within [0, source_distinct-1]
                    expr = f"div(rownum-1, {rows_per_largest})+{min_val}"
                    print(f"      FK+PK {col} -> {ref_table}.{ref_col}: "
                          f"source={source_distinct}, rows_per_value={rows_per_largest}, "
                          f"min={min_val} -> {expr}")
                else:
                    # Other dimensions: cycling via mod
                    expr = f"mod(rownum-1, {source_distinct})+{min_val}"
                    print(f"      FK+PK {col} -> {ref_table}.{ref_col}: "
                          f"source={source_distinct}, cycling, "
                          f"min={min_val} -> {expr}")
                appendages[col] = expr

        # Handle any remaining FK-only columns (not part of PK)
        for constraint_name, fk_cols in constraints.items():
            for col, ref_table, ref_col in fk_cols:
                if col not in pk_columns and col not in appendages:
                    actual_ref = tgt_canonical.get(ref_table.lower())
                    if actual_ref is None:
                        continue
                    expression, description = build_single_fk_appendage(col, actual_ref, ref_col)
                    appendages[col] = expression
                    print(f"      FK {col} -> {actual_ref}.{ref_col}: {description}")

        return appendages

    # Check for partial FK+PK case: composite PK where SOME (but not all) columns are FKs
    # Example: store_sales PK is (ss_item_sk, ss_ticket_number) where only ss_item_sk is FK
    # In this case, FK columns in PK must use mod() cycling (not random) to coordinate
    # with non-FK PK columns which use div() grouping in GenerateDbgen.py
    is_composite_pk = len(pk_columns) > 1
    has_pk_fk_columns = len(pk_fk_columns) > 0
    has_non_fk_pk_columns = len(pk_columns - pk_fk_columns) > 0

    if is_composite_pk and has_pk_fk_columns and has_non_fk_pk_columns:
        # Partial FK+PK case: FK columns in PK use mod() cycling
        # Non-FK PK columns use div() grouping to coordinate (avoid PK collisions)
        # Composite FKs (not in PK) use n-cycling to generate valid pairs
        non_fk_pk_cols = pk_columns - pk_fk_columns

        grouped_appendages = build_grouped_parent_sequence_appendages()
        if grouped_appendages:
            appendages.update(grouped_appendages)

        frequency_shape_appendages = build_two_column_fk_pk_frequency_shape_appendages()
        if frequency_shape_appendages:
            appendages.update(frequency_shape_appendages)

        # First, calculate the cycle length for FK+PK columns (product of their distinct counts)
        fk_pk_cycle_length = 1
        for constraint_name, fk_cols in constraints.items():
            for col, ref_table, ref_col in fk_cols:
                if col in pk_columns:
                    source_distinct = estimated_source_distinct(col)
                    fk_pk_cycle_length *= source_distinct

        # Handle non-FK PK columns with div() grouping
        # FK+PK columns use mod() (cycling), non-FK PK columns use div() (grouping)
        # This combination avoids PK collisions:
        #   Rows 1-12: ticket=1, item cycles 1->12
        #   Rows 13-24: ticket=2, item cycles 1->12
        for col in non_fk_pk_cols:
            if col in appendages:
                continue
            source_distinct = estimated_source_distinct(col)
            min_val = synthetic_pk_base()
            # Use proportional scaling: div((rownum-1)*D, N) maps N rows to exactly D distinct values
            # This distributes values evenly - some appear floor(N/D) times, others ceil(N/D) times
            # dbgen uses i128 arithmetic, so (rownum-1)*source_distinct won't overflow
            expr = f"div((rownum-1)*{source_distinct},{source_row_count})+{min_val}"
            print(f"      PK {col}: proportional div((rownum-1)*{source_distinct},{source_row_count})+{min_val} -> {source_distinct} distinct")
            appendages[col] = expr

        for constraint_name, fk_cols in constraints.items():
            # Separate columns into PK and non-PK groups
            pk_cols_in_constraint = [(c, rt, rc) for c, rt, rc in fk_cols if c in pk_columns]
            non_pk_cols_in_constraint = [(c, rt, rc) for c, rt, rc in fk_cols if c not in pk_columns]

            # Handle FK+PK columns with mod() cycling
            for col, ref_table, ref_col in pk_cols_in_constraint:
                if col in appendages:
                    continue
                actual_ref = tgt_canonical.get(ref_table.lower())
                if actual_ref is None:
                    continue
                source_distinct = estimated_source_distinct(col)
                cursor.execute(
                    f"SELECT MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                )
                min_val = cursor.fetchone()[0]
                min_val = min_val if min_val is not None else 1
                expr = f"mod(rownum-1, {source_distinct})+{min_val}"
                appendages[col] = expr
                print(f"      FK+PK {col} -> {actual_ref}.{ref_col}: cycling mod({source_distinct})+{min_val}")

            # Handle non-PK FK columns
            if len(non_pk_cols_in_constraint) == 1:
                # Single-column FK - use normal FK expression
                col, ref_table, ref_col = non_pk_cols_in_constraint[0]
                actual_ref = tgt_canonical.get(ref_table.lower())
                if actual_ref is None:
                    continue
                expression, description = build_single_fk_appendage(col, actual_ref, ref_col)
                appendages[col] = expression
                print(f"      FK {col} -> {actual_ref}.{ref_col}: {description}")

            elif len(non_pk_cols_in_constraint) >= 2:
                # Composite FK (not in PK) - use n-cycling to match referenced table
                ref_table = non_pk_cols_in_constraint[0][1]
                actual_ref = tgt_canonical.get(ref_table.lower())
                if actual_ref is None:
                    print(f"      Composite FK -> {ref_table}: "
                          f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
                    continue

                # Get row count of referenced table (total valid pairs)
                cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{actual_ref}`")
                ref_row_count = cursor.fetchone()[0]

                # Get distinct counts and min values for each column
                col_info = []
                for col, _, ref_col in non_pk_cols_in_constraint:
                    cursor.execute(
                        f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                    )
                    distinct_count, min_val = cursor.fetchone()
                    min_val = min_val if min_val is not None else 0
                    col_info.append((col, ref_col, distinct_count, min_val))

                # N-CYCLING: Sort by distinct count DESCENDING (largest first)
                col_info.sort(key=lambda x: x[2], reverse=True)

                # Use CEILING division (matches referenced table)
                largest_distinct = col_info[0][2]
                rows_per_largest = max(1, (ref_row_count + largest_distinct - 1) // largest_distinct)

                for i, (col, ref_col, distinct_count, min_val) in enumerate(col_info):
                    if i == 0:
                        # Largest dimension: div (grouped)
                        expr = f"div(mod(rownum-1, {ref_row_count}), {rows_per_largest})+{min_val}"
                        print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                              f"n-cycling {ref_row_count} pairs, rows_per_value={rows_per_largest} -> {expr}")
                    else:
                        # Other dimensions: mod (cycling)
                        expr = f"mod(mod(rownum-1, {ref_row_count}), {distinct_count})+{min_val}"
                        print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                              f"n-cycling {ref_row_count} pairs, cycling mod {distinct_count} -> {expr}")
                    appendages[col] = expr

        return appendages

    # Normal case: process each constraint
    for constraint_name, fk_cols in constraints.items():
        if len(fk_cols) == 1:
            # Single-column FK - use unified FK expression builder
            col, ref_table, ref_col = fk_cols[0]
            actual_ref = tgt_canonical.get(ref_table.lower())
            if actual_ref is None:
                print(f"      FK {col} -> {ref_table}.{ref_col}: "
                      f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
                continue

            expression, description = build_single_fk_appendage(col, actual_ref, ref_col)
            appendages[col] = expression
            print(f"      FK {col} -> {actual_ref}.{ref_col}: {description}")

        else:
            # Composite FK (multiple columns reference same table, e.g., LINEITEM -> PARTSUPP)
            ref_table = fk_cols[0][1]
            actual_ref = tgt_canonical.get(ref_table.lower())
            if actual_ref is None:
                print(f"      Composite FK -> {ref_table}: "
                      f"SKIPPED (referenced table not yet in {TARGET_SCHEMA})")
                continue

            # Get row count of referenced table (total valid pairs)
            cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{actual_ref}`")
            ref_row_count = cursor.fetchone()[0]

            # Get distinct counts and min values for each column in the composite FK
            col_info = []  # [(col, ref_col, distinct_count, min_val), ...]
            for col, _, ref_col in fk_cols:
                cursor.execute(
                    f"SELECT COUNT(DISTINCT `{ref_col}`), MIN(`{ref_col}`) FROM `{TARGET_SCHEMA}`.`{actual_ref}`"
                )
                distinct_count, min_val = cursor.fetchone()
                min_val = min_val if min_val is not None else 0
                col_info.append((col, ref_col, distinct_count, min_val))

            # N-CYCLING for composite FK references
            # Must generate pairs that MATCH the referenced table's n-cycling pattern.
            # The referenced table uses: largest=div, others=mod
            # We use the same pattern, but wrap with mod(rownum-1, ref_row_count) to cycle.

            # Sort by distinct count DESCENDING (largest first) - matches n-cycling
            col_info.sort(key=lambda x: x[2], reverse=True)

            # Calculate rows_per_largest using CEILING division (matches referenced table)
            largest_distinct = col_info[0][2]
            rows_per_largest = max(1, (ref_row_count + largest_distinct - 1) // largest_distinct)

            for i, (col, ref_col, distinct_count, min_val) in enumerate(col_info):
                if i == 0:
                    # Largest dimension: div (grouped), cycling through ref_row_count pairs
                    expr = f"div(mod(rownum-1, {ref_row_count}), {rows_per_largest})+{min_val}"
                    print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                          f"n-cycling {ref_row_count} pairs, rows_per_value={rows_per_largest} -> {expr}")
                else:
                    # Other dimensions: mod (cycling)
                    expr = f"mod(mod(rownum-1, {ref_row_count}), {distinct_count})+{min_val}"
                    print(f"      Composite FK {col} -> {actual_ref}.{ref_col}: "
                          f"n-cycling {ref_row_count} pairs, cycling mod {distinct_count} -> {expr}")
                appendages[col] = expr

    return appendages


def step_a_generate_dbgen(cursor, table, extractor=None):
    """Generate .dbgen template file.

    Dispatches based on DB_TYPE:
      - mysql: uses annotate_table_with_histogram (MySQL histogram system)
      - other extractors: use annotate_table_with_statistics (engine stats)
    """
    print(f"  [A] Generating .dbgen template ...")

    generated_appendages = build_fk_appendages(cursor, table, extractor=extractor)

    if DB_TYPE != 'mysql':
        ddl = annotate_table_with_statistics(
            extractor, SOURCE_SCHEMA, table,
            generated_appendages=generated_appendages,
        )
    else:
        ddl = annotate_table_with_histogram(
            HOST, USER, PASSWORD, SOURCE_SCHEMA, table,
            generated_appendages=generated_appendages,
        )

    if ddl is None:
        print(f"  [A] FAILED — annotation function returned None")
        return False

    path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    with open(path, "w") as f:
        f.write(ddl)
    print(f"  [A] Wrote {path}")
    return True


def step_b_run_dbgen(cursor, table):
    """Run dbgen binary to produce .csv file. Returns True on success."""
    print(f"  [B] Running dbgen binary ...")

    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    row_count = cursor.fetchone()[0]
    rows_to_generate = int(ROWS_COUNT) if ROWS_OVERRIDE else row_count
    print(f"      Source row count = {row_count}")
    if ROWS_OVERRIDE:
        print(f"      Generating {rows_to_generate} rows (--rows override)")
    else:
        print(f"      Generating {rows_to_generate} rows (matches source)")

    template_path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    dbgen_bin = _find_dbgen_binary()
    if not os.path.isfile(dbgen_bin) or not os.access(dbgen_bin, os.X_OK):
        print(f"  [B] FAILED — dbgen binary not found or not executable: {dbgen_bin}")
        return False

    cmd = [
        dbgen_bin,
        "--out-dir", DBGEN_TMP_OUT_DIR,
        "--files-count", FILES_COUNT,
        "--rows-per-file", str(rows_to_generate),
        "--rows-count", str(rows_to_generate),
        "--template", template_path,
        "--format", "csv",           # Generate CSV instead of SQL
        "--quiet",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  [B] FAILED — dbgen returned {result.returncode}")
        print(result.stderr)
        return False

    csv_path = os.path.join(DBGEN_TMP_OUT_DIR, f"{table}.1.csv")
    if not os.path.isfile(csv_path):
        print(f"  [B] FAILED — expected output file not found: {csv_path}")
        return False

    print(f"  [B] Generated {csv_path}")
    return True


def _load_source_cardinality_fast(cursor, database, table):
    """Load distinct counts from source using fast GROUP BY queries for key columns.

    For SingleStore, this is faster than SHOW INDEX which can be slow on large tables.
    Returns dict {column_name: distinct_count}.
    """
    if DB_TYPE == 'tidb' and database == SOURCE_SCHEMA and ROWS_OVERRIDE:
        cursor.execute(
            "SHOW STATS_HISTOGRAMS "
            f"WHERE Db_name = {_sql_string_literal(database)} "
            f"AND Table_name = {_sql_string_literal(table)} "
            "AND Is_index = 0"
        )
        names = [name.lower() for name in cursor.column_names]
        cardinalities = {}
        for row in cursor.fetchall():
            values = dict(zip(names, row))
            column = values.get("column_name")
            distinct = values.get("distinct_count")
            if column and distinct:
                cardinalities[column] = int(distinct)
        return cardinalities

    # Get PK columns
    cursor.execute("""
        SELECT COLUMN_NAME
        FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
    """, (database, table))
    pk_cols = [row[0] for row in cursor.fetchall()]

    # Also check for UNIQUE KEY `pk` (SingleStore convention)
    if not pk_cols:
        cursor.execute("""
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
              AND INDEX_NAME = 'pk' AND NON_UNIQUE = 0
            ORDER BY SEQ_IN_INDEX
        """, (database, table))
        pk_cols = [row[0] for row in cursor.fetchall()]

    # Get indexed columns
    cursor.execute("""
        SELECT DISTINCT COLUMN_NAME
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
    """, (database, table))
    indexed_cols = {row[0] for row in cursor.fetchall()}

    distinct_counts = {}

    # For PK and indexed columns, query actual distinct counts
    for col in pk_cols + list(indexed_cols):
        if col not in distinct_counts:
            try:
                cursor.execute(
                    f"SELECT COUNT(DISTINCT `{col}`) FROM `{database}`.`{table}`"
                )
                distinct_counts[col] = cursor.fetchone()[0]
            except Exception as e:
                print(f"      Warning: Could not get distinct count for {col}: {e}")

    return distinct_counts


def _parse_size_bytes(value, default):
    if not value:
        return default

    text = str(value).strip().lower()
    multiplier = 1
    if text[-1:] in {"k", "m", "g"}:
        suffix = text[-1]
        text = text[:-1]
        multiplier = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[suffix]

    try:
        return int(float(text) * multiplier)
    except ValueError:
        return default


def _sql_string(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _mysql_cli_base_command():
    mysql_bin = shutil.which("mysql")
    if not mysql_bin:
        return None

    cmd = [
        mysql_bin,
        "--local-infile=1",
        "--connect-timeout=30",
        "-h", HOST,
        "-u", USER,
    ]
    if DB_PORT is not None:
        cmd.extend(["-P", str(DB_PORT)])
    if HOST.endswith("tidbcloud.com"):
        cmd.append("--ssl")
    return cmd


def _run_mysql_cli_load(schema, sql):
    cmd = _mysql_cli_base_command()
    if cmd is None:
        return False

    env = os.environ.copy()
    if PASSWORD:
        env["MYSQL_PWD"] = PASSWORD

    completed = subprocess.run(
        cmd + [schema, "-e", sql],
        env=env,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(details or f"mysql exited with {completed.returncode}")
    return True


def _load_csv_file(schema, table, csv_path, column_list):
    load_stmt = f"""
        LOAD DATA LOCAL INFILE {_sql_string(csv_path)}
        INTO TABLE `{schema}`.`{table}`
        FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '\\\\'
        LINES TERMINATED BY '\\n'
        ({column_list})
    """
    if _run_mysql_cli_load(schema, load_stmt):
        return
    raise RuntimeError("mysql CLI is required for chunked TiDB LOAD DATA")


def _load_generated_csv(cursor, table, csv_path, column_list):
    default_chunk_size = "16m" if DB_TYPE == "tidb" else "128m"
    default_chunk_bytes = 16 * 1024 * 1024 if DB_TYPE == "tidb" else 128 * 1024 * 1024
    chunk_bytes = _parse_size_bytes(
        os.environ.get("DATAGENX_LOAD_CHUNK_BYTES", default_chunk_size),
        default_chunk_bytes,
    )
    attempts = int(os.environ.get("DATAGENX_LOAD_RETRY_ATTEMPTS", "3"))
    file_size = os.path.getsize(csv_path)

    if file_size <= chunk_bytes:
        load_stmt = f"""
            LOAD DATA LOCAL INFILE {_sql_string(csv_path)}
            INTO TABLE `{TARGET_SCHEMA}`.`{table}`
            FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '\\\\'
            LINES TERMINATED BY '\\n'
            ({column_list})
        """
        cursor.execute(load_stmt)
        return

    chunk_dir = os.path.join(DBGEN_TMP_OUT_DIR, ".load_chunks", table)

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            cursor.execute(f"TRUNCATE TABLE `{TARGET_SCHEMA}`.`{table}`")

        print(
            f"      Loading {os.path.basename(csv_path)} in "
            f"{chunk_bytes // (1024 * 1024)}MiB chunks "
            f"(attempt {attempt}/{attempts})"
        )

        try:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            os.makedirs(chunk_dir, exist_ok=True)

            chunk_index = 0
            current_size = 0
            current_path = None
            current = None

            def close_and_load_current():
                nonlocal current, current_path, current_size
                if current is None:
                    return
                current.close()
                _load_csv_file(TARGET_SCHEMA, table, current_path, column_list)
                os.remove(current_path)
                current = None
                current_path = None
                current_size = 0

            with open(csv_path, "rb") as source:
                for line in source:
                    if current is None:
                        current_path = os.path.join(
                            chunk_dir,
                            f"{table}_{chunk_index:05d}.csv",
                        )
                        current = open(current_path, "wb")
                        chunk_index += 1

                    if current_size and current_size + len(line) > chunk_bytes:
                        close_and_load_current()
                        current_path = os.path.join(
                            chunk_dir,
                            f"{table}_{chunk_index:05d}.csv",
                        )
                        current = open(current_path, "wb")
                        chunk_index += 1

                    current.write(line)
                    current_size += len(line)

            close_and_load_current()
            cursor._connection.ping(reconnect=True, attempts=3, delay=5)
            return
        except Exception as exc:
            if current is not None:
                current.close()
            shutil.rmtree(chunk_dir, ignore_errors=True)
            print(f"      WARN: chunked load failed for {table}: {exc}")
            if attempt == attempts:
                raise
            time.sleep(attempt * 30)


def _refresh_cursor(conn, cursor=None):
    try:
        conn.ping(reconnect=True, attempts=3, delay=5)
    except TypeError:
        conn.ping(reconnect=True)
    except Error:
        conn.reconnect(attempts=3, delay=5)

    try:
        if cursor is not None:
            cursor.close()
    except Error:
        pass
    return conn.cursor()


def _new_schema_extractor():
    extractor = create_schema_extractor(DB_TYPE, HOST, USER, PASSWORD, SOURCE_SCHEMA, DB_PORT)
    if not extractor.connect():
        raise RuntimeError(f"Failed to connect to {DB_TYPE}")
    return extractor


def step_c_create_insert_validate(cursor, table):
    """Create table in target schema, insert generated data, validate."""
    action = "creating table and inserting data" if SKIP_VALIDATION else "creating table, inserting data, validating"
    print(f"  [C] {action} ...")

    ddl_ok = rows_ok = hist_ok = distinct_ok = True

    # --- Read DDL from .dbgen file ---
    dbgen_path = os.path.join(DBGEN_FILES_DIR, f"{table}.dbgen")
    with open(dbgen_path) as f:
        dbgen_ddl = f.read()

    # Strip dbgen annotations to get clean DDL
    # Note: re.DOTALL makes . match newlines (annotations can span multiple lines)
    clean_ddl = re.sub(r"/\*\{\{.*?\}\}\*/", "", dbgen_ddl, flags=re.DOTALL)

    # Replace table name with target-schema-qualified name
    create_stmt = re.sub(
        r"CREATE\s+TABLE\s+`" + re.escape(table) + r"`",
        f"CREATE TABLE `{TARGET_SCHEMA}`.`{table}`",
        clean_ddl,
        count=1,
        flags=re.IGNORECASE,
    )

    # Update FK REFERENCES to point to target schema
    create_stmt = re.sub(
        r"REFERENCES\s+`([^`]+)`\s*\(",
        rf"REFERENCES `{TARGET_SCHEMA}`.`\1` (",
        create_stmt,
        flags=re.IGNORECASE,
    )

    cursor.execute(create_stmt)
    print(f"      Created `{TARGET_SCHEMA}`.`{table}`")

    # --- Load CSV data using LOAD DATA LOCAL INFILE ---
    csv_path = os.path.abspath(os.path.join(DBGEN_TMP_OUT_DIR, f"{table}.1.csv"))

    # Extract column names from DDL (columns are between first ( and PRIMARY KEY or first constraint)
    # Pattern: `column_name` type ... ,
    col_pattern = r"`(\w+)`\s+\w+"
    # Find content between CREATE TABLE ... ( and PRIMARY KEY or CONSTRAINT or KEY
    ddl_body_match = re.search(r"CREATE\s+TABLE[^(]*\((.*?)(?:PRIMARY\s+KEY|CONSTRAINT|KEY\s+`)", clean_ddl, re.DOTALL | re.IGNORECASE)
    if ddl_body_match:
        ddl_body = ddl_body_match.group(1)
        columns = re.findall(col_pattern, ddl_body)
    else:
        # Fallback: get all backtick-quoted identifiers before PRIMARY KEY
        columns = re.findall(col_pattern, clean_ddl.split("PRIMARY KEY")[0])

    column_list = ", ".join(f"`{col}`" for col in columns)

    _load_generated_csv(cursor, table, csv_path, column_list)

    # Clone optimizer histogram metadata as part of target creation, not
    # validation. The separate validator expects target histograms to exist.
    # Only for MySQL
    if DB_TYPE == 'mysql':
        clone_histograms(cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table)

    if SKIP_VALIDATION:
        print(f"      Loaded generated data into `{TARGET_SCHEMA}`.`{table}`")
        if DB_TYPE == 'mysql':
            print(f"      Cloned histograms from `{SOURCE_SCHEMA}`.`{table}`")
        return {
            "loaded": True,
            "validation_skipped": True,
        }

    # --- Validate row count ---
    cursor.execute(f"SELECT COUNT(*) FROM `{SOURCE_SCHEMA}`.`{table}`")
    src_rows = cursor.fetchone()[0]

    cursor.execute(f"SELECT COUNT(*) FROM `{TARGET_SCHEMA}`.`{table}`")
    tgt_rows = cursor.fetchone()[0]

    # When --rows is explicitly specified and differs from source, compare against it
    if ROWS_OVERRIDE and int(ROWS_COUNT) != src_rows:
        # User specified different row count
        expected_rows = int(ROWS_COUNT)
        if tgt_rows != expected_rows:
            rows_ok = False
            print(f"      DIVERGED: Row count mismatch - expected {expected_rows}, got {tgt_rows}")
        else:
            print(f"      Row count: {tgt_rows} (matches requested {ROWS_COUNT})")
    else:
        # Standard case - should match source
        if src_rows != tgt_rows:
            rows_ok = False
            report_rowcount_mismatch(src_rows, tgt_rows)
        else:
            print(f"      Row count: {tgt_rows} (matches source)")

    # --- Analyze tables for stats ---
    if DB_TYPE == 'mysql':
        cursor.execute(f"ANALYZE TABLE `{SOURCE_SCHEMA}`.`{table}`")
        cursor.fetchall()
    analyze_suffix = " ALL COLUMNS" if DB_TYPE == 'tidb' else ""
    cursor.execute(f"ANALYZE TABLE `{TARGET_SCHEMA}`.`{table}`{analyze_suffix}")
    cursor.fetchall()

    # --- DDL validation ---
    cursor.execute(f"SHOW CREATE TABLE `{SOURCE_SCHEMA}`.`{table}`")
    src_ddl = cursor.fetchone()[1]
    cursor.execute(f"SHOW CREATE TABLE `{TARGET_SCHEMA}`.`{table}`")
    tgt_ddl = cursor.fetchone()[1]

    if normalize_ddl(src_ddl, SOURCE_SCHEMA) != normalize_ddl(tgt_ddl, SOURCE_SCHEMA):
        ddl_ok = False
        report_ddl_mismatch(src_ddl, tgt_ddl)
    else:
        print(f"      DDL match: OK")

    # Get column metadata for categorizing mismatches
    indexed_cols = load_indexed_columns(cursor, SOURCE_SCHEMA, table)
    column_types = load_column_types(cursor, SOURCE_SCHEMA, table)

    if COMPARE_HISTOGRAMS:
        if DB_TYPE == 'mysql':
            # MySQL: clone histograms to target then compare via column_statistics
            clone_histograms(cursor, SOURCE_SCHEMA, TARGET_SCHEMA, table)
            src_hist = load_histograms(cursor, SOURCE_SCHEMA, table)
            tgt_hist = load_histograms(cursor, TARGET_SCHEMA, table)
        else:
            src_hist = _load_histograms_with_extractor(DB_TYPE, SOURCE_SCHEMA, table)
            tgt_hist = _load_histograms_with_extractor(DB_TYPE, TARGET_SCHEMA, table)

        if src_hist and tgt_hist:
            hist_results = compare_histograms(src_hist, tgt_hist)
            hist_results = _apply_tidb_exact_frequency_histogram_fallback(
                cursor, table, hist_results, src_hist, tgt_hist
            )
            hist_critical = report_histogram_comparison(hist_results, indexed_cols, column_types, VERBOSE)
            if hist_critical:
                hist_ok = False

    # --- Table stats ---
    report_table_stats(
        load_table_stats(cursor, SOURCE_SCHEMA, table),
        load_table_stats(cursor, TARGET_SCHEMA, table),
        VERBOSE,
    )

    # --- Index stats ---
    report_index_stats(
        load_index_stats(cursor, SOURCE_SCHEMA, table),
        load_index_stats(cursor, TARGET_SCHEMA, table),
        VERBOSE,
    )

    # --- Distinct counts ---
    if DB_TYPE != 'mysql' and ROWS_OVERRIDE and int(ROWS_COUNT) != src_rows:
        # Fast path for scaled-down replays: avoid expensive full-table
        # COUNT(DISTINCT) on large non-MySQL source tables.
        # Compare cardinality estimates and flag over-generation.
        src_cardinality = _load_source_cardinality_fast(cursor, SOURCE_SCHEMA, table)
        tgt_cardinality = _load_source_cardinality_fast(cursor, TARGET_SCHEMA, table)

        # Detect PK columns (expected to over-generate when --rows > source)
        pk_cols = set()
        pk_match = re.search(r"PRIMARY\s+KEY\s*\(([^)]+)\)", clean_ddl, re.IGNORECASE)
        if not pk_match:
            pk_match = re.search(r"UNIQUE\s+KEY\s+`pk`\s*\(([^)]+)\)", clean_ddl, re.IGNORECASE)
        if pk_match:
            pk_cols = {c.strip().strip('`') for c in pk_match.group(1).split(',')}

        print(f"\n\U0001f4ca DISTINCT VALUE COUNTS (--rows {ROWS_COUNT}, source has {src_rows})")
        issues = []
        for col in sorted(tgt_cardinality.keys()):
            tc = tgt_cardinality[col]
            if col not in src_cardinality:
                continue
            sc = src_cardinality[col]
            if sc <= 0:
                continue

            if col in pk_cols and int(ROWS_COUNT) > src_rows:
                if VERBOSE:
                    print(f"      `{col}`: src_est={sc}, replay={tc} -> OK (PK, --rows > source)")
                continue

            if tc > sc * 1.20:
                print(f"      `{col}`: src_est={sc}, replay={tc} -> OVER-GENERATED")
                issues.append(col)
            elif VERBOSE:
                print(f"      `{col}`: src_est={sc}, replay={tc} -> OK")

        if not issues:
            print("      All columns within expected range.")
        else:
            distinct_ok = False
    else:
        # Full path: exact COUNT(DISTINCT) comparison
        src_distinct = load_distinct_counts(cursor, SOURCE_SCHEMA, table)
        tgt_distinct = load_distinct_counts(cursor, TARGET_SCHEMA, table)
        distinct_mismatches = report_distinct_counts(src_distinct, tgt_distinct, VERBOSE)

        if distinct_mismatches:
            distinct_ok = False

    return {
        "loaded": True,
        "ddl": ddl_ok,
        "rows": rows_ok,
        "histograms": hist_ok,
        "distinct": distinct_ok,
    }


# ----------------------------------------------------------------
# Main
# ----------------------------------------------------------------
def main():
    # Make these global so they can be modified by CLI args
    global HOST, USER, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA, ROWS_COUNT, DB_TYPE, DB_PORT, ROWS_OVERRIDE, TABLES_FILTER

    start_time = time.time()
    COMPOSITE_PK_FREQUENCY_REGISTRY.clear()

    # --- Setup directories ---
    os.makedirs(DBGEN_FILES_DIR, exist_ok=True)
    os.makedirs(DBGEN_TMP_OUT_DIR, exist_ok=True)

    # --- Connect ---
    try:
        conn = mysql.connector.connect(**connection_kwargs_for(
            DB_TYPE, HOST, USER, PASSWORD, SOURCE_SCHEMA, DB_PORT,
            autocommit=True,
            allow_local_infile=True,  # Enable LOAD DATA LOCAL INFILE
        ))
        cursor = conn.cursor()
    except Error as e:
        print(f"{DB_TYPE} connection failed: {e}")
        sys.exit(1)

    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    cursor.execute("SET time_zone = '+00:00'")

    # Enable LOAD DATA LOCAL INFILE on server side (requires SUPER privilege)
    try:
        cursor.execute("SET GLOBAL local_infile = 1")
    except Error:
        pass  # May not have permission - will fail later if needed

    # --- Create extractor if needed ---
    extractor = None
    if DB_TYPE != 'mysql':
        extractor = create_schema_extractor(DB_TYPE, HOST, USER, PASSWORD, SOURCE_SCHEMA, DB_PORT)
        if not extractor.connect():
            print(f"Failed to connect to {DB_TYPE}")
            sys.exit(1)

    # --- Discover and sort tables ---
    if DB_TYPE != 'mysql' and extractor:
        # Use extractor to discover tables
        all_tables = extractor.get_tables()
        dependencies = extractor.get_table_dependencies()
        dependencies = add_benchmark_fk_fallback_dependencies(cursor, SOURCE_SCHEMA, all_tables, dependencies)
        sorted_tables = topological_sort(all_tables, dependencies)
    else:
        # Use MySQL discovery
        all_tables, dependencies = discover_tables_and_dependencies(cursor, SOURCE_SCHEMA)
        sorted_tables = topological_sort(all_tables, dependencies)

    # --- Apply table filter if specified ---
    if TABLES_FILTER:
        requested_tables = {t.strip().lower() for t in TABLES_FILTER.split(',')}
        available_tables = {t.lower(): t for t in sorted_tables}

        # Check for non-existent tables
        missing = requested_tables - set(available_tables.keys())
        if missing:
            print(f"WARNING: Requested tables not found in schema: {', '.join(sorted(missing))}")

        # Filter sorted_tables while preserving order
        sorted_tables = [t for t in sorted_tables if t.lower() in requested_tables]

        if not sorted_tables:
            print("ERROR: No valid tables to process after filtering.")
            sys.exit(1)

    print("=" * 60)
    print(f"MASTER RUN — {SOURCE_SCHEMA} -> {TARGET_SCHEMA}")
    print("=" * 60)
    print(f"Tables ({len(sorted_tables)}): {' -> '.join(sorted_tables)}")
    print()

    # --- Prepare target schema ---
    prepare_target_schema(
        cursor,
        TARGET_SCHEMA,
        tables_to_drop=sorted_tables if TABLES_FILTER else None,
    )
    print()

    # --- Regenerate histograms with full sampling (MySQL only) ---
    if DB_TYPE == 'mysql':
        cursor = regenerate_histograms_with_full_sampling(conn, cursor, SOURCE_SCHEMA)
    print()

    # --- Process each table ---
    results = {}

    for i, table in enumerate(sorted_tables, 1):
        cursor = _refresh_cursor(conn, cursor)
        table_extractor = extractor
        if DB_TYPE != 'mysql':
            table_extractor = _new_schema_extractor()

        deps = dependencies.get(table, [])
        dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
        print(f"[{i}/{len(sorted_tables)}] Table: {table}{dep_str}")
        print("-" * 50)

        # Step A
        if not step_a_generate_dbgen(cursor, table, table_extractor):
            results[table] = {"error": "dbgen template generation failed"}
            print()
            if table_extractor is not extractor and table_extractor:
                table_extractor.close()
            continue

        # Step B
        cursor = _refresh_cursor(conn, cursor)
        if not step_b_run_dbgen(cursor, table):
            results[table] = {"error": "dbgen binary execution failed"}
            print()
            if table_extractor is not extractor and table_extractor:
                table_extractor.close()
            continue

        # Step C
        cursor = _refresh_cursor(conn, cursor)
        results[table] = step_c_create_insert_validate(cursor, table)
        if table_extractor is not extractor and table_extractor:
            table_extractor.close()
        print()

    loaded_tables = [
        table
        for table in sorted_tables
        if results.get(table, {}).get("loaded")
    ]
    failed_tables = [
        table
        for table in sorted_tables
        if not results.get(table, {}).get("loaded")
    ]
    if failed_tables:
        print(
            "Skipping benchmark FK script because these table(s) did not load: "
            + ", ".join(failed_tables)
        )
    else:
        cursor = apply_benchmark_fk_script(cursor, TARGET_SCHEMA, loaded_tables)

    # --- Cleanup ---
    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    # --- Final summary ---
    print("=" * 60)
    print("FINAL SUMMARY")
    if not COMPARE_HISTOGRAMS:
        print("(Histogram comparison disabled - use --compare-histograms to enable)")
    print("=" * 60)

    any_failure = False
    for table in sorted_tables:
        r = results.get(table)
        if r is None:
            print(f"  {table}: SKIPPED (no result)")
            any_failure = True
        elif "error" in r:
            print(f"  {table}: FAILED — {r['error']}")
            any_failure = True
        elif r.get("validation_skipped"):
            print(f"  {table}: LOADED  [validation=SKIP]")
        else:
            # When histogram comparison is disabled, don't count it in pass/fail
            if COMPARE_HISTOGRAMS:
                all_ok = all(r.values())
                status_parts = []
                for key in ("ddl", "rows", "histograms", "distinct"):
                    status_parts.append(f"{key}={'OK' if r[key] else 'FAIL'}")
            else:
                # Skip histogram check in pass/fail determination
                all_ok = r["ddl"] and r["rows"] and r["distinct"]
                status_parts = [
                    f"ddl={'OK' if r['ddl'] else 'FAIL'}",
                    f"rows={'OK' if r['rows'] else 'FAIL'}",
                    "histograms=SKIP",
                    f"distinct={'OK' if r['distinct'] else 'FAIL'}",
                ]
            overall = "PASS" if all_ok else "FAIL"
            print(f"  {table}: {overall}  [{', '.join(status_parts)}]")
            if not all_ok:
                any_failure = True

    print("=" * 60)

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    print(f"\nTotal elapsed time: {minutes}m {seconds:.1f}s")

    if any_failure:
        print("Some tables had validation failures.")
        sys.exit(2)
    elif SKIP_VALIDATION:
        print("All tables loaded. Validation was skipped.")
    else:
        print("All tables passed validation.")

    cursor.close()
    conn.close()
    if extractor:
        extractor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="End-to-end data generation and validation"
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Verbose output (show all results, not just failures)")
    parser.add_argument("--compare-histograms", action="store_true",
                        help="Enable histogram comparison (disabled by default - unreliable)")
    validation_group = parser.add_mutually_exclusive_group()
    validation_group.add_argument("--skip-validation", dest="skip_validation",
                                  action="store_true", default=True,
                                  help="Create/load generated tables without running validation checks (default)")
    validation_group.add_argument("--run-validation", dest="skip_validation",
                                  action="store_false",
                                  help="Run built-in validation checks after loading each table")

    # Add arguments for backend selection and connection overrides
    parser.add_argument("--db-type", choices=available_extractor_types(), default=DB_TYPE,
                        help=f"Database type (default: {DB_TYPE})")
    parser.add_argument("--host", help="Database host (overrides config.py)")
    parser.add_argument("--port", type=int, help="Database port (defaults to the engine default)")
    parser.add_argument("--user", help="Database user (overrides config.py)")
    parser.add_argument("--password", help="Database password (overrides config.py)")
    parser.add_argument("--source-schema", help="Source schema name (overrides config.py)")
    parser.add_argument("--target-schema", help="Target schema name (overrides config.py)")
    parser.add_argument("--rows", type=str, help="Number of rows to generate (overrides config.py)")
    parser.add_argument("--tables", type=str, help="Comma-separated list of tables to process (default: all tables)")

    args = parser.parse_args()
    VERBOSE = args.verbose
    COMPARE_HISTOGRAMS = args.compare_histograms
    SKIP_VALIDATION = args.skip_validation

    # Override global configs from CLI args
    if args.db_type:
        DB_TYPE = args.db_type
    if args.host:
        HOST = args.host
    if args.port:
        DB_PORT = args.port
    if args.user:
        USER = args.user
    if args.password is not None:
        PASSWORD = args.password
    if args.source_schema:
        SOURCE_SCHEMA = args.source_schema
    if args.target_schema:
        TARGET_SCHEMA = args.target_schema
    if args.rows:
        ROWS_COUNT = args.rows
        ROWS_OVERRIDE = True
    if args.tables:
        TABLES_FILTER = args.tables

    main()
