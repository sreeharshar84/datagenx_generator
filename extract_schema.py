#!/usr/bin/env python3
"""
Extract schema from a supported database and generate .dbgen files.
Customer-facing tool for schema extraction.
"""

import argparse
import os
import sys
import re
import json
import math
from datetime import datetime

# Import existing code
from datagenx.generation.GenerateDbgen import (
    histogram_to_case, char_varchar_appendage, text_appendage,
    string_values_to_case, synthetic_string_expression, get_string_column_length,
    STRING_CARDINALITY_THRESHOLD,
    topological_sort, NUMERIC_TYPES, DATETIME_TYPES, CHAR_TYPES,
    TEXT_TYPES, YEAR, SYNTHETIC_BASE_DATETIME
)

# Import our new library
from lib.schema_extractor import available_extractor_types, create_schema_extractor


def _load_table_cardinality(extractor, table):
    """Load table cardinality metadata through the extractor abstraction."""
    try:
        return extractor.get_table_cardinality(table)
    except Exception as e:
        print(f"    Note: cardinality lookup unavailable ({e}), skipping cardinality lookup")
        return {"row_count": None, "columns": {}, "indexes": {}}


def _positive_int(value):
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        try:
            value = int(float(value))
        except (TypeError, ValueError):
            return None
    return value if value > 0 else None


def _exact_table_row_count(extractor, database, table):
    """Return exact source row count, avoiding optimizer estimates for generation."""
    extractor.cursor.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
    value = extractor.cursor.fetchone()[0]
    return int(value) if value is not None else None


def _column_distinct_count(
    extractor,
    table,
    column,
    col_cardinality,
    exact_distinct_cache,
    *,
    prefer_exact=False,
):
    """Return source NDV for a column without reading source literals."""
    if column in exact_distinct_cache:
        return exact_distinct_cache[column]

    if not prefer_exact:
        stats_count = _positive_int(col_cardinality.get(column))
        if stats_count:
            return stats_count

    try:
        exact_count = _positive_int(extractor.get_distinct_count(table, column))
        if exact_count:
            exact_distinct_cache[column] = exact_count
            return exact_count
    except Exception as e:
        print(f"    Note: exact NDV lookup unavailable for {table}.{column} ({e})")

    stats_count = _positive_int(col_cardinality.get(column))
    if stats_count:
        return stats_count
    return None


def _numeric_ndv_expression(ddl_line, distinct_count):
    """Generate synthetic numeric values preserving NDV, not source literals."""
    distinct_count = _positive_int(distinct_count)
    if not distinct_count:
        return None

    decimal_match = re.search(
        r"decimal\(\s*\d+\s*,\s*(\d+)\s*\)",
        ddl_line,
        re.IGNORECASE,
    )
    scale = 10 ** int(decimal_match.group(1)) if decimal_match else 1
    ordinal = f"mod(rownum-1, {distinct_count}) + 1"
    if scale == 1:
        return ordinal
    return f"({ordinal}) / {scale}"


def _is_integer_column_type(col_type):
    """Return whether a normalized column type is integer-like."""
    if not col_type:
        return False
    return bool(re.search(r"\b(tinyint|smallint|mediumint|int|integer|bigint)\b", col_type))


def _temporal_ndv_expression(col_type, distinct_count):
    """Generate synthetic temporal values preserving NDV."""
    distinct_count = _positive_int(distinct_count)
    if not distinct_count:
        return None

    offset = f"mod(rownum-1, {distinct_count})"
    if col_type == "date":
        return f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL {offset} DAY"
    if col_type in ("datetime", "timestamp"):
        return f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL {offset} SECOND"
    if col_type == "time":
        return f"INTERVAL {offset} SECOND"
    return None


