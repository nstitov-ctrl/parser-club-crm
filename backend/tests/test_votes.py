"""Rules of app/votes.py against an in-memory fake of the spreadsheet.

Run from backend/:  python -m unittest tests.test_votes -v
(No network, no real sheet — votes made here never touch production data.)
"""
import unittest
from datetime import date, datetime, timedelta, timezone

import gspread

from app import votes

HEADER = ["Ник", "Имя", "Фамилия", "Направление", "Оригинальный текст объявления",
          "Дополнительные контакты", "Дата публикации", "Отзывы +", "Отзывы -", "ID автора"]


class FakeWS:
    def __init__(self, header, spreadsheet=None):
        self.rows = [list(header)]
        self.spreadsheet = spreadsheet

    def row_values(self, i):
        return list(self.rows[i - 1])

    def get_all_records(self):
        head = self.rows[0]
        return [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in self.rows[1:]]

    def append_row(self, values, value_input_option=None):
        self.rows.append(list(values))

    def update(self, rng, values):
        self.rows[0] = list(values[0])

    def col_values(self, c):
        return [row[c - 1] if c - 1 < len(row) else "" for row in self.rows]

    def cell(self, r, c):
        row = self.rows[r - 1]
        return type("C", (), {"value": row[c - 1] if c - 1 < len(row) else ""})()

    def update_cells(self, cells, value_input_option=None):
        for c in cells:
            row = self.rows[c.row - 1]
            row.extend([""] * (c.col - len(row)))
            row[c.col - 1] = c.value


class FakeSS:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, name):
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return self.tabs[name]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWS([], self)
        return self.tabs[title]


def make(cards):
    ss = FakeSS()
    ws = FakeWS(HEADER, ss)
    ss.tabs["Карточки"] = ws
    for c in cards:
        ws.rows.append([c.get(h, "") for h in HEADER])
    return votes.Ledger(ws), ws


def fresh(ws):
    cards = ws.get_all_records()
    for i, c in enumerate(cards, start=2):
        c["_row"] = i
    return cards


IVAN = {"Ник": "@ivan_test", "Имя": "Иван", "Направление": "Массаж",
        "Дополнительные контакты": "@ivan_test, +62000000", "Отзывы +": 2, "Отзывы -": 1}
USER = {"id": 111, "username": "maria"}


