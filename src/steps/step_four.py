from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from src.databases.database import DEFAULT_DATABASE_PATH, connect


@dataclass(frozen=True)
class MedicineField:
    key: str
    label: str
    prompt: str
    required: bool = False


MEDICINE_FIELDS = (
    MedicineField("Name", "Наименование препарата", "Введите наименование препарата:", True),
    MedicineField("Unit", "Единица измерения", "Введите единицу измерения:", True),
    MedicineField(
        "Before",
        "Срок годности",
        'Введите срок годности в формате "dd.mm.yyyy":',
        True,
    ),
    MedicineField(
        "Instruction",
        "Ссылка на наставление по применению",
        "Введите ссылку на наставление по применению (https://...):",
    ),
    MedicineField("Photo", "Ссылка на фото препарата", "Введите ссылку на фото препарата (https://...):"),
    MedicineField("Price", "Цена", "Введите цену препарата:", True),
    MedicineField(
        "Manufacturer",
        "Предприятие-изготовитель",
        "Введите наименование предприятия-изготовителя:",
    ),
    MedicineField("Remainder", "Количество", "Введите количество препарата:"),
    MedicineField("Salesman", "Предприятие-продавец", "Введите предприятие-продавца:"),
)


def validate_medicine_field(field: MedicineField, raw_value: str) -> str | float:
    value = raw_value.strip()
    if not value:
        if field.required:
            raise ValueError("Это обязательное поле. Введите значение.")
        raise ValueError("Введите значение или нажмите «Пропустить».")

    if field.key in {"Name", "Unit", "Manufacturer", "Salesman"}:
        return value

    if field.key == "Before":
        try:
            parsed = datetime.strptime(value, "%d.%m.%Y")
        except ValueError as error:
            raise ValueError('Введите существующую дату в формате "dd.mm.yyyy".') from error
        if parsed.strftime("%d.%m.%Y") != value:
            raise ValueError('Введите дату в формате "dd.mm.yyyy", например 05.12.2027.')
        return value

    if field.key in {"Instruction", "Photo"}:
        try:
            parsed_url = urlsplit(value)
            port = parsed_url.port
        except ValueError as error:
            raise ValueError("Введите корректную ссылку, начинающуюся с https://.") from error
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.hostname
            or (port is not None and not 0 <= port <= 65535)
            or any(character.isspace() for character in value)
        ):
            raise ValueError("Введите корректную ссылку, начинающуюся с https://.")
        return value

    if field.key in {"Price", "Remainder"}:
        normalized = value.replace(" ", "").replace(",", ".")
        try:
            decimal_value = Decimal(normalized)
        except InvalidOperation as error:
            raise ValueError("Введите корректное числовое значение.") from error
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValueError("Значение должно быть неотрицательным числом.")
        if field.key == "Remainder":
            decimal_tuple = decimal_value.as_tuple()
            trailing_zero_count = 0
            for digit in reversed(decimal_tuple.digits):
                if digit != 0:
                    break
                trailing_zero_count += 1
            fractional_places = (
                0
                if trailing_zero_count == len(decimal_tuple.digits)
                else max(0, -decimal_tuple.exponent - trailing_zero_count)
            )
            if fractional_places > 2:
                raise ValueError(
                    "Количество может содержать не более двух знаков после запятой."
                )
        try:
            number = float(decimal_value)
        except OverflowError as error:
            raise ValueError("Указанное число слишком велико.") from error
        if not isfinite(number):
            raise ValueError("Указанное число слишком велико.")
        return number

    raise ValueError(f"Неизвестное поле препарата: {field.key}.")


def save_medicine(
    values: Mapping[str, str | float | None],
    database_path: str | Path = DEFAULT_DATABASE_PATH,
) -> int:
    missing = [
        field.label
        for field in MEDICINE_FIELDS
        if field.required and values.get(field.key) is None
    ]
    if missing:
        raise ValueError("Не заполнены обязательные поля: " + ", ".join(missing))

    normalized_values: dict[str, str | float | None] = {}
    for field in MEDICINE_FIELDS:
        value = values.get(field.key)
        normalized_values[field.key] = (
            None if value is None else validate_medicine_field(field, str(value))
        )

    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO Medicines
                (Name, Unit, "Before", Instruction, Photo, Price,
                 Manufacturer, Remainder, Salesman)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(normalized_values[field.key] for field in MEDICINE_FIELDS),
        )
        return int(cursor.lastrowid)