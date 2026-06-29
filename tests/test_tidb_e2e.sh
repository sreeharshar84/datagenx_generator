#!/usr/bin/env bash
# End-to-end verification runner for TiDB support.
#
# This script does not generate or load TPC-H/TPC-DS source data. Load the
# source schema into TiDB first, then run this script against that schema.
#
# Usage:
#   bash tests/test_tidb_e2e.sh tpch-sf1 [rows|full]
#   bash tests/test_tidb_e2e.sh tpch-sf5 [rows|full]
#   bash tests/test_tidb_e2e.sh tpch-sf10 [rows|full]
#   bash tests/test_tidb_e2e.sh tpcds-sf1 [rows|full]
#   bash tests/test_tidb_e2e.sh tpcds-sf5 [rows|full]
#   bash tests/test_tidb_e2e.sh tpcds-sf10 [rows|full]
#   bash tests/test_tidb_e2e.sh all [rows|full]
#
# Connection defaults:
#   TIDB_HOST=127.0.0.1
#   TIDB_PORT=4000
#   TIDB_USER=root
#   TIDB_PASSWORD=
#
# Source schema defaults:
#   TPCH_SF1_SOURCE_SCHEMA=tpch_sf1_tidb
#   TPCH_SF5_SOURCE_SCHEMA=tpch_sf5_tidb
#   TPCH_SF10_SOURCE_SCHEMA=tpch_10gb
#   TPCDS_SF1_SOURCE_SCHEMA=tpcds_sf1_tidb
#   TPCDS_SF5_SOURCE_SCHEMA=tpcds_sf5_tidb
#   TPCDS_SF10_SOURCE_SCHEMA=tpcds
#
# Example with an already-running TiDB:
#   START_TIDB=0 TIDB_PORT=4000 bash tests/test_tidb_e2e.sh tpch-sf10 1000
#   START_TIDB=0 TIDB_PORT=4000 bash tests/test_tidb_e2e.sh tpch-sf10 full
#
# Optional:
#   START_TIDB=0              # do not start tests/docker-compose TiDB
#   COMPARE_HISTOGRAMS=0      # omit --compare-histograms
#   RESULTS_DIR=results

set -euo pipefail

cd "$(dirname "$0")/.."

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

PROFILE="${1:-tpch-sf1}"
ROWS="${2:-${ROWS:-1000}}"
ROWS_OVERRIDE=1
ROWS_LABEL="$ROWS"

case "$ROWS" in
    full|source|match-source)
        ROWS_OVERRIDE=0
        ROWS_LABEL="match-source"
        ;;
    ''|*[!0-9]*)
        echo "ERROR: rows must be a positive integer or 'full'"
        echo ""
        usage
        exit 1
        ;;
    *)
        if [[ "$ROWS" -le 0 ]]; then
            echo "ERROR: rows must be a positive integer or 'full'"
            echo ""
            usage
            exit 1
        fi
        ;;
esac

TIDB_HOST="${TIDB_HOST:-127.0.0.1}"
TIDB_PORT="${TIDB_PORT:-4000}"
TIDB_USER="${TIDB_USER:-${TIDB_USERNAME:-root}}"
TIDB_PASSWORD="${TIDB_PASSWORD:-${TIDB_PASS:-}}"
START_TIDB="${START_TIDB:-1}"
COMPARE_HISTOGRAMS="${COMPARE_HISTOGRAMS:-1}"
RESULTS_DIR="${RESULTS_DIR:-results}"
TIDB_COMPOSE_FILE="${TIDB_COMPOSE_FILE:-tests/docker-compose/tidb-docker-compose.yml}"

export TIDB_HOST TIDB_PORT TIDB_USER TIDB_PASSWORD

if [[ "$PROFILE" == "-h" || "$PROFILE" == "--help" ]]; then
    usage
    exit 0
fi

require_python_connector() {
    python3 - <<'PY' >/dev/null
import mysql.connector
from lib.schema_extractor import connection_kwargs_for
PY
}

wait_for_tidb() {
    python3 - <<'PY'
import os
import time

import mysql.connector

from lib.schema_extractor import connection_kwargs_for

kwargs = connection_kwargs_for(
    "tidb",
    os.environ["TIDB_HOST"],
    os.environ["TIDB_USER"],
    os.environ.get("TIDB_PASSWORD", ""),
    "INFORMATION_SCHEMA",
    int(os.environ["TIDB_PORT"]),
    connection_timeout=3,
)

last_error = None
for _ in range(90):
    try:
        conn = mysql.connector.connect(**kwargs)
        cursor = conn.cursor()
        cursor.execute("SELECT VERSION()")
        print(f"TiDB ready: {cursor.fetchone()[0]}")
        cursor.close()
        conn.close()
        break
    except Exception as exc:
        last_error = exc
        time.sleep(1)
else:
    raise SystemExit(f"TiDB did not become ready: {last_error}")
PY
}

