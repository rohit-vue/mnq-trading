import unittest
from datetime import date, datetime

from data.contract_rollover import (
    assign_contract_per_day,
    days_to_expiry,
    evaluate_roll_decision,
    should_roll_volume,
)


class ContractRolloverTests(unittest.TestCase):
    def test_should_roll_when_next_volume_wins(self):
        current = {date(2026, 6, 12): 1000}
        nxt = {date(2026, 6, 12): 1200}

        should_roll, dates, current_vol, next_vol = should_roll_volume(current, nxt, 1)

        self.assertTrue(should_roll)
        self.assertEqual(dates, (date(2026, 6, 12),))
        self.assertEqual(current_vol, 1000)
        self.assertEqual(next_vol, 1200)

    def test_confirmation_days_requires_consecutive_wins(self):
        current = {
            date(2026, 6, 11): 1000,
            date(2026, 6, 12): 1300,
        }
        nxt = {
            date(2026, 6, 11): 1200,
            date(2026, 6, 12): 1100,
        }

        should_roll, _, _, _ = should_roll_volume(current, nxt, 2)

        self.assertFalse(should_roll)

    def test_evaluate_roll_respects_ten_day_window(self):
        cfg = {"rollover": {"enabled": True, "method": "volume", "roll_window_days": 10}}
        decision = evaluate_roll_decision(
            "MNQM6",
            "MNQU6",
            {date(2026, 6, 1): 1000},
            {date(2026, 6, 1): 2000},
            cfg,
            as_of=datetime(2026, 6, 1),
        )

        self.assertFalse(decision.should_roll)
        self.assertEqual(decision.reason, "outside_roll_window")

    def test_evaluate_roll_fallback_near_expiry(self):
        cfg = {
            "rollover": {
                "enabled": True,
                "method": "volume",
                "roll_window_days": 10,
                "fallback_days_before_expiry": 3,
            }
        }
        decision = evaluate_roll_decision(
            "MNQM6",
            "MNQU6",
            {},
            {},
            cfg,
            as_of=datetime(2026, 6, 17),
        )

        self.assertTrue(decision.should_roll)
        self.assertEqual(decision.reason, "fallback")

    def test_assign_contract_per_day_rolls_inside_window(self):
        import pandas as pd

        df = pd.DataFrame(
            [
                {"timestamp": datetime(2026, 6, 12, 9, 30), "symbol": "MNQM6", "volume": 1000},
                {"timestamp": datetime(2026, 6, 12, 9, 30), "symbol": "MNQU6", "volume": 1500},
            ]
        )
        contracts = [
            ("MNQM6", datetime(2026, 3, 20), datetime(2026, 6, 19, 23, 59, 59), "20260619"),
            ("MNQU6", datetime(2026, 6, 19), datetime(2026, 9, 18, 23, 59, 59), "20260918"),
        ]
        cfg = {"rollover": {"enabled": True, "method": "volume", "roll_window_days": 10}}

        assignments = assign_contract_per_day(df, contracts, cfg)

        self.assertEqual(assignments[date(2026, 6, 12)], "MNQU6")
        self.assertEqual(days_to_expiry("MNQM6", datetime(2026, 6, 12)), 7)


if __name__ == "__main__":
    unittest.main()
