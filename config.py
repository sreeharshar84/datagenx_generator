# config.py - Central configuration for DataGenX Generator
#
# Values can be overridden from environment variables or from a repo-root .env
# file. Generic DB_* variables take precedence; engine-specific variables such
# as TIDB_* are accepted as convenient aliases.

import os
from pathlib import Path


try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


if load_dotenv:
    load_dotenv(Path(__file__).resolve().parent / ".env")


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value is not None and value != "":
            return value
    return default


def _env_int(*names, default=None):
    value = _env(*names)
    if value is None:
        return default
    return int(value)


def _engine_env_names(suffix):
    prefixes_by_engine = {
        "mysql": ("MYSQL",),
        "singlestore": ("SINGLESTORE", "SINGLE_STORE"),
        "tidb": ("TIDB",),
    }
    return tuple(
        f"{prefix}_{suffix}"
        for prefix in prefixes_by_engine.get(DB_TYPE, ())
    )


_default_db_type = "mysql"
if _env("TIDB_HOST"):
    _default_db_type = "tidb"
elif _env("SINGLESTORE_HOST", "SINGLE_STORE_HOST"):
    _default_db_type = "singlestore"

# Database type (mysql, singlestore, or tidb)
DB_TYPE = _env("DB_TYPE", "DATAGENX_DB_TYPE", default=_default_db_type).lower()

# Database connection
HOST = _env(
    "DB_HOST",
    "DATAGENX_DB_HOST",
    *_engine_env_names("HOST"),
    default="localhost",
)
DB_PORT = _env_int(
    "DB_PORT",
    "DATAGENX_DB_PORT",
    *_engine_env_names("PORT"),
    default=None,
)
USER = _env(
    "DB_USER",
    "DB_USERNAME",
    "DATAGENX_DB_USER",
    *_engine_env_names("USER"),
    *_engine_env_names("USERNAME"),
    default="root",
)
PASSWORD = _env(
    "DB_PASSWORD",
    "DATAGENX_DB_PASSWORD",
    *_engine_env_names("PASSWORD"),
    default="newpassword",
)

# Schema configuration. For TiDB, TIDB_DATABASE is treated as SOURCE_SCHEMA.
SOURCE_SCHEMA = _env(
    "SOURCE_SCHEMA",
    "DB_DATABASE",
    "DATAGENX_SOURCE_SCHEMA",
    *_engine_env_names("DATABASE"),
    default="tpch",
)
_default_target_schema = (
    "tpch_dbgenx"
    if SOURCE_SCHEMA in ("tpch", "tpch_vanilla")
    else f"{SOURCE_SCHEMA}_dbgenx"
)
TARGET_SCHEMA = _env(
    "TARGET_SCHEMA",
    "DB_TARGET_SCHEMA",
    "DATAGENX_TARGET_SCHEMA",
    default=_default_target_schema,
)

# Paths. DBGEN_BINARY must point to the Rust DataGenX dbgen binary, not the
# official TPC-H dbgen binary.
_cargo_dbgen = str(Path.home() / ".cargo" / "bin" / "dbgen")
_local_dbgen = "/Users/sreeharshar/work/db/datagenx/code/dbgen/target/release/dbgen"
_legacy_dbgen = "/home/hmaduri/contribs/dbgen/target/release/dbgen"

def _find_dbgen_binary():
    for path in [_cargo_dbgen, _local_dbgen, _legacy_dbgen]:
        if os.path.isfile(path):
            return path
    return _legacy_dbgen

DBGEN_BINARY = os.path.expanduser(_env(
    "DBGEN_BINARY",
    "DATAGENX_DBGEN_BINARY",
    default=_find_dbgen_binary(),
))
DBGEN_FILES_DIR = _env("DBGEN_FILES_DIR", "DATAGENX_DBGEN_FILES_DIR",
                       default="generated/dbgen_files")
DBGEN_TMP_OUT_DIR = _env("DBGEN_TMP_OUT_DIR", "DATAGENX_DBGEN_TMP_OUT_DIR",
                         default="generated/dbgen_tmp_out")

# FK DDL file path (for databases that don't expose FK metadata, e.g., SingleStore)
FK_DDL_FILE = _env("FK_DDL_FILE", "DATAGENX_FK_DDL_FILE", default=None)

# Generation settings
FILES_COUNT = _env("FILES_COUNT", "DATAGENX_FILES_COUNT", default="1")
ROWS_COUNT = _env("ROWS_COUNT", "DATAGENX_ROWS_COUNT", default="1000")
