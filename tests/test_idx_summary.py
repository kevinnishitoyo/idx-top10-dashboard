import sqlite3
import tempfile
import unittest
from pathlib import Path

from idx_summary import create_schema, ingest_file, match_columns


class IdxSummaryTests(unittest.TestCase):
    def test_non_regular_headers_are_detected_separately(self):
        mapping = match_columns(
            [
                "Stock Code",
                "Volume",
                "Value",
                "NonRegularVolume",
                "NonRegularValue",
            ]
        )
        self.assertEqual(mapping["value"], "Value")
        self.assertEqual(mapping["non_regular_value"], "NonRegularValue")
        self.assertEqual(mapping["non_regular_volume"], "NonRegularVolume")

    def test_non_regular_activity_is_removed_from_rank_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Stock Summary-20260922.csv"
            path.write_text(
                "Stock Code,Volume,Value,NonRegularVolume,NonRegularValue\n"
                "TEST,1000,1000000,200,300000\n",
                encoding="utf-8",
            )
            connection = sqlite3.connect(":memory:")
            create_schema(connection)
            ingest_file(connection, path, False)
            row = connection.execute(
                "SELECT volume, value, reported_value, non_regular_volume, "
                "non_regular_value FROM idx_summary"
            ).fetchone()
            self.assertEqual(row, (800, 700000, 1000000, 200, 300000))


if __name__ == "__main__":
    unittest.main()