class VoteRules(unittest.TestCase):
    def test_vote_requires_contact_first(self):
        ledger, ws = make([IVAN])
        cid = votes.card_id(fresh(ws)[0])
        with self.assertRaises(votes.VoteError) as e:
            ledger.cast_vote(fresh(ws), cid, USER, True)
        self.assertEqual(e.exception.code, "no_contact")

    def test_vote_after_contact_updates_ledger_and_counters(self):
        ledger, ws = make([IVAN])
        card = fresh(ws)[0]
        self.assertTrue(ledger.record_contact(card, USER))
        self.assertFalse(ledger.record_contact(card, USER), "contact click is idempotent")
        r = ledger.cast_vote(fresh(ws), votes.card_id(card), USER, True)
        self.assertEqual((r["pos"], r["neg"], r["changed"]), (3, 1, True))
        self.assertEqual(ws.rows[1][7:9], [3, 1])           # legacy counters kept, +1 applied

    def test_same_vote_twice_is_not_stacked_and_costs_nothing(self):
        ledger, ws = make([IVAN]); card = fresh(ws)[0]; cid = votes.card_id(card)
        ledger.record_contact(card, USER)
        ledger.cast_vote(fresh(ws), cid, USER, True)
        r = ledger.cast_vote(fresh(ws), cid, USER, True)
        self.assertFalse(r["changed"])
        self.assertEqual(ws.rows[1][7], 3)
        self.assertEqual(r["left"], 2)

    def test_changing_vote_moves_it_between_counters(self):
        ledger, ws = make([IVAN]); card = fresh(ws)[0]; cid = votes.card_id(card)
        ledger.record_contact(card, USER)
        ledger.cast_vote(fresh(ws), cid, USER, True)         # 3 / 1
        r = ledger.cast_vote(fresh(ws), cid, USER, False)    # back to 2 / 2
        self.assertEqual((r["pos"], r["neg"]), (2, 2))
        self.assertEqual(ledger.user_state(USER["id"])["votes"][votes.card_key(card)], -1)

    def test_author_cannot_vote_for_own_card_by_id_or_username(self):
        ledger, ws = make([dict(IVAN, **{"ID автора": 555})])
        card = fresh(ws)[0]; cid = votes.card_id(card)
        for me in ({"id": 555, "username": None}, {"id": 999, "username": "Ivan_Test"}):
            ledger.record_contact(card, me)
            with self.assertRaises(votes.VoteError) as e:
                ledger.cast_vote(fresh(ws), cid, me, True)
            self.assertEqual(e.exception.code, "own")

    def test_author_found_through_extra_contacts_link(self):
        card = {"Ник": "", "Направление": "Йога", "Дополнительные контакты": "https://t.me/yoga_anna, +62111"}
        self.assertTrue(votes.is_own_card(card, {"id": 1, "username": "Yoga_Anna"}))
        self.assertFalse(votes.is_own_card(card, {"id": 1, "username": "someone"}))

    def test_daily_limit_is_three_and_counts_vote_changes(self):
        cards = [dict(IVAN, **{"Ник": f"@p{i}_test", "Направление": f"Кат{i}"}) for i in range(4)]
        ledger, ws = make(cards)
        got = fresh(ws)
        for c in got:
            ledger.record_contact(c, USER)
        for c in got[:3]:
            ledger.cast_vote(fresh(ws), votes.card_id(c), USER, True)
        with self.assertRaises(votes.VoteError) as e:
            ledger.cast_vote(fresh(ws), votes.card_id(got[3]), USER, True)
        self.assertEqual(e.exception.code, "limit")
        with self.assertRaises(votes.VoteError):               # a change is also an action
            ledger.cast_vote(fresh(ws), votes.card_id(got[0]), USER, False)
        other = {"id": 222, "username": "olga"}                # limit is per account
        ledger.record_contact(got[3], other)
        ledger.cast_vote(fresh(ws), votes.card_id(got[3]), other, True)

    def test_limit_window_rolls_after_24h(self):
        cards = [dict(IVAN, **{"Ник": f"@q{i}_test", "Направление": f"К{i}"}) for i in range(4)]
        ledger, ws = make(cards); got = fresh(ws)
        for c in got:
            ledger.record_contact(c, USER)
        for c in got[:3]:
            ledger.cast_vote(fresh(ws), votes.card_id(c), USER, True)
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        for row in ledger._votes_ws.rows[1:]:
            row[0] = old                                        # age every vote past the window
        ledger.cast_vote(fresh(ws), votes.card_id(got[3]), USER, True)

    def test_votes_survive_row_shifts_because_cards_are_keyed_not_row_numbered(self):
        ledger, ws = make([dict(IVAN, **{"Ник": "@first_test", "Направление": "А"}), IVAN])
        card = fresh(ws)[1]; cid = votes.card_id(card)
        ledger.record_contact(card, USER)
        del ws.rows[1]                                          # someone deleted the row above
        r = ledger.cast_vote(fresh(ws), cid, USER, True)
        self.assertEqual(r["pos"], 3)
        self.assertEqual(ws.rows[1][7], 3)

    def test_sorting_best_net_first_then_more_positive_then_relevance(self):
        def mk(n, p, m, cat="А"):
            return {"Ник": n, "Направление": cat, "Отзывы +": p, "Отзывы -": m}
        cards = [mk("a", 1, 0), mk("b", 5, 1), mk("c", 3, 0), mk("d", 0, 2),
                 mk("e", 4, 0, "Б"), mk("f", 4, 0, "А")]
        ordered = votes.sort_by_rating(cards, rank={"А": 0, "Б": 1})
        # net: b=4 (5 pos), f=4, e=4 (4 pos; "А" is more relevant than "Б"), c=3, a=1, d=-2
        self.assertEqual([c["Ник"] for c in ordered], ["b", "f", "e", "c", "a", "d"])
        self.assertEqual([votes.net_score(c) for c in ordered], [4, 4, 4, 3, 1, -2])


