import tempfile
import unittest
from pathlib import Path

from src.databases.database import connect
from src.steps.step_four import (
    MEDICINE_FIELDS,
    save_medicine,
    validate_medicine_field,
)


class MedicineEntryTests(unittest.TestCase):
    def test_validates_dates_urls_and_numbers(self) -> None:
        fields = {field.key: field for field in MEDICINE_FIELDS}
        self.assertEqual(validate_medicine_field(fields["Before"], "05.12.2027"), "05.12.2027")
        self.assertEqual(
            validate_medicine_field(fields["Instruction"], "https://example.test/info"),
            "https://example.test/info",
        )
        self.assertEqual(validate_medicine_field(fields["Price"], "12,50"), 12.5)
        self.assertEqual(validate_medicine_field(fields["Remainder"], "2.25"), 2.25)
        self.assertEqual(validate_medicine_field(fields["Remainder"], "2.2500"), 2.25)

        invalid_values = (
            ("Before", "31.02.2027"),
            ("Before", "5.12.2027"),
            ("Instruction", "http://example.test/info"),
            ("Photo", "https:///missing-host"),
            ("Price", "not a price"),
            ("Remainder", "-1"),
            ("Remainder", "1.234"),
            ("Photo", "https://[invalid"),
            ("Name", "   "),
        )
        for field_key, value in invalid_values:
            with self.subTest(field=field_key, value=value):
                with self.assertRaises(ValueError):
                    validate_medicine_field(fields[field_key], value)

    def test_saves_medicine_only_when_requested(self) -> None:
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

            values = {
                "Name": "Test medicine",
                "Unit": "bottle",
                "Before": "05.12.2027",
                "Instruction": "https://example.test/info",
                "Photo": "https://example.test/photo.jpg",
                "Price": 12.5,
                "Manufacturer": None,
                "Remainder": 2.25,
                "Salesman": None,
            }
            medicine_id = save_medicine(values, database_path)

            with connect(database_path) as connection:
                record = connection.execute(
                    """
                    SELECT Name, Unit, "Before", Instruction, Photo, Price,
                           Manufacturer, Remainder, Salesman
                    FROM Medicines WHERE ID = ?
                    """,
                    (medicine_id,),
                ).fetchone()

            self.assertEqual(tuple(record), tuple(values[field.key] for field in MEDICINE_FIELDS))

            with self.assertRaisesRegex(ValueError, "обязательные"):
                save_medicine({"Name": "Incomplete"}, database_path)


if __name__ == "__main__":
    unittest.main()