def _temporal_weighted_expression(col_type, histogram, total_distinct, total_rows):
    """Generate a weighted CASE temporal expression preserving histogram shape.

    Uses bucket cardinalities as weights to distribute rows non-uniformly,
    matching the source's histogram distribution shape.
    """
    buckets = histogram.get("buckets", [])
    if not buckets or len(buckets) < 2:
        return None
    total_rows = _positive_int(total_rows)
    if not total_rows:
        return None

    # Each bucket has [lo, hi, cum_freq, num_distinct]
    # Filter out empty buckets (cardinality=0, SingleStore uses these as gap markers)
    prev_cum_freq = 0.0
    active_buckets = []
    for bucket in buckets:
        if len(bucket) < 4:
            continue
        lo, hi, cum_freq, num_distinct = bucket[0], bucket[1], float(bucket[2]), int(bucket[3])
        bucket_freq = cum_freq - prev_cum_freq
        prev_cum_freq = cum_freq
        # Skip empty gap buckets
        if num_distinct <= 0 or bucket_freq <= 0:
            continue
        active_buckets.append((lo, hi, bucket_freq, num_distinct))

    if not active_buckets:
        return None

    # Calculate total non-NULL rows from bucket frequencies
    total_bucket_freq = sum(bf for _, _, bf, _ in active_buckets)
    non_null_rows = round(total_bucket_freq * total_rows) if total_bucket_freq < 0.999 else total_rows

    # Build CASE expression
    case_lines = []
    cumulative_rows = 0
    synthetic_offset = 0

    for lo, hi, bucket_freq, num_distinct in active_buckets:
        # Number of rows in this bucket (proportional to frequency)
        bucket_rows = max(1, round((bucket_freq / total_bucket_freq) * non_null_rows))
        cumulative_rows += bucket_rows

        # Within this bucket, cycle through num_distinct values
        if col_type == "date":
            expr = f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL ({synthetic_offset}+mod(rownum-1, {num_distinct})) DAY"
        elif col_type in ("datetime", "timestamp"):
            expr = f"TIMESTAMP '{SYNTHETIC_BASE_DATETIME}' + INTERVAL ({synthetic_offset}+mod(rownum-1, {num_distinct})) SECOND"
        elif col_type == "time":
            expr = f"INTERVAL ({synthetic_offset}+mod(rownum-1, {num_distinct})) SECOND"
        else:
            return None

        case_lines.append(f"when rownum <= {cumulative_rows} then {expr}")
        synthetic_offset += num_distinct

    if not case_lines:
        return None

    # If source has NULLs (total_bucket_freq < 1.0), remaining rows should be NULL
    if total_bucket_freq < 0.999:
        else_value = "NULL"
    else:
        last_expr = case_lines[-1].split(" then ")[1]
        else_value = last_expr

    return f"case\n    {' '.join(case_lines)}\n    else {else_value}\n    end"


def _year_ndv_expression(distinct_count):
    distinct_count = _positive_int(distinct_count)
    if not distinct_count:
        return None
    return f"mod(rownum-1, {distinct_count}) + 2000"


