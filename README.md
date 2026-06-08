# DataGenX Generator

Generate synthetic database data that preserves optimizer-relevant statistics
from a source schema while avoiding direct use of source data values.

## Current TPC-H Setup

The current local setup uses:

```text
source schema: tpch_vanilla
target schema: tpch_dbgenx
```

`tpch_vanilla` is loaded from TPC-H `dbgen` `.tbl` files. `tpch_dbgenx` is
created by this project.

Expected small TPC-H source data is scale factor `0.01`, with row counts around:

```text
region       5
nation       25
supplier     100
customer     1500
part         2000
partsupp     8000
orders       15000
lineitem     about 60000
```

## Prerequisites

Install MySQL and make password login work for the configured user:

```bash
sudo apt update
sudo apt install mysql-server
sudo mysql -u root
```

```sql
ALTER USER 'root'@'localhost'
IDENTIFIED WITH caching_sha2_password BY 'newpassword';
FLUSH PRIVILEGES;
EXIT;
```

Verify:

```bash
mysql -u root -pnewpassword -e "SELECT VERSION();"
```

Install the Python MySQL connector inside the active virtualenv:

```bash
python3 -m pip install mysql-connector-python
```

Build the DataGenX Rust `dbgen` binary:

```bash
cd /home/hmaduri/contribs/dbgen
cargo build --release --bin dbgen
```

Expected binary:

```text
/home/hmaduri/contribs/dbgen/target/release/dbgen
```

## Required config.py Values

For the current TPC-H workflow, `config.py` should contain:

```python
HOST = "localhost"
USER = "root"
PASSWORD = "newpassword"

SOURCE_SCHEMA = "tpch_vanilla"
TARGET_SCHEMA = "tpch_dbgenx"

DBGEN_BINARY = "/home/hmaduri/contribs/dbgen/target/release/dbgen"
DBGEN_FILES_DIR = "generated/dbgen_files"
DBGEN_TMP_OUT_DIR = "generated/dbgen_tmp_out"
```

Change only `SOURCE_SCHEMA`, `TARGET_SCHEMA`, and `DBGEN_BINARY` when switching
environments.

## Load TPC-H Source Data

Build the official TPC-H data generator:

```bash
cd /home/hmaduri/contribs/tpch-dbgen
make MACHINE=LINUX DATABASE=MYSQL WORKLOAD=TPCH
```

Generate small TPC-H source data:

```bash
./dbgen -vf -s 0.01
```

Load it into MySQL as `tpch_vanilla`:

```bash
cd /home/hmaduri/contribs/datagenx_generator
mysql --local-infile=1 -u root -pnewpassword < sql/load_tpch_vanilla.sql
```

The load script creates tables, loads `.tbl` files, adds primary/foreign keys,
and runs `ANALYZE TABLE`.

## Generate Synthetic Data

The main entry point is `MasterRun.py` in the repository root. It orchestrates
the full pipeline:

1. **Step A**: Generate `.dbgen` templates from schema + histograms
2. **Step B**: Run the DataGenX `dbgen` binary to produce CSV data
3. **Step C**: Create tables in target schema, load data, optionally validate

### Basic Usage

```bash
python3 MasterRun.py
```

By default, `MasterRun.py`:

- Generates `.dbgen` templates
- Runs the DataGenX dbgen binary
- Creates and loads target tables
- Clones MySQL histograms from source to target
- Skips built-in validation

### CLI Options

```bash
# Show all available options
python3 MasterRun.py --help

# Enable verbose output
python3 MasterRun.py -v

# Run with built-in validation after loading
python3 MasterRun.py --run-validation

# Enable histogram comparison (disabled by default - can be unreliable)
python3 MasterRun.py --compare-histograms

# Override row count (generate different number of rows than source)
python3 MasterRun.py --rows 100000
```

### SingleStore Support

```bash
# Run against SingleStore database
python3 MasterRun.py --db-type singlestore \
  --host <host> \
  --user <user> \
  --password <password> \
  --source-schema <source_db> \
  --target-schema <target_db>
```

### Override config.py Settings

All connection parameters can be overridden via CLI:

```bash
python3 MasterRun.py \
  --host localhost \
  --user root \
  --password mypassword \
  --source-schema tpch_vanilla \
  --target-schema tpch_synthetic \
  --rows 50000
```

### Internal Architecture

`MasterRun.py` orchestrates these modules:

