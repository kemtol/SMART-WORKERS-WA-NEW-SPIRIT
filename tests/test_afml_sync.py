import json
import sqlite3
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from afml_sync import (
    circular_time_delta,
    parse_afml_detail,
    parse_afml_list_response,
    init_schema,
    reconcile,
    route_tokens,
    sequence_candidates,
)


class AfmlParserTest(unittest.TestCase):
    def test_parse_list_response(self):
        html = """
        <table><tbody><tr>
          <th><a href="https://ams.smartaviation.co.id/v1/AFML/detail/MzA2ODY=">SCI-260623-020</a></th>
          <td>YOSEP A MAYAU</td><td>23 Jun 2026</td><td>PK-SCI</td>
          <td>04:21</td><td>05:20</td><td>Dandra<br>23/06/2026</td>
          <td>CHARTER</td><td>No</td><td>Upload</td><td>Logs Edit</td>
        </tr></tbody></table>
        """

        rows = parse_afml_list_response(json.dumps({"result": 1, "div": html}))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["afml_id"], 30686)
        self.assertEqual(rows[0]["operation_date"], "2026-06-23")
        self.assertEqual(rows[0]["registration"], "PK-SCI")
        self.assertEqual(rows[0]["total_flight_minutes"], 261)
        self.assertEqual(rows[0]["total_block_minutes"], 320)
        self.assertEqual(rows[0]["created_by"], "Dandra")

    def test_parse_detail_and_legs(self):
        html = """
        <table><tr>
          <td>DATE : <span>23/06/2026</span></td><td>A/C REG : <span>PK-SCI</span></td>
          <td>MSN : <span>203</span></td><td>TYPE : <span>P750XL</span></td>
          <td>PAGE : <span>SCI-260623-020</span></td>
        </tr></table>
        <table>
          <tr><td>CAPT : <span>YOSEP A MAYAU</span></td></tr>
          <tr><td>COPIL : <span></span></td></tr>
        </table>
        <table><tr><td>Check In : 20:23</td><td>Check Out : 04:28</td><td>Duty TIme : 08h 05m</td></tr></table>
        <table><tbody>
          <tr><td>TIM</td><td>ARW</td><td>21:08</td><td>21:26</td><td>00</td><td>18</td>
              <td>21:11</td><td>21:24</td><td>00</td><td>13</td><td>1</td><td>1</td>
              <td>0</td><td>0</td><td>350</td><td></td><td>0</td><td>0</td></tr>
          <tr><td>TTL BLOCK TIME</td><td>00</td><td>18</td><td>TTL FLT TIME &amp; LDG</td>
              <td>00</td><td>13</td><td>1</td><td>1</td></tr>
        </tbody></table>
        """

        detail = parse_afml_detail(html, 30686)

        self.assertEqual(detail["operation_date"], "2026-06-23")
        self.assertEqual(detail["registration"], "PK-SCI")
        self.assertEqual(detail["captain_name"], "YOSEP A MAYAU")
        self.assertEqual(detail["total_block_minutes"], 18)
        self.assertEqual(detail["total_flight_minutes"], 13)
        self.assertEqual(detail["route_chain"], "TIM-ARW")
        self.assertEqual(detail["legs"][0]["takeoff_time"], "21:11")

    def test_route_sequence_and_midnight_time_delta(self):
        legs = [
            {"leg_index": 1, "origin_code": "TIM", "destination_code": "ARW"},
            {"leg_index": 2, "origin_code": "ARW", "destination_code": "TIM"},
            {"leg_index": 3, "origin_code": "TIM", "destination_code": "ALA"},
        ]

        candidates = sequence_candidates(legs, route_tokens("TIM-ARW-TIM"))

        self.assertEqual(len(candidates), 1)
        self.assertEqual([leg["leg_index"] for leg in candidates[0]], [1, 2])
        self.assertEqual(circular_time_delta("23:58", "00:02"), 4)

    def test_reconciliation_deduplicates_departure_reposts(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE flight_movements (
                id INTEGER PRIMARY KEY, raw_message_id INTEGER, movement_type TEXT,
                operation_date TEXT, registration TEXT, flight_seq TEXT, route_full TEXT,
                takeoff_time TEXT, pic_name TEXT, sic_name TEXT, leg_index INTEGER,
                parse_confidence REAL
            )
            """
        )
        init_schema(conn)
        movement = (
            "departure", "2026-06-23", "PK-SCI", "01", "TIM-ARW-TIM", "21:11",
            "YOSEP A MAYAU", None, 1, 0.9,
        )
        conn.execute(
            "INSERT INTO flight_movements VALUES (1, 100, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            movement,
        )
        conn.execute(
            "INSERT INTO flight_movements VALUES (2, 101, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            movement,
        )
        conn.execute(
            """
            INSERT INTO afml_records (
                afml_id, encoded_id, page_no, operation_date, registration, captain_name,
                total_flight_minutes, total_block_minutes, leg_count, detail_status,
                list_hash, detail_hash, detail_fetched_at, source_url, raw_list_json,
                first_seen_at, last_seen_at, last_changed_at
            ) VALUES (30686, 'MzA2ODY=', 'SCI-260623-020', '2026-06-23', 'PK-SCI',
                      'YOSEP A MAYAU', 24, 35, 2, 'parsed', 'list', 'detail',
                      '2026-06-23T00:00:00+00:00', 'https://example.test', '{}',
                      '2026-06-23T00:00:00+00:00', '2026-06-23T00:00:00+00:00',
                      '2026-06-23T00:00:00+00:00')
            """
        )
        conn.executemany(
            """
            INSERT INTO afml_legs (
                afml_id, leg_index, origin_code, destination_code, block_off_time,
                block_on_time, block_minutes, takeoff_time, landing_time, flight_minutes
            ) VALUES (30686, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "TIM", "ARW", "21:08", "21:26", 18, "21:11", "21:24", 13),
                (2, "ARW", "TIM", "21:36", "21:53", 17, "21:38", "21:49", 11),
            ],
        )

        rows = reconcile(conn, "2026-06-23", "2026-06-23", pilot_mapping_path="/missing.json")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["departure_raw_message_id"], 101)
        self.assertEqual(rows[0]["match_status"], "MATCHED")


if __name__ == "__main__":
    unittest.main()
