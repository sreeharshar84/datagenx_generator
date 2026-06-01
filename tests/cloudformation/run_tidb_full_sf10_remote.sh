#!/usr/bin/env bash
# Run full-size TiDB verification on an EC2 host created by
# tests/cloudformation/tidb-full-sf10.yml.

set -Eeuo pipefail

export HOME="${HOME:-/root}"

PROFILE="${1:-all}"

BASE_DIR="${BASE_DIR:-/opt/datagenx-run}"
REPO_DIR="${REPO_DIR:-$(pwd)}"
BENCH_DIR="${BENCH_DIR:-$BASE_DIR/bench}"
RESULTS_DIR="${RESULTS_DIR:-$BASE_DIR/results}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"

TPCH_SCALE_FACTOR="${TPCH_SCALE_FACTOR:-10}"
TPCDS_SCALE_FACTOR="${TPCDS_SCALE_FACTOR:-10}"
TPCH_SCALE_LABEL="${TPCH_SCALE_LABEL:-sf${TPCH_SCALE_FACTOR//./_}}"
TPCDS_SCALE_LABEL="${TPCDS_SCALE_LABEL:-sf${TPCDS_SCALE_FACTOR//./_}}"
TPCH_SOURCE_SCHEMA="${TPCH_SOURCE_SCHEMA:-}"
TPCH_TARGET_SCHEMA="${TPCH_TARGET_SCHEMA:-}"
TPCDS_SOURCE_SCHEMA="${TPCDS_SOURCE_SCHEMA:-}"
TPCDS_TARGET_SCHEMA="${TPCDS_TARGET_SCHEMA:-}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
SCHEMA_NAMESPACE="${SCHEMA_NAMESPACE:-}"

START_LOCAL_TIDB="${START_LOCAL_TIDB:-0}"
TIDB_IMAGE="${TIDB_IMAGE:-pingcap/tidb:v8.5.6}"
TIDB_PORT="${TIDB_PORT:-4000}"
TIDB_HOST="${TIDB_HOST:-}"
TIDB_USER="${TIDB_USER:-${TIDB_USERNAME:-}}"
TIDB_PASSWORD="${TIDB_PASSWORD:-}"
TIDB_DATABASE="${TIDB_DATABASE:-}"
TIDB_ENV_FILE="${TIDB_ENV_FILE:-$REPO_DIR/.env}"
TIDB_BENCH_REPO="${TIDB_BENCH_REPO:-https://github.com/pingcap/tidb-bench.git}"
ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-}"
ARTIFACT_PREFIX="${ARTIFACT_PREFIX:-results}"
GENERATED_DIR="${GENERATED_DIR:-$BASE_DIR/generated/$ARTIFACT_PREFIX}"

DBGEN_CRATE_VERSION="${DBGEN_CRATE_VERSION:-0.8.0}"
DATAGENX_DBGEN_ROOT="${DATAGENX_DBGEN_ROOT:-$BASE_DIR/datagenx-dbgen}"
DATAGENX_DBGEN_BINARY="${DATAGENX_DBGEN_BINARY:-$DATAGENX_DBGEN_ROOT/bin/dbgen}"

CLEAN_SOURCE_FILES_AFTER_LOAD="${CLEAN_SOURCE_FILES_AFTER_LOAD:-1}"
CLEAN_GENERATED_FILES_AFTER_LOAD="${CLEAN_GENERATED_FILES_AFTER_LOAD:-1}"
ENABLE_TIFLASH="${ENABLE_TIFLASH:-1}"
TIFLASH_REPLICA_COUNT="${TIFLASH_REPLICA_COUNT:-3}"
TIFLASH_WAIT="${TIFLASH_WAIT:-0}"
TIFLASH_WAIT_TIMEOUT_SECONDS="${TIFLASH_WAIT_TIMEOUT_SECONDS:-3600}"
TIFLASH_FAIL_ON_ERROR="${TIFLASH_FAIL_ON_ERROR:-1}"
REUSE_SOURCE_FILES="${REUSE_SOURCE_FILES:-1}"
REUSE_LOADED_SOURCE_SCHEMA="${REUSE_LOADED_SOURCE_SCHEMA:-1}"
LOAD_CHUNK_SIZE="${LOAD_CHUNK_SIZE:-128m}"
LOAD_RETRY_ATTEMPTS="${LOAD_RETRY_ATTEMPTS:-3}"

MYSQL=()

mkdir -p "$BASE_DIR" "$BENCH_DIR" "$RESULTS_DIR" "$LOG_DIR" "$GENERATED_DIR"

sync_artifacts() {
    if [[ -z "$ARTIFACT_BUCKET" ]]; then
        return
    fi
    log "Syncing results/logs to s3://$ARTIFACT_BUCKET/$ARTIFACT_PREFIX"
    aws s3 sync "$RESULTS_DIR" "s3://$ARTIFACT_BUCKET/$ARTIFACT_PREFIX/results/" || true
    aws s3 sync "$LOG_DIR" "s3://$ARTIFACT_BUCKET/$ARTIFACT_PREFIX/logs/" || true
}

on_exit() {
    local status=$?
    if [[ "$status" -eq 0 ]]; then
        echo "success $(timestamp)" > "$BASE_DIR/SUCCESS"
    else
        echo "failed status=$status $(timestamp)" > "$BASE_DIR/FAILED"
    fi
    sync_artifacts
    exit "$status"
}

trap on_exit EXIT

timestamp() {
    date -u "+%Y-%m-%dT%H:%M:%SZ"
}

log() {
    echo "[$(timestamp)] $*"
}

run_sql() {
    "${MYSQL[@]}" -e "$1"
}

run_sql_best_effort() {
    "${MYSQL[@]}" -e "$1" || true
}

mysql_scalar() {
    "${MYSQL[@]}" -N -B -e "$1" 2>/dev/null | tail -n 1
}

