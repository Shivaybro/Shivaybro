#!/usr/bin/env python3
"""
Tests for parse_schedule_text.py -- run with:

    python3 tools/test_parse_schedule_text.py -v
"""

import datetime as dt
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import parse_schedule_text as P  # noqa: E402

FLAT = """День недели\tВремя\tДисциплина\tПреподаватель\tАудитория\tГруппа
Понедельник\t8.30-10.00\tАнатомия человека\tИванов И.И.\tауд. 312\t2421
Понедельник\t8.30-10.00\tАнатомия человека\tИванов И.И.\tауд. 312\t2422
Понедельник\t10.10-11.40\tБиохимия\tдоц. Петрова А.В.\tауд. 208\t2422
Вторник\t8.30-10.00\tФизиология\tСидоров П.Н.\tауд. 105\t2422
Вторник\t12.00-13.30\tГистология\tКузнецова М.И.\tкаб. 44а\t2422
Среда\t10.10-11.40\tМикробиология\tпроф. Смирнов Д.К.\tауд. 501\t2422
"""

BLOCK = """Группа 2421
Пн 8.30-10.00 Анатомия человека Иванов И.И. ауд. 312
Группа 2422
Пн 8.30-10.00 Анатомия человека Иванов И.И. ауд. 312
Пн 10.10-11.40 Биохимия Петрова А.В. ауд. 208
Вт 8.30-10.00 Физиология Сидоров П.Н. ауд. 105
Группа 2423
Пн 8.30-10.00 Химия Орлов С.С. ауд. 777
"""

MESSY = """2421 Пн 8.30-10.00 Анатомия человека Иванов И.И. ауд. 312
2422 Пн 8.30-10.00 Анатомия человека Иванов И.И. ауд. 312
2422 Пн 10.10-11.40 Биохимия доц. Петрова А.В. ауд. 208
2422 Вт 8.30-10.00 Физиология Сидоров П.Н. ауд. 105
2422 Ср 10.10-11.40 Микробиология проф. Смирнов Д.К. ауд. 501
"""

NUMDEN = """2422 Понедельник числитель 8.30-10.00 Анатомия человека Иванов И.И. ауд. 312
2422 Понедельник знаменатель 8.30-10.00 Биохимия Петрова А.В. ауд. 208
"""

START = dt.date(2026, 9, 14)


def parse(text, group="2422"):
    rows = P.collect_group_lines(text, group)
    assert rows, "no rows collected"
    layout = P.find_header_layout(rows)
    if layout:
        records = P.parse_positional(rows[layout[0] + 1:], layout[1])
        if len(records) >= 2:
            P.assign_pairs(records)
            return records
    records = P.parse_heuristic(rows)
    records = [r for r in records if r["subject"] or r["time"]]
    P.assign_pairs(records)
    return records


class TestGroupRegex(unittest.TestCase):
    def test_matches_group_forms(self):
        for token in ("2422", "24-22", "24–22"):
            self.assertTrue(P.GROUP_RE.search(f"Группа {token}"), token)

    def test_does_not_eat_rooms_or_times(self):
        self.assertIsNone(P.GROUP_RE.search("ауд. 312"))
        self.assertIsNone(P.GROUP_RE.search("8.30-10.00"))

    def test_header_detection(self):
        self.assertIsNotNone(P.HEADER_RE.match("Группа 2422"))
        self.assertIsNotNone(P.HEADER_RE.match("  2422 "))
        self.assertIsNone(P.HEADER_RE.match("2422 Пн 8.30-10.00 Анатомия"))


class TestSelection(unittest.TestCase):
    def test_flat_excludes_other_groups(self):
        rows = P.collect_group_lines(FLAT, "2422")
        self.assertEqual(len(rows), 6)          # header + 5 group rows
        self.assertFalse(any("2421" in r for r in rows))

    def test_block_stops_at_next_header(self):
        rows = P.collect_group_lines(BLOCK, "2422")
        self.assertEqual(len(rows), 4)          # "Группа 2422" + 3 rows
        self.assertNotIn("Химия", " ".join(rows))

    def test_missing_group_returns_empty(self):
        self.assertEqual(P.collect_group_lines(FLAT, "9999"), [])


class TestParsing(unittest.TestCase):
    def test_messy_heuristic(self):
        recs = parse(MESSY)
        self.assertEqual(len(recs), 4)
        self.assertEqual([r["room"] for r in recs], ["312", "208", "105", "501"])
        self.assertEqual([r["subject"] for r in recs],
                         ["Анатомия человека", "Биохимия", "Физиология",
                          "Микробиология"])
        self.assertEqual([r["day"] for r in recs],
                         ["Monday", "Monday", "Tuesday", "Wednesday"])
        self.assertEqual(recs[0]["teacher"], "Иванов И.И.")

    def test_flat_positional(self):
        recs = parse(FLAT)
        self.assertEqual(len(recs), 5)
        self.assertEqual(recs[0]["room"], "312")
        self.assertEqual(recs[0]["subject"], "Анатомия человека")

    def test_block_heuristic(self):
        recs = parse(BLOCK)
        self.assertEqual(len(recs), 3)

    def test_time_normalised(self):
        recs = parse(MESSY)
        self.assertEqual(recs[0]["time"], "08:30–10:00")

    def test_parity_detected(self):
        recs = parse(NUMDEN)
        self.assertEqual([r["parity"] for r in recs], ["числитель", "знаменатель"])

    def test_pairs_assigned_from_times(self):
        recs = parse(NUMDEN)
        self.assertEqual([r["pair"] for r in recs], ["1", "1"])


class TestOutput(unittest.TestCase):
    def test_parity_routes_to_correct_week(self):
        recs = parse(NUMDEN)
        with tempfile.TemporaryDirectory() as d:
            csv_path, _ = P.write_outputs(recs, START, 2, pathlib.Path(d), "2422",
                                          ("числитель", "знаменатель"))
            body = csv_path.read_text(encoding="utf-8-sig")
        self.assertIn("2026-09-14,Monday,W1", body)      # числитель -> week 1
        self.assertIn("2026-09-21,Monday,W2", body)      # знаменатель -> week 2
        self.assertNotIn("2026-09-21,Monday,W2,1,08:30–10:00,Анатомия", body)

    def test_no_weekday_is_not_fanned_out(self):
        recs = [{"day": "", "pair": "1", "time": "08:30–10:00",
                 "subject": "Анатомия", "teacher": "", "room": "312",
                 "parity": ""}]
        with tempfile.TemporaryDirectory() as d:
            csv_path, _ = P.write_outputs(recs, START, 2, pathlib.Path(d), "2422",
                                          ("", ""))
            body = csv_path.read_text(encoding="utf-8-sig")
        self.assertEqual(len(body.strip().splitlines()), 2)   # header + 1 row

    def test_window_covers_fourteen_days(self):
        recs = parse(NUMDEN)
        with tempfile.TemporaryDirectory() as d:
            csv_path, md_path = P.write_outputs(recs, START, 2, pathlib.Path(d),
                                                "2422", ("", ""))
            md = md_path.read_text(encoding="utf-8")
        self.assertIn("14.09.2026 – 20.09.2026", md)
        self.assertIn("21.09.2026 – 27.09.2026", md)
        self.assertEqual(csv_path.name, "group_2422_schedule.csv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