```text
MasterRun.py
│
├── config.py
│   └── HOST, USER, PASSWORD, SOURCE_SCHEMA, TARGET_SCHEMA, DBGEN_BINARY, etc.
│
├── lib/schema_extractor.py
│   └── MySQLExtractor, SingleStoreExtractor, TiDBExtractor
│       plus normalized optimizer-statistics model
│
├── datagenx/generation/GenerateDbgen.py
│   ├── annotate_table_with_histogram()  ← builds .dbgen templates (MySQL)
│   ├── build_single_fk_expression()     ← FK expression logic
│   └── topological_sort()               ← dependency ordering
│
├── extract_schema.py
│   └── annotate_table_with_statistics() ← builds .dbgen templates (SingleStore)
│
├── datagenx/validation/PopulateNewTableAndValidate.py
│   ├── clone_histograms()
│   ├── compare_histograms()
│   ├── load_histograms(), load_distinct_counts(), load_index_stats()
│   └── report_*() functions
│
└── [External] dbgen binary
    └── Rust binary at DBGEN_BINARY path, invoked via subprocess
```

| File | Purpose |
|------|---------|
| `config.py` | Connection settings and paths |
| `lib/schema_extractor.py` | Database abstraction layer (MySQL/SingleStore/TiDB) and optimizer stats model |
| `GenerateDbgen.py` | Core template generation logic |
| `extract_schema.py` | SingleStore-specific extraction |
| `PopulateNewTableAndValidate.py` | Validation helpers |

### Optimizer Statistics Abstraction

DataGenX normalizes backend-specific optimizer metadata before generation and
validation. The shared objects are:

```text
ColumnOptimizerStats
HistogramBucket
TopNEntry
```

`TopNEntry` represents TopN/MCV-style frequent-value statistics as synthetic
ordinals plus probability mass/count. It does not carry source literal values.
MySQL singleton histograms are exposed as TopN-like entries, TiDB native
`SHOW STATS_TOPN` rows map directly to `TopNEntry`, and SingleStore currently
uses histogram metadata with room for a native MCV adapter when available.

See [DataGenX Design](docs/DATAGENX_DESIGN.md) and
[Implementation Guide](docs/DATAGENX_IMPLEMENTATION.md) for the detailed model.

## Testing

Run the test suite after any code change to verify the pipeline works:

```bash
python3 tests/test_agent.py
```

The test agent uses MySQL's **Sakila** sample database and:
1. Downloads and installs Sakila if not present
2. Runs the full generation pipeline
3. Validates data quality (FK integrity, cardinality, distributions)
4. Cleans up the test schema

### Test Options

```bash
# Quick test (default)
python3 tests/test_agent.py

# Full test with validation
python3 tests/test_agent.py --full

# Custom row count
python3 tests/test_agent.py --rows 500

# Just setup Sakila (no tests)
python3 tests/test_agent.py --setup-only

# Keep target schema after test (for debugging)
python3 tests/test_agent.py --keep

# Verbose output
python3 tests/test_agent.py -v

# Force reinstall Sakila
python3 tests/test_agent.py --force-setup
```

### Test Configuration

Tests use connection settings from `config.py`. Override via CLI:

```bash
python3 tests/test_agent.py --host localhost --user root --password mypass
```

## Validate Separately

Use the unified validation entry point:

```bash
python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

By default, `validate.py stats` is intended to compare actual source and target
table data, for example `COUNT(*)` and `COUNT(DISTINCT column)`. Optimizer
metadata checks, such as histogram comparison, should be explicit options rather
than hidden defaults.

Faster stats validation:

```bash
python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --skip-distinct
```

Run benchmark-specific SQL validation:

```bash
python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Render SQL without executing:

```bash
python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --render-only
```

Other validation helpers:

```bash
python3 validate.py replay \
  --ddl-file generated/dbgen_tmp_out/orders-schema.sql \
  --insert-file generated/dbgen_tmp_out/orders.1.csv

python3 validate.py plans
python3 validate.py query q3
python3 validate.py all --skip-distinct
```

## Literal Mapping for Query Rewrites

Render the original TPC-H query templates into runnable MySQL SQL files. This
is optional and never runs unless explicitly requested:

```bash
python3 validate.py tpch-queries \
  --template-dir /home/hmaduri/contribs/tpch-dbgen/queries \
  --output-dir generated/tpch_queries_mysql
```

Build a sensitive local source-literal to synthetic-literal mapping:

```bash
python3 validate.py literal-map \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Default output:

```text
generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Use it to rewrite target-side query literals:

```bash
python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --sql "select * from lineitem where l_returnflag = 'R';"
```

Use it during plan/query validation:

```bash
python3 validate.py query q12 \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Run all rendered TPC-H query plan comparisons:

```bash
python3 validate.py plans \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --output-file /tmp/tpch_plan_comparison.txt
```

The mapping file contains source literals and must stay local/private. More
detail: [Literal Mapping](docs/LITERAL_MAPPING.md).

## Smoke Tests

Run these from the repository root:

```bash
cd /home/hmaduri/contribs/datagenx_generator
```

Check that the CLI commands are wired:

```bash
/home/hmaduri/myenv/bin/python3 MasterRun.py --help
/home/hmaduri/myenv/bin/python3 validate.py --help
/home/hmaduri/myenv/bin/python3 validate.py tpch-queries --help
/home/hmaduri/myenv/bin/python3 validate.py literal-map --help
/home/hmaduri/myenv/bin/python3 validation_report.py --help
```

Check Python syntax/imports:

```bash
/home/hmaduri/myenv/bin/python3 -m py_compile \
  MasterRun.py \
  validate.py \
  validation_report.py \
  datagenx/generation/GenerateDbgen.py \
  datagenx/validation/ValidateTableStats.py \
  datagenx/validation/literal_mapping.py \
  datagenx/validation/tpch_queries.py \
  datagenx/validation/validation_report.py
```

Validate current source and synthetic schemas:

```bash
/home/hmaduri/myenv/bin/python3 validate.py stats \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --skip-distinct
```

Expected result:

```text
Overall: ✅ ALL PASSED
```

Render benchmark SQL without executing it:

```bash
/home/hmaduri/myenv/bin/python3 validate.py sql tpch \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --render-only \
  --output-sql /tmp/tpch_validation_rendered.sql
```

Generate the HTML validation report:

```bash
/home/hmaduri/myenv/bin/python3 validation_report.py \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --output /tmp/tpch_validation_report.html
```

Render TPC-H query templates and build the sensitive literal mapping:

```bash
/home/hmaduri/myenv/bin/python3 validate.py tpch-queries \
  --template-dir /home/hmaduri/contribs/tpch-dbgen/queries \
  --output-dir generated/tpch_queries_mysql

/home/hmaduri/myenv/bin/python3 validate.py literal-map \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx
```

Check that query literal rewriting works:

```bash
/home/hmaduri/myenv/bin/python3 validate.py rewrite-query \
  --mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --sql "select * from lineitem where l_returnflag = 'R' and l_shipmode in ('MAIL','SHIP');"
```

Expected rewrite includes:

```sql
l_returnflag = '2'
l_shipmode in ('l__2','l__7')
```

Run one TPC-H plan comparison with target-side literal/date rewriting:

```bash
/home/hmaduri/myenv/bin/python3 validate.py query q12 \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json
```

Expected result:

```text
Plan Shape:      IDENTICAL
```

Optional full plan smoke test:

```bash
/home/hmaduri/myenv/bin/python3 validate.py plans \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --queries-dir generated/tpch_queries_mysql \
  --literal-mapping-file generated/literal_mappings/tpch_vanilla_to_tpch_dbgenx.json \
  --output-file /tmp/tpch_plan_comparison.txt
```

Generated local artifacts from these tests are intentionally ignored by git:

```text
generated/literal_mappings/
generated/tpch_queries_mysql/
/tmp/tpch_validation_report.html
/tmp/tpch_plan_comparison.txt
```

## Visualization Report

Use the project venv when generating validation visuals:

```bash
/home/hmaduri/myenv/bin/python3 validation_report.py \
  --source-schema tpch_vanilla \
  --target-schema tpch_dbgenx \
  --output /tmp/tpch_validation_report.html
```

The report includes:

```text
dashboard health cards
table-level validation matrix
top drift columns
histogram-difference heatmap
referential-integrity graph and orphan checks
exact source-vs-synthetic row overlap checks
selected source-vs-target frequency distributions
distinct-count differences
```

Referential-integrity checks are schema driven. The report first discovers
physical foreign keys from `information_schema.KEY_COLUMN_USAGE`. If a benchmark
schema was loaded without physical FK constraints, it falls back to built-in
TPC-H/TPC-DS relationship definitions and runs orphan checks against both source
and target schemas.

`MasterRun.py` applies benchmark FK metadata to the generated target after
a full MySQL or TiDB benchmark load. For a full TPC-DS run it applies
`scripts/tpcds_fk.sql` to the target schema after all tables are created and
loaded. For a full TPC-H run it similarly applies `scripts/tpch_fk.sql` when the
target constraints are not already present. Partial `--tables` runs skip this
step because referenced tables may be absent.

To attach the same physical FK metadata to an already-loaded source schema, use
the common FK utility:

```bash
/home/hmaduri/myenv/bin/python3 scripts/apply_benchmark_fk.py \
  --schema tpch_sf001 \
  --benchmark tpch

