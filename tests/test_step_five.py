import tempfile
import unittest
from pathlib import Path

from src.databases.database import connect
from src.steps.step_five import get_medicine_field, update_medicine_field


class MedicineEditTests(unittest.TestCase):
    def test_updates_allowed_fields_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
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
                connection.execute(
                    "INSERT INTO Medicines (Name, Price) VALUES (?, ?)",
                    ("Тестовый препарат", 12),
                )

            field, value = update_medicine_field(
                1,
                "Price",
                "25,50",
                database_path,
            )
            self.assertEqual(field.label, "Цена")
            self.assertEqual(value, 25.5)

            with connect(database_path) as connection:
                record = connection.execute(
                    "SELECT Name, Price FROM Medicines WHERE ID = 1"
                ).fetchone()
            self.assertEqual(tuple(record), ("Тестовый препарат", 25.5))

    def test_rejects_unknown_fields_invalid_values_and_missing_medicines(self) -> None:
        with self.assertRaisesRegex(ValueError, "Неизвестное поле"):
            get_medicine_field("Unknown")

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "medicines.db"
            with connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE Medicines (ID INTEGER PRIMARY KEY, Name TEXT)"
                )
                connection.execute(
                    "INSERT INTO Medicines (Name) VALUES (?)",
                    ("Тестовый препарат",),
                )

            with self.assertRaises(ValueError):
                update_medicine_field(1, "Before", "31.02.2027", database_path)
            with self.assertRaises(LookupError):
                update_medicine_field(2, "Name", "Другой препарат", database_path)

            with connect(database_path) as connection:
                name = connection.execute(
                    "SELECT Name FROM Medicines WHERE ID = 1"
                ).fetchone()["Name"]
            self.assertEqual(name, "Тестовый препарат")


if __name__ == "__main__":
    unittest.main()
