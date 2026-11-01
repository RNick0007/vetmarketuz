import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

from src.databases.database import connect
from src.databases.steps.medicine_lookup import MedicineInfo
from src.databases.steps.step_one import (
    enrich_medicine_info,
    import_medicine_workbook,
    medicine_names_missing_info,
)


class ImportMedicineWorkbookTests(unittest.TestCase):
    def test_import_adds_and_updates_medicines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "vetmarket.db"
            workbook_path = root / "medicines.xlsx"
            with connect(database_path) as connection:
                connection.execute(
                    'CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Unit TEXT, "Before" TEXT, '
                    "Instruction TEXT, Photo TEXT, Price REAL, Manufacturer TEXT, Remainder REAL, Salesman TEXT)"
                )
                connection.execute(
                    'INSERT INTO Medicines (Name, Unit, "Before", Instruction, Photo, Price, Manufacturer) '
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("ТЕСТОВОЕ средство", "old unit", "old date", "old instruction", "old photo", 1.0, "old maker"),
                )

            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["тестовое СРЕДСТВО", datetime(2027, 3, 2), None, "pack", 12.5])
            sheet.append(["New medicine", "2028-04-05", None, "bottle", 20])
            sheet.append(["Medicine without search result", "06.07.2029", None, "box", 5])
            sheet.append(["Medicine with invalid date", "not a date", None, "box", 5])
            workbook.save(workbook_path)

            result = import_medicine_workbook(workbook_path, database_path)

            self.assertEqual((result.added, result.updated, result.skipped), (2, 1, 1))
            self.assertEqual(len(result.errors), 1)
            self.assertIn("формат dd.mm.yyyy", result.errors[0])
            self.assertEqual(result.pending_enrichment, ("New medicine", "Medicine without search result"))
            enrichment = enrich_medicine_info(
                result.pending_enrichment,
                database_path,
                info_lookup=lambda name: (
                    MedicineInfo("instruction-url", "photo-url", "maker")
                    if name == "New medicine"
                    else MedicineInfo()
                ),
            )
            self.assertEqual(
                (
                    enrichment.instructions,
                    enrichment.photos,
                    enrichment.manufacturers,
                    enrichment.no_results,
                    enrichment.errors,
                ),
                (1, 1, 1, 1, 0),
            )

            with connect(database_path) as connection:
                records = connection.execute(
                    'SELECT Name, Unit, "Before", Instruction, Photo, Price, Manufacturer FROM Medicines ORDER BY ID'
                ).fetchall()
            self.assertEqual(
                tuple(records[0]),
                ("ТЕСТОВОЕ средство", "pack", "02.03.2027", "old instruction", "old photo", 12.5, "old maker"),
            )
            self.assertEqual(
                tuple(records[1]),
                ("New medicine", "bottle", "05.04.2028", "instruction-url", "photo-url", 20.0, "maker"),
            )
            self.assertEqual(
                tuple(records[2]),
                ("Medicine without search result", "box", "06.07.2029", None, None, 5.0, None),
            )
            self.assertEqual(
                medicine_names_missing_info(database_path),
                ("Medicine without search result",),
            )

    def test_enrichment_replaces_empty_metadata_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "vetmarket.db"
            with connect(database_path) as connection:
                connection.execute(
                    'CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT, Unit TEXT, "Before" TEXT, '
                    "Instruction TEXT, Photo TEXT, Price REAL, Manufacturer TEXT, Remainder REAL, Salesman TEXT)"
                )
                connection.execute(
                    "INSERT INTO Medicines (Name, Instruction, Photo, Manufacturer) VALUES (?, ?, ?, ?)",
                    ("Blank metadata medicine", "", "", ""),
                )

            result = enrich_medicine_info(
                ("Blank metadata medicine",),
                database_path,
                info_lookup=lambda _: MedicineInfo("instruction-url", "photo-url", "manufacturer"),
            )

            self.assertEqual(
                (
                    result.instructions,
                    result.photos,
                    result.manufacturers,
                    result.no_results,
                    result.errors,
                ),
                (1, 1, 1, 0, 0),
            )
            with connect(database_path) as connection:
                record = connection.execute(
                    "SELECT Instruction, Photo, Manufacturer FROM Medicines"
                ).fetchone()
            self.assertEqual(tuple(record), ("instruction-url", "photo-url", "manufacturer"))
            self.assertEqual(medicine_names_missing_info(database_path), ())


if __name__ == "__main__":
    unittest.main()
