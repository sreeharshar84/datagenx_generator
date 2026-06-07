#!/usr/bin/env python3
"""Attach benchmark foreign-key metadata to an already-loaded MySQL schema."""

import argparse
import os
import re
import sys

import mysql.connector
from mysql.connector import Error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import HOST, USER, PASSWORD, DB_PORT


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCHMARK_SCRIPTS = {
    "tpch": os.path.join(PROJECT_ROOT, "scripts", "tpch_fk.sql"),
    "tpcds": os.path.join(PROJECT_ROOT, "scripts", "tpcds_fk.sql"),
}


def strip_sql_comments(statement):
    lines = []
    for line in statement.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def detect_benchmark(cursor, schema):
    cursor.execute("""
        SELECT TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %s
          AND TABLE_TYPE = 'BASE TABLE'
    """, (schema,))
    tables = {row[0].lower() for row in cursor.fetchall()}

    if {"lineitem", "orders", "partsupp", "customer", "supplier"}.issubset(tables):
        return "tpch"
    if {"date_dim", "item", "customer", "store_sales", "catalog_sales", "web_sales"}.issubset(tables):
        return "tpcds"
    raise RuntimeError(
        f"Could not auto-detect benchmark for schema `{schema}`. "
        "Use --benchmark tpch or --benchmark tpcds."
    )


def existing_fk_count(cursor, schema):
    cursor.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = %s
          AND CONSTRAINT_TYPE = 'FOREIGN KEY'
    """, (schema,))
    return int(cursor.fetchone()[0])


def schema_exists(cursor, schema):
    cursor.execute("""
        SELECT COUNT(*)
        FROM INFORMATION_SCHEMA.SCHEMATA
        WHERE SCHEMA_NAME = %s
    """, (schema,))
    return int(cursor.fetchone()[0]) > 0


def rewrite_for_schema(statement, schema):
    statement = re.sub(
        r"^\s*ALTER\s+TABLE\s+`?([A-Za-z0-9_]+)`?",
        rf"ALTER TABLE `{schema}`.`\1`",
        statement,
        count=1,
        flags=re.IGNORECASE,
    )
    statement = re.sub(
        r"\bREFERENCES\s+`?([A-Za-z0-9_]+)`?\s*\(",
        rf"REFERENCES `{schema}`.`\1` (",
        statement,
        flags=re.IGNORECASE,
    )
    return statement


def apply_fk_script(cursor, schema, benchmark, force=False):
    if benchmark == "auto":
        benchmark = detect_benchmark(cursor, schema)

    script_path = BENCHMARK_SCRIPTS[benchmark]
    with open(script_path) as f:
        sql = f.read()

    current_fk_count = existing_fk_count(cursor, schema)
    if current_fk_count and not force:
        print(
            f"Skipping {benchmark.upper()} FK script for `{schema}`; "
            f"{current_fk_count} physical FK constraint(s) already exist. "
            "Use --force to attempt applying anyway."
        )
        return 0, current_fk_count, benchmark, script_path

    statements = [strip_sql_comments(stmt) for stmt in sql.split(";")]
    statements = [stmt for stmt in statements if stmt]

    applied = 0
    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    for statement in statements:
        if statement.upper().startswith("SET "):
            cursor.execute(statement)
            continue
        cursor.execute(rewrite_for_schema(statement, schema))
        applied += 1
    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    return applied, existing_fk_count(cursor, schema), benchmark, script_path


def main():
    parser = argparse.ArgumentParser(
        description="Apply TPC-H/TPC-DS FK constraints to an already-loaded MySQL schema."
    )
    parser.add_argument("--schema", required=True, help="Schema to alter")
    parser.add_argument(
        "--benchmark",
        choices=["auto", "tpch", "tpcds"],
        default="auto",
        help="Benchmark FK script to apply (default: auto-detect from tables)",
    )
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=DB_PORT)
    parser.add_argument("--user", default=USER)
    parser.add_argument("--password", default=PASSWORD)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Attempt to apply even if the schema already has physical FK constraints.",
    )

    args = parser.parse_args()

    kwargs = {
        "host": args.host,
        "user": args.user,
        "password": args.password,
        "autocommit": True,
    }
    if args.port:
        kwargs["port"] = args.port

    conn = mysql.connector.connect(**kwargs)
    cursor = conn.cursor()
    try:
        if not schema_exists(cursor, args.schema):
            raise RuntimeError(f"Schema `{args.schema}` does not exist.")
        applied, total, benchmark, script_path = apply_fk_script(
            cursor, args.schema, args.benchmark, force=args.force
        )
        print(
            f"{benchmark.upper()} FK script: {script_path}\n"
            f"Schema: `{args.schema}`\n"
            f"Applied statements: {applied}\n"
            f"Foreign keys now present: {total}"
        )
    except Error:
        cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
