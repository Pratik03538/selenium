"""Regression tests for restored baseline move selection and bounded rank variety."""
import unittest

import chess
import chesskida_strategy as strategy


class NearEqualRecoveryPoolTests(unittest.TestCase):
    def test_non_winning_position_allows_bounded_70cp_recovery_band(self):
        candidates = [
            {"rank": 0, "cp": -500},
            {"rank": 1, "cp": -530},
            {"rank": 2, "cp": -570},
            {"rank": 3, "cp": -571},
        ]

        pool, floor_cp, drop_cap = strategy._near_equal_selection_pool(
            candidates, best_cp=-500, rank_cap=14
        )

        self.assertEqual(drop_cap, 70)
        self.assertEqual(floor_cp, -570)
        self.assertEqual([item["rank"] for item in pool], [0, 1, 2])

    def test_clear_winning_position_keeps_baseline_20cp_fallback_band(self):
        candidates = [
            {"rank": 0, "cp": 500},
            {"rank": 1, "cp": 490},
            {"rank": 2, "cp": 479},
        ]

        pool, floor_cp, drop_cap = strategy._near_equal_selection_pool(
            candidates, best_cp=500, rank_cap=14
        )

        self.assertEqual(drop_cap, 20)
        self.assertEqual(floor_cp, 480)
        self.assertEqual([item["rank"] for item in pool], [0, 1])


class RankOneStreakTests(unittest.TestCase):
    def setUp(self):
        strategy._rank1_selection_streak = 0

    def test_baseline_candidate_count_is_15(self):
        self.assertEqual(strategy.TRAINING_MULTI_PV, 15)

    def test_rank_one_is_allowed_before_two_consecutive_picks(self):
        candidates = [
            {"rank": 0, "cp": -500},
            {"rank": 1, "cp": -520},
            {"rank": 2, "cp": -540},
        ]
        pool, was_limited = strategy._limit_rank1_streak_pool(candidates, 1)
        self.assertFalse(was_limited)
        self.assertEqual([item["rank"] for item in pool], [0, 1, 2])

    def test_rank_one_is_removed_after_two_consecutive_picks(self):
        candidates = [
            {"rank": 0, "cp": -500},
            {"rank": 1, "cp": -520},
            {"rank": 2, "cp": -540},
        ]
        pool, was_limited = strategy._limit_rank1_streak_pool(candidates, 2)
        self.assertTrue(was_limited)
        self.assertEqual([item["rank"] for item in pool], [1, 2])

    def test_rank_one_is_not_blocked_when_no_alternative_exists(self):
        candidates = [{"rank": 0, "cp": -500}]
        pool, was_limited = strategy._limit_rank1_streak_pool(candidates, 4)
        self.assertFalse(was_limited)
        self.assertEqual([item["rank"] for item in pool], [0])

    def test_record_rank_resets_streak_after_non_best(self):
        strategy._record_rank_selection(0)
        strategy._record_rank_selection(0)
        self.assertEqual(strategy._rank1_selection_streak, 2)
        strategy._record_rank_selection(1)
        self.assertEqual(strategy._rank1_selection_streak, 0)


class LosingPositionIntegrationTests(unittest.TestCase):
    def test_selector_can_choose_near_best_alternative_after_rank_one_streak(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("e5")
        moves = [
            chess.Move.from_uci("g1f3"),
            chess.Move.from_uci("d2d4"),
            chess.Move.from_uci("f1c4"),
            chess.Move.from_uci("b1c3"),
        ]
        scores_cp = [-500, -520, -540, -565]
        multipv_infos = [
            {
                "pv": [move],
                "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            }
            for move, cp in zip(moves, scores_cp)
        ]

        strategy._rank1_selection_streak = 2
        selected_move, _selected_info, meta = strategy.choose_stockfish_move(
            board, multipv_infos, bot_color=board.turn
        )

        self.assertIn(selected_move, moves)
        self.assertGreater(meta["rank"], 0)
        self.assertLessEqual(meta["current_cp"] - meta["selected_cp"], 70)


if __name__ == "__main__":
    unittest.main()
