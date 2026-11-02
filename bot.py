import asyncio
import logging
import os
import sqlite3
import tempfile
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.databases.steps.step_one import enrich_medicine_info, import_medicine_workbook
from src.steps.step_two import (
    RemainderImportResult,
    import_remainders_workbook,
)
from src.steps.step_three import (
    MedicineRecord,
    get_medicine,
    search_medicines_by_prefix,
    update_medicine_remainder,
)
from src.steps.step_four import (
    MEDICINE_FIELDS,
    MedicineField,
    save_medicine,
    validate_medicine_field,
)


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
IMPORT_BUTTON = "Автоматический ввод препаратов"
REMAINDER_BUTTON = "Автоматический ввод остатков"
MANUAL_REMAINDER_BUTTON = "Ручное изменение остатков"
FINISH_MANUAL_REMAINDER_BUTTON = "Завершить ввод остатков"
ADD_MEDICINE_BUTTON = "Ввод нового препарата"
MENU_BUTTONS = (
    IMPORT_BUTTON,
    REMAINDER_BUTTON,
    MANUAL_REMAINDER_BUTTON,
    ADD_MEDICINE_BUTTON,
    "Изменение характеристик препарата",
    "Удаление одного препарата",
    "Удаление всех препаратов",
)
SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".ods"}
MAX_DOCUMENT_SIZE = 20 * 1024 * 1024
_command_setup_task: asyncio.Task[None] | None = None


def _admin_id() -> int:
    raw_admin_id = os.getenv("ADMIN_TG_ID")
    if not raw_admin_id:
        raise RuntimeError("В файле .env не задан ADMIN_TG_ID.")
    try:
        return int(raw_admin_id)
    except ValueError as error:
        raise RuntimeError("ADMIN_TG_ID должен быть числом.") from error


def _is_admin(update: Update, admin_id: int) -> bool:
    user = update.effective_user
    return user is not None and user.id == admin_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is not None:
        LOGGER.info("Получена команда /start.")
        await message.reply_text("Здравствуйте! Добро пожаловать в VetMarketUz.")


