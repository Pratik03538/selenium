"""Regression tests for the live tracker safety and history helpers."""
import time
import unittest
from unittest.mock import patch

import chess
from seleniumbase_live_game_tracker import (
    PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MAX_EVAL_DROP_CP,
    PREMOVE_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MIN_BOT_EVAL_CP,
    PREMOVE_OPENING_FULLMOVE_MIN,
    PREMOVE_OPENING_FULLMOVE_MAX,
    JS_INSTALL_OBSERVER,
    PlayerSideResolver,
    MoveAttemptRetryGate,
    normalize_api_bot_color,
    is_low_clock_mode,
    calculate_engine_search_limit,
    _DeadlineBoundEngine,
    _PremoveDeadlineExceeded,
    choose_mate_phase_premove,
    infer_move_between_fens,
    premove_phase_allowed,
    reconstruct_history_from_dom_moves,
    uci_move_reaches_fen,
)


class PlayerSideResolverTests(unittest.TestCase):
    def test_name_only_side_requires_two_consistent_snapshots(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")

    def test_conflicting_api_and_name_signals_do_not_resolve(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve("black", "white"))
        self.assertEqual(resolver.resolve("black", "black"), "black")

    def test_api_side_does_not_flip_after_a_transient_conflicting_snapshot(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertEqual(resolver.resolve("black", None), "black")
        self.assertIsNone(resolver.resolve("white", None))
        self.assertIsNone(resolver.resolve("white", None))
        self.assertEqual(resolver.resolve("black", None), "black")

    def test_name_side_does_not_flip_until_new_game_reset(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        self.assertIsNone(resolver.resolve(None, "black"))
        self.assertEqual(resolver.resolve(None, "black"), "black")
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertEqual(resolver.resolve(None, "black"), "black")
        resolver.reset_for_new_game()
        self.assertIsNone(resolver.resolve(None, "white"))
        self.assertEqual(resolver.resolve(None, "white"), "white")

    def test_fixed_color_mismatch_pauses_instead_of_overriding_api(self):
        resolver = PlayerSideResolver(fixed_color="white", player_name_enabled=True)
        self.assertIsNone(resolver.resolve("black", "white"))
        self.assertEqual(resolver.resolve("white", "white"), "white")

    def test_reset_requires_fresh_name_evidence(self):
        resolver = PlayerSideResolver(player_name_enabled=True)
        resolver.resolve(None, "black")
        self.assertEqual(resolver.resolve(None, "black"), "black")
        resolver.reset_for_new_game()
        self.assertIsNone(resolver.resolve(None, "black"))

    def test_api_color_fields_must_agree_when_both_are_present(self):
        self.assertEqual(normalize_api_bot_color("white", "w"), ("white", False))
        self.assertEqual(normalize_api_bot_color("black", "white"), (None, True))
        self.assertEqual(normalize_api_bot_color(None, "black"), ("black", False))


class BoardInputSourceGuardTests(unittest.TestCase):
    def test_unreadable_dom_class_falls_back_to_authoritative_fen_after_side_check(self):
        self.assertIn("sourceColor !== wantedColor", JS_INSTALL_OBSERVER)
        self.assertIn(
            "if (renderedColor && renderedColor !== sourceColor)",
            JS_INSTALL_OBSERVER,
        )
        self.assertIn("unreadable-fen-verified", JS_INSTALL_OBSERVER)


class MoveRetryGateTests(unittest.TestCase):
    def test_retry_same_fen_only_after_backoff_and_with_bounded_attempts(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.should_attempt("fen-A", now=0.0))
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=0.19))
        self.assertTrue(gate.should_attempt("fen-A", now=0.20))
        self.assertTrue(gate.begin("fen-A", now=0.20))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=0.69))
        self.assertTrue(gate.should_attempt("fen-A", now=0.71))
        self.assertTrue(gate.begin("fen-A", now=0.71))
        gate.finish({"ok": False, "reason": "temporary-submit-failure"})
        self.assertFalse(gate.should_attempt("fen-A", now=10.0))

    def test_accepted_but_unconfirmed_move_is_not_blindly_resubmitted(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False, "api_accepted": True, "reason": "api-move-not-confirmed"})
        self.assertFalse(gate.should_attempt("fen-A", now=100.0))

    def test_confirmed_success_does_not_retry_same_fen(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": True, "confirmed": True})
        self.assertFalse(gate.should_attempt("fen-A", now=100.0))

    def test_new_fen_resets_retry_budget(self):
        gate = MoveAttemptRetryGate()
        self.assertTrue(gate.begin("fen-A", now=0.0))
        gate.finish({"ok": False})
        self.assertTrue(gate.begin("fen-B", now=0.1))
        self.assertEqual(gate.attempts, 1)


class LateClockAndPremoveSafetyTests(unittest.TestCase):
    def test_clock_mode_uses_the_same_sampled_boundary_everywhere(self):
        self.assertFalse(is_low_clock_mode(8.5, 7.5))
        self.assertTrue(is_low_clock_mode(7.5, 7.5))
        self.assertTrue(is_low_clock_mode(9.1, 9.79))
        self.assertFalse(is_low_clock_mode(10.1, 9.79))
        self.assertTrue(is_low_clock_mode(8.5, 7.5, force_fast_mode=True))
        self.assertFalse(is_low_clock_mode(None, 7.5, force_fast_mode=True))

    def test_missing_clock_uses_conservative_engine_budget(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75, remaining_seconds=None, initial_seconds=None,
            increment_seconds=0.0, board=chess.Board(),
        )
        self.assertEqual(limit, 0.05)

    def test_engine_search_uses_sampled_clock_boundary_not_fixed_ten(self):
        board = chess.Board()
        common = dict(
            base_seconds=5.0, remaining_seconds=8.5, initial_seconds=60.0,
            increment_seconds=0.0, board=board,
        )
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.28):
            normal_path = calculate_engine_search_limit(
                **common, low_clock_trigger_seconds=7.5
            )
            low_clock_path = calculate_engine_search_limit(
                **common, low_clock_trigger_seconds=9.0
            )
            forced_short_control = calculate_engine_search_limit(
                **{**common, "remaining_seconds": 11.0},
                low_clock_trigger_seconds=7.5, force_fast_mode=True,
            )
        self.assertGreater(normal_path, 0.035)
        self.assertLessEqual(low_clock_path, 0.035)
        self.assertLessEqual(forced_short_control, 0.035)

    def test_engine_search_is_capped_at_35_ms_with_7_to_10_seconds_left(self):
        board = chess.Board()
        for remaining in (7.0, 8.0, 9.0, 10.0):
            with self.subTest(remaining=remaining):
                limit = calculate_engine_search_limit(
                    base_seconds=5.0,
                    remaining_seconds=remaining,
                    initial_seconds=60.0,
                    increment_seconds=0.0,
                    board=board,
                )
                self.assertGreater(limit, 0.0)
                self.assertLessEqual(limit, 0.035)
                self.assertAlmostEqual(limit, min(0.035, remaining * 0.004))

    def test_opening_engine_search_is_bounded_in_one_minute_controls(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75,
            remaining_seconds=55.0,
            initial_seconds=60.0,
            increment_seconds=0.0,
            board=chess.Board(),
        )
        self.assertGreater(limit, 0.0)
        self.assertLessEqual(limit, 0.18)

    def test_opening_engine_search_has_a_slightly_larger_three_minute_cap(self):
        limit = calculate_engine_search_limit(
            base_seconds=0.75,
            remaining_seconds=170.0,
            initial_seconds=180.0,
            increment_seconds=0.0,
            board=chess.Board(),
        )
        self.assertGreater(limit, 0.0)
        self.assertLessEqual(limit, 0.30)

    def test_relative_clock_deficit_tightens_normal_search_budget(self):
        board = chess.Board()
        common = dict(
            base_seconds=2.0,
            remaining_seconds=45.0,
            initial_seconds=120.0,
            increment_seconds=0.0,
            board=board,
        )
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.10):
            behind = calculate_engine_search_limit(**common, opponent_seconds=60.0)
        with patch("seleniumbase_live_game_tracker.random.uniform", return_value=0.28):
            level = calculate_engine_search_limit(**common, opponent_seconds=45.0)
        self.assertLess(behind, level)

    def test_premove_deadline_reserves_cleanup_window_before_engine_call(self):
        class FakeEngine:
            called = False
            def analyse(self, *_args, **_kwargs):
                self.called = True
                return {}

        engine = FakeEngine()
        proxy = _DeadlineBoundEngine(engine, time.monotonic() + 0.005)
        with self.assertRaises(_PremoveDeadlineExceeded):
            proxy.analyse(chess.Board(), chess.engine.Limit(time=0.1, depth=8))
        self.assertFalse(engine.called)

    def test_expired_premove_deadline_never_calls_engine(self):
        class FakeEngine:
            called = False
            def analyse(self, *_args, **_kwargs):
                self.called = True
                return {}

        engine = FakeEngine()
        proxy = _DeadlineBoundEngine(engine, time.monotonic() - 1.0)
        with self.assertRaises(_PremoveDeadlineExceeded):
            proxy.analyse(chess.Board(), chess.engine.Limit(time=0.1, depth=8))
        self.assertFalse(engine.called)

    def test_premove_evaluation_guards_reject_large_drops(self):
        self.assertEqual(PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP, 50)
        self.assertEqual(PREMOVE_MAX_EVAL_DROP_CP, 50)
        self.assertEqual(PREMOVE_OPENING_MAX_EVAL_DROP_CP, 60)
        self.assertEqual(PREMOVE_OPENING_MIN_BOT_EVAL_CP, -50)


