import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram import InlineKeyboardMarkup, LinkPreviewOptions, ReplyKeyboardRemove

from bot import (
    ADD_MEDICINE_BUTTON,
    DELETE_ALL_MEDICINES_BUTTON,
    DELETE_MEDICINE_BUTTON,
    EDIT_MEDICINE_BUTTON,
    FINISH_MANUAL_REMAINDER_BUTTON,
    MANUAL_REMAINDER_BUTTON,
    MEDICINE_FIELDS,
    MENU_BUTTONS,
    REMAINDER_BUTTON,
    _format_remainder_report,
    _process_menu_action,
    finish_manual_remainder_callback,
    medicine_creation_callback,
    medicine_delete_callback,
    medicine_delete_all_callback,
    medicine_edit_callback,
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

    async def test_delete_menu_searches_by_prefix_and_offers_matching_medicines(self) -> None:
        message = _FakeMessage()
        context = SimpleNamespace(user_data={})
        with patch("bot._admin_id", return_value=42):
            await _process_menu_action(message, context, 42, DELETE_MEDICINE_BUTTON)

        self.assertEqual(context.user_data["medicine_delete"], {"step": "search"})
        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.search_medicines_by_prefix", return_value=(MedicineRecord(
                7, "Тестовый препарат", "упаковка", None, None, None, 10, None, 2, None
            ),)) as search,
        ):
            update = SimpleNamespace(
                effective_message=message,
                effective_user=SimpleNamespace(id=42),
            )
            message.text = "тЕсТ"
            await menu_action(update, context)

        search.assert_called_once_with("тЕсТ")
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            markup.inline_keyboard[0][0].callback_data,
            "medicine:delete:select:7",
        )

    async def test_delete_selection_shows_card_and_confirmation_buttons(self) -> None:
        message = _FakeMessage()
        medicine = MedicineRecord(
            7, "Тестовый препарат", "упаковка", None, None, None, 10, None, 2, None
        )
        context = SimpleNamespace(user_data={"medicine_delete": {"step": "search"}})
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:delete:select:7",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )

        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.get_medicine", return_value=medicine),
            patch("bot._send_medicine_card", new_callable=AsyncMock) as send_card,
        ):
            await medicine_delete_callback(update, context)

        send_card.assert_awaited_once_with(message, medicine)
        self.assertEqual(context.user_data["medicine_delete"], {"step": "confirm", "medicine_id": 7})
        self.assertEqual(message.reply_text.await_args.args[0], "Удалить препарат?")
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.text for button in markup.inline_keyboard[0]],
            ["Да", "Нет"],
        )

    async def test_delete_confirmation_yes_deletes_and_no_preserves_data(self) -> None:
        for action, expected_state in (
            ("medicine:delete:confirm:yes", None),
            ("medicine:delete:confirm:no", None),
        ):
            with self.subTest(action=action):
                message = _FakeMessage()
                context = SimpleNamespace(
                    user_data={"medicine_delete": {"step": "confirm", "medicine_id": 7}}
                )
                query = SimpleNamespace(
                    answer=AsyncMock(),
                    message=message,
                    data=action,
                )
                update = SimpleNamespace(
                    callback_query=query,
                    effective_user=SimpleNamespace(id=42),
                )
                with (
                    patch("bot.Message", _FakeMessage),
                    patch("bot._admin_id", return_value=42),
                    patch("bot.delete_medicine", return_value="Тестовый препарат") as delete,
                ):
                    await medicine_delete_callback(update, context)

                self.assertEqual(context.user_data.get("medicine_delete"), expected_state)
                if action.endswith(":yes"):
                    delete.assert_called_once_with(7)
                    self.assertIn("удален", message.reply_text.await_args.args[0])
                else:
                    delete.assert_not_called()
                    self.assertIn("отменено", message.reply_text.await_args.args[0])

    async def test_delete_all_menu_requests_confirmation_and_no_cancels(self) -> None:
        message = _FakeMessage()
        context = SimpleNamespace(user_data={})
        with patch("bot._admin_id", return_value=42):
            await _process_menu_action(message, context, 42, DELETE_ALL_MEDICINES_BUTTON)

        self.assertTrue(context.user_data["medicine_delete_all"])
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "Удалить все препараты из таблицы Medicines?",
        )
        markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.text for button in markup.inline_keyboard[0]],
            ["Да", "Нет"],
        )

        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:delete_all:confirm:no",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )
        with patch("bot.Message", _FakeMessage), patch("bot._admin_id", return_value=42):
            await medicine_delete_all_callback(update, context)

        self.assertNotIn("medicine_delete_all", context.user_data)
        self.assertIn("отменено", message.reply_text.await_args.args[0])

    async def test_delete_all_confirmation_yes_deletes_all_medicines(self) -> None:
        message = _FakeMessage()
        context = SimpleNamespace(user_data={"medicine_delete_all": True})
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:delete_all:confirm:yes",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )

        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.delete_all_medicines", return_value=3) as delete_all,
        ):
            await medicine_delete_all_callback(update, context)

        delete_all.assert_called_once_with()
        self.assertNotIn("medicine_delete_all", context.user_data)
        self.assertIn("Удалено препаратов: 3", message.reply_text.await_args.args[0])

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

    async def test_medicine_edit_search_update_and_confirmation(self) -> None:
        medicine = MedicineRecord(
            id=5,
            name="Тестовый препарат",
            unit="флакон",
            before="01.01.2028",
            instruction=None,
            photo=None,
            price=12.5,
            manufacturer=None,
            remainder=1,
            salesman=None,
        )
        message = _FakeMessage()
        context = SimpleNamespace(user_data={})
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
        )

        with patch("bot._admin_id", return_value=42):
            await _process_menu_action(message, context, 42, EDIT_MEDICINE_BUTTON)
        self.assertEqual(context.user_data["medicine_edit"]["step"], "search")

        with (
            patch("bot._admin_id", return_value=42),
            patch("bot.search_medicines_by_prefix", return_value=(medicine,)) as search,
        ):
            update.effective_message.text = "Тест"
            await menu_action(update, context)

        search.assert_called_once_with("Тест")
        search_markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            search_markup.inline_keyboard[0][0].callback_data,
            "medicine:edit:select:5",
        )

        select_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:edit:select:5",
        )
        select_update = SimpleNamespace(
            callback_query=select_query,
            effective_user=SimpleNamespace(id=42),
        )
        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.get_medicine", return_value=medicine),
            patch("bot._send_medicine_card", new_callable=AsyncMock),
        ):
            await medicine_edit_callback(select_update, context)

        self.assertEqual(context.user_data["medicine_edit"]["step"], "field")
        fields_markup = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertTrue(
            any(
                button.callback_data == "medicine:edit:field:Price"
                for row in fields_markup.inline_keyboard
                for button in row
            )
        )

        field_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:edit:field:Price",
        )
        field_update = SimpleNamespace(
            callback_query=field_query,
            effective_user=SimpleNamespace(id=42),
        )
        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.get_medicine", return_value=medicine),
        ):
            await medicine_edit_callback(field_update, context)

        self.assertEqual(context.user_data["medicine_edit"]["step"], "value")
        update.effective_message.text = "14,50"
        with patch("bot._admin_id", return_value=42):
            await menu_action(update, context)

        self.assertEqual(context.user_data["medicine_edit"]["step"], "confirm")
        confirmation = message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(
            [button.text for button in confirmation.inline_keyboard[0]],
            ["Да", "Нет"],
        )

        confirm_query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:edit:confirm:yes",
        )
        confirm_update = SimpleNamespace(
            callback_query=confirm_query,
            effective_user=SimpleNamespace(id=42),
        )
        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch(
                "bot.update_medicine_field",
                return_value=(
                    next(field for field in MEDICINE_FIELDS if field.key == "Price"),
                    14.5,
                ),
            ) as update_field,
        ):
            await medicine_edit_callback(confirm_update, context)

        update_field.assert_called_once_with(5, "Price", "14,50")
        self.assertNotIn("medicine_edit", context.user_data)
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "Значение поля Цена изменено на 14.5.",
        )

    async def test_medicine_edit_decline_does_not_update_database(self) -> None:
        message = _FakeMessage()
        context = SimpleNamespace(
            user_data={
                "medicine_edit": {
                    "step": "confirm",
                    "medicine_id": 5,
                    "field_key": "Price",
                    "raw_value": "14.50",
                }
            }
        )
        query = SimpleNamespace(
            answer=AsyncMock(),
            message=message,
            data="medicine:edit:confirm:no",
        )
        update = SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=42),
        )

        with (
            patch("bot.Message", _FakeMessage),
            patch("bot._admin_id", return_value=42),
            patch("bot.update_medicine_field") as update_field,
        ):
            await medicine_edit_callback(update, context)

        update_field.assert_not_called()
        self.assertNotIn("medicine_edit", context.user_data)
        self.assertEqual(
            message.reply_text.await_args.args[0],
            "Изменение отменено. Данные не изменены.",
        )


if __name__ == "__main__":
    unittest.main()