def _grouped_ndv_expression(distinct_count, row_count):
    """Use grouped cycling for composite PK components with repeated values."""
    distinct_count = _positive_int(distinct_count)
    row_count = _positive_int(row_count)
    if not distinct_count:
        return None
    if not row_count:
        return f"mod(rownum-1, {distinct_count}) + 1"

    rows_per_value = max(1, row_count // distinct_count)
    if rows_per_value >= 2:
        return f"mod(div(rownum-1, {rows_per_value}), {distinct_count}) + 1"
    return f"mod(rownum-1, {distinct_count}) + 1"


def _get_frequency_shape_groups(cursor, database, table, column):
    """Return grouped per-value frequencies without selecting source values."""
    cursor.execute(f"""
        SELECT frequency, COUNT(*) AS value_count
        FROM (
            SELECT COUNT(*) AS frequency
            FROM `{database}`.`{table}`
            WHERE `{column}` IS NOT NULL
            GROUP BY `{column}`
        ) grouped
        GROUP BY frequency
        ORDER BY frequency
    """)
    groups = []
    for frequency, value_count in cursor.fetchall():
        frequency = _positive_int(frequency)
        value_count = _positive_int(value_count)
        if frequency and value_count:
            groups.append((frequency, value_count))
    return groups


def _frequency_shape_expression(groups, distinct_count, row_count):
    """Build a synthetic ordinal expression that preserves frequency shape."""
    distinct_count = _positive_int(distinct_count)
    row_count = _positive_int(row_count)
    if not groups or not distinct_count or not row_count:
        return None

    total_distinct = sum(value_count for _frequency, value_count in groups)
    total_rows = sum(frequency * value_count for frequency, value_count in groups)
    if total_distinct != distinct_count:
        return None
    # Allow total_rows <= row_count (difference = NULL rows in source)
    if total_rows > row_count:
        return None

    case_lines = []
    cumulative_rows = 0
    ordinal_offset = 0
    for frequency, value_count in groups:
        band_start = cumulative_rows + 1
        band_rows = frequency * value_count
        cumulative_rows += band_rows
        expression = f"{ordinal_offset + 1}+div(rownum-{band_start},{frequency})"
        case_lines.append(f"when rownum <= {cumulative_rows} then {expression}")
        ordinal_offset += value_count

    # If source has NULLs (total_rows < row_count), remaining rows are NULL
    if total_rows < row_count:
        else_value = "NULL"
    else:
        else_value = str(distinct_count)

    return f"""case
    {' '.join(case_lines)}
    else {else_value}
    end"""


def _numeric_frequency_shape_expression(
    extractor,
    database,
    table,
    column,
    column_type,
    distinct_count,
    row_count,
):
    """Preserve low/medium-NDV integer frequency shape without source values."""
    if not _is_integer_column_type(column_type):
        return None

    distinct_count = _positive_int(distinct_count)
    row_count = _positive_int(row_count)
    if not distinct_count or not row_count or distinct_count >= row_count:
        return None

    max_distinct = int(os.environ.get(
        "DATAGENX_NUMERIC_FREQUENCY_SHAPE_MAX_DISTINCT",
        "10000",
    ))
    max_groups = int(os.environ.get(
        "DATAGENX_NUMERIC_FREQUENCY_SHAPE_MAX_GROUPS",
        "5000",
    ))
    if distinct_count > max_distinct:
        return None

    try:
        groups = _get_frequency_shape_groups(
            extractor.cursor,
            database,
            table,
            column,
        )
    except Exception as e:
        print(
            f"    Note: frequency-shape lookup unavailable for "
            f"{table}.{column} ({e})"
        )
        return None

    if not groups or len(groups) > max_groups:
        return None
    return _frequency_shape_expression(groups, distinct_count, row_count)


def _build_two_column_frequency_shape_pk_appendages(
    extractor,
    database,
    table,
    pk_info,
    row_count,
):
    """Preserve one composite-PK column's frequency shape while staying unique.

    For a two-column composite key, one column can be emitted in contiguous
    synthetic frequency bands while the partner column cycles.  If no frequency
    band is longer than the partner's NDV, each (banded, partner) pair remains
    unique.  The source query reads only counts grouped by counts, never source
    literal values.
    """
    row_count = _positive_int(row_count)
    if len(pk_info) != 2 or not row_count:
        return None

    max_groups = int(os.environ.get(
        "DATAGENX_COMPOSITE_PK_FREQUENCY_SHAPE_MAX_GROUPS",
        "5000",
    ))

    for shaped_col, shaped_distinct in sorted(pk_info, key=lambda item: item[1]):
        partner_col, partner_distinct = next(
            (col, distinct)
            for col, distinct in pk_info
            if col != shaped_col
        )
        partner_distinct = _positive_int(partner_distinct)
        if not partner_distinct:
            continue

        try:
            groups = _get_frequency_shape_groups(
                extractor.cursor,
                database,
                table,
                shaped_col,
            )
        except Exception as e:
            print(
                f"    Note: frequency-shape lookup unavailable for "
                f"{table}.{shaped_col} ({e})"
            )
            continue

        if not groups or len(groups) > max_groups:
            continue
        if max(frequency for frequency, _value_count in groups) > partner_distinct:
            continue

        shaped_expr = _frequency_shape_expression(
            groups,
            shaped_distinct,
            row_count,
        )
        if not shaped_expr:
            continue

        return {
            shaped_col: shaped_expr,
            partner_col: f"mod(rownum-1, {partner_distinct}) + 1",
        }

    return None


def _lcm(values):
    result = 1
    for value in values:
        value = _positive_int(value)
        if not value:
            continue
        result = result * value // math.gcd(result, value)
    return result


def _product(values):
    result = 1
    for value in values:
        value = _positive_int(value)
        if not value:
            continue
        result *= value
    return result


def _build_composite_pk_appendages(
    extractor,
    database,
    table,
    primary_key_columns,
    foreign_keys,
    generated_appendages,
    col_cardinality,
    table_row_count,
    exact_distinct_cache,
):
    """Precompute synthetic expressions for composite PK columns.

    The generic per-column fallback of rownum over-generates marginal NDV for
    composite keys. These expressions preserve per-column NDV while keeping the
    generated domain synthetic.
    """
    if len(primary_key_columns) <= 1:
        return {}

    pk_info = []
    for col in sorted(primary_key_columns):
        if col in generated_appendages or col in foreign_keys:
            continue
        distinct_count = _column_distinct_count(
            extractor,
            table,
            col,
            col_cardinality,
            exact_distinct_cache,
            prefer_exact=True,
        )
        distinct_count = _positive_int(distinct_count)
        if distinct_count:
            pk_info.append((col, distinct_count))

    if not pk_info:
        return {}

    row_count = _positive_int(table_row_count)
    frequency_shape_appendages = _build_two_column_frequency_shape_pk_appendages(
        extractor,
        database,
        table,
        pk_info,
        row_count,
    )
    if frequency_shape_appendages:
        return frequency_shape_appendages

    if len(pk_info) >= 2 and row_count:
        distinct_counts = [distinct_count for _col, distinct_count in pk_info]
        cycle_length = _lcm(distinct_counts)
        if cycle_length >= row_count:
            return {
                col: f"mod(rownum-1, {distinct_count}) + 1"
                for col, distinct_count in pk_info
            }

        value_space = _product(distinct_counts)
        if value_space >= row_count:
            ordered = sorted(pk_info, key=lambda item: item[1])
            largest_col, largest_distinct = ordered[-1]
            small_info = ordered[:-1]
            small_product = _product(distinct for _col, distinct in small_info)
            repeats_per_small_key = (row_count + small_product - 1) // small_product

            if small_product > 0 and repeats_per_small_key <= largest_distinct:
                appendages = {}
                prefix = 1
                for col, distinct_count in small_info:
                    appendages[col] = (
                        f"mod(div(mod(rownum-1, {small_product}), {prefix}), "
                        f"{distinct_count}) + 1"
                    )
                    prefix *= distinct_count

                appendages[largest_col] = (
                    f"mod(div(rownum-1, {small_product}) + "
                    f"mod(rownum-1, {small_product}) * {repeats_per_small_key}, "
                    f"{largest_distinct}) + 1"
                )
                return appendages

    appendages = {}
    for col, distinct_count in pk_info:
        expression = _grouped_ndv_expression(distinct_count, table_row_count)
        if expression:
            appendages[col] = expression
    return appendages


def _get_string_value_weights(cursor, database, table, column, cardinality):
    """Get per-value frequency weights for a low-cardinality string column.

    Queries GROUP BY to get only the frequency distribution. This is fast for
    low-cardinality columns (cardinality <= STRING_CARDINALITY_THRESHOLD).
    Returns list of (synthetic_value, count); source literals are not selected.
    """
    cardinality = _positive_int(cardinality) or 0
    try:
        cursor.execute(
            f"SELECT COUNT(*) AS cnt "
            f"FROM `{database}`.`{table}` "
            f"WHERE `{column}` IS NOT NULL "
            f"GROUP BY `{column}` ORDER BY cnt DESC"
        )
        rows = cursor.fetchall()
        if rows:
            return [(f"val_{i}", cnt) for i, (cnt,) in enumerate(rows, 1)]
    except Exception:
        pass

    # Fallback: equal weights
    return [(f"val_{i}", 1) for i in range(1, cardinality + 1)]


def annotate_table_with_statistics(extractor, database, table, generated_appendages=None):
    """
    Generate .dbgen file with statistical annotations.
    Uses schema extractor to get statistics from any supported database.
    """
    if generated_appendages is None:
        generated_appendages = {}

    # Get table DDL
    ddl = extractor.get_table_ddl(table)

    # Get column types
    column_types = extractor.get_columns(table)

    # Get primary keys
    primary_key_columns = extractor.get_primary_keys(table)

    # Get foreign keys
    foreign_keys = extractor.get_foreign_keys(table)

    # Analyze table to ensure statistics are up to date
    print(f"    Analyzing table {table}...")
    extractor.analyze_table(table)

    # Load cardinality metadata through the engine-specific extractor.
    table_cardinality = _load_table_cardinality(extractor, table)
    col_cardinality = table_cardinality.get("columns", {})
    try:
        table_row_count = _exact_table_row_count(extractor, database, table)
    except Exception as e:
        print(f"    Note: exact row count unavailable for {table} ({e}); using stats row count")
        table_row_count = table_cardinality.get("row_count")

    exact_distinct_cache = {}
    composite_pk_appendages = _build_composite_pk_appendages(
        extractor,
        database,
        table,
        primary_key_columns,
        foreign_keys,
        generated_appendages,
        col_cardinality,
        table_row_count,
        exact_distinct_cache,
    )

    new_lines = []

    for line in ddl.splitlines():
        m = re.match(r"\s*`([^`]+)`", line)
        if not m:
            new_lines.append(line)
            continue

        col = m.group(1)
        col_type = column_types.get(col)
        synthetic = ""

        # MasterRun.py may precompute expressions for FK, PK, or composite keys.
        if col in generated_appendages:
            synthetic = generated_appendages[col]

        elif col in composite_pk_appendages:
            synthetic = composite_pk_appendages[col]

        # FOREIGN KEY → use referenced generated domain
        elif col in foreign_keys:
            ref_table, ref_col = foreign_keys[col]
            comment = f"/*{{{{ @{col} := @{ref_col} }}}}*/"
            if line.rstrip().endswith(","):
                line = line.rstrip()[:-1] + f" {comment},"
            else:
                line = line + f" {comment}"

        # PRIMARY KEY or AUTO_INCREMENT
        elif (
            re.search(r"\bauto_increment\b", line, re.IGNORECASE)
            or col in primary_key_columns
        ):
            is_auto_increment = re.search(r"\bauto_increment\b", line, re.IGNORECASE)
            is_composite_pk = len(primary_key_columns) > 1
            is_fk = col in foreign_keys

            if is_composite_pk and not is_fk and not is_auto_increment:
                distinct_count = _column_distinct_count(
                    extractor,
                    table,
                    col,
                    col_cardinality,
                    exact_distinct_cache,
                    prefer_exact=True,
                )
                synthetic = _grouped_ndv_expression(distinct_count, table_row_count) or "rownum"
            else:
                # Single-column PK or AUTO_INCREMENT: synthetic dense key domain.
                synthetic = "rownum"

        elif col_type in CHAR_TYPES:
            # Use exact NDV for strings when engine stats are unavailable or
            # incomplete. COUNT(DISTINCT) is privacy-safe and runs once while
            # building this table's .dbgen template, not per generated row.
            card = _column_distinct_count(
                extractor,
                table,
                col,
                col_cardinality,
                exact_distinct_cache,
                prefer_exact=True,
            )
            # Check if actually all-NULL (cardinality estimate may report 1)
            if card and card <= 1:
                real_distinct = extractor.get_distinct_count(table, col)
                if real_distinct == 0:
                    card = 0
            col_max_length = get_string_column_length(line)
            if card == 0:
                # All-NULL column
                synthetic = "NULL"
            elif card and card <= STRING_CARDINALITY_THRESHOLD:
                values = _get_string_value_weights(
                    extractor.cursor, database, table, col, card)
                synthetic = string_values_to_case(
                    values,
                    col,
                    max_length=col_max_length,
                    row_count=table_row_count,
                )
            elif card:
                ordinal_expr = f"mod(rownum-1, {card}) + 1"
                synthetic = synthetic_string_expression(
                    col,
                    ordinal_expr,
                    card,
                    max_length=col_max_length,
                )
            else:
                synthetic = char_varchar_appendage(line)

        elif col_type in TEXT_TYPES:
            synthetic = text_appendage()

        elif col_type in DATETIME_TYPES:
            distinct_count = _column_distinct_count(
                extractor,
                table,
                col,
                col_cardinality,
                exact_distinct_cache,
                prefer_exact=True,
            )
            # Try histogram-weighted date expression to preserve distribution shape
            synthetic = None
            hist = extractor.get_column_histogram(table, col)
            if hist and hist.get("buckets") and distinct_count and distinct_count > 1:
                synthetic = _temporal_weighted_expression(col_type, hist, distinct_count, table_row_count)
            if not synthetic:
                synthetic = _temporal_ndv_expression(col_type, distinct_count) or "rand.u31_timestamp()"

        elif col_type in YEAR:
            distinct_count = _column_distinct_count(
                extractor,
                table,
                col,
                col_cardinality,
                exact_distinct_cache,
                prefer_exact=True,
            )
            synthetic = _year_ndv_expression(distinct_count) or "rand.range(1975,2025)"

        elif col_type in NUMERIC_TYPES:
            # Try to get histogram, with actual_distinct_count correction
            histogram = extractor.get_column_histogram(table, col)
            actual_distinct = _column_distinct_count(
                extractor,
                table,
                col,
                col_cardinality,
                exact_distinct_cache,
                prefer_exact=True,
            )
            # Check if column is actually all-NULL (distinct_count=0 from source)
            # col_cardinality may report 1 even for all-NULL columns
            all_null_column = False
            if actual_distinct and actual_distinct <= 1 and not histogram:
                real_distinct = extractor.get_distinct_count(table, col)
                if real_distinct == 0:
                    all_null_column = True
            frequency_shape = _numeric_frequency_shape_expression(
                extractor,
                database,
                table,
                col,
                col_type,
                actual_distinct,
                table_row_count,
            )
            if frequency_shape:
                synthetic = frequency_shape
            elif histogram:
                synthetic = histogram_to_case(
                    histogram,
                    line,
                    actual_distinct,
                    table_row_count,
                )
                if not synthetic:
                    synthetic = _numeric_ndv_expression(line, actual_distinct)
                elif actual_distinct:
                    # Verify generated expression doesn't undercount distinct values.
                    # SingleStore histogram num_distinct per bucket can sum to less
                    # than actual distinct count due to estimation.
                    import re as _re
                    gen_values = set(_re.findall(r'then (\d+)', synthetic))
                    if len(gen_values) < actual_distinct * 0.95:
                        synthetic = _numeric_ndv_expression(line, actual_distinct)
            else:
                synthetic = _numeric_ndv_expression(line, actual_distinct) or "rand.range(0,5)"
            # Override: if column is actually all-NULL, generate NULL
            if all_null_column:
                synthetic = "NULL"

        else:
            synthetic = ""

        if synthetic:
            comment = f"/*{{{{ @{col} := {synthetic} }}}}*/"
            if line.rstrip().endswith(","):
                line = line.rstrip()[:-1] + f" {comment},"
            else:
                line = line + f" {comment}"

        new_lines.append(line)

    return "\n".join(new_lines)


def build_fk_appendages_from_source(extractor, table):
    """Build FK expressions by querying distinct counts from source DB.

    For each FK column, queries the source (parent) table to find the
    distinct count of the referenced column, then generates a
    rand.range(1, distinct_count + 1) expression.

    This works because parent tables use rownum for PK (values 1..N),
    so rand.range(1, N+1) guarantees valid FK references as long as
    parent and child are generated with the same row count.
    """
    foreign_keys = extractor.get_foreign_keys(table)
    if not foreign_keys:
        return {}

    appendages = {}
    for col, (ref_table, ref_col) in foreign_keys.items():
        try:
            distinct_count = extractor.get_distinct_count(ref_table, ref_col)
            if distinct_count > 0:
                appendages[col] = f"rand.range(1,{distinct_count + 1})"
                print(f"    FK {col} -> {ref_table}.{ref_col}: "
                      f"rand.range(1,{distinct_count + 1})")
        except Exception as e:
            print(f"    FK {col} -> {ref_table}.{ref_col}: "
                  f"could not query ({e}), using placeholder")

    return appendages


def main():
    parser = argparse.ArgumentParser(
        description='Extract schema from a supported database',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Extract from MySQL
  %(prog)s --db-type mysql --host localhost --user root --database tpch

  # Extract from SingleStore
  %(prog)s --db-type singlestore --host prod.db.com --user admin --database mydb

  # Extract from TiDB
  %(prog)s --db-type tidb --host gateway01.region.prod.aws.tidbcloud.com --port 4000 --user tidb_user --database test

  # With password from environment
  export DB_PASSWORD=secret
  %(prog)s --db-type mysql --host localhost --user root --database tpch --password-env DB_PASSWORD
        '''
    )

    parser.add_argument('--db-type', required=True, choices=available_extractor_types(),
                        help='Database type')
    parser.add_argument('--host', required=True, help='Database host')
    parser.add_argument('--port', type=int, help='Database port (defaults to the engine default)')
    parser.add_argument('--user', required=True, help='Database user')
    parser.add_argument('--password', help='Database password')
    parser.add_argument('--password-env', help='Environment variable containing password')
    parser.add_argument('--database', required=True, help='Database name')
    parser.add_argument('--output-dir', default='dbgen_files', help='Output directory for .dbgen files')

    args = parser.parse_args()

    # Get password
    password = args.password
    if args.password_env:
        password = os.environ.get(args.password_env)
        if not password:
            print(f"❌ Environment variable {args.password_env} not set")
            sys.exit(1)
    if not password:
        print("❌ Password required (use --password or --password-env)")
        sys.exit(1)

    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)

    # Create extractor
    print(f"\n{'='*60}")
    print(f"EXTRACTING SCHEMA FROM {args.db_type.upper()}")
    print(f"{'='*60}")
    print(f"Database: {args.database}")
    print(f"Host: {args.host}")
    print(f"Output: {args.output_dir}/")
    print()

    extractor = create_schema_extractor(
        args.db_type,
        args.host,
        args.user,
        password,
        args.database,
        args.port,
    )

    if not extractor.connect():
        sys.exit(1)

    try:
        # Get all tables
        all_tables = extractor.get_tables()
        print(f"Found {len(all_tables)} tables: {', '.join(all_tables)}\n")

        # Get dependencies and sort
        dependencies = extractor.get_table_dependencies()
        sorted_tables = topological_sort(all_tables, dependencies)
        print(f"Processing order: {' -> '.join(sorted_tables)}\n")

        # Process each table
        for i, table in enumerate(sorted_tables, 1):
            deps = dependencies.get(table, [])
            dep_str = f" (depends on: {', '.join(deps)})" if deps else ""
            print(f"[{i}/{len(sorted_tables)}] Processing: {table}{dep_str}")

            # Build FK expressions from source DB distinct counts
            fk_appendages = build_fk_appendages_from_source(extractor, table)

            ddl = annotate_table_with_statistics(
                extractor, args.database, table,
                generated_appendages=fk_appendages,
            )

            output_file = os.path.join(args.output_dir, f"{table}.dbgen")
            with open(output_file, "w") as f:
                f.write(ddl)

            print(f"    ✅ Generated {output_file}\n")

        print(f"{'='*60}")
        print(f"✅ SUCCESS: {len(sorted_tables)} .dbgen files created in {args.output_dir}/")
        print(f"{'='*60}")

    finally:
        extractor.close()


if __name__ == "__main__":
    main()
