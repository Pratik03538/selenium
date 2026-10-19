"""Visible Windows cursor controls for an owned chessboard web app.

This module uses the real desktop pointer, not Chrome DevTools' synthetic mouse
events. It intentionally requires a fresh-position reader and a foreground-window
guard before it will click or drag. The caller supplies screen-coordinate board
geometry from read_selenium_board_geometry().

Usage:
    geometry = read_selenium_board_geometry(driver, "chess-board", bottom_side="white")
    backend = WindowsMouseBackend()
    controller = VisibleCursorController(backend=backend)
    focused = lambda: backend.foreground_title_contains("Mind War Chess")
    current_fen = lambda: driver.execute_script(
        "return window.__liveGameApiReader?.current?.()?.fen || '';"
    )

    move = controller.move_uci(
        "e2e4", expected_fen=fen, current_fen_reader=current_fen,
        geometry=geometry, bot_color="white", focus_guard=focused,
    )

The FEN reader is site-specific: replace the example with the read-only FEN
getter used by your own website. Do not run this controller against a board or
browser window you do not own or control.
"""
from __future__ import annotations

import ctypes
import math
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

try:
    import chess
except ImportError:  # Geometry/drawing helpers remain usable without python-chess.
    chess = None


Square = str
MouseButton = Literal["left", "right"]
FocusGuard = Callable[[], bool]
FenReader = Callable[[], str]


@dataclass(frozen=True, slots=True)
class BoardGeometry:
    """Board rectangle in physical desktop pixels."""

    left: float
    top: float
    width: float
    height: float
    bottom_side: str = "white"

    def __post_init__(self) -> None:
        side = str(self.bottom_side).strip().lower()
        if side not in {"white", "black"}:
            raise ValueError("bottom_side must be 'white' or 'black'")
        if not all(math.isfinite(float(v)) for v in (self.left, self.top, self.width, self.height)):
            raise ValueError("board geometry must contain finite coordinates")
        if float(self.width) < 80 or float(self.height) < 80:
            raise ValueError("board rectangle is too small; verify the board selector")
        if abs(float(self.width) - float(self.height)) / max(float(self.width), float(self.height)) > 0.12:
            raise ValueError("chessboard must be approximately square")
        object.__setattr__(self, "bottom_side", side)

    def square_center(self, square: Square) -> tuple[int, int]:
        """Map algebraic notation (e.g. e2) to a square centre on screen."""
        token = str(square or "").strip().lower()
        if not re.fullmatch(r"[a-h][1-8]", token):
            raise ValueError(f"invalid chess square: {square!r}")
        file_index = ord(token[0]) - ord("a")
        rank = int(token[1])
        if self.bottom_side == "white":
            column = file_index
            row = 8 - rank
        else:
            column = 7 - file_index
            row = rank - 1
        x = self.left + (column + 0.5) * self.width / 8.0
        y = self.top + (row + 0.5) * self.height / 8.0
        return round(x), round(y)


@dataclass(frozen=True, slots=True)
class CursorMotionConfig:
    """Visible movement profile. Durations are targets, not hard real-time guarantees."""

    approach_duration_s: float = 0.14
    move_drag_duration_s: float = 0.20
    arrow_drag_duration_s: float = 0.18
    settle_after_press_s: float = 0.012
    settle_after_release_s: float = 0.025
    min_steps: int = 8
    max_steps: int = 48
    curve_pixels: float = 3.0

    def __post_init__(self) -> None:
        for name in ("approach_duration_s", "move_drag_duration_s", "arrow_drag_duration_s"):
            value = float(getattr(self, name))
            if not 0.001 <= value <= 2.0:
                raise ValueError(f"{name} must be between 0.001 and 2.0 seconds")
        if not 0 <= self.settle_after_press_s <= 0.15:
            raise ValueError("settle_after_press_s must be between 0 and 0.15 seconds")
        if not 0 <= self.settle_after_release_s <= 0.2:
            raise ValueError("settle_after_release_s must be between 0 and 0.2 seconds")
        if not 4 <= self.min_steps <= self.max_steps <= 100:
            raise ValueError("cursor steps must satisfy 4 <= min_steps <= max_steps <= 100")
        if not 0 <= self.curve_pixels <= 12:
            raise ValueError("curve_pixels must be between 0 and 12")


@dataclass(frozen=True, slots=True)
class CursorActionResult:
    ok: bool
    action: str
    reason: str = ""
    route: tuple[str, ...] = ()
    duration_s: float = 0.0
    waypoints: int = 0


