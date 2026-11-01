import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup, ReplyKeyboardRemove

from bot import (
    MENU_BUTTONS,
    REMAINDER_BUTTON,
    _format_remainder_report,
    _process_menu_action,
    medicines,
)
from src.steps.step_two import RemainderImportResult


class MedicinesMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_medicines_shows_all_actions_as_inline_buttons(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
        )

        with patch("bot._admin_id", return_value=42):
            await medicines(update, SimpleNamespace())

        self.assertEqual(message.reply_text.await_count, 2)
        self.assertIsInstance(
            message.reply_text.await_args_list[0].kwargs["reply_markup"],
            ReplyKeyboardRemove,
        )
        markup = message.reply_text.await_args_list[1].kwargs["reply_markup"]
        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(len(markup.inline_keyboard), 7)
        self.assertEqual(
            [row[0].text for row in markup.inline_keyboard],
            list(MENU_BUTTONS),
        )
        self.assertEqual(
            len({row[0].callback_data for row in markup.inline_keyboard}),
            7,
        )

    async def test_remainder_button_requests_inventory_workbook(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        context = SimpleNamespace(user_data={})

        with patch("bot._admin_id", return_value=42):
            await _process_menu_action(message, context, 42, REMAINDER_BUTTON)

        self.assertEqual(context.user_data["awaiting_workbook_action"], "remainders")
        self.assertIn("столбце B", message.reply_text.await_args.args[0])

    def test_remainder_report_explicitly_shows_missing_and_skipped_rows(self) -> None:
        report = _format_remainder_report(
            RemainderImportResult(
                changed=3,
                not_found=(),
                errors=("строка 150: остаток 62.5 должен быть целым числом ('Coglavax 250 ml')",),
            )
        )

        self.assertIn("Изменено значений остатков: 3", report)
        self.assertIn("Не найдены в базе данных: нет.", report)
        self.assertIn("Пропущенные строки (1):", report)
        self.assertIn("Coglavax 250 ml", report)


if __name__ == "__main__":
    unittest.main()
