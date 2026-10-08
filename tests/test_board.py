"""Board seed, moves, and the rule that an emptied board is not refilled."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.board_store import (
    create_card,
    export_state,
    move_card,
    seed_if_empty,
    update_card,
)
from app.database import Base
from app.models import BoardCard


class BoardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)

    def test_seed_keeps_current_cards_and_does_not_refill(self) -> None:
        with self.Session() as session:
            self.assertTrue(seed_if_empty(session))
            ids = {row.id: row.column for row in session.scalars(select(BoardCard)).all()}
            self.assertIn("48", ids)
            self.assertEqual(ids["48"], "done")
            self.assertIn("52", ids)
            self.assertEqual(ids["52"], "todo")
            self.assertIn("13", ids)
            self.assertGreaterEqual(len(ids), 40)
            first = len(ids)

            self.assertFalse(seed_if_empty(session))
            self.assertEqual(session.scalar(select(func.count()).select_from(BoardCard)), first)

            moved = move_card(session, "52", column="doing", by="noah")
            card = next(item for item in moved["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "doing")
            self.assertTrue(card["events"])
            self.assertEqual(card["events"][0]["by"], "noah")

            moved_plain = move_card(session, "52", column="todo", by="noah")
            card = next(item for item in moved_plain["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "todo")
            self.assertEqual(card["events"][0]["reason"], "")

            move_card(session, "52", column="doing", by="noah")
            moved_back = move_card(session, "52", column="todo", by="noah", reason="Not ready.")
            card = next(item for item in moved_back["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "todo")
            self.assertEqual(card["events"][0]["reason"], "Not ready.")

            created = create_card(
                session,
                title="A new slice",
                by="michael",
                person="lewis",
                hours=2,
                column="backlog",
                brief="What done looks like.",
            )
            new_ids = {item["id"] for item in created["cards"]}
            self.assertIn("53", new_ids)
            self.assertTrue(any(item["id"] == "53" and item["column"] == "backlog" for item in created["cards"]))

            for row in list(session.scalars(select(BoardCard)).all()):
                session.delete(row)
            session.commit()
            self.assertFalse(seed_if_empty(session))
            self.assertEqual(session.scalar(select(func.count()).select_from(BoardCard)), 0)

            exported = export_state(session)
            self.assertEqual(exported["cards"], [])
            self.assertTrue(exported["people"])

    def test_side_pile_assignees_value_and_estimate(self) -> None:
        with self.Session() as session:
            self.assertTrue(seed_if_empty(session))
            move_card(session, "52", column="doing", by="noah")
            parked = move_card(session, "52", column="backlog", by="noah")
            card = next(item for item in parked["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "backlog")
            move_card(session, "52", column="ready", by="noah")
            back = move_card(session, "52", column="todo", by="noah")
            card = next(item for item in back["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "todo")
            lined = move_card(session, "52", column="next", by="noah")
            card = next(item for item in lined["cards"] if item["id"] == "52")
            self.assertEqual(card["column"], "next")
            move_card(session, "52", column="todo", by="noah")
            move_card(session, "52", column="doing", by="noah")

            created = create_card(
                session,
                title="Shared slice",
                by="michael",
                owners=["lewis", "noah"],
                hours=None,
                value=4,
                column="todo",
            )
            card = next(item for item in created["cards"] if item["title"] == "Shared slice")
            self.assertEqual(card["owners"], ["lewis", "noah"])
            self.assertEqual(card["person"], "lewis")
            self.assertIsNone(card["hours"])
            self.assertEqual(card["value"], 4)

            edited = update_card(
                session,
                card["id"],
                by="michael",
                fields={"owners": ["noah"], "value": 2},
            )
            card = next(item for item in edited["cards"] if item["id"] == card["id"])
            self.assertEqual(card["owners"], ["noah"])
            self.assertEqual(card["person"], "noah")
            self.assertEqual(card["value"], 2)
            self.assertEqual(card["events"][0]["action"], "edit")
            self.assertIn("assignees", card["events"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
