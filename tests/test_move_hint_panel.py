"""Unit tests for legal move and red-premove hint descriptions."""
import unittest

import chess

from move_hint_panel import build_hint, build_premove_hint, square_to_display


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