/home/hmaduri/myenv/bin/python3 scripts/apply_benchmark_fk.py \
  --schema tpcds \
  --benchmark tpcds
```

Use `--benchmark auto` to infer TPC-H or TPC-DS from table names. The utility
skips schemas that already have physical FKs unless `--force` is provided. The
TPC-DS script disables `FOREIGN_KEY_CHECKS` while adding constraints because
loaded TPC-DS data may contain `0` sentinel values for unknown/not-applicable
dimension references.

## Important Behavior

### Validation Semantics

DataGenX separates actual data validation from optimizer-metadata validation:

| Validation | Uses Actual Table Data? | Uses Optimizer Metadata? | Notes |
|------------|--------------------------|---------------------------|-------|
| Row count | Yes | No | Uses `COUNT(*)` on source and target tables. |
| Distinct count / NDV | Yes | No | Uses `COUNT(DISTINCT column)` on source and target tables. |
| Frequency distribution | Yes | No | Uses `GROUP BY column, COUNT(*)` when generated by reports. |
| FK integrity | Yes | Uses schema/FK metadata to find relationships | Checks actual child rows against parent rows. |
| Exact row overlap / privacy | Yes | Uses schema metadata for columns | Hashes rows to detect copied rows. |
| Histogram shape | No | Yes | Uses database histogram metadata and compares bucket probability mass, not literal values. |
| Query plan comparison | No | Yes | Uses `EXPLAIN` output to compare optimizer choices. |
| Index cardinality | No | Yes | Optimizer estimate of indexed distinct values; not the same as actual `COUNT(DISTINCT)`. |

Index cardinality is a database statistic stored for indexes. For example, an
index on `orders(o_custkey)` may have a cardinality estimate of `1000`, meaning
the optimizer believes the indexed column has about 1000 distinct values. That
estimate can be approximate, sampled, stale, or affected by `ANALYZE TABLE`.
Use actual `COUNT(DISTINCT o_custkey)` when validating generated data; use index
cardinality only for explicit optimizer-statistics analysis.

Histogram cloning is part of target creation, not validation. `MasterRun.py`
clones histograms even when validation is skipped, because `validate.py stats`
expects target histograms to exist.

Composite keys of the form `PRIMARY KEY(parent_fk, sequence_col)` use grouped
child generation. For example, TPC-H `lineitem(l_orderkey, l_linenumber)` is
generated from the source line-count-per-order distribution so `l_linenumber`
matches the source histogram while `(l_orderkey, l_linenumber)` remains unique.

Histogram validation compares distribution shape, not literal bucket values.
For each source/target histogram pair, validation extracts per-bucket frequency
mass from MySQL's cumulative bucket probabilities, sorts those masses, pads
missing buckets with zero, and compares the resulting bucket-frequency shape.
This lets synthetic domains differ from source domains while still checking
bucket count and frequency drift.

TopN/MCV statistics follow the same privacy rule: compare or generate from
probability mass and rank/ordinal, not from original literal values.

More detail: [Histogram Comparison](docs/HISTOGRAM_COMPARISON.md).

For low-cardinality string histograms, generation uses deterministic bucket
assignment instead of random weighted selection. This avoids random collisions
that can collapse target bucket counts.

If validation reports `missing in target` for histograms, rerun:

```bash
python3 MasterRun.py
```

then validate again.
## Repository Layout

```text
MasterRun.py                 root wrapper for generation
validate.py                  root wrapper for validation
validation_report.py         root wrapper for HTML report generation
config.py                    local database and generation settings
datagenx/orchestration/      end-to-end generation workflow
datagenx/generation/         dbgen template and insert helpers
datagenx/validation/         stats, SQL, plan, query, and report validators
datagenx/legacy/             older Sakila helper scripts
sql/                         reusable SQL scripts
docs/                        design notes, reports, and fix writeups
generated/dbgen_files/       generated .dbgen templates
generated/dbgen_tmp_out/     generated CSV/schema outputs from dbgen
scripts/                     standalone shell helpers
```