class MouseBackend(Protocol):
    """Minimal mouse backend interface; also useful for safe unit tests."""

    def get_position(self) -> tuple[int, int]: ...
    def set_position(self, x: int, y: int) -> bool: ...
    def press(self, button: MouseButton) -> None: ...
    def release(self, button: MouseButton) -> None: ...
    def release_all(self) -> None: ...
    def foreground_title_contains(self, fragment: str) -> bool: ...


class WindowsMouseBackend:
    """Win32 implementation which moves the actual visible desktop cursor."""

    _LEFT_DOWN = 0x0002
    _LEFT_UP = 0x0004
    _RIGHT_DOWN = 0x0008
    _RIGHT_UP = 0x0010

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("WindowsMouseBackend requires Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        self._point_type = POINT
        self._user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
        self._user32.GetCursorPos.restype = ctypes.c_bool
        self._user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._user32.SetCursorPos.restype = ctypes.c_bool
        self._user32.mouse_event.argtypes = [
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t
        ]
        self._user32.mouse_event.restype = None
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = ctypes.c_void_p
        self._user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [
            ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int
        ]
        self._user32.GetWindowTextW.restype = ctypes.c_int

        # Best effort: align this process's desktop coordinate space with the
        # physical-pixel coordinates returned by read_selenium_board_geometry().
        try:
            self._user32.SetProcessDPIAware()
        except Exception:
            pass

    def get_position(self) -> tuple[int, int]:
        point = self._point_type()
        if not self._user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(point.x), int(point.y)

    def set_position(self, x: int, y: int) -> bool:
        return bool(self._user32.SetCursorPos(int(x), int(y)))

    def press(self, button: MouseButton) -> None:
        flag = self._LEFT_DOWN if button == "left" else self._RIGHT_DOWN
        self._user32.mouse_event(flag, 0, 0, 0, 0)

    def release(self, button: MouseButton) -> None:
        flag = self._LEFT_UP if button == "left" else self._RIGHT_UP
        self._user32.mouse_event(flag, 0, 0, 0, 0)

    def release_all(self) -> None:
        # Always release both buttons, including after an interrupted gesture.
        self._user32.mouse_event(self._LEFT_UP, 0, 0, 0, 0)
        self._user32.mouse_event(self._RIGHT_UP, 0, 0, 0, 0)

    def foreground_title_contains(self, fragment: str) -> bool:
        """Return true only when the active desktop window title matches a fragment."""
        needle = str(fragment or "").strip().casefold()
        if not needle:
            return False
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return False
        length = max(1, min(self._user32.GetWindowTextLengthW(hwnd) + 1, 2048))
        buffer = ctypes.create_unicode_buffer(length)
        self._user32.GetWindowTextW(hwnd, buffer, length)
        return needle in buffer.value.casefold()


def read_selenium_board_geometry(
    driver,
    selector: str,
    bottom_side: str = "white",
) -> BoardGeometry:
    """Read visible board bounds and convert browser CSS pixels to desktop pixels.

    This uses the selected tab's current window position, browser-chrome height,
    and devicePixelRatio. Keep the browser window on-screen and re-read geometry
    after moving/resizing it or changing Windows display scaling.
    """
    script = r"""
        const selector = arguments[0];
        const el = document.querySelector(selector);
        if (!el) return {error: "board-not-found"};
        const r = el.getBoundingClientRect();
        const dpr = Number(window.devicePixelRatio || 1);
        const width = Number(r.width);
        const height = Number(r.height);
        if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
            return {error: "board-has-no-visible-size"};
        }
        return {
            left: (Number(window.screenX || 0) + Number(r.left)) * dpr,
            top: (Number(window.screenY || 0)
                + Number(window.outerHeight || window.innerHeight)
                - Number(window.innerHeight) + Number(r.top)) * dpr,
            width: width * dpr,
            height: height * dpr,
            dpr: dpr
        };
    """
    payload = driver.execute_script(script, selector)
    if not isinstance(payload, dict) or payload.get("error"):
        reason = payload.get("error", "invalid-geometry-response") if isinstance(payload, dict) else "invalid-geometry-response"
        raise RuntimeError(str(reason))
    return BoardGeometry(
        left=float(payload["left"]),
        top=float(payload["top"]),
        width=float(payload["width"]),
        height=float(payload["height"]),
        bottom_side=bottom_side,
    )


def positions_match(fen_a: str, fen_b: str) -> bool:
    """Compare board state fields while ignoring move counters and clock fields."""
    a = str(fen_a or "").strip().split()
    b = str(fen_b or "").strip().split()
    if len(a) < 4 or len(b) < 4:
        return False
    return a[:4] == b[:4]


class VisibleCursorController:
    """Visible pointer controller for a chessboard on a site you control.

    A fresh FEN reader and a positive foreground-window guard are required on
    every gesture. Drawn arrow routes are deduplicated once per game.
    """

    def __init__(
        self,
        backend: MouseBackend | None = None,
        motion: CursorMotionConfig | None = None,
        enabled: bool = True,
    ) -> None:
        self.backend: MouseBackend = backend if backend is not None else WindowsMouseBackend()
        self.motion = motion or CursorMotionConfig()
        self.enabled = bool(enabled)
        self._game_id: str | None = None
        self._drawn_arrows: set[tuple[str, str]] = set()

    def reset_game(self, game_id: str | int) -> None:
        self._game_id = str(game_id)
        self._drawn_arrows.clear()

    @staticmethod
    def _call_guard(focus_guard: FocusGuard | None) -> bool:
        if focus_guard is None:
            return False
        try:
            return bool(focus_guard())
        except Exception:
            return False

    @staticmethod
    def _read_fen(current_fen_reader: FenReader | None) -> str:
        if current_fen_reader is None:
            return ""
        try:
            return str(current_fen_reader() or "").strip()
        except Exception:
            return ""

    def _position_guard(
        self,
        expected_fen: str,
        current_fen_reader: FenReader | None,
    ) -> tuple[bool, str]:
        current = self._read_fen(current_fen_reader)
        if not current:
            return False, "fresh-position-unavailable"
        if not positions_match(expected_fen, current):
            return False, "board-position-changed"
        return True, ""

    def _set_position(self, point: tuple[int, int]) -> None:
        if not self.backend.set_position(int(point[0]), int(point[1])):
            raise OSError(f"SetCursorPos failed at {point}")

    def _follow_curve(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_s: float,
    ) -> int:
        x0, y0 = start
        x3, y3 = end
        dx, dy = x3 - x0, y3 - y0
        distance = math.hypot(dx, dy)
        if distance < 1:
            self._set_position(end)
            return 1

        steps = max(
            self.motion.min_steps,
            min(self.motion.max_steps, int(distance / 15.0) + self.motion.min_steps),
        )
        normal_x, normal_y = -dy / distance, dx / distance
        bend = min(self.motion.curve_pixels, distance * 0.025)
        # A small smooth arc makes the path continuous without leaving the target
        # square for long or generating a series of teleport-like jumps.
        sign = 1.0 if (x0 + y0 + x3 + y3) % 2 == 0 else -1.0
        offset_x, offset_y = normal_x * bend * sign, normal_y * bend * sign
        c1 = (x0 + dx * 0.30 + offset_x, y0 + dy * 0.30 + offset_y)
        c2 = (x0 + dx * 0.70 + offset_x, y0 + dy * 0.70 + offset_y)
        interval = max(0.0, float(duration_s)) / steps

        for index in range(1, steps + 1):
            t = index / steps
            eased = t * t * (3.0 - 2.0 * t)
            one_minus = 1.0 - eased
            x = (
                one_minus ** 3 * x0
                + 3.0 * one_minus ** 2 * eased * c1[0]
                + 3.0 * one_minus * eased ** 2 * c2[0]
                + eased ** 3 * x3
            )
            y = (
                one_minus ** 3 * y0
                + 3.0 * one_minus ** 2 * eased * c1[1]
                + 3.0 * one_minus * eased ** 2 * c2[1]
                + eased ** 3 * y3
            )
            self._set_position((round(x), round(y)))
            if interval:
                time.sleep(interval)

        # The final loop iteration already placed the cursor at the exact endpoint.
        return steps

    def _drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        button: MouseButton,
        duration_s: float,
        *,
        expected_fen: str,
        current_fen_reader: FenReader,
        focus_guard: FocusGuard,
    ) -> int:
        # The caller has already placed the cursor on the source square.
        # Avoid an extra OS cursor-position command immediately before the press.
        if not self._call_guard(focus_guard):
            raise PermissionError("foreground-window-guard-failed")
        valid, reason = self._position_guard(expected_fen, current_fen_reader)
        if not valid:
            raise RuntimeError(reason)

        pressed = False
        try:
            self.backend.press(button)
            pressed = True
            if self.motion.settle_after_press_s:
                time.sleep(self.motion.settle_after_press_s)
            drag_steps = self._follow_curve(start, end, duration_s)
        finally:
            try:
                if pressed:
                    self.backend.release(button)
            finally:
                self.backend.release_all()
        if self.motion.settle_after_release_s:
            time.sleep(self.motion.settle_after_release_s)
        return drag_steps

    def move_uci(
        self,
        uci: str,
        *,
        expected_fen: str,
        current_fen_reader: FenReader | None,
        geometry: BoardGeometry,
        bot_color: str,
        focus_guard: FocusGuard | None,
        promotion_handler: Callable[[str], bool] | None = None,
        allow_premove: bool = False,
        premove_prevalidated: bool = False,
        position_prevalidated: bool = False,
    ) -> CursorActionResult:
        """Legality-check and perform a visible left-button piece drag."""
        started = time.monotonic()
        token = str(uci or "").strip().lower()
        if not self.enabled:
            return CursorActionResult(False, "move", "controller-disabled", (token,))
        if chess is None:
            return CursorActionResult(False, "move", "python-chess-required", (token,))
        if not self._call_guard(focus_guard):
            return CursorActionResult(False, "move", "foreground-window-guard-failed", (token,))
        if not position_prevalidated:
            valid, reason = self._position_guard(expected_fen, current_fen_reader)
            if not valid:
                return CursorActionResult(False, "move", reason, (token,))

        normalized_color = str(bot_color or "").strip().lower()
        if normalized_color not in {"white", "black"}:
            return CursorActionResult(False, "move", "bot-color-unconfirmed", (token,))
        try:
            board = chess.Board(str(expected_fen))
            expected_turn = chess.WHITE if normalized_color == "white" else chess.BLACK
            if allow_premove:
                # A premove isn't necessarily legal in the current position.
                # The caller must first validate it with its site's queue/drag API.
                if not premove_prevalidated:
                    return CursorActionResult(False, "move", "premove-validation-required", (token,))
                move = chess.Move.from_uci(token)
            else:
                if board.turn != expected_turn:
                    return CursorActionResult(False, "move", "not-bot-turn", (token,))
                move = board.parse_uci(token)
            piece = board.piece_at(move.from_square)
            if piece is None or piece.color != expected_turn:
                return CursorActionResult(False, "move", "source-piece-not-owned-by-bot", (token,))
            if move.promotion is not None and promotion_handler is None and not allow_premove:
                return CursorActionResult(False, "move", "promotion-handler-required", (token,))
            source = geometry.square_center(chess.square_name(move.from_square))
            destination = geometry.square_center(chess.square_name(move.to_square))
        except (ValueError, TypeError, IndexError):
            return CursorActionResult(False, "move", "illegal-or-invalid-uci", (token,))

        try:
            current_pointer = self.backend.get_position()
            approach_steps = self._follow_curve(
                current_pointer, source, self.motion.approach_duration_s
            )
            drag_steps = self._drag(
                source,
                destination,
                "left",
                self.motion.move_drag_duration_s,
                expected_fen=expected_fen,
                current_fen_reader=current_fen_reader,  # type: ignore[arg-type]
                focus_guard=focus_guard,  # type: ignore[arg-type]
            )
            if move.promotion is not None:
                promotion_symbol = chess.piece_symbol(move.promotion)
                if not bool(promotion_handler(promotion_symbol)):
                    return CursorActionResult(
                        False, "move", "promotion-choice-failed", (token,),
                        time.monotonic() - started,
                    )
            return CursorActionResult(
                True, "move", "", (token,), time.monotonic() - started,
                approach_steps + drag_steps,
            )
        except Exception as exc:
            try:
                self.backend.release_all()
            except Exception:
                pass
            return CursorActionResult(
                False, "move", f"input-failed: {exc}", (token,),
                time.monotonic() - started,
            )

    def preview_legal_moves(
        self,
        fen: str,
        *,
        geometry: BoardGeometry,
        focus_guard: FocusGuard | None,
        max_moves: int = 2,
        current_fen_reader: FenReader | None = None,
        position_prevalidated: bool = False,
    ) -> CursorActionResult:
        """Move the pointer along one or two legal-looking next moves without clicking.

        This is intentionally engine-free. It only uses the already-known board
        position and legal move generation, and it never presses/releases a button.
        The caller can pass position_prevalidated=True when the tracker just
        received the same FEN from its existing confirmation watcher.
        """
        started = time.monotonic()
        if not self.enabled:
            return CursorActionResult(False, "preview", "controller-disabled")
        if chess is None:
            return CursorActionResult(False, "preview", "python-chess-required")
        if not self._call_guard(focus_guard):
            return CursorActionResult(False, "preview", "foreground-window-guard-failed")
        if not position_prevalidated:
            valid, reason = self._position_guard(fen, current_fen_reader)
            if not valid:
                return CursorActionResult(False, "preview", reason)

        try:
            board = chess.Board(str(fen or "").strip())
            legal_moves = list(board.legal_moves)
        except (ValueError, TypeError, IndexError):
            return CursorActionResult(False, "preview", "invalid-fen")
        if not legal_moves:
            return CursorActionResult(False, "preview", "no-legal-next-moves")

        try:
            requested = max(1, min(2, int(max_moves)))
        except (TypeError, ValueError):
            requested = 2
        target_count = random.SystemRandom().randint(1, min(requested, len(legal_moves)))

        # No Stockfish or tactical scoring here: shuffle the already-generated
        # legal moves, then visit source/destination squares for distinct pieces.
        candidate_pool = list(legal_moves)
        random.SystemRandom().shuffle(candidate_pool)

        selected = []
        seen_sources = set()
        for move in candidate_pool:
            if move.from_square in seen_sources:
                continue
            selected.append(move)
            seen_sources.add(move.from_square)
            if len(selected) >= target_count:
                break
        if not selected:
            return CursorActionResult(False, "preview", "no-distinct-legal-sources")

        try:
            current_point = self.backend.get_position()
            total_steps = 0
            # Preview legs are shorter than real move gestures; do not click or
            # hold either button during this pointer-only movement.
            leg_duration = max(0.001, min(0.002, self.motion.approach_duration_s))
            route = []
            for move in selected:
                if not self._call_guard(focus_guard):
                    return CursorActionResult(
                        False, "preview", "foreground-window-guard-failed",
                        tuple(route), time.monotonic() - started, total_steps,
                    )
                source = chess.square_name(move.from_square)
                destination = chess.square_name(move.to_square)
                for square in (source, destination):
                    point = geometry.square_center(square)
                    total_steps += self._follow_curve(current_point, point, leg_duration)
                    current_point = point
                route.append(move.uci())
            return CursorActionResult(
                True, "preview", "", tuple(route),
                time.monotonic() - started, total_steps,
            )
        except Exception as exc:
            try:
                self.backend.release_all()
            except Exception:
                pass
            return CursorActionResult(
                False, "preview", f"input-failed: {exc}", tuple(),
                time.monotonic() - started,
            )

    def draw_highlight(
        self,
        square: Square,
        *,
        expected_fen: str,
        current_fen_reader: FenReader | None,
        geometry: BoardGeometry,
        focus_guard: FocusGuard | None,
        auto_draw_enabled: bool,
        opening_fast_mode: bool,
        fast_mode: bool,
        bot_clock_seconds: float | None,
        low_clock_trigger_seconds: float = 10.0,
        position_prevalidated: bool = False,
    ) -> CursorActionResult:
        """Perform a native right-click on one square to highlight it."""
        started = time.monotonic()
        token = str(square or "").strip().lower()
        route = (token,)
        if not self.enabled:
            return CursorActionResult(False, "highlight", "controller-disabled", route)
        if not auto_draw_enabled:
            return CursorActionResult(False, "highlight", "auto-draw-disabled", route)
        if opening_fast_mode:
            return CursorActionResult(False, "highlight", "opening-fast-mode", route)
        if fast_mode:
            return CursorActionResult(False, "highlight", "fast-mode", route)
        if bot_clock_seconds is None:
            return CursorActionResult(False, "highlight", "bot-clock-unavailable", route)
        try:
            threshold = min(10.0, max(5.0, float(low_clock_trigger_seconds)))
            if float(bot_clock_seconds) <= threshold:
                return CursorActionResult(False, "highlight", "bot-low-clock", route)
        except (TypeError, ValueError):
            return CursorActionResult(False, "highlight", "invalid-bot-clock", route)
        if not re.fullmatch(r"[a-h][1-8]", token):
            return CursorActionResult(False, "highlight", "invalid-square", route)
        if not self._call_guard(focus_guard):
            return CursorActionResult(False, "highlight", "foreground-window-guard-failed", route)
        if not position_prevalidated:
            valid, reason = self._position_guard(expected_fen, current_fen_reader)
            if not valid:
                return CursorActionResult(False, "highlight", reason, route)

        point = geometry.square_center(token)
        try:
            approach_steps = self._follow_curve(
                self.backend.get_position(), point, self.motion.approach_duration_s
            )
            if not self._call_guard(focus_guard):
                return CursorActionResult(
                    False, "highlight", "foreground-window-guard-failed", route,
                    time.monotonic() - started,
                )
            valid, reason = self._position_guard(expected_fen, current_fen_reader)
            if not valid:
                return CursorActionResult(
                    False, "highlight", reason, route, time.monotonic() - started
                )
            self._set_position(point)
            self.backend.press("right")
            try:
                if self.motion.settle_after_press_s:
                    time.sleep(self.motion.settle_after_press_s)
            finally:
                self.backend.release("right")
                self.backend.release_all()
            if self.motion.settle_after_release_s:
                time.sleep(self.motion.settle_after_release_s)
            return CursorActionResult(
                True, "highlight", "", route, time.monotonic() - started,
                approach_steps + 1,
            )
        except Exception as exc:
            try:
                self.backend.release_all()
            except Exception:
                pass
            return CursorActionResult(
                False, "highlight", f"input-failed: {exc}", route,
                time.monotonic() - started,
            )

    def draw_arrow(
        self,
        start_square: Square,
        end_square: Square,
        *,
        expected_fen: str,
        current_fen_reader: FenReader | None,
        geometry: BoardGeometry,
        game_id: str | int,
        focus_guard: FocusGuard | None,
        auto_draw_enabled: bool,
        opening_fast_mode: bool,
        fast_mode: bool,
        bot_clock_seconds: float | None,
        low_clock_trigger_seconds: float = 10.0,
        position_prevalidated: bool = False,
    ) -> CursorActionResult:
        """Perform a native right-button drag to draw an arrow on the board."""
        started = time.monotonic()
        start = str(start_square or "").strip().lower()
        end = str(end_square or "").strip().lower()
        route = (start, end)
        if not self.enabled:
            return CursorActionResult(False, "draw", "controller-disabled", route)
        if not auto_draw_enabled:
            return CursorActionResult(False, "draw", "auto-draw-disabled", route)
        if opening_fast_mode:
            return CursorActionResult(False, "draw", "opening-fast-mode", route)
        if fast_mode:
            return CursorActionResult(False, "draw", "fast-mode", route)
        if bot_clock_seconds is None:
            return CursorActionResult(False, "draw", "bot-clock-unavailable", route)
        try:
            threshold = min(10.0, max(5.0, float(low_clock_trigger_seconds)))
            if float(bot_clock_seconds) <= threshold:
                return CursorActionResult(False, "draw", "bot-low-clock", route)
        except (TypeError, ValueError):
            return CursorActionResult(False, "draw", "invalid-bot-clock", route)
        if not re.fullmatch(r"[a-h][1-8]", start) or not re.fullmatch(r"[a-h][1-8]", end):
            return CursorActionResult(False, "draw", "invalid-square", route)
        if start == end:
            return CursorActionResult(False, "draw", "same-square-route", route)
        if not self._call_guard(focus_guard):
            return CursorActionResult(False, "draw", "foreground-window-guard-failed", route)
        if not position_prevalidated:
            valid, reason = self._position_guard(expected_fen, current_fen_reader)
            if not valid:
                return CursorActionResult(False, "draw", reason, route)

        current_game_id = str(game_id)
        if current_game_id != self._game_id:
            self._game_id = current_game_id
            self._drawn_arrows.clear()
        if route in self._drawn_arrows:
            return CursorActionResult(False, "draw", "route-already-drawn-this-game", route)

        start_point = geometry.square_center(start)
        end_point = geometry.square_center(end)
        try:
            current_pointer = self.backend.get_position()
            approach_steps = self._follow_curve(
                current_pointer, start_point, self.motion.approach_duration_s
            )
            drag_steps = self._drag(
                start_point,
                end_point,
                "right",
                self.motion.arrow_drag_duration_s,
                expected_fen=expected_fen,
                current_fen_reader=current_fen_reader,  # type: ignore[arg-type]
                focus_guard=focus_guard,  # type: ignore[arg-type]
            )
            self._drawn_arrows.add(route)
            return CursorActionResult(
                True, "draw", "", route, time.monotonic() - started,
                approach_steps + drag_steps,
            )
        except Exception as exc:
            try:
                self.backend.release_all()
            except Exception:
                pass
            return CursorActionResult(
                False, "draw", f"input-failed: {exc}", route,
                time.monotonic() - started,
            )
