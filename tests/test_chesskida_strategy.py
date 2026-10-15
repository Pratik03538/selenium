"""Regression tests for restored baseline move selection and bounded rank variety."""
import unittest
from unittest.mock import patch

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

    def test_opponent_centipawn_loss_requires_comparable_numeric_scores(self):
        self.assertEqual(strategy.opponent_move_centipawn_loss(100, 30), 70.0)
        self.assertEqual(strategy.opponent_move_centipawn_loss(100, 130), 0.0)
        self.assertIsNone(strategy.opponent_move_centipawn_loss(None, 30))
        self.assertIsNone(strategy.opponent_move_centipawn_loss(100, "unknown"))


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
    def test_selector_uses_baseline_near_equal_shuffle_within_eval_band(self):
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

        selected_move, _selected_info, meta = strategy.choose_stockfish_move(
            board, multipv_infos, bot_color=board.turn
        )

        self.assertIn(selected_move, moves)
        self.assertIn(meta["rank"], (0, 1))
        self.assertLessEqual(meta["current_cp"] - meta["selected_cp"], 20)


class MaterialSafetyAndPriorityTests(unittest.TestCase):
    def test_detects_a_move_that_uncovers_a_free_rook(self):
        # The knight is not itself hanging after e2-c1, but vacating e2 opens
        # the queen's diagonal to the rook on d1.
        board = chess.Board("4k3/8/8/8/8/5q2/4N3/K2R4 w - - 0 1")
        self.assertTrue(
            strategy.candidate_exposes_free_major_piece(
                board, chess.Move.from_uci("e2c1")
            )
        )

    def test_selector_avoids_the_hanging_piece_move_when_a_safe_line_exists(self):
        board = chess.Board("4k3/8/8/8/8/5q2/4N3/K2R4 w - - 0 1")
        hanging = chess.Move.from_uci("e2c1")
        safe_rook_move = chess.Move.from_uci("d1d2")
        safe_king_move = chess.Move.from_uci("a1a2")
        moves = [hanging, safe_rook_move, safe_king_move]
        scores = [100, 95, 90]
        infos = [
            {
                "pv": [move],
                "score": chess.engine.PovScore(
                    chess.engine.Cp(score), board.turn
                ),
            }
            for move, score in zip(moves, scores)
        ]
        selected, _info, _meta = strategy.choose_stockfish_move(
            board, infos, bot_color=board.turn
        )
        self.assertNotEqual(selected, hanging)

    def test_low_clock_promotes_a_safe_queen_even_outside_single_pv(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 w - - 0 1")
        ordinary_move = chess.Move.from_uci("e1d1")
        info = {
            "pv": [ordinary_move],
            "score": chess.engine.PovScore(chess.engine.Cp(250), board.turn),
        }
        selected, _selected_info, meta = strategy.choose_stockfish_move(
            board, [info], bot_color=board.turn, low_clock_mode=True
        )
        self.assertEqual(selected, chess.Move.from_uci("a7a8q"))
        self.assertEqual(meta.get("source"), "LOW_CLOCK_TACTIC")

    def test_low_clock_prioritizes_a_safe_free_capture_outside_single_pv(self):
        board = chess.Board("q6k/8/8/8/8/8/8/R3K3 w - - 0 1")
        ordinary_move = chess.Move.from_uci("e1d1")
        info = {
            "pv": [ordinary_move],
            "score": chess.engine.PovScore(chess.engine.Cp(250), board.turn),
        }
        selected, _selected_info, meta = strategy.choose_stockfish_move(
            board, [info], bot_color=board.turn, low_clock_mode=True
        )
        self.assertEqual(selected, chess.Move.from_uci("a1a8"))
        self.assertEqual(meta.get("source"), "LOW_CLOCK_TACTIC")
    def test_negative_position_respects_baseline_near_equal_eval_band(self):
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

        self.assertIn(selected_move, moves)
        self.assertEqual(meta["rank"], 0)
        self.assertEqual(meta["selected_cp"], -500)

    def test_safe_free_capture_has_priority_in_a_losing_evaluation(self):
        board = chess.Board("q6k/8/8/8/8/8/8/R3K3 w - - 0 1")
        moves = [
            chess.Move.from_uci("e1d1"),
            chess.Move.from_uci("a1a8"),
            chess.Move.from_uci("e1f1"),
        ]
        scores_cp = [250, 230, 220]
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
        scores_cp = [250, 240, 230]
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


class ClockParityProtectionTests(unittest.TestCase):
    def test_close_healthy_clocks_keep_varied_midgame_base_delay(self):
        board = chess.Board("4k3/8/8/8/8/8/R7/4K3 w - - 0 30")
        move = chess.Move.from_uci("a2a3")
        profile = {
            "seconds_per_decision": 2.0,
            "clock_margin": 0.0,
            "lead_buffer": 2.0,
            "spendable_lead": 0.0,
            "pace_scale": 1.0,
        }
        strategy._thinking_budget_remaining = None
        with patch.object(strategy, "_sample_base_move_delay", return_value=1.2):
            with patch.object(strategy, "_clock_time_profile", return_value=profile):
                delay = strategy.natural_bot_move_delay(
                    board,
                    move,
                    thinking_enabled=True,
                    initial_time_seconds=60.0,
                    remaining_time_seconds=50.0,
                    opponent_time_seconds=50.0,
                    low_clock_trigger_seconds=7.3,
                    low_clock_mode=False,
                )
        # Near-equal healthy clocks must not force every move into the old
        # 35-220ms base-delay cap; long pauses remain governed by position gates.
        self.assertGreater(delay, 0.22)
        self.assertLessEqual(delay, 2.1)
        strategy._thinking_budget_remaining = None


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

    def test_missing_own_clock_suppresses_optional_pause(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        strategy._thinking_budget_remaining = None
        with patch.object(strategy, "_sample_base_move_delay", return_value=0.8):
            with patch("chesskida_strategy.random.uniform", return_value=0.1):
                delay = strategy.natural_bot_move_delay(
                    board, move, thinking_enabled=True,
                    initial_time_seconds=None, remaining_time_seconds=None,
                    opponent_time_seconds=None,
                )
        self.assertGreaterEqual(delay, 0.0)
        self.assertLessEqual(delay, 0.22)
        strategy._thinking_budget_remaining = None


class OpeningFastPathTests(unittest.TestCase):
    def test_opening_quiet_move_uses_a_varied_short_cap_and_skips_pause(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        profile = {
            "seconds_per_decision": 1.5,
            "clock_margin": 0.0,
            "lead_buffer": 0.0,
            "spendable_lead": 0.0,
            "pace_scale": 1.0,
        }
        strategy._thinking_budget_remaining = None
        with patch.object(strategy, "_sample_base_move_delay", return_value=1.1):
            with patch.object(strategy, "_clock_time_profile", return_value=profile):
                with patch("chesskida_strategy.random.uniform", return_value=0.18):
                    delay = strategy.natural_bot_move_delay(
                        board,
                        move,
                        thinking_enabled=True,
                        initial_time_seconds=60.0,
                        remaining_time_seconds=55.0,
                        opponent_time_seconds=55.0,
                    )
        self.assertAlmostEqual(delay, 0.18)
        strategy._thinking_budget_remaining = None

    def test_opening_capture_uses_forced_fast_path_not_quiet_cap(self):
        board = chess.Board()
        board.push_san("e4")
        board.push_san("d5")
        move = chess.Move.from_uci("e4d5")
        profile = {
            "seconds_per_decision": 1.5,
            "clock_margin": 0.0,
            "lead_buffer": 0.0,
            "spendable_lead": 0.0,
            "pace_scale": 1.0,
        }
        strategy._thinking_budget_remaining = None
        def sampled_delay(_board, _move, forced=False, opening_book=False, **_kwargs):
            return 0.035 if forced else 0.8
        with patch.object(strategy, "_sample_base_move_delay", side_effect=sampled_delay):
            with patch.object(strategy, "_clock_time_profile", return_value=profile):
                delay = strategy.natural_bot_move_delay(
                    board,
                    move,
                    thinking_enabled=True,
                    initial_time_seconds=60.0,
                    remaining_time_seconds=55.0,
                    opponent_time_seconds=55.0,
                )
        self.assertLessEqual(delay, 0.035)
        strategy._thinking_budget_remaining = None


class MoveDelayBandTests(unittest.TestCase):
    def test_quick_medium_and_long_base_delay_bands_are_available(self):
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        with patch.object(strategy, "_timing_position_complexity", return_value=0.10):
            with patch("chesskida_strategy.random.choices", side_effect=lambda population, weights, k: [population[0]]):
                quick = strategy._sample_base_move_delay(board, move)
        self.assertGreaterEqual(quick, 0.18)
        self.assertLess(quick, 0.51)

        with patch.object(strategy, "_timing_position_complexity", return_value=0.45):
            with patch("chesskida_strategy.random.choices", side_effect=lambda population, weights, k: [population[1]]):
                medium = strategy._sample_base_move_delay(board, move)
        self.assertGreaterEqual(medium, 0.55)
        self.assertLess(medium, 1.44)

        with patch.object(strategy, "_timing_position_complexity", return_value=0.90):
            with patch("chesskida_strategy.random.choices", side_effect=lambda population, weights, k: [population[2]]):
                long = strategy._sample_base_move_delay(board, move)
        self.assertGreaterEqual(long, 1.15)
        self.assertLessEqual(long, strategy.BOT_EVERY_MOVE_DELAY_MAX)


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