configure_local_tidb() {
    python3 - <<'PY'
import os

import mysql.connector

from lib.schema_extractor import connection_kwargs_for

kwargs = connection_kwargs_for(
    "tidb",
    os.environ["TIDB_HOST"],
    os.environ["TIDB_USER"],
    os.environ.get("TIDB_PASSWORD", ""),
    "INFORMATION_SCHEMA",
    int(os.environ["TIDB_PORT"]),
)

statements = [
    "set global tidb_enable_window_function = off",
    "set global tidb_enable_noop_functions = on",
    "set global tidb_txn_mode = pessimistic",
    "set global time_zone = '+00:00'",
    "set global tidb_enable_async_commit = 0",
    "set global tidb_enable_1pc = 0",
]

conn = mysql.connector.connect(**kwargs)
cursor = conn.cursor()
for sql in statements:
    try:
        cursor.execute(sql)
        print(f"OK: {sql}")
    except Exception as exc:
        print(f"WARN: {sql}: {exc}")
cursor.close()
conn.close()
PY
}

start_local_tidb() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "ERROR: docker is required when START_TIDB=1."
        exit 1
    fi

    echo "Starting local TiDB with $TIDB_COMPOSE_FILE on 127.0.0.1:$TIDB_PORT ..."
    LOCAL_PORT="$TIDB_PORT" docker compose -f "$TIDB_COMPOSE_FILE" up -d
    wait_for_tidb
    configure_local_tidb
}

check_source_schema() {
    local source_schema="$1"
    local expected_tables="$2"

    SOURCE_SCHEMA="$source_schema" EXPECTED_TABLES="$expected_tables" python3 - <<'PY'
import os
import sys

import mysql.connector

from lib.schema_extractor import connection_kwargs_for

source_schema = os.environ["SOURCE_SCHEMA"]
expected_tables = int(os.environ["EXPECTED_TABLES"])

kwargs = connection_kwargs_for(
    "tidb",
    os.environ["TIDB_HOST"],
    os.environ["TIDB_USER"],
    os.environ.get("TIDB_PASSWORD", ""),
    "INFORMATION_SCHEMA",
    int(os.environ["TIDB_PORT"]),
)

conn = mysql.connector.connect(**kwargs)
cursor = conn.cursor()
cursor.execute(
    """
    SELECT TABLE_NAME
    FROM INFORMATION_SCHEMA.TABLES
    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
    ORDER BY TABLE_NAME
    """,
    (source_schema,),
)
tables = [row[0] for row in cursor.fetchall()]
cursor.close()
conn.close()

if len(tables) < expected_tables:
    print(
        f"ERROR: source schema `{source_schema}` has {len(tables)} base tables; "
        f"expected at least {expected_tables}."
    )
    print("Load the benchmark source schema into TiDB first, then rerun this script.")
    sys.exit(1)

print(f"Source schema `{source_schema}`: {len(tables)} base tables")
PY
}

profile_config() {
    local profile="$1"
    case "$profile" in
        tpch-sf1|tpch_sf1|sf1)
            PROFILE_LABEL="TPC-H SF=1"
            SOURCE_SCHEMA="${TPCH_SF1_SOURCE_SCHEMA:-tpch_sf1_tidb}"
            TARGET_SCHEMA="${TPCH_SF1_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCH_SF1_RESULT_FILE:-$RESULTS_DIR/results_tpch_sf1_tidb.txt}"
            EXPECTED_TABLES=8
            ;;
        tpch-sf5|tpch_sf5|sf5)
            PROFILE_LABEL="TPC-H SF=5"
            SOURCE_SCHEMA="${TPCH_SF5_SOURCE_SCHEMA:-tpch_sf5_tidb}"
            TARGET_SCHEMA="${TPCH_SF5_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCH_SF5_RESULT_FILE:-$RESULTS_DIR/results_tpch_sf5_tidb.txt}"
            EXPECTED_TABLES=8
            ;;
        tpch-sf10|tpch_sf10)
            PROFILE_LABEL="TPC-H SF=10"
            SOURCE_SCHEMA="${TPCH_SF10_SOURCE_SCHEMA:-tpch_10gb}"
            TARGET_SCHEMA="${TPCH_SF10_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCH_SF10_RESULT_FILE:-$RESULTS_DIR/results_tpch_sf10_tidb.txt}"
            EXPECTED_TABLES=8
            ;;
        tpcds-sf1|tpcds_sf1)
            PROFILE_LABEL="TPC-DS SF=1"
            SOURCE_SCHEMA="${TPCDS_SF1_SOURCE_SCHEMA:-tpcds_sf1_tidb}"
            TARGET_SCHEMA="${TPCDS_SF1_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCDS_SF1_RESULT_FILE:-$RESULTS_DIR/results_tpcds_sf1_tidb.txt}"
            EXPECTED_TABLES=24
            ;;
        tpcds-sf5|tpcds_sf5)
            PROFILE_LABEL="TPC-DS SF=5"
            SOURCE_SCHEMA="${TPCDS_SF5_SOURCE_SCHEMA:-tpcds_sf5_tidb}"
            TARGET_SCHEMA="${TPCDS_SF5_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCDS_SF5_RESULT_FILE:-$RESULTS_DIR/results_tpcds_sf5_tidb.txt}"
            EXPECTED_TABLES=24
            ;;
        tpcds-sf10|tpcds_sf10)
            PROFILE_LABEL="TPC-DS SF=10"
            SOURCE_SCHEMA="${TPCDS_SF10_SOURCE_SCHEMA:-tpcds}"
            TARGET_SCHEMA="${TPCDS_SF10_TARGET_SCHEMA:-${SOURCE_SCHEMA}_test}"
            RESULT_FILE="${TPCDS_SF10_RESULT_FILE:-$RESULTS_DIR/results_tpcds_sf10_tidb.txt}"
            EXPECTED_TABLES=24
            ;;
        *)
            echo "ERROR: unknown TiDB E2E profile: $profile"
            echo ""
            usage
            exit 1
            ;;
    esac
}

