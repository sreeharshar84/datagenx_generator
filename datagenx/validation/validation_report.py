#!/usr/bin/env python3
"""Generate an HTML validation report for DataGenX source/target schemas."""

import argparse
import html
from pathlib import Path

import mysql.connector
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import DB_PORT, DB_TYPE, HOST, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA, USER
from lib.schema_extractor import create_schema_extractor, connection_kwargs_for


DEFAULT_COLUMNS = [
    ("customer", "c_nationkey"),
    ("nation", "n_regionkey"),
    ("supplier", "s_nationkey"),
    ("lineitem", "l_linenumber"),
    ("part", "p_size"),
    ("orders", "o_custkey"),
    ("lineitem", "l_orderkey"),
    ("lineitem", "l_partkey"),
    ("lineitem", "l_suppkey"),
]


def connect(args):
    database = args.database or args.source_schema
    kwargs = connection_kwargs_for(
        args.db_type,
        args.host,
        args.user,
        args.password,
        database,
        args.port,
        autocommit=True,
    )
    return mysql.connector.connect(**kwargs)


def extractor_for_cursor(args, cursor, schema):
    extractor = create_schema_extractor(
        args.db_type,
        args.host,
        args.user,
        args.password,
        schema,
        args.port,
    )
    extractor.cursor = cursor
    return extractor


def fetch_df(cursor, query, params=None):
    cursor.execute(query, params or ())
    rows = cursor.fetchall()
    cols = cursor.column_names
    return pd.DataFrame(rows, columns=cols)


def pct_diff(source, target):
    if source == 0 and target == 0:
        return 0.0
    if source is None or target is None:
        return 1.0
    return abs(source - target) / max(source, target)


def get_tables(cursor, schema):
    df = fetch_df(
        cursor,
        """
        SELECT TABLE_NAME
        FROM information_schema.tables
        WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        """,
        (schema,),
    )
    return df["TABLE_NAME"].tolist()