async def medicines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    if not _is_admin(update, _admin_id()):
        LOGGER.warning("Команда /medicines получена не от администратора.")
        await message.reply_text("Команда доступна только администратору.")
        return

    context.user_data.pop("awaiting_workbook_action", None)
    _clear_manual_remainder_state(context)
    _clear_medicine_creation_state(context)
    LOGGER.info("Администратор запустил команду /medicines.")
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"medicines:{index}")]
        for index, label in enumerate(MENU_BUTTONS)
    ]
    await message.reply_text(
        "Используйте кнопки меню ниже.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await message.reply_text(
        "Выберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def menu_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    action = (message.text or "").strip()
    if action in MENU_BUTTONS:
        await _process_menu_action(message, context, user.id, action)
        return

    if context.user_data.get("manual_remainder_state") in {"search", "quantity"}:
        await _process_manual_remainder_text(message, context, action)
    elif context.user_data.get("medicine_creation") is not None:
        await _process_medicine_creation_text(message, context, action)


async def medicine_creation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()
    message = query.message
    if not isinstance(message, Message):
        LOGGER.warning("Невозможно продолжить ввод препарата без доступного сообщения.")
        return
    if user.id != _admin_id():
        await message.reply_text("Это действие доступно только администратору.")
        return

    form = context.user_data.get("medicine_creation")
    if form is None:
        await message.reply_text("Опрос не активен. Начните его через /medicines.")
        return

    action = query.data or ""
    if action == "medicine:create:back":
        if form["step"] == "review":
            field_index = form["review_cursor"]
            if field_index < 0:
                await message.reply_text(
                    "Вы уже вернулись к началу анкеты.",
                    reply_markup=_medicine_review_keyboard(),
                )
                return
            form["step"] = "question"
            form["field_index"] = field_index
            form["return_to_review"] = True
        else:
            field_index = form["field_index"]
            if field_index == 0:
                _clear_medicine_creation_state(context)
                await message.reply_text("Добавление препарата отменено.")
                return
            form["field_index"] = field_index - 1
        await _send_medicine_question(message, context)
        return

    if action == "medicine:create:skip":
        if form["step"] != "question":
            return
        field = MEDICINE_FIELDS[form["field_index"]]
        if field.required:
            await message.reply_text(
                "Это обязательное поле. Нажмите «Назад» или введите значение."
            )
            return
        await _accept_medicine_answer(message, context, field, None)
        return

    if action == "medicine:create:save":
        if form["step"] != "review":
            return
        try:
            await asyncio.to_thread(save_medicine, form["draft"])
        except ValueError as error:
            await message.reply_text(
                f"Не удалось сохранить анкету: {error}",
                reply_markup=_medicine_review_keyboard(),
            )
            return
        except sqlite3.Error:
            LOGGER.exception("Не удалось сохранить новый препарат в базе данных.")
            await message.reply_text(
                "Не удалось записать препарат в базу данных. "
                "Данные анкеты сохранены; попробуйте сохранить еще раз."
            )
            return
        _clear_medicine_creation_state(context)
        await message.reply_text("Информация о препарате записана в базу данных.")


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()
    message = query.message
    if not isinstance(message, Message):
        LOGGER.warning("Невозможно обработать нажатие кнопки без доступного сообщения.")
        return

    callback_data = query.data or ""
    prefix, _, index_text = callback_data.partition(":")
    if prefix != "medicines" or not index_text.isdigit():
        return
    index = int(index_text)
    if index >= len(MENU_BUTTONS):
        return

    await _process_menu_action(message, context, user.id, MENU_BUTTONS[index])


async def manual_remainder_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()
    message = query.message
    if not isinstance(message, Message):
        LOGGER.warning("Невозможно выбрать препарат без доступного сообщения.")
        return
    if user.id != _admin_id():
        await message.reply_text("Это действие доступно только администратору.")
        return

    if context.user_data.get("manual_remainder_state") != "search":
        await message.reply_text("Сначала начните ручное изменение остатков через /medicines.")
        return

    callback_data = query.data or ""
    _, _, medicine_id_text = callback_data.partition(":select:")
    if not medicine_id_text.isdigit():
        return
    medicine = await asyncio.to_thread(get_medicine, int(medicine_id_text))
    if medicine is None:
        _clear_manual_remainder_state(context)
        await message.reply_text("Препарат больше не найден в базе. Начните поиск заново.")
        return

    context.user_data["manual_remainder_medicine_id"] = medicine.id
    context.user_data["manual_remainder_state"] = "quantity"
    await _send_medicine_card(message, medicine)
    await message.reply_text("Введите новый остаток препарата числом и отправьте сообщение.")


async def finish_manual_remainder_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    await query.answer()
    message = query.message
    if not isinstance(message, Message):
        LOGGER.warning("Невозможно завершить ввод остатков без доступного сообщения.")
        return
    if user.id != _admin_id():
        await message.reply_text("Это действие доступно только администратору.")
        return
    if context.user_data.get("manual_remainder_state") not in {"search", "quantity"}:
        await message.reply_text("Режим ручного изменения остатков уже завершен.")
        return

    _clear_manual_remainder_state(context)
    await message.reply_text("Ввод остатков завершен.")


async def _process_menu_action(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    action: str,
) -> None:
    if user_id != _admin_id():
        await message.reply_text("Это действие доступно только администратору.")
        return

    if action == IMPORT_BUTTON:
        _clear_medicine_creation_state(context)
        _clear_manual_remainder_state(context)
        context.user_data["awaiting_workbook_action"] = "medicines"
        await message.reply_text("Отправьте документ .xlsx, .xls или .ods без строки заголовков.")
        return

    if action == REMAINDER_BUTTON:
        _clear_medicine_creation_state(context)
        _clear_manual_remainder_state(context)
        context.user_data["awaiting_workbook_action"] = "remainders"
        await message.reply_text(
            "Отправьте документ .xlsx, .xls или .ods без строки заголовков. "
            "Наименования должны быть в столбце A, остатки — в столбце B."
        )
        return

    if action == MANUAL_REMAINDER_BUTTON:
        _clear_medicine_creation_state(context)
        context.user_data.pop("awaiting_workbook_action", None)
        _clear_manual_remainder_state(context)
        context.user_data["manual_remainder_state"] = "search"
        await message.reply_text(
            "Введите несколько начальных букв наименования препарата и отправьте сообщение."
        )
        return

    if action == ADD_MEDICINE_BUTTON:
        context.user_data.pop("awaiting_workbook_action", None)
        _clear_manual_remainder_state(context)
        context.user_data["medicine_creation"] = {
            "step": "question",
            "field_index": 0,
            "review_cursor": len(MEDICINE_FIELDS) - 1,
            "return_to_review": False,
            "draft": {},
        }
        await _send_medicine_question(message, context)
        return

    _clear_medicine_creation_state(context)
    _clear_manual_remainder_state(context)
    context.user_data.pop("awaiting_workbook_action", None)
    await message.reply_text("Эта функция будет реализована по отдельному техническому заданию.")


async def _process_manual_remainder_text(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    state = context.user_data.get("manual_remainder_state")
    if state == "search":
        if not text:
            await message.reply_text("Введите несколько начальных букв наименования препарата.")
            return

        matches = await asyncio.to_thread(search_medicines_by_prefix, text)
        if not matches:
            await message.reply_text(
                "Препараты с таким началом наименования не найдены. Попробуйте еще раз."
            )
            return

        keyboard = [
            [
                InlineKeyboardButton(
                    medicine.name if len(medicine.name) <= 64 else medicine.name[:61] + "...",
                    callback_data=f"remainder:select:{medicine.id}",
                )
            ]
            for medicine in matches
        ]
        response = "Выберите препарат:"
        if len(matches) == 20:
            response += " показаны первые 20 совпадений, уточните поиск при необходимости."
        await message.reply_text(response, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    medicine_id = context.user_data.get("manual_remainder_medicine_id")
    if not text or medicine_id is None:
        await message.reply_text("Введите остаток препарата числом.")
        return

    try:
        remainder = _parse_manual_remainder(text)
        medicine_name = await asyncio.to_thread(
            update_medicine_remainder,
            medicine_id,
            remainder,
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return
    except LookupError:
        _clear_manual_remainder_state(context)
        await message.reply_text("Препарат больше не найден в базе. Начните поиск заново.")
        return

    _clear_manual_remainder_state(context)
    context.user_data["manual_remainder_state"] = "search"
    await message.reply_text(
        f"Остаток препарата {medicine_name}: {_format_remainder_quantity(remainder)}\n\n"
        "Введите начальные буквы следующего препарата или завершите ввод остатков.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        FINISH_MANUAL_REMAINDER_BUTTON,
                        callback_data="remainder:finish",
                    )
                ]
            ]
        ),
    )


