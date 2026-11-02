from pathlib import Path

from src.databases.database import DEFAULT_DATABASE_PATH, connect
from src.steps.step_four import MEDICINE_FIELDS, MedicineField, validate_medicine_field


def get_medicine_field(field_key: str) -> MedicineField:
    field = next((field for field in MEDICINE_FIELDS if field.key == field_key), None)
    if field is None:
        raise ValueError(f"Неизвестное поле препарата: {field_key}.")
    return field


def update_medicine_field(
    medicine_id: int,
    field_key: str,
    raw_value: str,
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> tuple[MedicineField, str | float]:
    field = get_medicine_field(field_key)
    value = validate_medicine_field(field, raw_value)

    with connect(database_path) as connection:
        medicine = connection.execute(
            "SELECT ID FROM Medicines WHERE ID = ?",
            (medicine_id,),
        ).fetchone()
        if medicine is None:
            raise LookupError(f"Препарат с ID {medicine_id} не найден.")

        connection.execute(
            f'UPDATE Medicines SET "{field.key}" = ? WHERE ID = ?',
            (value, medicine_id),
        )

    return field, value
