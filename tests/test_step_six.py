import tempfile
import unittest
from pathlib import Path

from src.databases.database import connect
from src.steps.step_six import delete_medicine


class MedicineDeletionTests(unittest.TestCase):
    def test_deletes_medicine_and_vacuums_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
            with connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Details TEXT)"
                )
                connection.executemany(
                    "INSERT INTO Medicines (Name, Details) VALUES (?, ?)",
                    (
                        ("Удаляемый препарат", "x" * 700_000),
                        ("Оставляемый препарат", "другие данные"),
                    ),
                )
                page_count_before = connection.execute("PRAGMA page_count").fetchone()[0]

            deleted_name = delete_medicine(1, database_path)

            with connect(database_path) as connection:
                record = connection.execute(
                    "SELECT ID, Name FROM Medicines"
                ).fetchone()
                page_count_after = connection.execute("PRAGMA page_count").fetchone()[0]
                free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]

            self.assertEqual(deleted_name, "Удаляемый препарат")
            self.assertEqual(tuple(record), (2, "Оставляемый препарат"))
            self.assertLess(page_count_after, page_count_before)
            self.assertEqual(free_pages, 0)

    def test_missing_medicine_is_not_vacuumed_or_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
            with connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT)"
                )
                connection.execute(
                    "INSERT INTO Medicines (Name) VALUES (?)",
                    ("Оставляемый препарат",),
                )

            with self.assertRaisesRegex(LookupError, "не найден"):
                delete_medicine(2, database_path)

            with connect(database_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM Medicines").fetchone()[0]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
