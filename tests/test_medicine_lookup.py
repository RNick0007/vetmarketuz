import os
import unittest
from unittest.mock import Mock, patch

from src.databases.steps.medicine_lookup import (
    BRAVE_IMAGE_SEARCH_URL,
    BRAVE_WEB_SEARCH_URL,
    lookup_medicine_info,
)


class MedicineLookupTests(unittest.TestCase):
    @patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key"})
    @patch("src.databases.steps.medicine_lookup.requests.get")
    def test_reads_instruction_manufacturer_and_image(self, mock_get: Mock) -> None:
        def response_for_url(url: str, **kwargs: object) -> Mock:
            response = Mock()
            response.raise_for_status.return_value = None
            if url == BRAVE_WEB_SEARCH_URL:
                response.json.return_value = {
                    "web": {
                        "results": [
                            {
                                "title": "Инструкция по применению",
                                "url": "https://manufacturer.example/instruction",
                                "description": "Производитель: ООО ВетФарм",
                            }
                        ]
                    }
                }
            else:
                response.json.return_value = {
                    "results": [
                        {
                            "title": "Another medicine, 20 ml",
                            "properties": {"url": "https://images.example/unrelated.jpg"},
                            "thumbnail": {"src": "https://images.example/unrelated-thumb.jpg"},
                        },
                        {
                            "title": "Тестовый препарат, 20 мл",
                            "properties": {"url": "https://images.example/medicine.jpg"},
                            "thumbnail": {"src": "https://images.example/thumb.jpg"},
                        }
                    ]
                }
            return response

        mock_get.side_effect = response_for_url

        result = lookup_medicine_info("Тестовый препарат")

        self.assertEqual(result.instruction, "https://manufacturer.example/instruction")
        self.assertEqual(result.photo, "https://images.example/medicine.jpg")
        self.assertEqual(result.manufacturer, "ООО ВетФарм")
        self.assertEqual(result.errors, 0)
        self.assertEqual(mock_get.call_count, 2)
        web_call = next(
            call for call in mock_get.call_args_list if call.args[0] == BRAVE_WEB_SEARCH_URL
        )
        image_call = next(
            call for call in mock_get.call_args_list if call.args[0] == BRAVE_IMAGE_SEARCH_URL
        )
        self.assertEqual(web_call.kwargs["params"]["q"], "Тестовый препарат инструкция")
        self.assertEqual(image_call.kwargs["params"]["safesearch"], "strict")
        for call in mock_get.call_args_list:
            self.assertEqual(call.kwargs["headers"]["X-Subscription-Token"], "test-key")

    @patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""})
    def test_requires_brave_api_key(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "BRAVE_SEARCH_API_KEY"):
            lookup_medicine_info("Тестовый препарат")


if __name__ == "__main__":
    unittest.main()