def _clear_medicine_creation_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("medicine_creation", None)


def _medicine_question_keyboard(field: MedicineField) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("Назад", callback_data="medicine:create:back")
    ]
    if not field.required:
        buttons.append(
            InlineKeyboardButton("Пропустить", callback_data="medicine:create:skip")
        )
    return InlineKeyboardMarkup([buttons])


def _medicine_review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Назад", callback_data="medicine:create:back"),
                InlineKeyboardButton("Сохранить", callback_data="medicine:create:save"),
            ]
        ]
    )


async def _send_medicine_question(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    form = context.user_data["medicine_creation"]
    field = MEDICINE_FIELDS[form["field_index"]]
    current_value = form["draft"].get(field.key)
    prompt = field.prompt
    if current_value is not None:
        prompt += f"\nТекущее значение: {current_value}"
    await message.reply_text(
        prompt,
        reply_markup=_medicine_question_keyboard(field),
    )


async def _process_medicine_creation_text(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> None:
    form = context.user_data["medicine_creation"]
    if form["step"] != "question":
        await message.reply_text(
            "Проверьте введенные данные и нажмите «Сохранить» либо «Назад».",
            reply_markup=_medicine_review_keyboard(),
        )
        return

    field = MEDICINE_FIELDS[form["field_index"]]
    try:
        value = validate_medicine_field(field, text)
    except ValueError as error:
        await message.reply_text(
            f"{error}\n{field.prompt}",
            reply_markup=_medicine_question_keyboard(field),
        )
        return
    await _accept_medicine_answer(message, context, field, value)


async def _accept_medicine_answer(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    field: MedicineField,
    value: str | float | None,
) -> None:
    form = context.user_data["medicine_creation"]
    form["draft"][field.key] = value
    field_index = form["field_index"]
    if form["return_to_review"]:
        form["step"] = "review"
        form["return_to_review"] = False
        form["review_cursor"] = field_index - 1
        await _send_medicine_review(message, context)
        return

    next_index = field_index + 1
    if next_index < len(MEDICINE_FIELDS):
        form["field_index"] = next_index
        await _send_medicine_question(message, context)
        return

    form["step"] = "review"
    form["review_cursor"] = len(MEDICINE_FIELDS) - 1
    await _send_medicine_review(message, context)


async def _send_medicine_review(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    draft = context.user_data["medicine_creation"]["draft"]
    lines = ["Проверьте правильность введенной информации:"]
    for field in MEDICINE_FIELDS:
        value = draft.get(field.key)
        lines.append(f"{field.label}: {value if value is not None else 'не указано'}")
    parts = _split_telegram_message("\n".join(lines))
    for part in parts[:-1]:
        await message.reply_text(part)
    await message.reply_text(
        parts[-1],
        reply_markup=_medicine_review_keyboard(),
    )


def _clear_manual_remainder_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("manual_remainder_state", None)
    context.user_data.pop("manual_remainder_medicine_id", None)


def _parse_manual_remainder(value: str) -> float:
    try:
        quantity = Decimal(value.strip().replace(" ", "").replace(",", "."))
        if (
            not quantity.is_finite()
            or quantity < 0
            or quantity != quantity.quantize(Decimal("0.01"))
        ):
            raise ValueError(
                "Остаток должен быть неотрицательным числом не более чем "
                "с двумя знаками после запятой."
            )
    except InvalidOperation as error:
        raise ValueError(
            "Введите корректное неотрицательное число, например 12 или 2,5."
        ) from error
    try:
        parsed_quantity = float(quantity)
    except OverflowError as error:
        raise ValueError("Указанный остаток слишком велик.") from error
    if not isfinite(parsed_quantity):
        raise ValueError("Указанный остаток слишком велик.")
    return parsed_quantity


def _format_remainder_quantity(quantity: float) -> str:
    return f"{quantity:g}"


async def _send_medicine_card(message: Message, medicine: MedicineRecord) -> None:
    photo = medicine.photo.strip() if medicine.photo else ""
    if photo:
        try:
            await message.reply_photo(photo=photo)
        except TelegramError:
            LOGGER.exception("Не удалось загрузить фото препарата %r.", medicine.name)
            photo = f"ссылка: {photo}"
    else:
        photo = "не указано"

    def display(value: object | None) -> str:
        return "не указано" if value is None or not str(value).strip() else str(value)

    card = (
        f"Фото: {photo}\n"
        f"Наименование: {medicine.name}\n"
        f"Единица измерения: {display(medicine.unit)}\n"
        f"Срок годности: {display(medicine.before)}\n"
        f"Инструкция: {display(medicine.instruction)}\n"
        f"Цена: {display(medicine.price)}\n"
        f"Производитель: {display(medicine.manufacturer)}\n"
        f"Остаток: {display(medicine.remainder)}\n"
        f"Поставщик: {display(medicine.salesman)}"
    )
    for part in _split_telegram_message(card):
        await message.reply_text(
            part,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


async def receive_workbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    document = message.document if message is not None else None
    if message is None or user is None or document is None:
        return
    if user.id != _admin_id():
        await message.reply_text("Загружать препараты может только администратор.")
        return
    workbook_action = context.user_data.get("awaiting_workbook_action")
    if workbook_action not in {"medicines", "remainders"}:
        await message.reply_text("Сначала выберите действие загрузки файла в команде /medicines.")
        return

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await message.reply_text("Неподдерживаемый формат. Отправьте .xlsx, .xls или .ods.")
        return
    if document.file_size is not None and document.file_size > MAX_DOCUMENT_SIZE:
        await message.reply_text("Файл превышает лимит Telegram Bot API в 20 МБ.")
        return

    context.user_data["awaiting_workbook_action"] = None
    await message.reply_text("Обрабатываю файл. Это может занять некоторое время.")
    try:
        with tempfile.TemporaryDirectory(prefix="vetmarket-import-") as temp_dir:
            local_path = Path(temp_dir) / f"workbook{suffix}"
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=local_path)
            if workbook_action == "medicines":
                result = await asyncio.to_thread(import_medicine_workbook, local_path)
            else:
                result = await asyncio.to_thread(import_remainders_workbook, local_path)
    except Exception:
        LOGGER.exception("Не удалось обработать таблицу препаратов")
        await message.reply_text(
            "Не удалось получить или обработать файл. Проверьте его формат и содержимое, затем повторите загрузку."
        )
        return

    if isinstance(result, RemainderImportResult):
        response = _format_remainder_report(result)
        for part in _split_telegram_message(response):
            await message.reply_text(part)
        return

    response = (
        "Импорт завершен.\n"
        f"Добавлено: {result.added}\n"
        f"Обновлено: {result.updated}\n"
        f"Пропущено: {result.skipped}"
    )
    if result.errors:
        response += "\nОшибки:\n" + "\n".join(result.errors[:10])
        if len(result.errors) > 10:
            response += f"\nИ еще ошибок: {len(result.errors) - 10}"
    await message.reply_text(response)
    if result.pending_enrichment:
        if not os.getenv("BRAVE_SEARCH_API_KEY"):
            await message.reply_text(
                "Препараты сохранены, но интернет-поиск не запущен: задайте "
                "BRAVE_SEARCH_API_KEY в .env и перезапустите контейнер."
            )
        else:
            await message.reply_text(
                "Поиск инструкций, изображений и производителей выполняется отдельно; "
                "добавленные препараты уже сохранены в базе."
            )
            context.application.create_task(
                _enrich_and_notify(context, message.chat_id, result.pending_enrichment),
                name="enrich-medicine-info",
            )


def _split_telegram_message(text: str, limit: int = 4000) -> tuple[str, ...]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return tuple(parts)


def _format_remainder_report(result: RemainderImportResult) -> str:
    response = f"Изменено значений остатков: {result.changed}"
    if result.not_found:
        response += (
            f"\nНе найдены в базе данных ({len(result.not_found)}):\n"
            + "\n".join(result.not_found)
        )
    else:
        response += "\nНе найдены в базе данных: нет."

    if result.errors:
        response += (
            f"\nПропущенные строки ({len(result.errors)}):\n"
            + "\n".join(result.errors)
        )
    else:
        response += "\nПропущенных строк: нет."
    return response


async def _enrich_and_notify(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    medicine_names: tuple[str, ...],
) -> None:
    try:
        result = await asyncio.to_thread(enrich_medicine_info, medicine_names)
    except RuntimeError as error:
        LOGGER.error("Поиск сведений не запущен: %s", error)
        await context.bot.send_message(
            chat_id=chat_id,
            text="Поиск не запущен: добавьте BRAVE_SEARCH_API_KEY в .env и перезапустите контейнер.",
        )
        return
    except Exception:
        LOGGER.exception("Не удалось завершить поиск сведений о препаратах.")
        await context.bot.send_message(
            chat_id=chat_id,
            text="Препараты сохранены, но поиск дополнительной информации завершился ошибкой.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Поиск дополнительной информации завершен.\n"
            f"Инструкций: {result.instructions}\n"
            f"Изображений: {result.photos}\n"
            f"Изготовителей: {result.manufacturers}\n"
            f"Без результатов: {result.no_results}\n"
            f"Ошибок API: {result.errors}"
        ),
    )


async def _set_commands_with_retry(
    application: Application,
    commands: list[BotCommand],
    scope: BotCommandScopeDefault | BotCommandScopeChat,
    scope_name: str,
) -> bool:
    try:
        current_commands = await application.bot.get_my_commands(scope=scope)
    except Exception:
        LOGGER.exception("Не удалось проверить команды Telegram для %s.", scope_name)
        return False

    if tuple(current_commands) == tuple(commands):
        LOGGER.info("Команды Telegram уже актуальны для %s.", scope_name)
        return True

    while True:
        try:
            await application.bot.set_my_commands(commands, scope=scope)
        except RetryAfter as error:
            delay = int(error.retry_after) + 1
            LOGGER.warning(
                "Telegram ограничил регистрацию команд для %s; повтор через %s секунд.",
                scope_name,
                delay,
            )
            await asyncio.sleep(delay)
        except Exception:
            LOGGER.exception("Не удалось зарегистрировать команды Telegram для %s.", scope_name)
            return False
        else:
            LOGGER.info("Команды Telegram зарегистрированы для %s.", scope_name)
            return True


async def configure_commands(application: Application) -> None:
    admin_id = _admin_id()
    if not await _set_commands_with_retry(
        application,
        [BotCommand("start", "Запуск бота")],
        BotCommandScopeDefault(),
        "всех пользователей",
    ):
        return

    await _set_commands_with_retry(
        application,
        [
            BotCommand("start", "Начать работу"),
            BotCommand("medicines", "Управление препаратами"),
        ],
        BotCommandScopeChat(chat_id=admin_id),
        "администратора",
    )


async def startup(application: Application) -> None:
    global _command_setup_task
    _command_setup_task = asyncio.create_task(
        configure_commands(application),
        name="configure-telegram-commands",
    )


async def shutdown(application: Application) -> None:
    global _command_setup_task
    if _command_setup_task is not None:
        _command_setup_task.cancel()
        try:
            await _command_setup_task
        except asyncio.CancelledError:
            pass
        _command_setup_task = None


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        LOGGER.error("Telegram update завершился неизвестной ошибкой.")
        return
    LOGGER.error(
        "Ошибка обработки Telegram update.",
        exc_info=(type(error), error, error.__traceback__),
    )


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("В файле .env не задан BOT_TOKEN.")
    admin_id = _admin_id()

    application = ApplicationBuilder().token(token).post_init(startup).post_shutdown(shutdown).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("medicines", medicines))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^medicines:\d+$"))
    application.add_handler(
        CallbackQueryHandler(manual_remainder_callback, pattern=r"^remainder:select:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            finish_manual_remainder_callback,
            pattern=r"^remainder:finish$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            medicine_creation_callback,
            pattern=r"^medicine:create:(back|skip|save)$",
        )
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_action))
    application.add_handler(MessageHandler(filters.Document.ALL, receive_workbook))
    application.add_error_handler(handle_error)
    LOGGER.info("Бот запускается; Telegram ID администратора: %s", admin_id)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
