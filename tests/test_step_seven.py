import tempfile
import unittest
from pathlib import Path

from src.databases.database import connect
from src.steps.step_seven import delete_all_medicines


class DeleteAllMedicinesTests(unittest.TestCase):
    def test_deletes_every_medicine_and_vacuums_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
            with connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Details TEXT)"
                )
                connection.executemany(
                    "INSERT INTO Medicines (Name, Details) VALUES (?, ?)",
                    (
                        ("Препарат 1", "x" * 700_000),
                        ("Препарат 2", "другие данные"),
                    ),
                )
                page_count_before = connection.execute("PRAGMA page_count").fetchone()[0]

            deleted_count = delete_all_medicines(database_path)

            with connect(database_path) as connection:
                remaining_count = connection.execute(
                    "SELECT COUNT(*) FROM Medicines"
                ).fetchone()[0]
                page_count_after = connection.execute("PRAGMA page_count").fetchone()[0]
                free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]

            self.assertEqual(deleted_count, 2)
            self.assertEqual(remaining_count, 0)
            self.assertLess(page_count_after, page_count_before)
            self.assertEqual(free_pages, 0)

    def test_empty_table_returns_zero_and_keeps_database_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
            with connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT)"
                )

            self.assertEqual(delete_all_medicines(database_path), 0)

            with connect(database_path) as connection:
                remaining_count = connection.execute(
                    "SELECT COUNT(*) FROM Medicines"
                ).fetchone()[0]
            self.assertEqual(remaining_count, 0)


if __name__ == "__main__":
    unittest.main()