class Verification(unittest.TestCase):
    def test_check_mark_lasts_about_six_months(self):
        today = date(2026, 10, 15)
        fresh_ = {"Подтверждено": (today - timedelta(days=100)).isoformat()}
        edge = {"Подтверждено": (today - timedelta(days=votes.VERIFIED_DAYS)).isoformat()}
        old = {"Подтверждено": (today - timedelta(days=votes.VERIFIED_DAYS + 1)).isoformat()}
        self.assertTrue(votes.is_verified(fresh_, today))
        self.assertTrue(votes.is_verified(edge, today))
        self.assertFalse(votes.is_verified(old, today))
        self.assertFalse(votes.is_verified({"Подтверждено": ""}, today))
        self.assertFalse(votes.is_verified({"Подтверждено": "не дата"}, today))
        self.assertTrue(votes.is_verified({"Подтверждено": "2026-10-14T09:00:00+00:00"}, today), "full ISO accepted")

    def test_verified_goes_first_only_at_equal_score(self):
        today = votes._now().date().isoformat()
        mk = lambda n, p, m, ok: {"Ник": n, "Направление": "А", "Отзывы +": p, "Отзывы -": m, "Подтверждено": today if ok else ""}
        cards = [mk("plain_same", 3, 0, False), mk("verified_same", 3, 0, True),
                 mk("plain_best", 5, 0, False), mk("verified_low", 1, 0, True)]
        order = [c["Ник"] for c in votes.sort_by_rating(cards)]
        self.assertEqual(order, ["plain_best", "verified_same", "plain_same", "verified_low"])

    def test_author_text_replaces_scraped_text(self):
        self.assertEqual(votes.display_text({"Оригинальный текст объявления": "старый", "Текст от автора": "новый"}), "новый")
        self.assertEqual(votes.display_text({"Оригинальный текст объявления": "старый", "Текст от автора": "  "}), "старый")

    def test_changing_category_keeps_votes_and_contacts_attached(self):
        ledger, ws = make([IVAN]); card = fresh(ws)[0]
        ledger.record_contact(card, USER)
        ledger.cast_vote(fresh(ws), votes.card_id(card), USER, True)
        old_key = votes.card_key(card)
        new_key = "ivan_test|йога"
        self.assertEqual(ledger.migrate_key(old_key, new_key), 2)      # one vote row + one contact row
        self.assertEqual(ledger.user_state(USER["id"])["votes"], {new_key: 1})
        self.assertIn(new_key, ledger.contacted_keys(USER["id"]))