class PremovePhaseTests(unittest.TestCase):
    def test_opening_premove_window_is_sampled_between_five_and_ten_fullmoves(self):
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MIN, 5)
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MAX, 10)
        for opening_limit in range(5, 11):
            with self.subTest(opening_limit=opening_limit):
                inside = chess.Board(
                    f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 {opening_limit}"
                )
                outside = chess.Board(
                    f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 {opening_limit + 1}"
                )
                self.assertEqual(
                    premove_phase_allowed(inside, opening_limit), (True, "opening")
                )
                self.assertEqual(
                    premove_phase_allowed(outside, opening_limit),
                    (False, "outside-opening-endgame"),
                )

    def test_mate_phase_premove_uses_forced_mate_continuation(self):
        board = chess.Board()
        board.push_san("e4")
        reply = chess.Move.from_uci("e7e5")
        mate_move = chess.Move.from_uci("d1h5")

        class FakeEngine:
            def __init__(self):
                self.calls = 0

            def analyse(self, _board, _limit, multipv=1, root_moves=None):
                self.calls += 1
                if self.calls == 1:
                    return [{
                        "pv": [reply],
                        "score": chess.engine.PovScore(
                            chess.engine.Mate(5), chess.WHITE
                        ),
                    }]
                return [{
                    "pv": [mate_move],
                    "score": chess.engine.PovScore(
                        chess.engine.Mate(3), chess.WHITE
                    ),
                }]

        class Args:
            engine_time = 0.02
            move_input = "api"

        selected, predicted_reply, reason = choose_mate_phase_premove(
            FakeEngine(), board, "white", Args()
        )
        self.assertEqual(predicted_reply, reply)
        self.assertEqual(selected, mate_move)
        self.assertIn("mate-phase-premove", reason)


class MoveConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.before = chess.Board()
        self.before_fen = self.before.fen()

    def test_confirms_selected_move(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        self.assertTrue(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))

    def test_reconstructs_double_pawn_push_when_fen_omits_inert_ep_square(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        self.assertEqual(infer_move_between_fens(self.before_fen, after.fen())["san"], "e4")

    def test_confirms_move_after_fast_opponent_reply(self):
        after = self.before.copy()
        after.push_uci("e2e4")
        after.push_uci("e7e5")
        self.assertTrue(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))

    def test_does_not_confirm_unrelated_position(self):
        after = self.before.copy()
        after.push_uci("d2d4")
        self.assertFalse(uci_move_reaches_fen(self.before_fen, after.fen(), "e2e4"))


class DomHistoryRecoveryTests(unittest.TestCase):
    def test_rebuilds_only_when_full_san_list_matches_fen(self):
        board = chess.Board()
        sans = ["e4", "e5", "Nf3", "Nc6", "Bb5"]
        dom_moves = []
        for index, san in enumerate(sans, start=1):
            move = board.parse_san(san)
            dom_moves.append({"ply": str(index), "text": san})
            board.push(move)

        history = reconstruct_history_from_dom_moves(dom_moves, board.fen())
        self.assertIsNotNone(history)
        self.assertEqual(len(history), len(sans))
        self.assertEqual(history[-1]["san"], "Bb5")
        self.assertEqual(history[-1]["game_ply"], str(len(sans)))
        self.assertIsNone(reconstruct_history_from_dom_moves(dom_moves[:-1], board.fen()))


if __name__ == "__main__":
    unittest.main()
