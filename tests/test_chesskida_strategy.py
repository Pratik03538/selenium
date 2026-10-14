"""Regression tests for restored baseline move selection and bounded rank variety."""
import unittest

import chess
import chesskida_strategy as strategy


class OpponentMoveQualityTests(unittest.TestCase):
    def test_centipawn_loss_maps_to_smooth_bounded_quality(self):
        self.assertEqual(strategy.opponent_quality_from_centipawn_loss(0), 100.0)
        self.assertEqual(strategy.opponent_quality_from_centipawn_loss(20), 95.0)
        self.assertEqual(strategy.opponent_quality_from_centipawn_loss(100), 75.0)
        self.assertEqual(strategy.opponent_quality_from_centipawn_loss(400), 0.0)

    def test_quality_handles_negative_and_invalid_loss_safely(self):
        self.assertEqual(strategy.opponent_quality_from_centipawn_loss(-25), 100.0)
        self.assertIsNone(strategy.opponent_quality_from_centipawn_loss("unknown"))


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


class MaterialSafetyAndPriorityTests(unittest.TestCase):
    def test_detects_a_move_that_uncovers_a_free_rook(self):
        board = chess.Board("4k3/8/8/8/8/5q2/4P3/K2R4 w - - 0 1")
        self.assertTrue(
            strategy.candidate_hangs_own_piece(
                board, chess.Move.from_uci("e2e3")
            )
        )

    def test_negative_evaluation_selection_avoids_hanging_the_moving_piece(self):
        board = chess.Board("4k3/8/8/8/1p6/8/8/1N2K3 w - - 0 1")
        moves = [
            chess.Move.from_uci("b1a3"),
            chess.Move.from_uci("b1d2"),
            chess.Move.from_uci("e1d1"),
        ]
        scores_cp = [-500, -550, -560]
        multipv_infos = [
            {
                "pv": [move],
                "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            }
            for move, cp in zip(moves, scores_cp)
        ]

        selected_move, _selected_info, meta = strategy.choose_stockfish_move(
            board, multipv_infos, bot_color=board.turn
        )

        self.assertNotEqual(selected_move, chess.Move.from_uci("b1a3"))
        self.assertGreater(meta["rank"], 0)

    def test_safe_free_capture_has_priority_in_a_losing_evaluation(self):
        board = chess.Board("q6k/8/8/8/8/8/8/R3K3 w - - 0 1")
        moves = [
            chess.Move.from_uci("e1d1"),
            chess.Move.from_uci("a1a8"),
            chess.Move.from_uci("e1f1"),
        ]
        scores_cp = [-500, -520, -530]
        multipv_infos = [
            {
                "pv": [move],
                "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            }
            for move, cp in zip(moves, scores_cp)
        ]

        selected_move, _selected_info, meta = strategy.choose_stockfish_move(
            board, multipv_infos, bot_color=board.turn
        )

        self.assertEqual(selected_move, chess.Move.from_uci("a1a8"))
        self.assertEqual(meta.get("source"), "CAPTURE_PRIORITY")

    def test_safe_queen_promotion_has_priority_in_a_losing_evaluation(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        moves = [
            chess.Move.from_uci("a7a8q"),
            chess.Move.from_uci("e1d1"),
            chess.Move.from_uci("e1f1"),
        ]
        scores_cp = [-500, -510, -530]
        multipv_infos = [
            {
                "pv": [move],
                "score": chess.engine.PovScore(chess.engine.Cp(cp), board.turn),
            }
            for move, cp in zip(moves, scores_cp)
        ]

        selected_move, _selected_info, meta = strategy.choose_stockfish_move(
            board, multipv_infos, bot_color=board.turn
        )

        self.assertEqual(selected_move, chess.Move.from_uci("a7a8q"))
        self.assertEqual(meta.get("source"), "QUEEN_PROMOTION")


class FastClockProtectionTests(unittest.TestCase):
    def test_endgame_pauses_are_available_but_bounded(self):
        self.assertGreater(strategy.BOT_ENDGAME_PAUSE_CHANCE, 0.0)
        self.assertLessEqual(strategy.BOT_ENDGAME_PAUSE_CHANCE, 0.25)

    def test_low_clock_delay_budget_is_capped_at_60ms(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        delay = strategy.natural_bot_move_delay(
            board,
            move,
            thinking_enabled=True,
            remaining_time_seconds=8.0,
            low_clock_trigger_seconds=9.0,
            low_clock_total_budget_seconds=0.200,
            elapsed_seconds_before_delay=0.0,
            submission_reserve_seconds=0.0,
        )
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, 0.060)


class RandomizedTimingTests(unittest.TestCase):
    def test_base_move_delay_uses_continuous_samples(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        samples = [
            round(strategy._sample_base_move_delay(board, move), 4)
            for _ in range(50)
        ]
        self.assertGreater(len(set(samples)), 35)
        self.assertTrue(all(strategy.BOT_EVERY_MOVE_DELAY_MIN <= x <= strategy.BOT_EVERY_MOVE_DELAY_MAX for x in samples))

    def test_thinking_pause_uses_continuous_samples(self):
        samples = [
            round(strategy._sample_thinking_pause(2.5, complexity=0.55), 4)
            for _ in range(50)
        ]
        self.assertGreater(len(set(samples)), 35)
        self.assertTrue(all(0.0 <= x <= 2.5 for x in samples))


class BotSideGuardTests(unittest.TestCase):
    def setUp(self):
        self.board = chess.Board()
        self.move = chess.Move.from_uci("e2e4")
        self.infos = [{
            "pv": [self.move],
            "score": chess.engine.PovScore(chess.engine.Cp(25), chess.WHITE),
        }]

    def test_selector_refuses_to_choose_for_opposite_color(self):
        selected, _info, meta = strategy.choose_stockfish_move(
            self.board, self.infos, bot_color=chess.BLACK
        )
        self.assertIsNone(selected)
        self.assertIn("not the verified bot's turn", meta["reason"])

    def test_selector_refuses_to_choose_when_bot_color_is_unknown(self):
        selected, _info, meta = strategy.choose_stockfish_move(
            self.board, self.infos, bot_color=None
        )
        self.assertIsNone(selected)
        self.assertIn("verified bot side is unknown", meta["reason"])


if __name__ == "__main__":
    unittest.main()