schema_base_table_count() {
    local schema="$1"
    mysql_scalar "
SELECT COUNT(*)
FROM information_schema.tables
WHERE table_schema = '$schema'
  AND table_type = 'BASE TABLE';
"
}

schema_has_base_tables() {
    local schema="$1"
    local expected="$2"
    local count
    count="$(schema_base_table_count "$schema")"
    [[ "${count:-0}" -ge "$expected" ]]
}

run_sql_with_retry() {
    local label="$1"
    local sql="$2"
    local attempt

    for attempt in $(seq 1 "$LOAD_RETRY_ATTEMPTS"); do
        if "${MYSQL[@]}" -e "$sql"; then
            return 0
        fi
        log "WARN: $label failed on attempt $attempt/$LOAD_RETRY_ATTEMPTS"
        sleep $((attempt * 15))
    done

    log "ERROR: $label failed after $LOAD_RETRY_ATTEMPTS attempts"
    return 1
}

mysql_db_statement() {
    local schema="$1"
    local sql="$2"
    "${MYSQL[@]}" "$schema" -e "$sql"
}

load_data_file_once() {
    local schema="$1"
    local table="$2"
    local file="$3"
    local columns="${4:-}"
    local sql

    sql="LOAD DATA LOCAL INFILE '$file' INTO TABLE \`$table\` FIELDS TERMINATED BY '|' LINES TERMINATED BY '\n'"
    if [[ -n "$columns" ]]; then
        sql+=" $columns"
    fi
    sql+=";"

    mysql_db_statement "$schema" "$sql"
}

