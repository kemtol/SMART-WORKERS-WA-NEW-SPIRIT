import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import build_sortie_log as sortie


class SortieLogTest(unittest.TestCase):
    def test_route_metadata_repairs_concatenated_airport_codes(self):
        self.assertEqual(
            sortie.route_metadata("MKQ-MGLTMH"),
            {"route_type": "multi_leg", "from": "MKQ", "to": "TMH", "via": "MGL"},
        )

    def test_route_metadata_counts_out_and_back_as_one_route(self):
        self.assertEqual(
            sortie.route_metadata("TIM-BEO-TIM"),
            {"route_type": "out_and_back", "from": "TIM", "to": "TIM", "via": "BEO"},
        )

    def test_pax_total_includes_foc_notes(self):
        self.assertEqual(sortie.parse_pax_total("09/01/00(1 FOC SCA)"), (11, "COMPLETE"))
        self.assertEqual(
            sortie.parse_pax_total("07/00/00(1 FOC SCA & 2 FOC UPBU)"),
            (10, "COMPLETE"),
        )

    def test_pax_quality_marks_nonstandard_text(self):
        self.assertEqual(sortie.parse_pax_total("seven adults"), (None, "NEEDS_REVIEW"))

    def test_unique_missing_flight_sequence_can_match_final_destination(self):
        event_time = datetime(2026, 6, 13, 2, 20, tzinfo=timezone.utc)
        departure = {
            "id": 1,
            "raw_message_id": 10,
            "operation_date_resolved": "2026-06-13",
            "registration": "PK-SNH",
            "flight_seq": None,
            "route_full": "AAP-LPU-AAP",
            "event_datetime_utc": event_time,
        }
        arrival = {
            "id": 2,
            "raw_message_id": 11,
            "operation_date_resolved": "2026-06-13",
            "registration": "PK-SNH",
            "flight_seq": "02",
            "arrival_airport_code": "AAP",
            "ata_airport_code": "AAP",
            "leg_destination_code": "AAP",
            "event_datetime_utc": event_time,
        }
        matches, used = sortie.match_arrivals([departure], [arrival])
        self.assertEqual(matches[1]["id"], 2)
        self.assertEqual(used, {2})

    def test_route_mismatch_is_not_accepted_as_ack(self):
        event_time = datetime(2026, 6, 13, 2, 20, tzinfo=timezone.utc)
        departure = {
            "id": 1,
            "raw_message_id": 10,
            "operation_date_resolved": "2026-06-13",
            "registration": "PK-SNH",
            "flight_seq": "02",
            "route_full": "AAP-LPU-AAP",
            "event_datetime_utc": event_time,
        }
        arrival = {
            "id": 2,
            "raw_message_id": 11,
            "operation_date_resolved": "2026-06-13",
            "registration": "PK-SNH",
            "flight_seq": "02",
            "arrival_airport_code": "LPU",
            "ata_airport_code": "LPU",
            "leg_destination_code": "LPU",
            "event_datetime_utc": event_time,
        }
        matches, used = sortie.match_arrivals([departure], [arrival])
        self.assertEqual(matches, {})
        self.assertEqual(used, set())

    def test_call_sign_can_match_across_operational_rank(self):
        pilot = {"pilot_id": "68", "pilot_name": "Capt. Tegar Bintang Haryo Lukito"}
        full_name, issue = sortie.match_pilot("Fo. TBH", "FO", {("", "TBH"): [pilot]})
        self.assertEqual(full_name, pilot["pilot_name"])
        self.assertIsNone(issue)


if __name__ == "__main__":
    unittest.main()
