"""Usage log: rows per user, counters, status, bot+app writing the same tab."""
import datetime as dt
import re
import unittest

import gspread

from app import usage


def _cell(ref: str):
    col, row = re.fullmatch(r"([A-Z])(\d+)", ref).groups()
    return int(row) - 1, ord(col) - ord("A")


class FakeTab:
    def __init__(self):
        self.rows = [list(usage.HEADER)]
        self.spreadsheet = self

    def get_all_values(self):
        return [list(map(str, r)) for r in self.rows]

    def update(self, ref, values):
        self.rows = [list(v) for v in values]

    def batch_update(self, updates, value_input_option=None):
        for item in updates:
            first = item["range"].split(":")[0]
            r, c = _cell(first)
            for dr, values in enumerate(item["values"]):
                while len(self.rows) <= r + dr:
                    self.rows.append([""] * len(usage.HEADER))
                row = self.rows[r + dr]
                row += [""] * (len(usage.HEADER) - len(row))
                for dc, v in enumerate(values):
                    row[c + dc] = v

    def append_rows(self, rows, value_input_option=None):
        self.rows += [list(r) for r in rows]

    # spreadsheet interface
    def worksheet(self, name):
        if name != usage.USERS_TAB:
            raise gspread.WorksheetNotFound(name)
        return self

    def add_worksheet(self, title, rows, cols):
        return self


def person(uid=1, username="anna", first="Anna", language="ru", **extra):
    return {"id": uid, "username": username, "first_name": first, "language_code": language, **extra}


class UsageLogTests(unittest.TestCase):
    def setUp(self):
        self.tab = FakeTab()
        self.log = usage.UsageLog(lambda: self.tab)

    def test_new_user_gets_a_row(self):
        self.log.add(person(), usage.SOURCE_BOT, searches=1)
        self.assertEqual(self.log.flush(), 1)
        row = self.tab.rows[1]
        self.assertEqual(row[usage.C_ID], 1)
        self.assertEqual(row[usage.C_NICK], "@anna")
        self.assertEqual(row[usage.C_SOURCE], usage.SOURCE_BOT)
        self.assertEqual(row[usage.C_SEARCHES], 1)
        self.assertEqual(row[usage.C_STATUS], usage.STATUS_ACTIVE)

    def test_events_between_flushes_collapse_into_one_row_and_counters_add_up(self):
        for _ in range(3):
            self.log.add(person(), usage.SOURCE_BOT, searches=1)
        self.log.add(person(), usage.SOURCE_BOT, voices=1)
        self.log.flush()
        self.log.add(person(), usage.SOURCE_BOT, searches=2)
        self.log.flush()
        self.assertEqual(len(self.tab.rows), 2)                     # header + one person
        row = self.tab.rows[1]
        self.assertEqual((int(row[usage.C_SEARCHES]), int(row[usage.C_VOICES])), (5, 1))

    def test_flush_with_nothing_pending_does_not_touch_the_sheet(self):
        self.assertEqual(self.log.flush(), 0)
        self.assertEqual(self.tab.rows, [list(usage.HEADER)])

    def test_bots_are_ignored(self):
        self.log.add(person(is_bot=True), usage.SOURCE_BOT)
        self.assertEqual(self.log.flush(), 0)

    def test_blocking_and_coming_back(self):
        self.log.add(person(), usage.SOURCE_BOT)
        self.log.flush()
        self.log.add(person(), usage.SOURCE_BOT, status=usage.STATUS_BLOCKED)
        self.log.flush()
        self.assertEqual(self.tab.rows[1][usage.C_STATUS], usage.STATUS_BLOCKED)
        self.log.add(person(), usage.SOURCE_BOT, searches=1)        # wrote again → not blocked any more
        self.log.flush()
        self.assertEqual(self.tab.rows[1][usage.C_STATUS], usage.STATUS_ACTIVE)

    def test_profile_refreshes_but_first_visit_and_source_stay(self):
        self.log.add(person(username="anna"), usage.SOURCE_BOT)
        self.log.flush()
        first_visit, source = self.tab.rows[1][usage.C_FIRST], self.tab.rows[1][usage.C_SOURCE]
        self.log.add(person(username="anna_new"), usage.SOURCE_APP, opens=1)
        self.log.flush()
        row = self.tab.rows[1]
        self.assertEqual(row[usage.C_NICK], "@anna_new")
        self.assertEqual((row[usage.C_FIRST], row[usage.C_SOURCE]), (first_visit, source))

    def test_bot_and_app_each_keep_their_own_columns(self):
        bot, app = usage.UsageLog(lambda: self.tab), usage.UsageLog(lambda: self.tab)
        bot.add(person(), usage.SOURCE_BOT, searches=2)
        bot.flush()
        app.record_now(person(), usage.SOURCE_APP, opens=1)
        bot.add(person(), usage.SOURCE_BOT, searches=1, voices=1)
        bot.flush()
        app.record_now(person(), usage.SOURCE_APP, opens=1)
        row = self.tab.rows[1]
        self.assertEqual(len(self.tab.rows), 2)
        self.assertEqual((int(row[usage.C_SEARCHES]), int(row[usage.C_VOICES]), int(row[usage.C_OPENS])), (3, 1, 2))
        self.assertEqual(row[usage.C_SOURCE], usage.SOURCE_BOT)     # first contact wins

    def test_failed_flush_keeps_events(self):
        self.log.add(person(), usage.SOURCE_BOT, searches=1)
        original = self.tab.append_rows
        self.tab.append_rows = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota"))
        with self.assertRaises(RuntimeError):
            self.log.flush()
        self.tab.append_rows = original
        self.log.add(person(), usage.SOURCE_BOT, searches=1)        # arrived during the outage
        self.assertEqual(self.log.flush(), 1)
        self.assertEqual(int(self.tab.rows[1][usage.C_SEARCHES]), 2)

    def test_user_without_nick(self):
        self.log.add(person(username=None), usage.SOURCE_BOT)
        self.log.flush()
        self.assertEqual(self.tab.rows[1][usage.C_NICK], "")