def get_columns(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return df["COLUMN_NAME"].tolist()


def get_column_types(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME, COLUMN_TYPE
        FROM information_schema.columns
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (schema, table),
    )
    return dict(zip(df["COLUMN_NAME"], df["COLUMN_TYPE"]))


def get_indexed_columns(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT DISTINCT COLUMN_NAME
        FROM information_schema.statistics
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        """,
        (schema, table),
    )
    return set(df["COLUMN_NAME"].tolist())


def get_primary_key_columns(cursor, schema, table):
    df = fetch_df(
        cursor,
        """
        SELECT COLUMN_NAME
        FROM information_schema.key_column_usage
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND CONSTRAINT_NAME = 'PRIMARY'
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return df["COLUMN_NAME"].tolist()


def is_string_type(col_type):
    col_type = (col_type or "").lower()
    return any(kind in col_type for kind in ("char", "varchar", "text", "blob"))


def is_decimal_type(col_type):
    col_type = (col_type or "").lower()
    return any(kind in col_type for kind in ("decimal", "numeric"))


def is_numeric_type(col_type):
    col_type = (col_type or "").lower()
    return any(
        kind in col_type
        for kind in ("int", "decimal", "numeric", "float", "double", "real", "bit")
    )


def get_row_counts(cursor, source_schema, target_schema, tables):
    rows = []
    for table in tables:
        cursor.execute(f"SELECT COUNT(*) FROM `{source_schema}`.`{table}`")
        source_count = cursor.fetchone()[0]
        cursor.execute(f"SELECT COUNT(*) FROM `{target_schema}`.`{table}`")
        target_count = cursor.fetchone()[0]
        diff = pct_diff(source_count, target_count)
        rows.append({
            "table": table,
            "source_rows": source_count,
            "target_rows": target_count,
            "diff_pct": diff * 100,
            "status": "PASS" if diff == 0 else "FAIL",
        })
    return pd.DataFrame(rows)


def get_distinct_summary(cursor, source_schema, target_schema, tables):
    rows = []
    for table in tables:
        indexed_cols = get_indexed_columns(cursor, source_schema, table)
        column_types = get_column_types(cursor, source_schema, table)
        for col in get_columns(cursor, source_schema, table):
            cursor.execute(f"SELECT COUNT(DISTINCT `{col}`) FROM `{source_schema}`.`{table}`")
            source_count = cursor.fetchone()[0]
            cursor.execute(f"SELECT COUNT(DISTINCT `{col}`) FROM `{target_schema}`.`{table}`")
            target_count = cursor.fetchone()[0]
            diff = pct_diff(source_count, target_count)
            col_type = column_types.get(col, "unknown")
            indexed = col in indexed_cols
            if diff < 0.05:
                status = "PASS"
            elif is_string_type(col_type) and not indexed:
                status = "NOTE"
            elif is_decimal_type(col_type) and not indexed:
                status = "NOTE"
            else:
                status = "FAIL"
            rows.append({
                "table": table,
                "column": col,
                "column_type": col_type,
                "indexed": indexed,
                "source_distinct": source_count,
                "target_distinct": target_count,
                "diff_pct": diff * 100,
                "status": status,
            })
    return pd.DataFrame(rows)


def histogram_probabilities(hist):
    buckets = hist.get("buckets", [])
    hist_type = hist.get("histogram-type")
    probs = []
    prev = 0.0
    for bucket in buckets:
        cumulative = bucket[-2] if hist_type == "equi-height" else bucket[1]
        probs.append(max(0.0, cumulative - prev))
        prev = cumulative
    # Sort by mass so validation compares distribution shape, not literal
    # bucket endpoint values. This is important for synthetic string domains.
    return sorted(probs, reverse=True)


def histogram_diff(source_hist, target_hist):
    if not source_hist or not target_hist:
        return 1.0, 0, 0, "unknown", "unknown"
    source_probs = histogram_probabilities(source_hist)
    target_probs = histogram_probabilities(target_hist)
    n = max(len(source_probs), len(target_probs))
    if n == 0:
        return 1.0, len(source_probs), len(target_probs), source_hist.get("histogram-type", "unknown"), target_hist.get("histogram-type", "unknown")
    source_probs = source_probs + [0.0] * (n - len(source_probs))
    target_probs = target_probs + [0.0] * (n - len(target_probs))
    diff = 0.5 * sum(abs(source_probs[i] - target_probs[i]) for i in range(n))
    return (
        diff,
        len(source_hist.get("buckets", [])),
        len(target_hist.get("buckets", [])),
        source_hist.get("histogram-type", "unknown"),
        target_hist.get("histogram-type", "unknown"),
    )


def frequency_shape_diff(source_counts, target_counts):
    source_total = sum(source_counts)
    target_total = sum(target_counts)
    if source_total <= 0 or target_total <= 0:
        return 1.0
    source_probs = sorted((count / source_total for count in source_counts), reverse=True)
    target_probs = sorted((count / target_total for count in target_counts), reverse=True)
    n = max(len(source_probs), len(target_probs))
    source_probs = source_probs + [0.0] * (n - len(source_probs))
    target_probs = target_probs + [0.0] * (n - len(target_probs))
    return 0.5 * sum(abs(source_probs[i] - target_probs[i]) for i in range(n))


def get_frequency_counts(cursor, schema, table, column):
    cursor.execute(f"""
        SELECT COUNT(*) AS frequency
        FROM `{schema}`.`{table}`
        WHERE `{column}` IS NOT NULL
        GROUP BY `{column}`
    """)
    return [int(row[0]) for row in cursor.fetchall()]


def get_frequency_count_groups(cursor, schema, table, column):
    cursor.execute(f"""
        SELECT frequency, COUNT(*) AS value_count
        FROM (
            SELECT COUNT(*) AS frequency
            FROM `{schema}`.`{table}`
            WHERE `{column}` IS NOT NULL
            GROUP BY `{column}`
        ) grouped
        GROUP BY frequency
        ORDER BY frequency DESC
    """)
    return [(int(frequency), int(value_count)) for frequency, value_count in cursor.fetchall()]


def frequency_group_shape_diff(source_groups, target_groups):
    source_total = sum(frequency * value_count for frequency, value_count in source_groups)
    target_total = sum(frequency * value_count for frequency, value_count in target_groups)
    if source_total <= 0 or target_total <= 0:
        return 1.0

    source_groups = sorted(source_groups, key=lambda row: row[0], reverse=True)
    target_groups = sorted(target_groups, key=lambda row: row[0], reverse=True)
    i = j = 0
    source_remaining = source_groups[0][1] if source_groups else 0
    target_remaining = target_groups[0][1] if target_groups else 0
    distance = 0.0

    while i < len(source_groups) or j < len(target_groups):
        source_frequency = source_groups[i][0] if i < len(source_groups) else 0
        target_frequency = target_groups[j][0] if j < len(target_groups) else 0
        source_count = source_remaining if i < len(source_groups) else float("inf")
        target_count = target_remaining if j < len(target_groups) else float("inf")
        take = min(source_count, target_count)

        source_prob = source_frequency / source_total if i < len(source_groups) else 0.0
        target_prob = target_frequency / target_total if j < len(target_groups) else 0.0
        distance += take * abs(source_prob - target_prob)

        if i < len(source_groups):
            source_remaining -= take
            if source_remaining == 0:
                i += 1
                if i < len(source_groups):
                    source_remaining = source_groups[i][1]
        if j < len(target_groups):
            target_remaining -= take
            if target_remaining == 0:
                j += 1
                if j < len(target_groups):
                    target_remaining = target_groups[j][1]

    return 0.5 * distance


def lookup_metric(df, table, column, value_col):
    if df is None or df.empty:
        return None
    rows = df[(df["table"] == table) & (df["column"] == column)]
    if rows.empty:
        return None
    value = rows[value_col].iloc[0]
    if pd.isna(value):
        return None
    return int(value)


def lookup_table_rows(df, table, value_col):
    if df is None or df.empty:
        return None
    rows = df[df["table"] == table]
    if rows.empty:
        return None
    value = rows[value_col].iloc[0]
    if pd.isna(value):
        return None
    return int(value)


def has_matching_unique_shape(source_rows, target_rows, source_distinct, target_distinct):
    values = (source_rows, target_rows, source_distinct, target_distinct)
    if any(value is None for value in values):
        return False
    return (
        source_rows == target_rows
        and source_rows > 0
        and source_distinct == source_rows
        and target_distinct == target_rows
    )


def get_histograms(cursor, args, schema, table):
    """Return histograms using the backend abstraction.

    MySQL exposes INFORMATION_SCHEMA.COLUMN_STATISTICS. TiDB and SingleStore
    expose different optimizer-statistics surfaces, so the report should not
    read the MySQL table directly.
    """
    try:
        extractor = extractor_for_cursor(args, cursor, schema)
        return extractor.get_table_histograms(table)
    except Exception as exc:
        print(f"Warning: could not read histograms for {schema}.{table}: {exc}")
        return {}


def get_histogram_summary(cursor, args, source_schema, target_schema, tables, row_df=None, distinct_df=None):
    rows = []
    for table in tables:
        source_hist = get_histograms(cursor, args, source_schema, table)
        target_hist = get_histograms(cursor, args, target_schema, table)
        indexed_cols = get_indexed_columns(cursor, source_schema, table)
        column_types = get_column_types(cursor, source_schema, table)
        for col in sorted(set(source_hist) | set(target_hist)):
            col_type = column_types.get(col, "unknown")
            indexed = col in indexed_cols
            missing_histogram = False
            if col not in source_hist:
                diff = 1.0
                reason = "missing in source"
                missing_histogram = True
            elif col not in target_hist:
                diff = 1.0
                reason = "missing in target"
                missing_histogram = True
                source_buckets = len(source_hist[col].get("buckets", []))
                target_buckets = 0
                source_histogram_type = source_hist[col].get("histogram-type", "unknown")
                target_histogram_type = "missing"
            else:
                (
                    diff,
                    source_buckets,
                    target_buckets,
                    source_histogram_type,
                    target_histogram_type,
                ) = histogram_diff(source_hist[col], target_hist[col])
                reason = "distribution compared"
            if col not in source_hist:
                source_buckets = 0
                target_buckets = len(target_hist[col].get("buckets", []))
                source_histogram_type = "missing"
                target_histogram_type = target_hist[col].get("histogram-type", "unknown")
            if diff >= 0.05:
                source_rows = lookup_table_rows(row_df, table, "source_rows")
                target_rows = lookup_table_rows(row_df, table, "target_rows")
                source_distinct = lookup_metric(distinct_df, table, col, "source_distinct")
                target_distinct = lookup_metric(distinct_df, table, col, "target_distinct")
                max_rows = max(value for value in (source_rows, target_rows, 0) if value is not None)
                max_distinct = max(value for value in (source_distinct, target_distinct, 0) if value is not None)
                if has_matching_unique_shape(source_rows, target_rows, source_distinct, target_distinct):
                    diff = 0.0
                    source_buckets = int(source_distinct)
                    target_buckets = int(target_distinct)
                    source_histogram_type = "unique-cardinality"
                    target_histogram_type = "unique-cardinality"
                    if missing_histogram:
                        reason = "histogram missing; exact unique cardinality fallback"
                    else:
                        reason = "optimizer histogram buckets diverged; exact unique cardinality fallback"
                critical_histogram = indexed or not (is_string_type(col_type) or is_decimal_type(col_type))
                effective_max_distinct = args.histogram_fallback_max_distinct
                if args.db_type == "tidb" and critical_histogram:
                    effective_max_distinct = max(
                        effective_max_distinct,
                        args.tidb_histogram_fallback_max_distinct,
                    )
                allow_frequency_fallback = (
                    max_distinct <= effective_max_distinct
                    and (
                        max_rows <= args.histogram_fallback_max_rows
                        or (
                            args.db_type == "tidb"
                            and (not missing_histogram or critical_histogram)
                        )
                    )
                )
                if diff >= 0.05 and allow_frequency_fallback:
                    source_groups = get_frequency_count_groups(cursor, source_schema, table, col)
                    target_groups = get_frequency_count_groups(cursor, target_schema, table, col)
                    fallback_diff = frequency_group_shape_diff(source_groups, target_groups)
                    if fallback_diff < diff:
                        diff = fallback_diff
                        source_buckets = sum(value_count for _frequency, value_count in source_groups)
                        target_buckets = sum(value_count for _frequency, value_count in target_groups)
                        source_histogram_type = "frequency-shape"
                        target_histogram_type = "frequency-shape"
                        if missing_histogram:
                            reason = "histogram missing; exact frequency shape fallback"
                        else:
                            reason = "optimizer histogram buckets diverged; exact frequency shape fallback"
            if diff < 0.05:
                status = "PASS"
            elif is_string_type(col_type) and not indexed:
                status = "NOTE"
            elif is_decimal_type(col_type) and not indexed:
                status = "NOTE"
            else:
                status = "FAIL"
            rows.append({
                "table": table,
                "column": col,
                "column_type": col_type,
                "indexed": indexed,
                "source_buckets": source_buckets,
                "target_buckets": target_buckets,
                "source_histogram_type": source_histogram_type,
                "target_histogram_type": target_histogram_type,
                "histogram_diff": diff,
                "diff_pct": diff * 100,
                "reason": reason,
                "status": status,
            })
    return pd.DataFrame(rows)


def get_frequency_df(cursor, source_schema, target_schema, table, column):
    query = f"""
        WITH source_freq AS (
            SELECT CAST(`{column}` AS CHAR) AS value, COUNT(*) AS source_count
            FROM `{source_schema}`.`{table}`
            GROUP BY `{column}`
        ),
        target_freq AS (
            SELECT CAST(`{column}` AS CHAR) AS value, COUNT(*) AS target_count
            FROM `{target_schema}`.`{table}`
            GROUP BY `{column}`
        )
        SELECT
            COALESCE(source_freq.value, target_freq.value) AS value,
            COALESCE(source_count, 0) AS source_count,
            COALESCE(target_count, 0) AS target_count
        FROM source_freq
        LEFT JOIN target_freq USING (value)
        UNION
        SELECT
            COALESCE(source_freq.value, target_freq.value) AS value,
            COALESCE(source_count, 0) AS source_count,
            COALESCE(target_count, 0) AS target_count
        FROM target_freq
        LEFT JOIN source_freq USING (value)
        WHERE source_freq.value IS NULL
    """
    df = fetch_df(cursor, query)
    if df.empty:
        return df
    df["source_count"] = df["source_count"].astype(int)
    df["target_count"] = df["target_count"].astype(int)
    df["sort_value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.sort_values(["sort_value", "value"], na_position="last")
    return df.drop(columns=["sort_value"])


def get_fk_orphans(cursor, source_schema, target_schema):
    # TPC-H checks. If a table is absent, skip that check.
    checks = [
        ("nation_region", "nation n", "region r", "n.n_regionkey = r.r_regionkey", "r.r_regionkey IS NULL"),
        ("supplier_nation", "supplier s", "nation n", "s.s_nationkey = n.n_nationkey", "n.n_nationkey IS NULL"),
        ("customer_nation", "customer c", "nation n", "c.c_nationkey = n.n_nationkey", "n.n_nationkey IS NULL"),
        ("orders_customer", "orders o", "customer c", "o.o_custkey = c.c_custkey", "c.c_custkey IS NULL"),
        ("lineitem_orders", "lineitem l", "orders o", "l.l_orderkey = o.o_orderkey", "o.o_orderkey IS NULL"),
        ("lineitem_partsupp", "lineitem l", "partsupp ps", "l.l_partkey = ps.ps_partkey AND l.l_suppkey = ps.ps_suppkey", "ps.ps_partkey IS NULL"),
    ]
    rows = []
    for schema_name, label_prefix in ((source_schema, "source"), (target_schema, "target")):
        for name, child, parent, join_expr, orphan_expr in checks:
            child_table = child.split()[0]
            parent_table = parent.split()[0]
            try:
                cursor.execute(f"""
                    SELECT COUNT(*)
                    FROM `{schema_name}`.{child}
                    LEFT JOIN `{schema_name}`.{parent}
                      ON {join_expr}
                    WHERE {orphan_expr}
                """)
                count = cursor.fetchone()[0]
            except mysql.connector.Error:
                continue
            rows.append({
                "schema": label_prefix,
                "check": name,
                "child_table": child_table,
                "parent_table": parent_table,
                "orphan_count": count,
                "status": "PASS" if count == 0 else "FAIL",
            })
    return pd.DataFrame(rows)


def get_row_overlap(
    cursor,
    source_schema,
    target_schema,
    tables,
    db_type="mysql",
    overlap_chunk_rows=500000,
    tidb_overlap_strategy="auto",
):
    rows = []
    for table in tables:
        columns = get_columns(cursor, source_schema, table)
        if not columns:
            continue
        source_pk = get_primary_key_columns(cursor, source_schema, table)
        target_pk = get_primary_key_columns(cursor, target_schema, table)
        if source_pk and source_pk == target_pk:
            cursor.execute(f"SELECT COUNT(*) FROM `{source_schema}`.`{table}`")
            source_unique = cursor.fetchone()[0]
            cursor.execute(f"SELECT COUNT(*) FROM `{target_schema}`.`{table}`")
            target_unique = cursor.fetchone()[0]
            column_types = get_column_types(cursor, source_schema, table)
            join_expr = " AND ".join(
                f"s.`{col}` <=> t.`{col}`"
                for col in source_pk
            )
            equality_expr = " AND ".join(
                f"s.`{col}` <=> t.`{col}`"
                for col in columns
            )
            overlap = None
            use_numeric_chunks = (
                db_type == "tidb"
                and source_unique > overlap_chunk_rows
                and is_numeric_type(column_types.get(source_pk[0]))
            )
            if db_type == "tidb" and tidb_overlap_strategy == "mpp" and use_numeric_chunks:
                pk_col = source_pk[0]
                cursor.execute(f"SELECT MIN(`{pk_col}`), MAX(`{pk_col}`) FROM `{source_schema}`.`{table}`")
                min_pk, max_pk = cursor.fetchone()
                overlap = 0
                if min_pk is not None and max_pk is not None:
                    min_pk = int(min_pk)
                    max_pk = int(max_pk)
                    span = max_pk - min_pk + 1
                    chunk_count = max(1, (int(source_unique) + overlap_chunk_rows - 1) // overlap_chunk_rows)
                    chunk_width = max(1, (span + chunk_count - 1) // chunk_count)
                    print(
                        f"Running exact row overlap for {table} with TiFlash MPP in {chunk_count} primary-key chunks",
                        flush=True,
                    )
                    chunk_index = 0
                    chunk_start = min_pk
                    while chunk_start <= max_pk:
                        chunk_end = min(chunk_start + chunk_width, max_pk + 1)
                        cursor.execute(
                            f"""
                            SELECT /*+ READ_FROM_STORAGE(TIFLASH[s,t]) HASH_JOIN(t) */ COUNT(*)
                            FROM `{source_schema}`.`{table}` AS s
                            INNER JOIN `{target_schema}`.`{table}` AS t
                              ON {join_expr}
                            WHERE {equality_expr}
                              AND s.`{pk_col}` >= %s
                              AND s.`{pk_col}` < %s
                              AND t.`{pk_col}` >= %s
                              AND t.`{pk_col}` < %s
                            """,
                            (chunk_start, chunk_end, chunk_start, chunk_end),
                        )
                        overlap += cursor.fetchone()[0] or 0
                        chunk_index += 1
                        if chunk_index == chunk_count or chunk_index % 10 == 0:
                            print(
                                f"  {table}: completed {chunk_index}/{chunk_count} MPP overlap chunks",
                                flush=True,
                            )
                        chunk_start = chunk_end
                reason = "primary-key TiFlash MPP chunked exact row comparison"
            elif db_type == "tidb" and tidb_overlap_strategy == "mpp":
                print(f"Running exact row overlap for {table} with TiFlash MPP", flush=True)
                cursor.execute(f"""
                    SELECT /*+ READ_FROM_STORAGE(TIFLASH[s,t]) HASH_JOIN(t) */ COUNT(*)
                    FROM `{source_schema}`.`{table}` AS s
                    INNER JOIN `{target_schema}`.`{table}` AS t
                      ON {join_expr}
                    WHERE {equality_expr}
                """)
                reason = "primary-key TiFlash MPP exact row comparison"
            elif use_numeric_chunks:
                pk_col = source_pk[0]
                cursor.execute(f"SELECT MIN(`{pk_col}`), MAX(`{pk_col}`) FROM `{source_schema}`.`{table}`")
                min_pk, max_pk = cursor.fetchone()
                overlap = 0
                if min_pk is not None and max_pk is not None:
                    min_pk = int(min_pk)
                    max_pk = int(max_pk)
                    span = max_pk - min_pk + 1
                    chunk_count = max(1, (int(source_unique) + overlap_chunk_rows - 1) // overlap_chunk_rows)
                    chunk_width = max(1, (span + chunk_count - 1) // chunk_count)
                    print(
                        f"Running exact row overlap for {table} in {chunk_count} primary-key chunks",
                        flush=True,
                    )
                    chunk_index = 0
                    chunk_start = min_pk
                    while chunk_start <= max_pk:
                        chunk_end = min(chunk_start + chunk_width, max_pk + 1)
                        cursor.execute(
                            f"""
                            SELECT /*+ INL_JOIN(t) */ COUNT(*)
                            FROM `{source_schema}`.`{table}` AS s
                            STRAIGHT_JOIN `{target_schema}`.`{table}` AS t USE INDEX (PRIMARY)
                              ON {join_expr}
                            WHERE {equality_expr}
                              AND s.`{pk_col}` >= %s
                              AND s.`{pk_col}` < %s
                            """,
                            (chunk_start, chunk_end),
                        )
                        overlap += cursor.fetchone()[0] or 0
                        chunk_index += 1
                        if chunk_index == chunk_count or chunk_index % 10 == 0:
                            print(
                                f"  {table}: completed {chunk_index}/{chunk_count} overlap chunks",
                                flush=True,
                            )
                        chunk_start = chunk_end
                reason = "primary-key chunked exact row comparison"
            else:
                if db_type == "tidb":
                    print(f"Running exact row overlap for {table} without chunking", flush=True)
                    cursor.execute(f"""
                        SELECT /*+ INL_JOIN(t) */ COUNT(*)
                        FROM `{source_schema}`.`{table}` AS s
                        STRAIGHT_JOIN `{target_schema}`.`{table}` AS t USE INDEX (PRIMARY)
                          ON {join_expr}
                        WHERE {equality_expr}
                    """)
                    reason = "primary-key index nested-loop exact row comparison"
                else:
                    cursor.execute(f"""
                        SELECT COUNT(*)
                        FROM `{source_schema}`.`{table}` AS s
                        INNER JOIN `{target_schema}`.`{table}` AS t
                          ON {join_expr}
                        WHERE {equality_expr}
                    """)
                    reason = "primary-key join exact row comparison"
            if overlap is None:
                overlap = cursor.fetchone()[0]
        else:
            reason = "hash exact row comparison"
            hash_expr = "MD5(CONCAT_WS('|', {}))".format(
                ", ".join(f"COALESCE(CAST(`{col}` AS CHAR), '<NULL>')" for col in columns)
            )
            cursor.execute(f"SELECT COUNT(DISTINCT {hash_expr}) FROM `{source_schema}`.`{table}`")
            source_unique = cursor.fetchone()[0]
            cursor.execute(f"SELECT COUNT(DISTINCT {hash_expr}) FROM `{target_schema}`.`{table}`")
            target_unique = cursor.fetchone()[0]
            cursor.execute(f"""
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT {hash_expr} AS row_hash
                    FROM `{source_schema}`.`{table}`
                ) AS source_rows
                INNER JOIN (
                    SELECT DISTINCT {hash_expr} AS row_hash
                    FROM `{target_schema}`.`{table}`
                ) AS target_rows USING (row_hash)
            """)
            overlap = cursor.fetchone()[0]
        denom = max(source_unique or 0, target_unique or 0, 1)
        overlap_pct = (overlap or 0) * 100 / denom
        rows.append({
            "table": table,
            "source_unique_rows": source_unique,
            "target_unique_rows": target_unique,
            "overlapping_unique_rows": overlap,
            "overlap_pct": overlap_pct,
            "status": "PASS" if overlap_pct < 1 else "NOTE",
            "reason": reason,
        })
    return pd.DataFrame(rows)


def get_skipped_overlap(tables, reason):
    return pd.DataFrame([
        {
            "table": table,
            "source_unique_rows": None,
            "target_unique_rows": None,
            "overlapping_unique_rows": None,
            "overlap_pct": 0.0,
            "status": "SKIP",
            "reason": reason,
        }
        for table in tables
    ])


def figure_to_html(fig, include_plotlyjs=False):
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs)


def status_rank(status):
    return {"FAIL": 3, "NOTE": 2, "PASS": 1, "SKIP": 0}.get(status, 0)


def worst_status(statuses):
    statuses = [status for status in statuses if status]
    if not statuses:
        return "PASS"
    return max(statuses, key=status_rank)


def status_badge(status):
    safe = html.escape(str(status))
    return f"<span class='badge {safe.lower()}'>{safe}</span>"


def status_counts(df):
    if df.empty or "status" not in df:
        return {"PASS": 0, "NOTE": 0, "FAIL": 0, "SKIP": 0}
    return {status: int((df["status"] == status).sum()) for status in ("PASS", "NOTE", "FAIL", "SKIP")}


def build_summary_cards(row_df, hist_df, distinct_df, orphan_df, overlap_df):
    groups = [
        ("Rows", status_counts(row_df), "table row counts"),
        ("Histograms", status_counts(hist_df), "column distribution shape"),
        ("Distinct", status_counts(distinct_df), "per-column cardinality"),
        ("FK Integrity", status_counts(orphan_df), "source and target orphan checks"),
        ("Privacy", status_counts(overlap_df), "exact row overlap"),
    ]
    cards = ["<section><h2>Dashboard</h2><div class='cards'>"]
    for title, counts, subtitle in groups:
        overall = "FAIL" if counts["FAIL"] else "NOTE" if counts["NOTE"] else "PASS"
        skip_html = f"<span class='skip'>SKIP {counts['SKIP']}</span>" if counts["SKIP"] else ""
        cards.append(
            "<div class='card'>"
            f"<div class='card-title'>{html.escape(title)}</div>"
            f"<div class='card-status'>{status_badge(overall)}</div>"
            f"<div class='card-counts'>"
            f"<span class='pass'>PASS {counts['PASS']}</span>"
            f"<span class='note'>NOTE {counts['NOTE']}</span>"
            f"<span class='fail'>FAIL {counts['FAIL']}</span>"
            f"{skip_html}"
            "</div>"
            f"<div class='card-subtitle'>{html.escape(subtitle)}</div>"
            "</div>"
        )
    cards.append("</div></section>")
    return "".join(cards)


def build_table_matrix(row_df, hist_df, distinct_df, orphan_df, overlap_df, tables):
    rows = []
    target_orphans = orphan_df[orphan_df["schema"] == "target"] if not orphan_df.empty else orphan_df
    for table in tables:
        row_status = row_df.loc[row_df["table"] == table, "status"].tolist()
        hist_rows = hist_df[hist_df["table"] == table] if not hist_df.empty else hist_df
        distinct_rows = distinct_df[distinct_df["table"] == table] if not distinct_df.empty else distinct_df
        orphan_rows = target_orphans[target_orphans["child_table"] == table] if not target_orphans.empty else target_orphans
        overlap_rows = overlap_df[overlap_df["table"] == table] if not overlap_df.empty else overlap_df

        hist_status = worst_status(hist_rows["status"].tolist()) if not hist_rows.empty else "PASS"
        distinct_status = worst_status(distinct_rows["status"].tolist()) if not distinct_rows.empty else "PASS"
        orphan_status = worst_status(orphan_rows["status"].tolist()) if not orphan_rows.empty else "PASS"
        overlap_status = worst_status(overlap_rows["status"].tolist()) if not overlap_rows.empty else "PASS"
        overall = worst_status([
            row_status[0] if row_status else "PASS",
            hist_status,
            distinct_status,
            orphan_status,
            overlap_status,
        ])

        top_hist = ""
        max_hist = 0.0
        if not hist_rows.empty:
            top = hist_rows.sort_values("histogram_diff", ascending=False).iloc[0]
            top_hist = f"{top['column']} ({top['diff_pct']:.2f}%)"
            max_hist = float(top["diff_pct"])

        max_distinct = float(distinct_rows["diff_pct"].max()) if not distinct_rows.empty else 0.0
        overlap_pct = float(overlap_rows["overlap_pct"].iloc[0]) if not overlap_rows.empty else 0.0

        rows.append({
            "table": table,
            "overall": overall,
            "rows": row_status[0] if row_status else "PASS",
            "histograms": hist_status,
            "distinct": distinct_status,
            "fk_integrity": orphan_status,
            "privacy": overlap_status,
            "top_histogram_drift": top_hist,
            "max_histogram_diff_pct": max_hist,
            "max_distinct_diff_pct": max_distinct,
            "exact_row_overlap_pct": overlap_pct,
        })
    return pd.DataFrame(rows)


def build_summary_figure(row_df, hist_df, distinct_df, orphan_df):
    labels = ["row counts", "histograms", "distinct counts", "FK orphans"]
    pass_counts = [
        int((row_df["status"] == "PASS").sum()),
        int((hist_df["status"] == "PASS").sum()) if not hist_df.empty else 0,
        int((distinct_df["status"] == "PASS").sum()),
        int((orphan_df["status"] == "PASS").sum()) if not orphan_df.empty else 0,
    ]
    fail_counts = [
        int((row_df["status"] == "FAIL").sum()),
        int((hist_df["status"] == "FAIL").sum()) if not hist_df.empty else 0,
        int((distinct_df["status"] == "FAIL").sum()),
        int((orphan_df["status"] == "FAIL").sum()) if not orphan_df.empty else 0,
    ]
    fig = go.Figure()
    fig.add_bar(name="PASS", x=labels, y=pass_counts, marker_color="#2ca02c")
    fig.add_bar(name="FAIL", x=labels, y=fail_counts, marker_color="#d62728")
    if not hist_df.empty:
        note_counts = [0, int((hist_df["status"] == "NOTE").sum()), 0, 0]
        fig.add_bar(name="NOTE", x=labels, y=note_counts, marker_color="#ffbf00")
    fig.update_layout(
        title="Validation Status Summary",
        barmode="stack",
        height=420,
        margin=dict(l=40, r=20, t=60, b=40),
    )
    return fig


def build_top_drift_figure(hist_df, distinct_df):
    rows = []
    if not hist_df.empty:
        for _, row in hist_df.iterrows():
            rows.append({
                "label": f"{row['table']}.{row['column']}",
                "type": "histogram",
                "diff_pct": float(row["diff_pct"]),
                "status": row["status"],
            })
    if not distinct_df.empty:
        for _, row in distinct_df.iterrows():
            rows.append({
                "label": f"{row['table']}.{row['column']}",
                "type": "distinct",
                "diff_pct": float(row["diff_pct"]),
                "status": row["status"],
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return None
    df = df.sort_values("diff_pct", ascending=False).head(20).sort_values("diff_pct")
    color_map = {"PASS": "#2ca02c", "NOTE": "#ffbf00", "FAIL": "#d62728"}
    fig = go.Figure()
    fig.add_bar(
        x=df["diff_pct"],
        y=df["label"],
        orientation="h",
        marker_color=[color_map.get(status, "#888") for status in df["status"]],
        customdata=df[["type", "status"]],
        hovertemplate="%{y}<br>type=%{customdata[0]}<br>status=%{customdata[1]}<br>diff=%{x:.2f}%<extra></extra>",
    )
    fig.update_layout(
        title="Top Drift Columns",
        height=max(420, 28 * len(df) + 120),
        margin=dict(l=180, r=30, t=60, b=40),
        xaxis_title="difference %",
    )
    return fig


def build_histogram_heatmap(hist_df):
    if hist_df.empty:
        return None
    pivot = hist_df.pivot_table(
        index="table",
        columns="column",
        values="histogram_diff",
        aggfunc="max",
        fill_value=0,
    )
    fig = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=pivot.columns,
            y=pivot.index,
            colorscale="RdYlGn_r",
            zmin=0,
            zmax=max(0.10, float(pivot.values.max()) if pivot.size else 0.10),
            colorbar=dict(title="diff"),
        )
    )
    fig.update_layout(
        title="Histogram Difference Heatmap",
        height=max(420, 40 * len(pivot.index)),
        margin=dict(l=120, r=20, t=60, b=160),
    )
    return fig


def build_table_matrix_figure(matrix_df):
    if matrix_df.empty:
        return None
    metrics = ["rows", "histograms", "distinct", "fk_integrity", "privacy"]
    status_to_value = {"SKIP": 0, "PASS": 1, "NOTE": 2, "FAIL": 3}
    z = [
        [status_to_value.get(row[metric], 0) for metric in metrics]
        for _, row in matrix_df.iterrows()
    ]
    text = [
        [row[metric] for metric in metrics]
        for _, row in matrix_df.iterrows()
    ]
    fig = go.Figure(
        data=go.Heatmap(
            z=z,
            x=[m.replace("_", " ") for m in metrics],
            y=matrix_df["table"],
            text=text,
            texttemplate="%{text}",
            colorscale=[
                [0.0, "#eeeeee"],
                [0.24, "#eeeeee"],
                [0.25, "#2ca02c"],
                [0.49, "#2ca02c"],
                [0.50, "#ffbf00"],
                [0.74, "#ffbf00"],
                [0.75, "#d62728"],
                [1.0, "#d62728"],
            ],
            zmin=0,
            zmax=3,
            showscale=False,
        )
    )
    fig.update_layout(
        title="Table Validation Matrix",
        height=max(360, 40 * len(matrix_df) + 120),
        margin=dict(l=120, r=30, t=60, b=70),
    )
    return fig


def build_overlap_figure(overlap_df):
    if overlap_df.empty:
        return None
    df = overlap_df.sort_values("overlap_pct", ascending=True)
    fig = go.Figure()
    fig.add_bar(
        x=df["overlap_pct"],
        y=df["table"],
        orientation="h",
        marker_color=[
            "#bdbdbd" if status == "SKIP" else "#ffbf00" if status == "NOTE" else "#2ca02c"
            for status in df["status"]
        ],
        customdata=df[["status"]],
        hovertemplate="%{y}<br>status=%{customdata[0]}<br>exact row overlap=%{x:.4f}%<extra></extra>",
    )
    fig.update_layout(
        title="Exact Row Overlap: Source vs Synthetic",
        height=max(360, 36 * len(df) + 120),
        margin=dict(l=120, r=30, t=60, b=40),
        xaxis_title="overlapping unique rows %",
    )
    return fig


def build_fk_graph(orphan_df):
    if orphan_df.empty:
        return None
    target_rows = orphan_df[orphan_df["schema"] == "target"].copy()
    if target_rows.empty:
        return None

    positions = {
        "region": (0, 3),
        "nation": (1, 3),
        "supplier": (2, 4),
        "customer": (2, 2),
        "orders": (3, 2),
        "part": (2, 5),
        "partsupp": (3, 5),
        "lineitem": (4, 3.5),
    }
    edge_map = {
        "nation_region": ("region", "nation"),
        "supplier_nation": ("nation", "supplier"),
        "customer_nation": ("nation", "customer"),
        "orders_customer": ("customer", "orders"),
        "lineitem_orders": ("orders", "lineitem"),
        "lineitem_partsupp": ("partsupp", "lineitem"),
    }

    fig = go.Figure()
    for _, row in target_rows.iterrows():
        parent, child = edge_map.get(row["check"], (row["parent_table"], row["child_table"]))
        if parent not in positions or child not in positions:
            continue
        x0, y0 = positions[parent]
        x1, y1 = positions[child]
        color = "#2ca02c" if row["status"] == "PASS" else "#d62728"
        fig.add_trace(go.Scatter(
            x=[x0, x1],
            y=[y0, y1],
            mode="lines",
            line=dict(color=color, width=4),
            hovertext=f"{row['check']}: {row['orphan_count']} orphans",
            hoverinfo="text",
            showlegend=False,
        ))

    nodes = sorted(set(target_rows["child_table"]).union(set(target_rows["parent_table"])))
    fig.add_trace(go.Scatter(
        x=[positions[node][0] for node in nodes if node in positions],
        y=[positions[node][1] for node in nodes if node in positions],
        text=[node for node in nodes if node in positions],
        mode="markers+text",
        textposition="middle center",
        marker=dict(size=54, color="#f5f5f5", line=dict(color="#555", width=1.5)),
        hoverinfo="text",
        showlegend=False,
    ))
    fig.update_layout(
        title="TPC-H Referential Integrity Graph",
        height=430,
        margin=dict(l=30, r=30, t=60, b=30),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        plot_bgcolor="white",
    )
    return fig


def build_frequency_figure(freq_map):
    if not freq_map:
        return None
    rows = len(freq_map)
    fig = make_subplots(
        rows=rows,
        cols=1,
        subplot_titles=list(freq_map.keys()),
        vertical_spacing=min(0.08, 0.35 / max(rows, 1)),
    )
    for idx, (label, df) in enumerate(freq_map.items(), start=1):
        fig.add_bar(
            x=df["value"],
            y=df["source_count"],
            name="source" if idx == 1 else None,
            marker_color="#1f77b4",
            showlegend=idx == 1,
            row=idx,
            col=1,
        )
        df = df.copy()
        denom = df[["source_count", "target_count"]].max(axis=1).replace(0, 1)
        df["drift_pct"] = (df["source_count"] - df["target_count"]).abs() * 100 / denom
        max_drift = df["drift_pct"].max()
        fig.add_annotation(
            text=f"max drift {max_drift:.2f}%",
            xref=f"x{idx if idx > 1 else ''} domain",
            yref=f"y{idx if idx > 1 else ''} domain",
            x=0.98,
            y=0.92,
            showarrow=False,
            bgcolor="rgba(255,255,255,0.8)",
            bordercolor="#ddd",
            font=dict(size=11),
            row=idx,
            col=1,
        )
        fig.add_bar(
            x=df["value"],
            y=df["target_count"],
            name="target" if idx == 1 else None,
            marker_color="#ff7f0e",
            showlegend=idx == 1,
            row=idx,
            col=1,
        )
    fig.update_layout(
        title="Source vs Target Frequency Distributions",
        barmode="group",
        height=max(420, 280 * rows),
        margin=dict(l=60, r=20, t=80, b=50),
    )
    return fig


def table_html_from_records(records, title):
    if not records:
        return f"<section><h2>{html.escape(title)}</h2><p>No rows.</p></section>"
    headers = list(records[0].keys())
    parts = [f"<section><h2>{html.escape(title)}</h2><table class='data-table'><thead><tr>"]
    for header in headers:
        parts.append(f"<th>{html.escape(header.replace('_', ' '))}</th>")
    parts.append("</tr></thead><tbody>")
    for record in records:
        parts.append("<tr>")
        for header in headers:
            value = record[header]
            if isinstance(value, float):
                value = f"{value:.4f}"
            if header in {"overall", "rows", "histograms", "distinct", "fk_integrity", "privacy", "status"}:
                parts.append(f"<td>{status_badge(value)}</td>")
            else:
                parts.append(f"<td>{html.escape(str(value))}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></section>")
    return "".join(parts)


def table_html(df, title, max_rows=30):
    if df.empty:
        body = "<p>No rows.</p>"
    else:
        display = df.head(max_rows).copy()
        body = display.to_html(index=False, classes="data-table", escape=True)
        if len(df) > max_rows:
            body += f"<p class='note'>Showing first {max_rows} of {len(df)} rows.</p>"
    return f"<section><h2>{title}</h2>{body}</section>"


def generate_report(args):
    conn = connect(args)
    cursor = conn.cursor()
    try:
        if args.db_type == "tidb" and args.tidb_mem_quota_query:
            cursor.execute("SET SESSION tidb_mem_quota_query = %s", (args.tidb_mem_quota_query,))
        tables = sorted(set(get_tables(cursor, args.source_schema)) & set(get_tables(cursor, args.target_schema)))
        row_df = get_row_counts(cursor, args.source_schema, args.target_schema, tables)
        distinct_df = get_distinct_summary(cursor, args.source_schema, args.target_schema, tables)
        hist_df = get_histogram_summary(
            cursor,
            args,
            args.source_schema,
            args.target_schema,
            tables,
            row_df=row_df,
            distinct_df=distinct_df,
        )
        orphan_df = get_fk_orphans(cursor, args.source_schema, args.target_schema)
        if args.skip_overlap:
            overlap_df = get_skipped_overlap(
                tables,
                "skipped by --skip-overlap; exact row overlap hashes every row",
            )
        else:
            tidb_mpp_enabled = False
            try:
                if args.db_type == "tidb" and args.tidb_overlap_strategy == "mpp":
                    cursor.execute("SET SESSION tidb_allow_mpp = 1")
                    cursor.execute("SET SESSION tidb_enforce_mpp = 1")
                    tidb_mpp_enabled = True
                overlap_df = get_row_overlap(
                    cursor,
                    args.source_schema,
                    args.target_schema,
                    tables,
                    db_type=args.db_type,
                    overlap_chunk_rows=args.overlap_chunk_rows,
                    tidb_overlap_strategy=args.tidb_overlap_strategy,
                )
            finally:
                if tidb_mpp_enabled:
                    cursor.execute("SET SESSION tidb_enforce_mpp = 0")
        matrix_df = build_table_matrix(row_df, hist_df, distinct_df, orphan_df, overlap_df, tables)

        freq_map = {}
        source_tables = set(get_tables(cursor, args.source_schema))
        for table, column in DEFAULT_COLUMNS:
            if table not in source_tables:
                continue
            cols = set(get_columns(cursor, args.source_schema, table))
            if column not in cols:
                continue
            distinct_rows = distinct_df[
                (distinct_df["table"] == table) & (distinct_df["column"] == column)
            ] if not distinct_df.empty else distinct_df
            if not distinct_rows.empty:
                max_distinct = max(
                    int(distinct_rows["source_distinct"].iloc[0] or 0),
                    int(distinct_rows["target_distinct"].iloc[0] or 0),
                )
                if max_distinct > args.max_frequency_values:
                    continue
            df = get_frequency_df(cursor, args.source_schema, args.target_schema, table, column)
            if not df.empty and len(df) <= args.max_frequency_values:
                freq_map[f"{table}.{column}"] = df

        top_hist = hist_df.sort_values("histogram_diff", ascending=False) if not hist_df.empty else hist_df
        top_distinct = distinct_df.sort_values("diff_pct", ascending=False)

        figures = [
            build_summary_figure(row_df, hist_df, distinct_df, orphan_df),
            build_table_matrix_figure(matrix_df),
            build_top_drift_figure(hist_df, distinct_df),
            build_histogram_heatmap(hist_df),
            build_fk_graph(orphan_df),
            build_overlap_figure(overlap_df),
            build_frequency_figure(freq_map),
        ]

        html_parts = [
            "<!doctype html><html><head><meta charset='utf-8'>",
            "<title>DataGenX Validation Report</title>",
            "<style>",
            "body{font-family:Arial,sans-serif;margin:32px;color:#222;background:#fafafa}",
            "h1,h2{margin-bottom:8px}",
            ".meta,.note{color:#666}",
            ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}",
            ".card{background:white;border:1px solid #ddd;border-radius:8px;padding:16px}",
            ".card-title{font-weight:700;font-size:14px;color:#444}",
            ".card-status{font-size:22px;margin:10px 0}",
            ".card-counts{display:flex;gap:10px;font-size:12px;margin-bottom:8px;flex-wrap:wrap}",
            ".card-subtitle{font-size:12px;color:#777}",
            ".badge{display:inline-block;border-radius:999px;padding:3px 8px;font-weight:700;font-size:12px}",
            ".badge.pass{background:#e7f5e7;color:#1d6b1d}",
            ".badge.note{background:#fff4cc;color:#805b00}",
            ".badge.fail{background:#fde2e2;color:#9b1c1c}",
            ".badge.skip{background:#eeeeee;color:#555}",
            ".pass{color:#1d6b1d}.note{color:#805b00}.fail{color:#9b1c1c}.skip{color:#555}",
            ".data-table{border-collapse:collapse;width:100%;font-size:13px}",
            ".data-table th,.data-table td{border:1px solid #ddd;padding:6px;text-align:left}",
            ".data-table th{background:#f5f5f5}",
            "section{margin:32px 0;background:white;border:1px solid #eee;border-radius:8px;padding:18px}",
            "</style></head><body>",
            "<h1>DataGenX Validation Report</h1>",
            (
                f"<p class='meta'>Backend: <code>{html.escape(args.db_type)}</code> "
                f"&nbsp; Source: <code>{html.escape(args.source_schema)}</code> "
                f"&nbsp; Target: <code>{html.escape(args.target_schema)}</code></p>"
            ),
            build_summary_cards(row_df, hist_df, distinct_df, orphan_df, overlap_df),
        ]

        first_figure = True
        for fig in figures:
            if fig is not None:
                html_parts.append(figure_to_html(fig, include_plotlyjs=first_figure))
                first_figure = False

        html_parts.extend([
            table_html_from_records(matrix_df.to_dict("records"), "Table-Level Validation Matrix"),
            table_html(row_df, "Row Count Comparison"),
            table_html(top_hist, "Top Histogram Differences"),
            table_html(top_distinct, "Top Distinct Count Differences"),
            table_html(orphan_df, "Referential Integrity Orphan Checks"),
            table_html(overlap_df, "Exact Row Overlap Checks"),
            "</body></html>",
        ])

        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(html_parts))
        return output
    finally:
        cursor.close()
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Generate an HTML DataGenX validation report.")
    parser.add_argument("--db-type", default=DB_TYPE, choices=("mysql", "singlestore", "tidb"))
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DB_PORT)
    parser.add_argument("--database", default=None, help="Default database for the connection; defaults to source schema.")
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument("--source-schema", default=SOURCE_SCHEMA)
    parser.add_argument("--target-schema", default=TARGET_SCHEMA)
    parser.add_argument("--output", default="/tmp/tpch_validation_report.html")
    parser.add_argument("--max-frequency-values", type=int, default=100)
    parser.add_argument(
        "--histogram-fallback-max-rows",
        type=int,
        default=10000,
        help="Maximum table rows for exact frequency-shape fallback when backend histograms are missing.",
    )
    parser.add_argument(
        "--histogram-fallback-max-distinct",
        type=int,
        default=100000,
        help="Maximum column NDV for exact frequency-shape fallback when backend histograms are missing.",
    )
    parser.add_argument(
        "--tidb-histogram-fallback-max-distinct",
        type=int,
        default=2000000,
        help="TiDB-only NDV ceiling for exact frequency-shape fallback on critical histogram columns.",
    )
    parser.add_argument(
        "--tidb-mem-quota-query",
        type=int,
        default=None,
        help="Optional TiDB session tidb_mem_quota_query override for heavy report queries.",
    )
    parser.add_argument(
        "--skip-overlap",
        action="store_true",
        help="Skip exact source-vs-target row hash overlap checks for large schemas.",
    )
    parser.add_argument(
        "--overlap-chunk-rows",
        type=int,
        default=500000,
        help="Approximate TiDB primary-key range chunk size for exact row overlap checks.",
    )
    parser.add_argument(
        "--tidb-overlap-strategy",
        choices=("auto", "mpp"),
        default="auto",
        help="TiDB execution strategy for exact row overlap checks.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output = generate_report(args)
    print(f"Wrote validation report to {output}")


if __name__ == "__main__":
    main()
