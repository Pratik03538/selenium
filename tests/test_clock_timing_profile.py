"""Deterministic checks for the adaptive clock-budget profile."""
import unittest

import chess
from chesskida_strategy import _clock_deficit_forces_fast_pacing, _clock_time_profile


class ClockTimingProfileTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()

    def test_longer_clock_and_increment_allow_more_base_delay(self):
        bullet_10s = _clock_time_profile(
            self.board, remaining_seconds=10, initial_seconds=10,
            increment_seconds=0, opponent_seconds=10,
        )
        one_minute = _clock_time_profile(
            self.board, remaining_seconds=60, initial_seconds=60,
            increment_seconds=0, opponent_seconds=60,
        )
        one_plus_one = _clock_time_profile(
            self.board, remaining_seconds=60, initial_seconds=60,
            increment_seconds=1, opponent_seconds=60,
        )
        three_plus_two = _clock_time_profile(
            self.board, remaining_seconds=180, initial_seconds=180,
            increment_seconds=2, opponent_seconds=180,
        )

        self.assertLess(bullet_10s["pace_scale"], one_minute["pace_scale"])
        self.assertLess(one_minute["pace_scale"], one_plus_one["pace_scale"])
        self.assertLess(one_plus_one["pace_scale"], three_plus_two["pace_scale"])

    def test_clock_deficit_speeds_up_and_clock_lead_allows_some_headroom(self):
        behind = _clock_time_profile(
            self.board, remaining_seconds=60, initial_seconds=60,
            increment_seconds=1, opponent_seconds=75,
        )
        level = _clock_time_profile(
            self.board, remaining_seconds=60, initial_seconds=60,
            increment_seconds=1, opponent_seconds=60,
        )
        ahead = _clock_time_profile(
            self.board, remaining_seconds=60, initial_seconds=60,
            increment_seconds=1, opponent_seconds=50,
        )

        self.assertLess(behind["pace_scale"], level["pace_scale"])
        self.assertGreater(ahead["pace_scale"], level["pace_scale"])
        self.assertGreater(ahead["spendable_lead"], 0)

    def test_small_clock_deficit_keeps_midgame_timing_but_large_deficit_speeds_up(self):
        self.assertFalse(
            _clock_deficit_forces_fast_pacing(
                own_seconds=50, opponent_seconds=53, initial_seconds=60
            )
        )
        self.assertTrue(
            _clock_deficit_forces_fast_pacing(
                own_seconds=45, opponent_seconds=52, initial_seconds=60
            )
        )

    def test_missing_clock_uses_safe_neutral_profile(self):
        profile = _clock_time_profile(self.board)
        self.assertIsNone(profile["seconds_per_decision"])
        self.assertEqual(profile["pace_scale"], 1.0)


if __name__ == "__main__":
    unittest.main()
