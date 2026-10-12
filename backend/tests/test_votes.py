"""Rules of app/votes.py against an in-memory fake of the spreadsheet.

Run from backend/:  python -m unittest tests.test_votes -v
(No network, no real sheet — votes made here never touch production data.)
"""
import unittest
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    unittest.main()