load_data_file_with_chunks() {
    local schema="$1"
    local table="$2"
    local file="$3"
    local columns="${4:-}"
    local chunk_dir="$BASE_DIR/work/chunks/${schema}/${table}"
    local attempt chunk

    for attempt in $(seq 1 "$LOAD_RETRY_ATTEMPTS"); do
        log "Loading $schema.$table from $(basename "$file") attempt $attempt/$LOAD_RETRY_ATTEMPTS"
        run_sql_with_retry "truncate $schema.$table before load" \
            "TRUNCATE TABLE \`$schema\`.\`$table\`"

        rm -rf "$chunk_dir"
        mkdir -p "$chunk_dir"

        if split -C "$LOAD_CHUNK_SIZE" -d -a 5 --additional-suffix=.part "$file" "$chunk_dir/${table}_"; then
            local failed=0
            for chunk in "$chunk_dir"/*.part; do
                if ! load_data_file_once "$schema" "$table" "$chunk" "$columns"; then
                    failed=1
                    log "WARN: chunk load failed for $schema.$table: $(basename "$chunk")"
                    break
                fi
            done
            rm -rf "$chunk_dir"

            if [[ "$failed" == "0" ]]; then
                log "Loaded $schema.$table"
                return 0
            fi
        else
            rm -rf "$chunk_dir"
            log "WARN: split failed for $file"
        fi

        sleep $((attempt * 30))
    done

    log "ERROR: failed loading $schema.$table after $LOAD_RETRY_ATTEMPTS attempts"
    return 1
}

setup_python() {
    log "Setting up Python environment"
    cd "$REPO_DIR"
    python3 -m venv .venv
    .venv/bin/python3 -m pip install --upgrade pip
    .venv/bin/python3 -m pip install -r requirements.txt
    export VIRTUAL_ENV="$REPO_DIR/.venv"
    export PATH="$VIRTUAL_ENV/bin:$PATH"
}

load_tidb_env() {
    if [[ ! -f "$TIDB_ENV_FILE" ]]; then
        log "No TiDB env file found at $TIDB_ENV_FILE; using current environment"
        return
    fi

    log "Loading TiDB Cloud connection environment from $TIDB_ENV_FILE"
    cd "$REPO_DIR"
    eval "$(.venv/bin/python3 - "$TIDB_ENV_FILE" <<'PY'
import re
import shlex
import sys
from pathlib import Path
from dotenv import dotenv_values

env_path = Path(sys.argv[1])
allowed = re.compile(r"^(TIDB|DB|DATAGENX|SOURCE_SCHEMA|TARGET_SCHEMA|TPCH|TPCDS|TIFLASH|SCHEMA)_")
for key, value in dotenv_values(env_path).items():
    if value is None:
        continue
    if allowed.match(key) or key in {"DB_TYPE"}:
        print(f"export {key}={shlex.quote(value)}")
PY
)"

    TIDB_HOST="${TIDB_HOST:-${DB_HOST:-${DATAGENX_DB_HOST:-}}}"
    TIDB_PORT="${TIDB_PORT:-${DB_PORT:-${DATAGENX_DB_PORT:-4000}}}"
    TIDB_USER="${TIDB_USER:-${TIDB_USERNAME:-${DB_USER:-${DB_USERNAME:-${DATAGENX_DB_USER:-root}}}}}"
    TIDB_PASSWORD="${TIDB_PASSWORD:-${DB_PASSWORD:-${DATAGENX_DB_PASSWORD:-}}}"
    TIDB_DATABASE="${TIDB_DATABASE:-${DB_DATABASE:-${DATAGENX_SOURCE_SCHEMA:-test}}}"

    local schema_prefix="$TIDB_DATABASE"
    if [[ -n "$SCHEMA_NAMESPACE" ]]; then
        schema_prefix="${TIDB_DATABASE}_${SCHEMA_NAMESPACE}"
    fi

    TPCH_SOURCE_SCHEMA="${TPCH_SOURCE_SCHEMA:-${schema_prefix}_tpch_${TPCH_SCALE_LABEL}_source}"
    TPCH_TARGET_SCHEMA="${TPCH_TARGET_SCHEMA:-${schema_prefix}_tpch_${TPCH_SCALE_LABEL}_datagenx_${RUN_ID}}"
    TPCDS_SOURCE_SCHEMA="${TPCDS_SOURCE_SCHEMA:-${schema_prefix}_tpcds_${TPCDS_SCALE_LABEL}_source}"
    TPCDS_TARGET_SCHEMA="${TPCDS_TARGET_SCHEMA:-${schema_prefix}_tpcds_${TPCDS_SCALE_LABEL}_datagenx_${RUN_ID}}"

    if [[ -z "$TIDB_HOST" ]]; then
        echo "ERROR: TIDB_HOST is required" >&2
        exit 1
    fi
}

configure_mysql_client() {
    MYSQL=(mysql --local-infile=1 --connect-timeout=30 -h "$TIDB_HOST" -P "$TIDB_PORT" -u "$TIDB_USER")
    if [[ -n "$TIDB_PASSWORD" ]]; then
        export MYSQL_PWD="$TIDB_PASSWORD"
    fi

    if [[ "$TIDB_HOST" == *tidbcloud.com ]]; then
        MYSQL+=(--ssl)
        if [[ -n "${TIDB_SSL_CA:-}" ]]; then
            MYSQL+=(--ssl-ca="$TIDB_SSL_CA")
        fi
    fi
}

setup_datagenx_dbgen() {
    if [[ -x "$DATAGENX_DBGEN_BINARY" ]]; then
        log "Using existing DataGenX dbgen binary: $DATAGENX_DBGEN_BINARY"
        return
    fi

    log "Installing Rust toolchain for DataGenX dbgen"
    if ! command -v cargo >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
            | sh -s -- -y --profile minimal
        # shellcheck disable=SC1090
        source "$HOME/.cargo/env"
    fi

    log "Building patched dbgen crate v$DBGEN_CRATE_VERSION"
    local build_dir="$BASE_DIR/build/dbgen-$DBGEN_CRATE_VERSION"
    rm -rf "$build_dir"
    mkdir -p "$build_dir"
    cd "$build_dir"

    curl -fL "https://static.crates.io/crates/dbgen/dbgen-$DBGEN_CRATE_VERSION.crate" \
        -o dbgen.crate
    tar -xzf dbgen.crate --strip-components=1

    python3 - <<'PY'
from pathlib import Path

path = Path("Cargo.toml")
text = path.read_text()
old = '[dependencies.rand]\nversion = "0.8"\ndefault-features = false'
new = '[dependencies.rand]\nversion = "0.8"\nfeatures = ["getrandom"]\ndefault-features = false'
if old not in text:
    raise SystemExit("Could not patch rand getrandom feature in Cargo.toml")
path.write_text(text.replace(old, new))
PY

    export PATH="$HOME/.cargo/bin:$PATH"
    cargo install --path . --root "$DATAGENX_DBGEN_ROOT"
    "$DATAGENX_DBGEN_BINARY" --help >/dev/null
}

start_local_tidb() {
    configure_mysql_client
    log "Starting TiDB Docker container: $TIDB_IMAGE"
    mkdir -p "$BASE_DIR/tidb-data"
    docker rm -f datagenx-tidb >/dev/null 2>&1 || true
    docker pull "$TIDB_IMAGE"
    docker run -d \
        --name datagenx-tidb \
        --restart unless-stopped \
        -v "$BASE_DIR/config:/config:ro" \
        -v "$BASE_DIR/tidb-data:/data" \
        -p "127.0.0.1:$TIDB_PORT:4000" \
        "$TIDB_IMAGE" \
        -config /config/tidb.toml \
        --store=unistore \
        --path=/data/tidb

    log "Waiting for TiDB to accept connections"
    for _ in $(seq 1 120); do
        if "${MYSQL[@]}" -e "SELECT 1" >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    "${MYSQL[@]}" -e "SELECT VERSION()"

    configure_tidb_session
}

configure_tidb_session() {
    log "Configuring TiDB compatibility variables"
    run_sql_best_effort "set global tidb_enable_window_function = off"
    run_sql_best_effort "set global tidb_enable_noop_functions = on"
    run_sql_best_effort "set global tidb_txn_mode = pessimistic"
    run_sql_best_effort "set global time_zone = UTC"
    run_sql_best_effort "set global tidb_enable_async_commit = 0"
    run_sql_best_effort "set global tidb_enable_1pc = 0"
    run_sql_best_effort "set global tidb_enable_foreign_key = on"
    run_sql_best_effort "set global local_infile = 1"
}

setup_tidb_connection() {
    if [[ "$START_LOCAL_TIDB" == "1" ]]; then
        start_local_tidb
        return
    fi

    configure_mysql_client
    log "Checking TiDB Cloud connection at $TIDB_HOST:$TIDB_PORT"
    "${MYSQL[@]}" -e "SELECT VERSION(), DATABASE()"
    configure_tidb_session
}

prepare_tidb_bench() {
    if [[ -d "$BENCH_DIR/tidb-bench/.git" ]]; then
        log "Updating existing tidb-bench checkout"
        git -C "$BENCH_DIR/tidb-bench" fetch --depth 1 origin master
        git -C "$BENCH_DIR/tidb-bench" checkout -q FETCH_HEAD
    else
        log "Cloning tidb-bench"
        git clone --depth 1 "$TIDB_BENCH_REPO" "$BENCH_DIR/tidb-bench"
    fi
}

tpch_source_files_ready() {
    local tpch_dir="$1"
    local table
    for table in region nation part supplier customer partsupp orders lineitem; do
        if [[ ! -s "$tpch_dir/$table.tbl" ]]; then
            return 1
        fi
    done
    return 0
}

tpcds_source_files_ready() {
    local tools_dir="$1"
    compgen -G "$tools_dir/*.dat" >/dev/null
}

tpch_foreign_key_count() {
    local schema="$1"
    mysql_scalar "
SELECT COUNT(*)
FROM information_schema.referential_constraints
WHERE constraint_schema = '$schema';
"
}

write_tpch_constraints_sql() {
    local dst="$1"
    python3 - "$REPO_DIR/sql/load_tpch_vanilla.sql" "$dst" "$TPCH_SOURCE_SCHEMA" <<'PY'
import sys
from pathlib import Path

src, dst, schema = sys.argv[1:]
text = Path(src).read_text()
alter_pos = text.index("ALTER TABLE")
analyze_pos = text.index("ANALYZE TABLE")
Path(dst).write_text(f"USE `{schema}`;\n" + text[alter_pos:analyze_pos])
PY
}

apply_tpch_constraints_and_analyze() {
    local constraints_sql="$1"
    local fk_count

    fk_count="$(tpch_foreign_key_count "$TPCH_SOURCE_SCHEMA")"
    if [[ "${fk_count:-0}" -ge 8 ]]; then
        log "TPC-H source foreign keys already exist for $TPCH_SOURCE_SCHEMA"
    else
        if [[ "${fk_count:-0}" -gt 0 ]]; then
            log "WARN: $TPCH_SOURCE_SCHEMA has only $fk_count foreign keys; applying TPC-H constraints may report duplicates"
        fi
        "${MYSQL[@]}" "$TPCH_SOURCE_SCHEMA" < "$constraints_sql"
    fi

    run_sql_with_retry "analyze TPC-H source tables" \
        "ANALYZE TABLE \`$TPCH_SOURCE_SCHEMA\`.\`region\`, \`$TPCH_SOURCE_SCHEMA\`.\`nation\`, \`$TPCH_SOURCE_SCHEMA\`.\`part\`, \`$TPCH_SOURCE_SCHEMA\`.\`supplier\`, \`$TPCH_SOURCE_SCHEMA\`.\`customer\`, \`$TPCH_SOURCE_SCHEMA\`.\`partsupp\`, \`$TPCH_SOURCE_SCHEMA\`.\`orders\`, \`$TPCH_SOURCE_SCHEMA\`.\`lineitem\` ALL COLUMNS"
}

record_tiflash_status() {
    local schema="$1"
    local report="$RESULTS_DIR/tiflash_replica_status.txt"

    {
        echo "============================================================"
        echo "TiFlash replica status for $schema"
        echo "timestamp=$(timestamp)"
        echo "============================================================"
    } >> "$report"

    "${MYSQL[@]}" -e "
SELECT
  TABLE_SCHEMA,
  TABLE_NAME,
  REPLICA_COUNT,
  AVAILABLE,
  ROUND(PROGRESS, 4) AS PROGRESS
FROM information_schema.tiflash_replica
WHERE TABLE_SCHEMA = '$schema'
ORDER BY TABLE_NAME;
" >> "$report" || true

    {
        echo ""
        echo "Tables without TiFlash replicas:"
    } >> "$report"

    "${MYSQL[@]}" -e "
SELECT TABLE_NAME
FROM information_schema.tables
WHERE TABLE_SCHEMA = '$schema'
  AND TABLE_TYPE = 'BASE TABLE'
  AND TABLE_NAME NOT IN (
    SELECT TABLE_NAME
    FROM information_schema.tiflash_replica
    WHERE TABLE_SCHEMA = '$schema'
  )
ORDER BY TABLE_NAME;
" >> "$report" || true

    echo "" >> "$report"
}

wait_for_tiflash_replica() {
    local schema="$1"
    local deadline=$((SECONDS + TIFLASH_WAIT_TIMEOUT_SECONDS))

    while (( SECONDS < deadline )); do
        local pending
        pending="$("${MYSQL[@]}" -N -B -e "
SELECT COUNT(*)
FROM information_schema.tiflash_replica
WHERE TABLE_SCHEMA = '$schema'
  AND (AVAILABLE = 0 OR PROGRESS < 1);
" 2>/dev/null || echo 1)"
        if [[ "$pending" == "0" ]]; then
            log "TiFlash replicas are available for $schema"
            return 0
        fi
        log "Waiting for TiFlash replicas for $schema; pending=$pending"
        sleep 60
    done

    log "Timed out waiting for TiFlash replicas for $schema"
    return 1
}

apply_tiflash_replica() {
    local schema="$1"

    if [[ "$ENABLE_TIFLASH" != "1" ]]; then
        log "TiFlash replica creation disabled for $schema"
        return 0
    fi

    log "Setting TiFlash replica count=$TIFLASH_REPLICA_COUNT for database $schema"
    local output
    if ! output="$("${MYSQL[@]}" -e "ALTER DATABASE \`$schema\` SET TIFLASH REPLICA $TIFLASH_REPLICA_COUNT" 2>&1)"; then
        {
            echo "============================================================"
            echo "TiFlash replica DDL failed for $schema"
            echo "timestamp=$(timestamp)"
            echo "$output"
            echo ""
        } >> "$RESULTS_DIR/tiflash_replica_status.txt"

        if [[ "$TIFLASH_FAIL_ON_ERROR" == "1" ]]; then
            echo "$output" >&2
            return 1
        fi
        return 0
    fi

    record_tiflash_status "$schema"
    if [[ "$TIFLASH_WAIT" == "1" ]]; then
        wait_for_tiflash_replica "$schema"
        record_tiflash_status "$schema"
    fi
}

load_tpch_source() {
    local tpch_dir="$BENCH_DIR/tidb-bench/tpch/dbgen"
    local constraints_sql="$BASE_DIR/work/load_tpch_${TPCH_SCALE_FACTOR}_constraints.sql"

    write_tpch_constraints_sql "$constraints_sql"

    if [[ "$REUSE_LOADED_SOURCE_SCHEMA" == "1" ]] && schema_has_base_tables "$TPCH_SOURCE_SCHEMA" 8; then
        log "Reusing existing loaded TPC-H source schema: $TPCH_SOURCE_SCHEMA"
        apply_tpch_constraints_and_analyze "$constraints_sql"
        apply_tiflash_replica "$TPCH_SOURCE_SCHEMA"
        if [[ "$CLEAN_SOURCE_FILES_AFTER_LOAD" == "1" ]]; then
            log "Cleaning TPC-H raw .tbl files after source schema reuse"
            rm -f "$tpch_dir"/*.tbl
        fi
        return
    fi

    cd "$tpch_dir"

    if [[ "$REUSE_SOURCE_FILES" == "1" ]] && tpch_source_files_ready "$tpch_dir"; then
        log "Reusing existing TPC-H SF=$TPCH_SCALE_FACTOR .tbl files"
    else
        log "Generating TPC-H SF=$TPCH_SCALE_FACTOR source data"
        make clean >/dev/null 2>&1 || true
        make -j"$(nproc)"
        rm -f ./*.tbl
        ./dbgen -f -s "$TPCH_SCALE_FACTOR"
    fi

    log "Loading TPC-H source schema: $TPCH_SOURCE_SCHEMA"
    local schema_sql="$BASE_DIR/work/load_tpch_${TPCH_SCALE_FACTOR}_schema.sql"
    python3 - "$REPO_DIR/sql/load_tpch_vanilla.sql" "$schema_sql" "$constraints_sql" "$TPCH_SOURCE_SCHEMA" <<'PY'
import sys
from pathlib import Path

src, schema_dst, constraints_dst, schema = sys.argv[1:]
text = Path(src).read_text()
text = text.replace("`tpch_vanilla`", f"`{schema}`")
text = text.replace("SET GLOBAL local_infile = 1;\n\n", "")
load_pos = text.index("LOAD DATA LOCAL INFILE")
alter_pos = text.index("ALTER TABLE")
analyze_pos = text.index("ANALYZE TABLE")
Path(schema_dst).write_text(text[:load_pos])
Path(constraints_dst).write_text(f"USE `{schema}`;\n" + text[alter_pos:analyze_pos])
PY
    "${MYSQL[@]}" < "$schema_sql"

    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" region "$tpch_dir/region.tbl" \
        '(`r_regionkey`, `r_name`, `r_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" nation "$tpch_dir/nation.tbl" \
        '(`n_nationkey`, `n_name`, `n_regionkey`, `n_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" part "$tpch_dir/part.tbl" \
        '(`p_partkey`, `p_name`, `p_mfgr`, `p_brand`, `p_type`, `p_size`, `p_container`, `p_retailprice`, `p_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" supplier "$tpch_dir/supplier.tbl" \
        '(`s_suppkey`, `s_name`, `s_address`, `s_nationkey`, `s_phone`, `s_acctbal`, `s_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" customer "$tpch_dir/customer.tbl" \
        '(`c_custkey`, `c_name`, `c_address`, `c_nationkey`, `c_phone`, `c_acctbal`, `c_mktsegment`, `c_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" partsupp "$tpch_dir/partsupp.tbl" \
        '(`ps_partkey`, `ps_suppkey`, `ps_availqty`, `ps_supplycost`, `ps_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" orders "$tpch_dir/orders.tbl" \
        '(`o_orderkey`, `o_custkey`, `o_orderstatus`, `o_totalprice`, `o_orderdate`, `o_orderpriority`, `o_clerk`, `o_shippriority`, `o_comment`, @dummy)'
    load_data_file_with_chunks "$TPCH_SOURCE_SCHEMA" lineitem "$tpch_dir/lineitem.tbl" \
        '(`l_orderkey`, `l_partkey`, `l_suppkey`, `l_linenumber`, `l_quantity`, `l_extendedprice`, `l_discount`, `l_tax`, `l_returnflag`, `l_linestatus`, `l_shipdate`, `l_commitdate`, `l_receiptdate`, `l_shipinstruct`, `l_shipmode`, `l_comment`, @dummy)'

    apply_tpch_constraints_and_analyze "$constraints_sql"
    apply_tiflash_replica "$TPCH_SOURCE_SCHEMA"

    if [[ "$CLEAN_SOURCE_FILES_AFTER_LOAD" == "1" ]]; then
        log "Cleaning TPC-H raw .tbl files after load"
        rm -f "$tpch_dir"/*.tbl
    fi
}

load_tpcds_source() {
    local tools_dir="$BENCH_DIR/tidb-bench/tpcds/tools"
    cd "$tools_dir"

    if [[ "$REUSE_LOADED_SOURCE_SCHEMA" == "1" ]] && schema_has_base_tables "$TPCDS_SOURCE_SCHEMA" 24; then
        log "Reusing existing loaded TPC-DS source schema: $TPCDS_SOURCE_SCHEMA"
        run_sql "ANALYZE TABLE \`$TPCDS_SOURCE_SCHEMA\`.\`call_center\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_page\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer_address\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer_demographics\`, \`$TPCDS_SOURCE_SCHEMA\`.\`date_dim\`, \`$TPCDS_SOURCE_SCHEMA\`.\`household_demographics\`, \`$TPCDS_SOURCE_SCHEMA\`.\`income_band\`, \`$TPCDS_SOURCE_SCHEMA\`.\`inventory\`, \`$TPCDS_SOURCE_SCHEMA\`.\`item\`, \`$TPCDS_SOURCE_SCHEMA\`.\`promotion\`, \`$TPCDS_SOURCE_SCHEMA\`.\`reason\`, \`$TPCDS_SOURCE_SCHEMA\`.\`ship_mode\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`time_dim\`, \`$TPCDS_SOURCE_SCHEMA\`.\`warehouse\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_page\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_site\` ALL COLUMNS"
        apply_tiflash_replica "$TPCDS_SOURCE_SCHEMA"
        if [[ "$CLEAN_SOURCE_FILES_AFTER_LOAD" == "1" ]]; then
            log "Cleaning TPC-DS raw .dat files after source schema reuse"
            rm -f "$tools_dir"/*.dat
        fi
        return
    fi

    if [[ "$REUSE_SOURCE_FILES" == "1" ]] && tpcds_source_files_ready "$tools_dir"; then
        log "Reusing existing TPC-DS SF=$TPCDS_SCALE_FACTOR .dat files"
    else
        log "Generating TPC-DS SF=$TPCDS_SCALE_FACTOR source data"
        # TPC-DS tools have legacy tentative global definitions. Newer GCC
        # defaults to -fno-common, so dsdgen needs -fcommon to link cleanly.
        make OS=LINUX LINUX_CFLAGS="-g -Wall -fcommon" dsdgen
        rm -f ./*.dat
        ./dsdgen -sc "$TPCDS_SCALE_FACTOR" -f
    fi

    log "Loading TPC-DS source schema: $TPCDS_SOURCE_SCHEMA"
    run_sql "DROP DATABASE IF EXISTS \`$TPCDS_SOURCE_SCHEMA\`"
    run_sql "CREATE DATABASE \`$TPCDS_SOURCE_SCHEMA\` DEFAULT COLLATE=utf8mb4_general_ci"
    "${MYSQL[@]}" "$TPCDS_SOURCE_SCHEMA" < "$tools_dir/tpcds.sql"

    local file table
    for file in "$tools_dir"/*.dat; do
        table="$(basename "$file" .dat)"
        log "Loading TPC-DS table: $table"
        load_data_file_with_chunks "$TPCDS_SOURCE_SCHEMA" "$table" "$file"
    done

    run_sql_best_effort "DROP TABLE IF EXISTS \`$TPCDS_SOURCE_SCHEMA\`.\`dbgen_version\`"
    run_sql "ANALYZE TABLE \`$TPCDS_SOURCE_SCHEMA\`.\`call_center\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_page\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`catalog_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer_address\`, \`$TPCDS_SOURCE_SCHEMA\`.\`customer_demographics\`, \`$TPCDS_SOURCE_SCHEMA\`.\`date_dim\`, \`$TPCDS_SOURCE_SCHEMA\`.\`household_demographics\`, \`$TPCDS_SOURCE_SCHEMA\`.\`income_band\`, \`$TPCDS_SOURCE_SCHEMA\`.\`inventory\`, \`$TPCDS_SOURCE_SCHEMA\`.\`item\`, \`$TPCDS_SOURCE_SCHEMA\`.\`promotion\`, \`$TPCDS_SOURCE_SCHEMA\`.\`reason\`, \`$TPCDS_SOURCE_SCHEMA\`.\`ship_mode\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`store_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`time_dim\`, \`$TPCDS_SOURCE_SCHEMA\`.\`warehouse\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_page\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_returns\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_sales\`, \`$TPCDS_SOURCE_SCHEMA\`.\`web_site\` ALL COLUMNS"
    apply_tiflash_replica "$TPCDS_SOURCE_SCHEMA"

    if [[ "$CLEAN_SOURCE_FILES_AFTER_LOAD" == "1" ]]; then
        log "Cleaning TPC-DS raw .dat files after load"
        rm -f "$tools_dir"/*.dat
    fi
}

append_size_report() {
    local label="$1"
    shift
    local report="$RESULTS_DIR/sizes_tidb_full_${TPCH_SCALE_LABEL}.txt"

    {
        echo "============================================================"
        echo "$label"
        echo "timestamp=$(timestamp)"
        echo "============================================================"
        echo ""
        echo "Disk usage:"
        df -h "$BASE_DIR" || true
        echo ""
        echo "TiDB data directory:"
        if [[ -d "$BASE_DIR/tidb-data" ]]; then
            du -sh "$BASE_DIR/tidb-data" || true
        else
            echo "not applicable (external TiDB/TiDB Cloud)"
        fi
        echo ""
        echo "Generated file directories:"
        du -sh "$BASE_DIR/generated"/* 2>/dev/null || true
        echo ""
        echo "Information schema table sizes:"
    } >> "$report"

    local schemas=("$@")
    local in_list=""
    local schema
    for schema in "${schemas[@]}"; do
        if [[ -n "$in_list" ]]; then
            in_list+=","
        fi
        in_list+="'$schema'"
    done

    "${MYSQL[@]}" -e "
SELECT
  TABLE_SCHEMA,
  COUNT(*) AS tables,
  SUM(TABLE_ROWS) AS approx_rows,
  ROUND(SUM(DATA_LENGTH) / 1024 / 1024, 2) AS data_mb,
  ROUND(SUM(INDEX_LENGTH) / 1024 / 1024, 2) AS index_mb
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA IN ($in_list)
GROUP BY TABLE_SCHEMA
ORDER BY TABLE_SCHEMA;
" >> "$report" || true

    for schema in "${schemas[@]}"; do
        {
            echo ""
            echo "Per-table sizes for $schema:"
        } >> "$report"
        "${MYSQL[@]}" -e "
SELECT
  TABLE_NAME,
  TABLE_ROWS AS approx_rows,
  ROUND(DATA_LENGTH / 1024 / 1024, 2) AS data_mb,
  ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = '$schema'
ORDER BY TABLE_NAME;
" >> "$report" || true
    done

    echo "" >> "$report"
}

run_datagenx_tpch() {
    log "Running full-size DataGenX target generation for TPC-H SF=$TPCH_SCALE_FACTOR"
    cd "$REPO_DIR"
    local status=0
    local result_file="$RESULTS_DIR/results_tpch_${TPCH_SCALE_LABEL}_tidb_full.txt"
    local password_arg=()
    if [[ -z "$TIDB_PASSWORD" ]]; then
        password_arg=("--password=")
    else
        password_arg=("--password" "$TIDB_PASSWORD")
    fi
    set +e
    (
        echo "DataGenX TiDB E2E verification"
        echo "profile=tpch-${TPCH_SCALE_LABEL}"
        echo "label=TPC-H SF=$TPCH_SCALE_FACTOR"
        echo "host=$TIDB_HOST"
        echo "port=$TIDB_PORT"
        echo "source=$TPCH_SOURCE_SCHEMA"
        echo "target=$TPCH_TARGET_SCHEMA"
        echo "rows=match-source"
        echo ""

        PYTHONUNBUFFERED=1 \
        DBGEN_BINARY="$DATAGENX_DBGEN_BINARY" \
        DBGEN_FILES_DIR="$GENERATED_DIR/tpch/dbgen_files" \
        DBGEN_TMP_OUT_DIR="$GENERATED_DIR/tpch/dbgen_tmp_out" \
        python3 MasterRun.py \
            --db-type tidb \
            --host "$TIDB_HOST" \
            --port "$TIDB_PORT" \
            --user "$TIDB_USER" \
            "${password_arg[@]}" \
            --source-schema "$TPCH_SOURCE_SCHEMA" \
            --target-schema "$TPCH_TARGET_SCHEMA" \
            --run-validation \
            --verbose \
            --compare-histograms
    ) 2>&1 | tee "$result_file"
    status=$?
    set -e

    {
        echo "TPC-H SF=$TPCH_SCALE_FACTOR DataGenX exit_status=$status timestamp=$(timestamp)"
    } >> "$RESULTS_DIR/run_status.txt"
    if [[ "$status" -ne 0 ]]; then
        log "WARN: TPC-H DataGenX validation exited with status $status; continuing to collect TiFlash and size reports"
    fi

    apply_tiflash_replica "$TPCH_TARGET_SCHEMA"
    append_size_report "TPC-H SF=$TPCH_SCALE_FACTOR after full DataGenX target generation" \
        "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA"

    if [[ "$CLEAN_GENERATED_FILES_AFTER_LOAD" == "1" ]]; then
        log "Cleaning TPC-H generated CSV files after load"
        rm -rf "$GENERATED_DIR/tpch/dbgen_tmp_out"
    fi
}

generate_validation_report() {
    local benchmark="$1"
    local scale_label="$2"
    local source_schema="$3"
    local target_schema="$4"
    local output="$RESULTS_DIR/${benchmark^^}_TIDB_${scale_label^^}.html"
    local password_arg=()
    if [[ -z "$TIDB_PASSWORD" ]]; then
        password_arg=("--password=")
    else
        password_arg=("--password" "$TIDB_PASSWORD")
    fi

    log "Generating validation HTML report: $output"
    cd "$REPO_DIR"
    python3 validation_report.py \
        --db-type tidb \
        --host "$TIDB_HOST" \
        --port "$TIDB_PORT" \
        --user "$TIDB_USER" \
        "${password_arg[@]}" \
        --source-schema "$source_schema" \
        --target-schema "$target_schema" \
        --output "$output" \
        --tidb-overlap-strategy mpp \
        --overlap-chunk-rows 500000
}

collect_topn_summary() {
    local benchmark="$1"
    local scale_label="$2"
    local source_schema="$3"
    local target_schema="$4"
    local output="$RESULTS_DIR/topn_${benchmark}_${scale_label}_tidb_summary.tsv"
    local password_arg=()
    if [[ -z "$TIDB_PASSWORD" ]]; then
        password_arg=("--password=")
    else
        password_arg=("--password" "$TIDB_PASSWORD")
    fi

    log "Collecting TiDB TopN summary without literal values: $output"
    cd "$REPO_DIR"
    python3 - "$output" "$TIDB_HOST" "$TIDB_PORT" "$TIDB_USER" "$TIDB_PASSWORD" "$source_schema" "$target_schema" <<'PY'
import csv
import sys
from collections import defaultdict
from pathlib import Path

import mysql.connector

from lib.schema_extractor import connection_kwargs_for

output, host, port, user, password, source_schema, target_schema = sys.argv[1:]
kwargs = connection_kwargs_for("tidb", host, user, password, "INFORMATION_SCHEMA", int(port), autocommit=True)

def pct_diff(source, target):
    if source == 0 and target == 0:
        return 0.0
    if source is None or target is None:
        return 1.0
    return abs(source - target) / max(source, target)

def frequency_shape_diff(source_counts, target_counts):
    source_total = sum(source_counts)
    target_total = sum(target_counts)
    if source_total <= 0 or target_total <= 0:
        return 1.0
    source_probs = sorted((count / source_total for count in source_counts), reverse=True)
    target_probs = sorted((count / target_total for count in target_counts), reverse=True)
    n = max(len(source_probs), len(target_probs))
    source_probs += [0.0] * (n - len(source_probs))
    target_probs += [0.0] * (n - len(target_probs))
    return 0.5 * sum(abs(source_probs[i] - target_probs[i]) for i in range(n))

def fetch_topn(cursor, schema):
    cursor.execute(
        "SHOW STATS_TOPN WHERE Db_name = %s AND Is_index = 0",
        (schema,),
    )
    names = [name.lower() for name in cursor.column_names]
    rows = [dict(zip(names, row)) for row in cursor.fetchall()]
    grouped = defaultdict(list)
    for row in rows:
        table = row.get("table_name")
        column = row.get("column_name")
        count = int(row.get("count") or 0)
        if table and column and count > 0:
            grouped[(table, column)].append(count)
    return grouped

conn = mysql.connector.connect(**kwargs)
cursor = conn.cursor()
try:
    source = fetch_topn(cursor, source_schema)
    target = fetch_topn(cursor, target_schema)
finally:
    cursor.close()
    conn.close()

keys = sorted(set(source) | set(target))
Path(output).parent.mkdir(parents=True, exist_ok=True)
with open(output, "w", newline="") as fp:
    writer = csv.writer(fp, delimiter="\t")
    writer.writerow([
        "table",
        "column",
        "source_topn_entries",
        "target_topn_entries",
        "source_topn_total_count",
        "target_topn_total_count",
        "topn_total_diff_pct",
        "frequency_shape_diff_pct",
        "status",
    ])
    for table, column in keys:
        source_counts = source.get((table, column), [])
        target_counts = target.get((table, column), [])
        total_diff = pct_diff(sum(source_counts), sum(target_counts)) * 100
        shape_diff = frequency_shape_diff(source_counts, target_counts) * 100
        status = "PASS" if total_diff < 5.0 and shape_diff < 5.0 else "NOTE"
        writer.writerow([
            table,
            column,
            len(source_counts),
            len(target_counts),
            sum(source_counts),
            sum(target_counts),
            f"{total_diff:.4f}",
            f"{shape_diff:.4f}",
            status,
        ])
PY
}

run_datagenx_tpcds() {
    log "Running full-size DataGenX target generation for TPC-DS SF=$TPCDS_SCALE_FACTOR"
    cd "$REPO_DIR"
    local status=0
    local result_file="$RESULTS_DIR/results_tpcds_${TPCDS_SCALE_LABEL}_tidb_full.txt"
    local password_arg=()
    if [[ -z "$TIDB_PASSWORD" ]]; then
        password_arg=("--password=")
    else
        password_arg=("--password" "$TIDB_PASSWORD")
    fi
    set +e
    (
        echo "DataGenX TiDB E2E verification"
        echo "profile=tpcds-${TPCDS_SCALE_LABEL}"
        echo "label=TPC-DS SF=$TPCDS_SCALE_FACTOR"
        echo "host=$TIDB_HOST"
        echo "port=$TIDB_PORT"
        echo "source=$TPCDS_SOURCE_SCHEMA"
        echo "target=$TPCDS_TARGET_SCHEMA"
        echo "rows=match-source"
        echo ""

        PYTHONUNBUFFERED=1 \
        DBGEN_BINARY="$DATAGENX_DBGEN_BINARY" \
        DBGEN_FILES_DIR="$GENERATED_DIR/tpcds/dbgen_files" \
        DBGEN_TMP_OUT_DIR="$GENERATED_DIR/tpcds/dbgen_tmp_out" \
        python3 MasterRun.py \
            --db-type tidb \
            --host "$TIDB_HOST" \
            --port "$TIDB_PORT" \
            --user "$TIDB_USER" \
            "${password_arg[@]}" \
            --source-schema "$TPCDS_SOURCE_SCHEMA" \
            --target-schema "$TPCDS_TARGET_SCHEMA" \
            --run-validation \
            --verbose \
            --compare-histograms
    ) 2>&1 | tee "$result_file"
    status=$?
    set -e

    {
        echo "TPC-DS SF=$TPCDS_SCALE_FACTOR DataGenX exit_status=$status timestamp=$(timestamp)"
    } >> "$RESULTS_DIR/run_status.txt"
    if [[ "$status" -ne 0 ]]; then
        log "WARN: TPC-DS DataGenX validation exited with status $status; continuing to collect TiFlash and size reports"
    fi

    apply_tiflash_replica "$TPCDS_TARGET_SCHEMA"
    append_size_report "TPC-DS SF=$TPCDS_SCALE_FACTOR after full DataGenX target generation" \
        "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"

    if [[ "$CLEAN_GENERATED_FILES_AFTER_LOAD" == "1" ]]; then
        log "Cleaning TPC-DS generated CSV files after load"
        rm -rf "$GENERATED_DIR/tpcds/dbgen_tmp_out"
    fi
}

main() {
    log "Starting full-size TiDB verification profile=$PROFILE"
    setup_python
    load_tidb_env
    setup_datagenx_dbgen
    setup_tidb_connection
    prepare_tidb_bench

    case "$PROFILE" in
        tpch|tpch-sf*|tpch_sf*)
            load_tpch_source
            append_size_report "TPC-H SF=$TPCH_SCALE_FACTOR source loaded" "$TPCH_SOURCE_SCHEMA"
            run_datagenx_tpch
            generate_validation_report "tpch" "$TPCH_SCALE_LABEL" "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA"
            collect_topn_summary "tpch" "$TPCH_SCALE_LABEL" "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA"
            ;;
        tpcds|tpcds-sf*|tpcds_sf*)
            load_tpcds_source
            append_size_report "TPC-DS SF=$TPCDS_SCALE_FACTOR source loaded" "$TPCDS_SOURCE_SCHEMA"
            run_datagenx_tpcds
            generate_validation_report "tpcds" "$TPCDS_SCALE_LABEL" "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"
            collect_topn_summary "tpcds" "$TPCDS_SCALE_LABEL" "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"
            ;;
        all)
            load_tpch_source
            append_size_report "TPC-H SF=$TPCH_SCALE_FACTOR source loaded" "$TPCH_SOURCE_SCHEMA"
            run_datagenx_tpch
            generate_validation_report "tpch" "$TPCH_SCALE_LABEL" "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA"
            collect_topn_summary "tpch" "$TPCH_SCALE_LABEL" "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA"
            load_tpcds_source
            append_size_report "TPC-DS SF=$TPCDS_SCALE_FACTOR source loaded" "$TPCDS_SOURCE_SCHEMA"
            run_datagenx_tpcds
            generate_validation_report "tpcds" "$TPCDS_SCALE_LABEL" "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"
            collect_topn_summary "tpcds" "$TPCDS_SCALE_LABEL" "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"
            ;;
        *)
            echo "Usage: $0 [tpch|tpcds|all]" >&2
            exit 1
            ;;
    esac

    append_size_report "Final TiDB size summary" \
        "$TPCH_SOURCE_SCHEMA" "$TPCH_TARGET_SCHEMA" "$TPCDS_SOURCE_SCHEMA" "$TPCDS_TARGET_SCHEMA"
    log "Done. Results are in $RESULTS_DIR"
}

main "$@" 2>&1 | tee "$LOG_DIR/run_tidb_full_sf10_$(date -u +%Y%m%dT%H%M%SZ).log"