class SummaryTests(unittest.TestCase):
    NOW = dt.datetime(2026, 11, 10, 12, 0, 0)

    @staticmethod
    def row(uid, first, last, source="бот", searches=0, voices=0, opens=0, status=usage.STATUS_ACTIVE):
        return [str(uid), "", "", "", first, last, source, str(searches), str(voices), str(opens), "", status]

    def test_totals_and_windows(self):
        rows = [usage.HEADER,
                self.row(1, "2026-11-10 08:00:00", "2026-11-10 09:00:00", searches=4, voices=1),
                self.row(2, "2026-11-05 08:00:00", "2026-11-06 09:00:00", source="приложение", opens=3),
                self.row(3, "2026-10-20 08:00:00", "2026-10-21 09:00:00", status=usage.STATUS_BLOCKED),
                self.row(4, "2026-08-01 08:00:00", "2026-08-02 09:00:00")]
        s = usage.summarize(rows, self.NOW)
        self.assertEqual((s["total"], s["new_1d"], s["new_7d"], s["new_30d"]), (4, 1, 2, 3))
        self.assertEqual((s["active_7d"], s["blocked"]), (2, 1))
        self.assertEqual((s["from_bot"], s["from_app"]), (3, 1))
        self.assertEqual((s["searches"], s["voices"], s["app_users"], s["app_opens"]), (4, 1, 1, 3))

    def test_same_person_on_two_rows_counts_once(self):
        rows = [usage.HEADER,
                self.row(7, "2026-11-10 08:00:00", "2026-11-10 08:00:00", source="приложение", opens=1),
                self.row(7, "2026-11-10 08:00:01", "2026-11-10 08:30:00", searches=2)]
        s = usage.summarize(rows, self.NOW)
        self.assertEqual((s["total"], s["searches"], s["app_opens"]), (1, 2, 1))
        self.assertEqual(s["from_app"], 1)                          # earliest row decides the source

    def test_empty_and_garbage_rows(self):
        self.assertEqual(usage.summarize([usage.HEADER], self.NOW)["total"], 0)
        rows = [usage.HEADER, ["", "", ""], ["9", "", "", "", "not a date", "", "бот"]]
        self.assertEqual(usage.summarize(rows, self.NOW)["total"], 1)


if __name__ == "__main__":
    unittest.main()
