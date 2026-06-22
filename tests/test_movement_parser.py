import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from movement_parser import parse_movements


MAPPING = {
    "airports": {
        "PLM": {
            "iata": "PLM",
            "icao": "WIPP",
            "name": "Sultan Mahmud Badaruddin II International Airport",
            "municipality": "Palembang",
            "source": "test",
        }
    },
    "aliases": {"SMH": "PLM"},
}


class FirePatrolMovementParserTest(unittest.TestCase):
    def test_departure_movement_sortie_uses_atd_as_takeoff(self):
        text = """
        DEPARTURE MOVEMENT SORTIE 1
        minggu, 21 juni 2026
        ENG. ON :04:01z/11:01LT
        ATD SMH :04:09z/11:09LT
        ETA SMH :07:29z/14:29LT
        Crew:
        PIC : Capt. Afif N
        SIC : FO. Kevin Lim
        Rute:
        PLM – indralaya utara - PLM
        PT. MSP/OPS-PLM FIRE PATROL/PK-SCH
        """
        rows = parse_movements(text, MAPPING)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["movement_type"], "departure")
        self.assertEqual(rows[0]["operation_date"], "2026-06-21")
        self.assertEqual(rows[0]["registration"], "PK-SCH")
        self.assertEqual(rows[0]["flight_seq"], "01")
        self.assertEqual(rows[0]["engine_start_time"], "04:01")
        self.assertEqual(rows[0]["takeoff_time"], "04:09")
        self.assertEqual(rows[0]["pic_name"], "Capt. Afif N")
        self.assertEqual(rows[0]["sic_name"], "FO. Kevin Lim")
        self.assertEqual(rows[-1]["leg_destination_code"], "PLM")
        self.assertEqual(rows[-1]["eta_time"], "07:29")

    def test_arrival_movement_sortie_maps_smh_to_plm(self):
        text = """
        ARRIVAL MOVEMENT SORTIE 1
        minggu, 21 juni 2026
        ATA SMH :07:24z/14:24LT
        PIC : Capt. Afif N
        SIC : FO. Kevin Lim
        Rute:
        PLM - indralaya utara - PLM
        D 14 Nm From PLB
        PT. MSP/OPS-PLM FIRE PATROL/PK-SCH
        """
        rows = parse_movements(text, MAPPING)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["movement_type"], "arrival")
        self.assertEqual(rows[0]["flight_seq"], "01")
        self.assertEqual(rows[0]["ata_airport_code"], "PLM")
        self.assertEqual(rows[0]["ata_time"], "07:24")
        self.assertIsNone(rows[0]["from_place"])
        self.assertEqual(rows[0]["route_full"], "PLM - indralaya utara - PLM")


if __name__ == "__main__":
    unittest.main()
