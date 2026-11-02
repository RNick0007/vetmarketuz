import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from src.databases.database import connect
from src.steps.step_three import (
    get_medicine,
    search_medicines_by_prefix,
    update_medicine_remainder,
)
from src.steps.step_two import import_remainders_workbook


class ImportRemaindersWorkbookTests(unittest.TestCase):
    def test_import_updates_remainders_from_second_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "vetmarket.db"
            workbook_path = root / "remainders.xlsx"
            with connect(database_path) as connection:
                connection.execute(
                    'CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Remainder REAL, Salesman TEXT)'
                )
                connection.executemany(
                    "INSERT INTO Medicines (Name, Remainder) VALUES (?, ?)",
                    (("Тестовый препарат", 1), ("Без изменений", 4), ("Дробный остаток", 0)),
                )

            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["ТЕСТОВЫЙ ПРЕПАРАТ", 7, "не использовать"])
            sheet.append(["Без изменений", 4, 99])
            sheet.append(["Неизвестный препарат", None, 100])
            sheet.append(["Тестовый препарат", 7, "дублирующая строка"])
            sheet.append(["Дробный остаток", 1.5, None])
            sheet.append(["Остаток с двумя знаками", "2,75", None])
            sheet.append(["Остаток с лишней точностью", 1.234, None])
            with connect(database_path) as connection:
                connection.executemany(
                    "INSERT INTO Medicines (Name, Remainder) VALUES (?, ?)",
                    (("Остаток с двумя знаками", 0), ("Остаток с лишней точностью", 0)),
                )
            workbook.save(workbook_path)

            result = import_remainders_workbook(workbook_path, database_path)

            self.assertEqual(result.changed, 3)
            self.assertEqual(result.not_found, ("Неизвестный препарат",))
            self.assertEqual(len(result.errors), 1)
            self.assertIn("1.234", result.errors[0])
            with connect(database_path) as connection:
                remainders = connection.execute(
                    "SELECT Name, Remainder FROM Medicines ORDER BY ID"
                ).fetchall()
                salesmen = connection.execute("SELECT DISTINCT Salesman FROM Medicines").fetchall()
            self.assertEqual(
                [tuple(row) for row in remainders],
                [
                    ("Тестовый препарат", 7),
                    ("Без изменений", 4),
                    ("Дробный остаток", 1.5),
                    ("Остаток с двумя знаками", 2.75),
                    ("Остаток с лишней точностью", 0),
                ],
            )
            self.assertEqual([row[0] for row in salesmen], ["Vetco"])

    def test_existing_integer_remainder_column_is_migrated_to_real(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.db"
            with connect(database_path) as connection:
                connection.execute(
                    'CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Remainder INTEGER)'
                )
                connection.execute(
                    "INSERT INTO Medicines (Name, Remainder) VALUES (?, ?)",
                    ("Existing medicine", 12),
                )

            with connect(database_path) as connection:
                column = next(
                    column
                    for column in connection.execute("PRAGMA table_info(Medicines)")
                    if column["name"] == "Remainder"
                )
                record = connection.execute(
                    "SELECT Remainder FROM Medicines WHERE Name = ?",
                    ("Existing medicine",),
                ).fetchone()

            self.assertEqual(column["type"], "REAL")
            self.assertEqual(record["Remainder"], 12)

    def test_invalid_workbook_extension_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Поддерживаются только"):
            import_remainders_workbook("inventory.csv")

    def test_manual_remainder_search_and_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "manual.db"
            with connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE Medicines (
                        ID INTEGER PRIMARY KEY,
                        Name TEXT,
                        Unit TEXT,
                        "Before" TEXT,
                        Instruction TEXT,
                        Photo TEXT,
                        Price REAL,
                        Manufacturer TEXT,
                        Remainder REAL,
                        Salesman TEXT
                    )
                    """
                )
                connection.executemany(
                    "INSERT INTO Medicines (Name, Remainder) VALUES (?, ?)",
                    (("Тестовый препарат", 1), ("Другой товар", 4), ("Straße gel", 3)),
                )

            matches = search_medicines_by_prefix("тЕсТ", database_path)
            expanded_case_match = search_medicines_by_prefix("STRASSE", database_path)

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].name, "Тестовый препарат")
            self.assertEqual(matches[0].remainder, 1)
            self.assertEqual([medicine.name for medicine in expanded_case_match], ["Straße gel"])
            self.assertEqual(update_medicine_remainder(matches[0].id, 2.5, database_path), "Тестовый препарат")
            self.assertEqual(get_medicine(matches[0].id, database_path).remainder, 2.5)

            with self.assertRaisesRegex(ValueError, "неотрицательным"):
                update_medicine_remainder(matches[0].id, -1, database_path)


if __name__ == "__main__":
    unittest.main()