class MyCards(unittest.TestCase):
    """Author flow of the bot's "Мои карточки", on the real sheets_writer functions."""

    def setUp(self):
        from app import sheets_writer
        self.sw = sheets_writer
        self.ss = FakeSS()
        self.ws = FakeWS(sheets_writer.HEADER, self.ss)
        self.ss.tabs["Карточки"] = self.ws
        sheets_writer._worksheet, sheets_writer._ledger = self.ws, None     # point the module at the fake sheet
        self.addCleanup(lambda: (setattr(sheets_writer, "_worksheet", None), setattr(sheets_writer, "_ledger", None)))

    def add(self, nick, category, text="Делаю массаж", extra=""):
        row = {h: "" for h in self.sw.HEADER}
        row.update({"Ник": nick, "Имя": "Иван", "Направление": category, "Оригинальный текст объявления": text,
                    "Дополнительные контакты": extra, "Отзывы +": 0, "Отзывы -": 0})
        self.ws.rows.append([row[h] for h in self.sw.HEADER])
        return votes.card_id(dict(row))

    def cell(self, r, name):
        return self.ws.rows[r - 1][self.sw.HEADER.index(name)]

    def test_matches_by_nick_only_case_insensitive(self):
        self.add("@Ivan_Test", "Массаж"); self.add("@other_one", "Йога", extra="@ivan_test")
        mine = self.sw.find_my_cards("ivan_test")
        self.assertEqual([c["Направление"] for c in mine], ["Массаж"], "a @handle inside contacts is NOT enough")
        self.assertEqual(self.sw.find_my_cards(None), [])

    def test_confirm_stamps_today_and_makes_card_verified(self):
        cid = self.add("@ivan_test", "Массаж")
        card = self.sw.update_my_card(cid, "Ivan_Test", {})
        self.assertTrue(votes.is_verified(card))
        self.assertEqual(self.cell(2, "Подтверждено"), date.today().isoformat())
        self.assertTrue(votes.is_verified(self.sw.list_cards_with_rows()[0]))

    def test_someone_else_cannot_confirm(self):
        cid = self.add("@ivan_test", "Массаж")
        with self.assertRaises(self.sw.CardUpdateError):
            self.sw.update_my_card(cid, "intruder", {})
        self.assertEqual(self.cell(2, "Подтверждено"), "")

    def test_corrected_text_is_shown_instead_of_original(self):
        cid = self.add("@ivan_test", "Массаж", text="старый текст")
        card = self.sw.update_my_card(cid, "ivan_test", {"Текст от автора": "новый текст"})
        self.assertEqual(votes.display_text(card), "новый текст")
        self.assertEqual(self.cell(2, "Оригинальный текст объявления"), "старый текст")   # original is kept

    def test_unknown_columns_are_refused(self):
        cid = self.add("@ivan_test", "Массаж")
        with self.assertRaises(self.sw.CardUpdateError):
            self.sw.update_my_card(cid, "ivan_test", {"Отзывы +": "999"})

    def test_category_change_moves_votes_and_blocks_collisions(self):
        cid = self.add("@ivan_test", "Массаж"); self.add("@ivan_test", "Йога")
        led = self.sw.get_ledger()
        card = self.sw.list_cards_with_rows()[0]
        led.record_contact(card, USER); led.cast_vote(self.sw.list_cards_with_rows(), cid, USER, True)
        with self.assertRaises(self.sw.CardUpdateError):                       # already has a "Йога" card
            self.sw.update_my_card(cid, "ivan_test", {"Направление": "Йога"})
        updated = self.sw.update_my_card(cid, "ivan_test", {"Направление": "Спорт"})
        self.assertEqual(updated["Направление"], "Спорт")
        self.assertEqual(led.user_state(USER["id"])["votes"], {votes.card_key(updated): 1})

    def test_author_delete_is_soft_hidden_everywhere_and_keeps_the_row(self):
        cid = self.add("@ivan_test", "Массаж"); self.add("@other_one", "Йога")
        with self.assertRaises(self.sw.CardUpdateError):
            self.sw.delete_my_card(cid, "intruder")                         # someone else can't delete it
        self.assertEqual(len(self.sw.list_cards_with_rows()), 2)
        self.sw.delete_my_card(cid, "Ivan_Test")
        self.assertEqual([c["Ник"] for c in self.sw.list_cards_with_rows()], ["@other_one"])
        self.assertEqual(self.sw.find_my_cards("ivan_test"), [])
        self.assertEqual(len(self.ws.rows), 3, "the row is still in the sheet")
        self.assertTrue(self.cell(2, "Удалить?").startswith("УДАЛЕНО автором"))
        self.assertEqual(self.sw.list_cards_with_rows()[0]["_row"], 3, "remaining cards keep their true row")
        with self.assertRaises(self.sw.CardUpdateError):                    # already gone
            self.sw.update_my_card(cid, "ivan_test", {})

    def test_after_deleting_the_author_can_create_the_category_again(self):
        cid = self.add("@ivan_test", "Массаж")
        self.assertTrue(self.sw.has_card_in_category("ivan_test", "массаж"))
        self.sw.delete_my_card(cid, "ivan_test")
        self.assertFalse(self.sw.has_card_in_category("ivan_test", "массаж"))

    def test_admin_cleanup_mark_does_not_hide_a_card(self):
        self.add("@ivan_test", "Массаж"); self.ws.rows[1][self.sw.HEADER.index("Удалить?")] = "УДАЛИТЬ"
        self.assertEqual(len(self.sw.list_cards_with_rows()), 1)

    def test_create_card_from_the_app_is_verified_and_row_matches_header(self):
        from app import authoring
        authoring.create_card(self.ws, self.sw.list_cards_with_rows(), {"id": 77, "username": "New_Author"},
                              "йога", "Анна", "Занятия йогой", self.sw.normalize_category)
        row = self.ws.rows[1]
        self.assertEqual(len(row), len(self.sw.HEADER))
        self.assertEqual((row[0], row[3], row[1]), ("@new_author", "Йога", "Анна"))   # nick, normalized category, name
        self.assertTrue(votes.is_verified(self.sw.list_cards_with_rows()[0]))
        self.assertEqual(self.cell(2, "ID автора"), 77)
        with self.assertRaises(authoring.CardUpdateError):                   # same category again
            authoring.create_card(self.ws, self.sw.list_cards_with_rows(), {"id": 77, "username": "new_author"},
                                  "Йога", "Анна", "ещё раз", self.sw.normalize_category)
        with self.assertRaises(authoring.CardUpdateError):                   # no @username
            authoring.create_card(self.ws, self.sw.list_cards_with_rows(), {"id": 5, "username": None},
                                  "Спорт", "Без ника", "текст", self.sw.normalize_category)

    def test_field_limits_and_empty_values(self):
        cid = self.add("@ivan_test", "Массаж")
        for bad in ({"Имя": "  "}, {"Текст от автора": "x" * 1001}, {"Направление": "к" * 81}):
            with self.assertRaises(self.sw.CardUpdateError):
                self.sw.update_my_card(cid, "ivan_test", bad)

    def test_bot_added_card_is_verified_at_once(self):
        today = date.today().isoformat()
        self.sw.append_card("@ivan_test", "Иван", "", "Массаж", "текст", "", "2026-10-15", author_id=5, confirmed_on=today)
        self.assertTrue(votes.is_verified(self.sw.list_cards_with_rows()[0]))
        self.assertEqual(len(self.ws.rows[1]), len(self.sw.HEADER), "row width matches the header")


if __name__ == "__main__":
    unittest.main()
