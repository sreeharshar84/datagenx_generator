# DataGenX: Design

This document describes the high-level design of how DataGenX transforms database metadata into annotated DDL files (.dbgen) for synthetic data generation.

For implementation details, see [DATAGENX_IMPLEMENTATION.md](DATAGENX_IMPLEMENTATION.md).

---

## 1. Overview

```
┌─────────────────────┐      ┌─────────────────────┐      ┌─────────────────────┐
│   INPUT: Metadata   │ ──▶  │  PROCESS: Annotate  │ ──▶  │  OUTPUT: .dbgen     │
│                     │      │                     │      │                     │
│ • Schema DDL        │      │ • Column classify   │      │ • Valid SQL DDL     │
│ • Optimizer stats   │      │ • Expression gen    │      │ • Inline expressions│
│ • Row counts        │      │ • Privacy filter    │      │ • Ready for dbgen   │
│ • Distinct counts   │      │                     │      │                     │
└─────────────────────┘      └─────────────────────┘      └─────────────────────┘
```

## 2. Input: Metadata

| Metadata Type | Source | Purpose |
|---------------|--------|---------|
| Schema DDL | `SHOW CREATE TABLE` | Column types, constraints, PKs, FKs |
| Histograms | Optimizer statistics adapter | Distribution shapes, bucket weights, bucket NDV |
| TopN / MCV | Optimizer statistics adapter | Frequent-value probability mass without exposing source literals |
| Row counts | Optimizer statistics or `SELECT COUNT(*)` | Scaling, small-table detection |
| Distinct counts | Optimizer statistics or `SELECT COUNT(DISTINCT col)` | FK coverage, cardinality matching |
| FK relationships | `information_schema.KEY_COLUMN_USAGE` | Reference graph, topological sort |

DataGenX does not treat TopN/MCV as a MySQL-specific feature. It normalizes
engine-specific statistics into a common optimizer-statistics model:

| Model Object | Meaning |
|--------------|---------|
| `ColumnOptimizerStats` | All optimizer-visible stats for one column |
| `HistogramBucket` | Bucket ordinal, probability mass, cumulative probability, and optional NDV |
| `TopNEntry` | Frequent-value ordinal, probability mass, and optional count |

The key rule is that the neutral model uses ordinals and probabilities for
generation. Source literal values are not exposed through `TopNEntry`; a backend
adapter may read native values internally, but DataGenX should emit synthetic
domain values.

**Native MySQL histogram shape before normalization:**
```json
{
  "histogram-type": "singleton" | "equi-height",
  "buckets": [
    [value, cumulative_freq],                    // singleton
    [min, max, cumulative_freq, num_distinct]    // equi-height
  ]
}
```

For MySQL, singleton histograms are treated as TopN-like/MCV-like statistics:
one bucket corresponds to one distinct/frequent value and cumulative
probabilities are converted to per-value probability masses. TiDB can expose
native TopN through its statistics tables, and the TiDB adapter maps those rows
to `TopNEntry`. SingleStore can use its histogram metadata and can add a native
TopN adapter when that metadata is available.

## 3. Output: Annotated DDL (.dbgen)

The output is valid SQL DDL with embedded generation expressions:

```sql
CREATE TABLE `table_name` (
  `pk_col` int NOT NULL /*{{ @pk_col := rownum }}*/,
  `fk_col` int /*{{ @fk_col := mod(rownum-1, 1000) + 1 }}*/,
  `str_col` varchar(25) /*{{ @str_col := case rand.weighted(array[0.3, 0.7])
    when 1 then 'str_col_1________________'
    when 2 then 'str_col_2________________'
  end }}*/,
  `date_col` date /*{{ @date_col := TIMESTAMP '2000-01-01' + INTERVAL rand.range(0, 365) DAY }}*/,
  `num_col` decimal(10,2) /*{{ @num_col := rand.range(100, 10000) / 100 }}*/,
  PRIMARY KEY (`pk_col`),
  FOREIGN KEY (`fk_col`) REFERENCES `ref_table` (`ref_pk`)
)
```

**Key Properties:**
- Valid SQL syntax (annotations are comments)
- Expressions use `@column := value` syntax
- `rownum` is the row counter (1, 2, 3, ...)
- `rand.range()`, `rand.weighted()` for randomness
- `mod()`, `div()` for deterministic cycling

## 4. Expression Strategy by Column Type

