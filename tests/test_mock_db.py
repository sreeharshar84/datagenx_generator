"""
Unit tests to verify mock database functionality.

Run with: pytest tests/test_mock_db.py -v
"""

import json


class TestMockCursor:
    """Test mock cursor returns correct data from JSON fixtures."""

    def test_count_star(self, mock_cursor):
        """COUNT(*) returns row count from stats.json."""
        mock_cursor.execute("SELECT COUNT(*) FROM `datagenx_test_src`.`film`")
        result = mock_cursor.fetchone()
        assert result[0] == 20

    def test_count_distinct(self, mock_cursor):
        """COUNT(DISTINCT col) returns distinct count from stats.json."""
        mock_cursor.execute("SELECT COUNT(DISTINCT `language_id`) FROM `datagenx_test_src`.`film`")
        result = mock_cursor.fetchone()
        assert result[0] == 1  # All films use language_id=1

    def test_min_max(self, mock_cursor):
        """MIN/MAX returns values from stats.json."""
        mock_cursor.execute("SELECT MIN(`film_id`), MAX(`film_id`) FROM `datagenx_test_src`.`film`")
        result = mock_cursor.fetchone()
        assert result[0] == 1
        assert result[1] == 20

    def test_show_index(self, mock_cursor):
        """SHOW INDEX returns index info from indexes.json."""
        mock_cursor.execute("SHOW INDEX FROM `datagenx_test_src`.`film`")
        results = mock_cursor.fetchall()
        assert len(results) > 0

        # Check PRIMARY key exists
        key_names = [r[2] for r in results]  # Key_name is index 2
        assert "PRIMARY" in key_names

    def test_column_statistics(self, mock_cursor):
        """COLUMN_STATISTICS returns histogram data."""
        mock_cursor.execute("""
            SELECT COLUMN_NAME, HISTOGRAM
            FROM information_schema.COLUMN_STATISTICS
            WHERE TABLE_NAME = 'film'
        """)
        results = mock_cursor.fetchall()
        assert len(results) > 0

        # Verify histogram structure
        col_name, hist_json = results[0][2], results[0][3]
        hist = json.loads(hist_json)
        assert "histogram-type" in hist
        assert "buckets" in hist

    def test_show_create_table(self, mock_cursor):
        """SHOW CREATE TABLE returns DDL from schema.json."""
        mock_cursor.execute("SHOW CREATE TABLE `datagenx_test_src`.`film`")
        result = mock_cursor.fetchone()
        assert result[0] == "film"
        assert "CREATE TABLE" in result[1]
        assert "film_id" in result[1]

    def test_information_schema_columns(self, mock_cursor):
        """INFORMATION_SCHEMA.COLUMNS returns column metadata."""
        mock_cursor.execute("""
            SELECT COLUMN_NAME, COLUMN_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'film'
        """)
        results = mock_cursor.fetchall()
        column_names = [r[1] for r in results]
        assert "film_id" in column_names
        assert "title" in column_names

    def test_key_column_usage_fk(self, mock_cursor):
        """KEY_COLUMN_USAGE returns FK relationships."""
        mock_cursor.execute("""
            SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE REFERENCED_TABLE_NAME IS NOT NULL
        """)
        results = mock_cursor.fetchall()
        assert len(results) > 0

        # film.language_id -> language.language_id should exist
        fk_found = any(
            r[0] == "film" and r[1] == "language_id" and r[2] == "language"
            for r in results
        )
        assert fk_found


class TestMockMySQLPatch:
    """Test that mysql.connector is properly patched."""

    def test_mysql_connector_patched(self, mock_mysql):
        """mysql.connector.connect returns mock connection."""
        import mysql.connector
        conn = mysql.connector.connect(host="localhost", user="test")
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM `datagenx_test_src`.`actor`")
        result = cursor.fetchone()
        assert result[0] == 20

    def test_multiple_cursors(self, mock_mysql):
        """Multiple cursors work independently."""
        import mysql.connector
        conn = mysql.connector.connect()

        cursor1 = conn.cursor()
        cursor2 = conn.cursor()

        cursor1.execute("SELECT COUNT(*) FROM `datagenx_test_src`.`film`")
        cursor2.execute("SELECT COUNT(*) FROM `datagenx_test_src`.`actor`")

        assert cursor1.fetchone()[0] == 20  # film
        assert cursor2.fetchone()[0] == 20  # actor


class TestAllTables:
    """Verify all exported tables are accessible."""

    def test_all_tables_have_stats(self, mock_cursor):
        """Every table in schema.json has stats."""
        from tests.mock_db import MockDatabase
        db = MockDatabase()

        for table_name in db.schema["tables"]:
            mock_cursor.execute(f"SELECT COUNT(*) FROM `datagenx_test_src`.`{table_name}`")
            result = mock_cursor.fetchone()
            assert result[0] >= 0, f"Table {table_name} should have row count"

    def test_tables_list(self, mock_cursor):
        """INFORMATION_SCHEMA.TABLES returns all tables."""
        mock_cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
        results = mock_cursor.fetchall()
        table_names = [r[0] for r in results]

        expected = ["actor", "category", "customer", "film", "film_actor",
                    "film_category", "inventory", "language", "payment", "rental", "store"]
        for t in expected:
            assert t in table_names