run_profile() {
    local profile="$1"
    profile_config "$profile"

    local dbgen_files_dir="${DBGEN_FILES_DIR:-generated/dbgen_files}"
    local dbgen_tmp_out_dir="${DBGEN_TMP_OUT_DIR:-generated/dbgen_tmp_out}"
    local histogram_arg=()

    if [[ "$COMPARE_HISTOGRAMS" == "1" ]]; then
        histogram_arg=(--compare-histograms)
    fi

    mkdir -p "$(dirname "$RESULT_FILE")" "$dbgen_files_dir" "$dbgen_tmp_out_dir"
    check_source_schema "$SOURCE_SCHEMA" "$EXPECTED_TABLES"

    echo "============================================================"
    echo "  TiDB E2E Test: $PROFILE_LABEL"
    echo "============================================================"
    echo "Host:    $TIDB_HOST:$TIDB_PORT"
    echo "Source:  $SOURCE_SCHEMA"
    echo "Target:  $TARGET_SCHEMA"
    echo "Rows:    $ROWS_LABEL"
    echo "Output:  $RESULT_FILE"
    echo ""

    (
        echo "DataGenX TiDB E2E verification"
        echo "profile=$profile"
        echo "label=$PROFILE_LABEL"
        echo "host=$TIDB_HOST"
        echo "port=$TIDB_PORT"
        echo "source=$SOURCE_SCHEMA"
        echo "target=$TARGET_SCHEMA"
        echo "rows=$ROWS_LABEL"
        echo ""

        rows_arg=()
        if [[ "$ROWS_OVERRIDE" == "1" ]]; then
            rows_arg=(--rows "$ROWS")
        fi
        password_arg=()
        if [[ -z "$TIDB_PASSWORD" ]]; then
            password_arg=("--password=")
        fi

        PYTHONUNBUFFERED=1 \
        DBGEN_FILES_DIR="$dbgen_files_dir" \
        DBGEN_TMP_OUT_DIR="$dbgen_tmp_out_dir" \
        python3 MasterRun.py \
            --db-type tidb \
            --host "$TIDB_HOST" \
            --port "$TIDB_PORT" \
            --user "$TIDB_USER" \
            "${password_arg[@]}" \
            --source-schema "$SOURCE_SCHEMA" \
            --target-schema "$TARGET_SCHEMA" \
            "${rows_arg[@]}" \
            --run-validation \
            --verbose \
            "${histogram_arg[@]}"
    ) 2>&1 | tee "$RESULT_FILE"

    local status="${PIPESTATUS[0]}"
    if [[ "$status" -ne 0 ]]; then
        echo ""
        echo "FAILED: $PROFILE_LABEL. See $RESULT_FILE"
        return "$status"
    fi

    echo ""
    echo "PASSED: $PROFILE_LABEL. Results written to $RESULT_FILE"
}

require_python_connector

if [[ "$START_TIDB" == "1" ]]; then
    start_local_tidb
else
    wait_for_tidb
fi

case "$PROFILE" in
    all)
        run_profile tpch-sf1
        run_profile tpch-sf5
        run_profile tpch-sf10
        run_profile tpcds-sf1
        run_profile tpcds-sf5
        run_profile tpcds-sf10
        ;;
    *)
        run_profile "$PROFILE"
        ;;
esac

echo ""
echo "TiDB E2E run complete."
