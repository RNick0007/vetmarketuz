import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, ReplyKeyboardRemove

from bot import (
    ADD_MEDICINE_BUTTON,
    FINISH_MANUAL_REMAINDER_BUTTON,
    MANUAL_REMAINDER_BUTTON,
    MENU_BUTTONS,
    REMAINDER_BUTTON,
    _format_remainder_report,
    _process_menu_action,
    finish_manual_remainder_callback,
    medicine_creation_callback,
    manual_remainder_callback,
    menu_action,
    menu_callback,
    medicines,
)
from src.steps.step_three import MedicineRecord
from src.steps.step_two import RemainderImportResult


class _FakeMessage:
    def __init__(self) -> None:
        self.reply_text = AsyncMock()
        self.reply_photo = AsyncMock()


class MedicinesMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_medicines_shows_all_actions_as_inline_buttons(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
        )

        with patch("bot._admin_id", return_value=42):
            await medicines(update, SimpleNamespace(user_data={}))

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

    async def test_manual_remainder_menu_callback_starts_case_insensitive_search(self) -> None:
        message = _FakeMessage()
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data=f"medicines:{MENU_BUTTONS.index(MANUAL_REMAINDER_BUTTON)}",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )
        context = SimpleNamespace(user_data={})

        with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
            await menu_callback(update, context)

        query.answer.assert_awaited_once()
        self.assertEqual(context.user_data["manual_remainder_state"], "search")
        self.assertIn("начальных букв", message.reply_text.await_args.args[0])

        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.search_medicines_by_prefix", return_value=()) as search,
        ):
            update = SimpleNamespace(
                effective_message=SimpleNamespace(text="тЕсТ", reply_text=AsyncMock()),
                effective_user=SimpleNamespace(id=42),
            )
            await menu_action(update, context)

        search.assert_called_once_with("тЕсТ")

    async def test_new_medicine_form_keeps_draft_until_save_and_returns_to_review(self) -> None:
        message = _FakeMessage()
        context = SimpleNamespace(user_data={})
        menu_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data=f"medicines:{MENU_BUTTONS.index(ADD_MEDICINE_BUTTON)}",
        )
        menu_update = SimpleNamespace(
            callback_query=menu_query,
            effective_user=SimpleNamespace(id=42),
        )

        with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
            await menu_callback(menu_update, context)

        menu_query.answer.assert_awaited_once()
        first_keyboard = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(first_keyboard.inline_keyboard[0][0].text, "Назад")

        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
        )
        answers_before_optional = (
            "Test medicine",
            "bottle",
            "05.12.2027",
        )
        for answer in answers_before_optional:
            message.text = answer
            await menu_action(update, context)

        for _ in range(2):
            skip_query = SimpleNamespace(
                answer=AsyncMock(),
                message=message,
                data="medicine:create:skip",
            )
            skip_update = SimpleNamespace(
                callback_query=skip_query,
                effective_user=SimpleNamespace(id=42),
            )
            with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
                await medicine_creation_callback(skip_update, context)

        self.assertIsNone(context.user_data["medicine_creation"]["draft"]["Instruction"])
        self.assertIsNone(context.user_data["medicine_creation"]["draft"]["Photo"])

        answers_after_optional = (
            "12.5",
            "Test manufacturer",
            "2.25",
            "Original seller",
        )
        for answer in answers_after_optional:
            message.text = answer
            await menu_action(update, context)

        self.assertEqual(context.user_data["medicine_creation"]["step"], "review")
        self.assertIn("Original seller", message.reply_text.await_args.args[0])

        back_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:create:back",
        )
        back_update = SimpleNamespace(
            callback_query=back_query,
            effective_user=SimpleNamespace(id=42),
        )
        with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
            await medicine_creation_callback(back_update, context)

        self.assertIn("Введите предприятие-продавца", message.reply_text.await_args.args[0])
        message.text = "Corrected seller"
        await menu_action(update, context)
        self.assertIn("Corrected seller", message.reply_text.await_args.args[0])
        self.assertEqual(context.user_data["medicine_creation"]["step"], "review")

        save_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:create:save",
        )
        save_update = SimpleNamespace(
            callback_query=save_query,
            effective_user=SimpleNamespace(id=42),
        )
        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.save_medicine", return_value=1) as save,
        ):
            await medicine_creation_callback(save_update, context)

        save.assert_called_once()
        self.assertEqual(save.call_args.args[0]["Salesman"], "Corrected seller")
        self.assertNotIn("medicine_creation", context.user_data)
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "Информация о препарате записана в базу данных.",
        )

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

    async def test_manual_remainder_search_select_and_update(self) -> None:
        medicine = MedicineRecord(
            id=5,
            name="Тестовый препарат",
            unit="флакон",
            before="01.01.2028",
            instruction="https://example.test/instruction",
            photo="https://example.test/photo.jpg",
            price=12.5,
            manufacturer="ВетФарм",
            remainder=1,
            salesman="Vetco",
        )
        message = _FakeMessage()
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
        )

        with patch("bot._admin_id", return_value=42):
            await _process_menu_action(message, context, 42, MANUAL_REMAINDER_BUTTON)

        self.assertEqual(context.user_data["manual_remainder_state"], "search")

        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.search_medicines_by_prefix", return_value=(medicine,)),
        ):
            update.effective_message.text = "Тест"
            await menu_action(update, context)

        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].text, medicine.name)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "remainder:select:5")

        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="remainder:select:5",
        )
        callback_update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )
        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.get_medicine", return_value=medicine),
        ):
            await manual_remainder_callback(callback_update, context)

        message.reply_photo.assert_awaited_once_with(photo=medicine.photo)
        self.assertIn("Производитель: ВетФарм", message.reply_text.await_args_list[-2].args[0])
        self.assertEqual(
            message.reply_text.await_args_list[-2].kwargs["link_preview_options"],
            LinkPreviewOptions(is_disabled=True),
        )
        self.assertEqual(context.user_data["manual_remainder_state"], "quantity")

        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.update_medicine_remainder", return_value=medicine.name) as update_remainder,
        ):
            update.effective_message.text = "2,5"
            await menu_action(update, context)

        update_remainder.assert_called_once_with(5, 2.5)
        self.assertIn("Остаток препарата Тестовый препарат: 2.5", message.reply_text.await_args.args[0])
        self.assertIn("Введите начальные буквы следующего препарата", message.reply_text.await_args.args[0])
        self.assertEqual(context.user_data["manual_remainder_state"], "search")
        self.assertNotIn("manual_remainder_medicine_id", context.user_data)
        finish_markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            finish_markup.inline_keyboard[0][0].text,
            FINISH_MANUAL_REMAINDER_BUTTON,
        )

        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.search_medicines_by_prefix", return_value=()) as search,
        ):
            update.effective_message.text = "Следующий"
            await menu_action(update, context)

        search.assert_called_once_with("Следующий")

        finish_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="remainder:finish",
        )
        finish_update = SimpleNamespace(
            callback_query=finish_query,
            effective_user=SimpleNamespace(id=42),
        )
        with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
            await finish_manual_remainder_callback(finish_update, context)

        finish_query.answer.assert_awaited_once()
        self.assertNotIn("manual_remainder_state", context.user_data)
        self.assertEqual(message.reply_text.await_args.args[0], "Ввод остатков завершен.")


if __name__ == "__main__":
    unittest.main()
