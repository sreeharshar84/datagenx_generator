import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def mock_mysql(monkeypatch):
    """
    Patch mysql.connector to use mock database from JSON fixtures.

    Usage in tests:
        def test_something(mock_mysql):
            # mysql.connector.connect() now returns mock connection
            from datagenx.generation import GenerateDbgen
            # ... test code that uses cursor
    """
    from tests import mock_db
    monkeypatch.setattr("mysql.connector.connect", mock_db.connect)
    monkeypatch.setattr("mysql.connector.Error", mock_db.Error)
    monkeypatch.setattr("mysql.connector.DatabaseError", mock_db.DatabaseError)
    monkeypatch.setattr("mysql.connector.InterfaceError", mock_db.InterfaceError)
    return mock_db.MockDatabase()


@pytest.fixture
def mock_cursor(mock_mysql):
    """
    Provide a ready-to-use mock cursor.

    Usage in tests:
        def test_query(mock_cursor):
            mock_cursor.execute("SELECT COUNT(*) FROM `schema`.`film`")
            result = mock_cursor.fetchone()
            assert result[0] > 0
    """
    return mock_mysql.connect().cursor()
