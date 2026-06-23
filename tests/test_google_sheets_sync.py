import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from google_sheets_sync import safe_sheet_cell


class GoogleSheetsSyncTest(unittest.TestCase):
    def test_long_cell_is_truncated_with_audit_marker(self):
        result = safe_sheet_cell("x" * 120, "payload_json", max_chars=80)

        self.assertEqual(len(result), 80)
        self.assertIn("payload_json truncated from 120 chars", result)

    def test_short_cell_is_unchanged(self):
        self.assertEqual(safe_sheet_cell("normal", "text", max_chars=80), "normal")


if __name__ == "__main__":
    unittest.main()
