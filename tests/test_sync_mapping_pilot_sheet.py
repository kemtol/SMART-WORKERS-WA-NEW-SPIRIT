import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import sync_mapping_pilot_sheet as pilot_sync


class PilotCallsignMappingTest(unittest.TestCase):
    def test_callsign_is_added_to_match_keys(self):
        rows = [
            {
                "pilot_id": "29",
                "_match_keys": ["EV", "EGIE VISTANTYO"],
                "match_keys": "EV, EGIE VISTANTYO",
                "call_sign": "",
                "cpl_number": "",
                "callsign_match_method": "",
                "callsign_source": "",
            }
        ]
        payload = {
            "source": {"name": "CALL SIGN SCREW SCA.pdf"},
            "mappings": [
                {
                    "pilot_id": "29",
                    "call_sign": "EVT",
                    "cpl_number": "19-0094",
                    "match_method": "exact_name",
                }
            ],
            "unmatched": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "callsigns.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            stats = pilot_sync.apply_callsign_mappings(rows, path)

        self.assertEqual(stats["mapped"], 1)
        self.assertEqual(rows[0]["call_sign"], "EVT")
        self.assertEqual(rows[0]["cpl_number"], "19-0094")
        self.assertEqual(rows[0]["_match_keys"][0], "EVT")


if __name__ == "__main__":
    unittest.main()
