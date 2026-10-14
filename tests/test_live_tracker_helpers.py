"""Regression tests for the live tracker safety and history helpers."""
import unittest

import chess
from seleniumbase_live_game_tracker import (
    PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MAX_EVAL_DROP_CP,
    PREMOVE_OPENING_MIN_BOT_EVAL_CP,
    PREMOVE_OPENING_FULLMOVE_MIN,
    PREMOVE_OPENING_FULLMOVE_MAX,
    PlayerSideResolver,
    normalize_api_bot_color,
    calculate_engine_search_limit,
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


class LateClockAndPremoveSafetyTests(unittest.TestCase):
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

    def test_premove_evaluation_guards_reject_large_drops(self):
        self.assertEqual(PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP, 50)
        self.assertEqual(PREMOVE_OPENING_MAX_EVAL_DROP_CP, 60)
        self.assertEqual(PREMOVE_OPENING_MIN_BOT_EVAL_CP, -50)


class PremovePhaseTests(unittest.TestCase):
    def test_opening_premove_window_is_sampled_between_three_and_six_fullmoves(self):
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MIN, 3)
        self.assertEqual(PREMOVE_OPENING_FULLMOVE_MAX, 6)
        for opening_limit in range(3, 7):
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
