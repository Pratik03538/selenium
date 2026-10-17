"""Unit tests for legal move and red-premove hint descriptions."""
import unittest

import chess

from move_hint_panel import (
    build_hint, build_premove_hint, build_coach_variations,
    square_to_display, toggle_output_mode, toggle_hint_visibility,
)


class TacticalCoachVariationTests(unittest.TestCase):
    def test_multipv_lines_become_legal_step_by_step_variations(self):
        board = chess.Board()
        variations = build_coach_variations(board.fen(), [
            {"pv": [
                chess.Move.from_uci("e2e4"),
                chess.Move.from_uci("e7e5"),
                chess.Move.from_uci("g1f3"),
            ]},
            {"pv": [
                chess.Move.from_uci("d2d4"),
                chess.Move.from_uci("d7d5"),
                chess.Move.from_uci("c2c4"),
            ]},
        ])
        self.assertEqual(len(variations), 2)
        self.assertEqual(variations[0]["sequence"], "e4 → e5 → Nf3")
        self.assertEqual(variations[1]["sequence"], "d4 → d5 → c4")
        self.assertEqual(variations[0]["moves"][0]["side"], "white")
        self.assertEqual(variations[0]["moves"][1]["side"], "black")
        self.assertEqual(variations[0]["moves"][2]["uci"], "g1f3")

    def test_empty_or_missing_engine_lines_returns_no_variations(self):
        board = chess.Board()
        self.assertEqual(build_coach_variations(board.fen(), []), [])
        self.assertEqual(build_coach_variations(board.fen(), [{"pv": []}]), [])

    def test_illegal_move_truncates_a_principal_variation(self):
        board = chess.Board()
        variations = build_coach_variations(board.fen(), [{
            "pv": [
                chess.Move.from_uci("e2e4"),
                chess.Move.from_uci("e2e5"),
                chess.Move.from_uci("e7e5"),
            ]
        }])
        self.assertEqual(len(variations), 1)
        self.assertEqual(variations[0]["sequence"], "e4")

class HintVisibilityToggleTests(unittest.TestCase):
    def test_enabled_overlay_switches_off(self):
        self.assertFalse(toggle_hint_visibility(True))

    def test_disabled_overlay_switches_on(self):
        self.assertTrue(toggle_hint_visibility(False))


class OutputModeToggleTests(unittest.TestCase):
    def test_hint_mode_switches_to_existing_autoplay_path(self):
        self.assertEqual(toggle_output_mode("hint"), "execute")

    def test_autoplay_mode_switches_back_to_hint(self):
        self.assertEqual(toggle_output_mode("execute"), "hint")

    def test_unknown_mode_fails_safe_to_hint(self):
        self.assertEqual(toggle_output_mode("unexpected"), "hint")


class MoveHintDescriptionTests(unittest.TestCase):
    def test_regular_suggestion_is_validated_against_current_position(self):
        board = chess.Board()
        hint = build_hint(board.fen(), "e2e4")
        self.assertEqual(hint["uci"], "e2e4")
        self.assertEqual(hint["san"], "e4")

    def test_regular_illegal_move_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_hint(board.fen(), "e2e5")

    def test_premove_is_validated_after_predicted_opponent_reply(self):
        board = chess.Board()
        hint = build_premove_hint(board.fen(), "g8f6", "e2e4")
        self.assertEqual(hint["predicted_reply_san"], "e4")
        self.assertEqual(hint["san"], "Nf6")
        self.assertEqual(hint["uci"], "g8f6")

    def test_premove_illegal_after_predicted_reply_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_premove_hint(board.fen(), "e2e3", "e2e4")

    def test_illegal_predicted_reply_is_rejected(self):
        board = chess.Board()
        with self.assertRaises(ValueError):
            build_premove_hint(board.fen(), "g8f6", "e2e5")

    def test_promotion_premove_is_recognized(self):
        board = chess.Board("4k3/P7/8/8/8/8/8/4K3 b - - 0 1")
        hint = build_premove_hint(board.fen(), "a7a8q", "e8d7")
        self.assertEqual(hint["promotion"], chess.QUEEN)
        self.assertEqual(hint["san"], "a8=Q")

    def test_white_orientation_square_coordinates(self):
        self.assertEqual(square_to_display(chess.A1, "white"), (0, 7))
        self.assertEqual(square_to_display(chess.H8, "white"), (7, 0))

    def test_black_orientation_square_coordinates(self):
        self.assertEqual(square_to_display(chess.A1, "black"), (7, 0))
        self.assertEqual(square_to_display(chess.H8, "black"), (0, 7))


if __name__ == "__main__":
    unittest.main()
