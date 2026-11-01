import asyncio
import logging
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import RetryAfter
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
from src.steps.step_two import RemainderImportResult, import_remainders_workbook


LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
IMPORT_BUTTON = "Автоматический ввод препаратов"
REMAINDER_BUTTON = "Автоматический ввод остатков"
MENU_BUTTONS = (
    IMPORT_BUTTON,
    REMAINDER_BUTTON,
    "Ручное изменение остатков",
    "Ввод нового препарата",
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
    if action not in MENU_BUTTONS:
        return

    await _process_menu_action(message, context, user.id, action)


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
        context.user_data["awaiting_workbook_action"] = "medicines"
        await message.reply_text("Отправьте документ .xlsx, .xls или .ods без строки заголовков.")
        return

    if action == REMAINDER_BUTTON:
        context.user_data["awaiting_workbook_action"] = "remainders"
        await message.reply_text(
            "Отправьте документ .xlsx, .xls или .ods без строки заголовков. "
            "Наименования должны быть в столбце A, остатки — в столбце B."
        )
        return

    context.user_data.pop("awaiting_workbook_action", None)
    await message.reply_text("Эта функция будет реализована по отдельному техническому заданию.")


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
