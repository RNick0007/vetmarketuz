import os
import sys

from dotenv import load_dotenv

from .database import PROJECT_ROOT
from .steps.step_one import enrich_medicine_info, medicine_names_missing_info


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("BRAVE_SEARCH_API_KEY"):
        raise SystemExit("Добавьте BRAVE_SEARCH_API_KEY в .env и перезапустите контейнер.")

    names = medicine_names_missing_info()
    if not names:
        print("Нет препаратов с незаполненными сведениями.")
        return

    print(f"Запускаю поиск для {len(names)} препаратов.")
    result = enrich_medicine_info(names)
    print(
        "Поиск завершен. "
        f"Инструкций: {result.instructions}; "
        f"изображений: {result.photos}; "
        f"изготовителей: {result.manufacturers}; "
        f"без результатов: {result.no_results}; "
        f"ошибок API: {result.errors}."
    )
    if result.no_results or result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