```
┌─────────────────────────────────────────────────────────────────┐
│                    Column Classification                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Column                                                         │
│    │                                                            │
│    ├── PRIMARY KEY?                                             │
│    │     ├── Single PK ──────────▶ rownum                       │
│    │     ├── Parent+sequence PK ─▶ grouped child generation     │
│    │     └── Composite PK ───────▶ div/mod cycling              │
│    │                                                            │
│    ├── FOREIGN KEY?                                             │
│    │     ├── High coverage (>80%) ▶ rand.range(min, max)        │
│    │     ├── Singleton histogram ─▶ weighted CASE               │
│    │     └── Composite FK ────────▶ N-cycling (div + mod)       │
│    │                                                            │
│    ├── DATE/DATETIME?                                           │
│    │     ├── Low cardinality ─────▶ weighted CASE (synthetic)   │
│    │     └── High cardinality ────▶ base + INTERVAL rand.range  │
│    │                                                            │
│    ├── NUMERIC?                                                 │
│    │     ├── Singleton histogram ─▶ weighted CASE               │
│    │     └── Equi-height histogram▶ bucket cycling              │
│    │                                                            │
│    └── STRING?                                                  │
│          ├── Low cardinality ─────▶ weighted CASE (synthetic)   │
│          └── High cardinality ────▶ bucket NDV synthetic cycling│
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

Parent-plus-sequence composite keys need special handling. In schemas such as
TPC-H, `lineitem` has `PRIMARY KEY (l_orderkey, l_linenumber)`, where
`l_orderkey` is a parent FK and `l_linenumber` is the child position within that
order. Generating both columns independently keeps the key unique, but it makes
`l_linenumber` uniform. DataGenX therefore learns the source child-counts per
parent, for example how many orders have 1, 2, ..., 7 lineitems, and emits
synthetic parent groups with sequence values `1..k` inside each group. This
preserves the sequence-column histogram, the parent-child fanout distribution,
referential integrity, and primary-key uniqueness.

## 5. Privacy Guarantees

**Used (Statistical Patterns):**
- Distribution shapes (histogram bucket weights)
- TopN/MCV probability masses
- Bucket distinct counts
- Cardinality counts
- Row counts
- Column metadata (types, lengths)

**Never Emitted Into Synthetic Data:**
- Actual values from histograms or TopN/MCV catalogs
- MIN/MAX from source data
- Row samples from source tables

Some database adapters may need to read native optimizer statistic values to
decode histograms or estimate string lengths. Those values are treated as
metadata inputs only. Generated values are mapped to synthetic ordinals such as
`column_1`, `column_2`, or synthetic numeric/date ranges.

**Example - Date Column:**
```
Source data:     1995-01-01 to 1998-12-31 (TPC-H dates)
Histogram:       span = 1461 days
Generated expr:  TIMESTAMP '2000-01-01' + INTERVAL rand.range(0, 1461) DAY
Output:          2000-01-01 to 2004-01-01 (synthetic range, same span)
```

## 6. Validation Dimensions

After generation, DataGenX validates synthetic data across data-level and
optimizer-level dimensions:

| Check | What It Measures | Uses Actual Table Data? | Uses Optimizer Metadata? | Pass Criteria |
|-------|------------------|--------------------------|---------------------------|---------------|
| **Row Counts** | Total rows per table | Yes | No | Exact match |
| **Distinct Counts** | Cardinality per column | Yes | No | <5% difference |
| **Frequency Distributions** | Per-value counts for selected columns | Yes | No | Distribution-specific threshold |
| **FK Integrity** | Orphan rows in child tables | Yes | Physical FK metadata, or built-in TPC-H/TPC-DS fallback relationships when constraints are absent | 0 orphans |
| **Privacy** | Exact row overlap (MD5 hash) | Yes | Schema metadata identifies columns | <1% overlap |
| **Histograms** | Optimizer-visible distribution shape | No | Yes | <5% total variation distance |
| **Query Plans** | Optimizer execution strategy | No | Yes | Same plan shape or acceptable drift |

Index cardinality is optimizer metadata, not an actual data count. For an index
such as `orders(o_custkey)`, the database may store an estimated cardinality
that says how many distinct indexed values the optimizer believes exist. This is
different from `COUNT(DISTINCT o_custkey)`, which scans the table data and
returns the actual number of distinct values. DataGenX should use actual
`COUNT(DISTINCT ...)` for data validation, and compare index cardinality only in
explicit optimizer-statistics analysis.

## 7. Supported Databases

| Database | Extractor | Histogram Source |
|----------|-----------|------------------|
| MySQL | `MySQLExtractor` | `information_schema.column_statistics` |
| SingleStore | `SingleStoreExtractor` | `information_schema.ADVANCED_HISTOGRAMS` or compatible column statistics |
| TiDB | `TiDBExtractor` | `SHOW STATS_HISTOGRAMS` / `SHOW STATS_BUCKETS` / `SHOW STATS_TOPN` |
