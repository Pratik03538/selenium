"""Private-output live chess DOM/API reader with optional owned-site autoplay.

Attaches to an existing Chrome debugging session, reads the exposed game API,
reconstructs moves from FEN changes, and writes a local CSV. Optional Stockfish
autoplay is available only on the exact configured authorized site; known public
competitive chess hosts are always rejected. URLs, hostnames and page titles are
never printed or stored in the CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import ipaddress
import re
import random
import sys
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.common.action_chains import ActionChains
except ImportError as exc:
    raise SystemExit(
        "Missing Selenium. Install with: python -m pip install -U selenium"
    ) from exc

try:
    from seleniumbase import Driver as SeleniumBaseDriver
except ImportError:
    SeleniumBaseDriver = None  # Optional unless --browser-mode uc is selected.

try:
    import chess
    import chess.engine
except ImportError:
    chess = None  # FEN/DOM capture works; reconstruction/autoplay need python-chess.

BASE_DIR = Path(__file__).resolve().parent
BLOCKED_PLATFORMS_FILE = BASE_DIR / "blocked_platforms.txt"
# Use a new private log so a prior CSV with raw URLs is not appended to.
DEFAULT_CSV = BASE_DIR / "live_dom_events_private_v2.csv"
CSV_FIELDS = [
    "timestamp_utc", "board_found", "game_api_found", "api_methods",
    "fen", "turn", "playing_as", "is_my_turn", "bottom_side", "bottom_side_source",
    "game_number", "move_count", "last_move_san", "last_move_uci",
    "history_source", "moves_json", "dom_moves_json",
    "legal_move_count", "legal_moves_json", "white_clock",
    "black_clock", "result", "note",
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def normalize_chess_color(value: object) -> str | None:
    """Normalize explicit color tokens; never infer a player's color from board orientation."""
    if value is None or isinstance(value, bool):
        return None
    token = str(value).strip().lower()
    if token in {"white", "w", "light", "white-side", "white_side"}:
        return "white"
    if token in {"black", "b", "dark", "black-side", "black_side"}:
        return "black"
    return None


def normalize_api_bot_color(playing_as_color: object, playing_as: object) -> tuple[str | None, bool]:
    """Normalize both API side fields and flag disagreement instead of picking one."""
    primary = normalize_chess_color(playing_as_color)
    fallback = normalize_chess_color(playing_as)
    if primary and fallback and primary != fallback:
        return None, True
    return primary or fallback, False


class PlayerSideResolver:
    """Resolve the bot's side once per game; conflicting signals must pause autoplay."""

    def __init__(self, fixed_color: str | None = None, player_name_enabled: bool = False):
        self.fixed_color = normalize_chess_color(fixed_color)
        self.player_name_enabled = bool(player_name_enabled)
        self.active_color = self.fixed_color
        self.name_candidate: str | None = None
        self.name_candidate_count = 0

    def reset_for_new_game(self) -> None:
        self.active_color = self.fixed_color
        self.name_candidate = None
        self.name_candidate_count = 0

    def resolve(self, api_color: str | None, player_name_color: str | None) -> str | None:
        """Lock side per game; a disagreeing source returns None rather than flipping colors."""
        api_color = normalize_chess_color(api_color)
        name_color = (
            normalize_chess_color(player_name_color)
            if self.player_name_enabled else None
        )

        if self.fixed_color:
            self.active_color = self.fixed_color
            if (
                (api_color and api_color != self.fixed_color)
                or (name_color and name_color != self.fixed_color)
            ):
                return None
            return self.active_color

        # Once confirmed, a bot cannot change color until a new game is detected.
        # This prevents one transient API/name snapshot from reversing move preference.
        if self.active_color:
            if (
                (api_color and api_color != self.active_color)
                or (name_color and name_color != self.active_color)
            ):
                return None
            return self.active_color

        # API evidence is preferred, but conflicting simultaneously visible name
        # evidence must be resolved before locking the color.
        if api_color:
            if name_color and name_color != api_color:
                return None
            self.active_color = api_color
            self.name_candidate = api_color
            self.name_candidate_count = 2
            return self.active_color

        if not self.player_name_enabled:
            return None
        if not name_color:
            self.name_candidate = None
            self.name_candidate_count = 0
            return None

        if name_color == self.name_candidate:
            self.name_candidate_count += 1
        else:
            self.name_candidate = name_color
            self.name_candidate_count = 1

        if self.name_candidate_count >= 2:
            self.active_color = name_color
            return self.active_color
        return None


class MoveAttemptRetryGate:
    """Bounded retries for a position, without duplicate submissions after ambiguous acceptance."""

    MAX_ATTEMPTS = 3
    RETRY_BACKOFF_SECONDS = (0.20, 0.50)

    def __init__(self):
        self.fen: str | None = None
        self.attempts = 0
        self.last_attempt_at = 0.0
        self.retryable = False
        self.pending = False
        self.accepted_unconfirmed = False

    def should_attempt(self, fen: str, now: float | None = None) -> bool:
        if not fen:
            return False
        if fen != self.fen:
            return True
        if (
            self.pending
            or self.accepted_unconfirmed
            or not self.retryable
            or self.attempts >= self.MAX_ATTEMPTS
        ):
            return False
        current = time.monotonic() if now is None else float(now)
        delay_index = min(max(0, self.attempts - 1), len(self.RETRY_BACKOFF_SECONDS) - 1)
        return current - self.last_attempt_at >= self.RETRY_BACKOFF_SECONDS[delay_index]

    def begin(self, fen: str, now: float | None = None) -> bool:
        if not fen:
            return False
        current = time.monotonic() if now is None else float(now)
        if fen != self.fen:
            self.fen = fen
            self.attempts = 0
            self.last_attempt_at = 0.0
            self.retryable = False
            self.pending = False
            self.accepted_unconfirmed = False
        elif not self.should_attempt(fen, current):
            return False
        if self.attempts >= self.MAX_ATTEMPTS:
            return False
        self.attempts += 1
        self.last_attempt_at = current
        self.retryable = False
        self.pending = True
        self.accepted_unconfirmed = False
        return True

    def finish(self, result: object) -> None:
        if not self.pending:
            return
        self.pending = False
        if isinstance(result, dict) and result.get("ok"):
            self.retryable = False
            self.accepted_unconfirmed = False
        elif isinstance(result, dict) and (
            result.get("api_accepted")
            or result.get("reason") == "api-submit-unavailable"
        ):
            # Ambiguous acceptance must not be blindly duplicated on the same FEN.
            self.retryable = False
            self.accepted_unconfirmed = True
        else:
            self.retryable = self.attempts < self.MAX_ATTEMPTS
            self.accepted_unconfirmed = False

    def finish_exception(self) -> None:
        # Exceptions after a confirmed submission must not reopen that position.
        if self.pending:
            self.pending = False
            self.retryable = self.attempts < self.MAX_ATTEMPTS
            self.accepted_unconfirmed = False


def normalize_host(value: str):
    """Accept a specific hostname or URL; reject public-suffix-only values."""
    value = (value or "").strip().lower()
    if not value:
        return ""
    # Wildcard subdomains are fine only when a real base hostname follows.
    value = value.removeprefix("*.")
    parsed = urlsplit(value if "://" in value else "//" + value)
    host = (parsed.hostname or "").rstrip(".")
    if not host or host.startswith("."):
        return ""
    host = host.removeprefix("www.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if host == "localhost":
        return host
    # Do not let an incomplete value such as ".com" or "com" match every
    # website under that public suffix.
    public_suffixes = {
        "com", "org", "net", "edu", "gov", "mil", "int", "in", "uk",
        "io", "dev", "app", "au", "ca", "de", "fr", "jp", "us",
        "co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "org.in",
        "net.in", "gen.in", "firm.in", "ac.in", "edu.in", "gov.in",
        "com.au", "net.au", "org.au", "co.jp", "co.nz",
    }
    labels = host.split(".")
    if len(labels) < 2 or host in public_suffixes or any(not label for label in labels):
        return ""
    return host


def load_allowed_host_from_txt() -> tuple[str, str]:
    """Load the allowed hostname from a neighbouring TXT settings file."""
    candidates = [
        BASE_DIR / "tracker_settings.txt",
        Path.cwd() / "tracker_settings.txt",
        BASE_DIR / "site_settings.txt",
        Path.cwd() / "site_settings.txt",
        BASE_DIR / "bot_settings.txt",
        Path.cwd() / "bot_settings.txt",
    ]
    seen: set[str] = set()
    for settings_file in candidates:
        resolved = str(settings_file.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        if not settings_file.is_file():
            continue
        try:
            lines = settings_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise SystemExit(
                f"Cannot read the settings TXT file ({settings_file.name})."
            ) from exc

        values: dict[str, str] = {}
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().lower()] = value.strip()

        raw = (
            values.get("site_domain")
            or values.get("allowed_domain")
            or values.get("domain")
            or values.get("site_url")
            or values.get("url")
            or ""
        )
        host = normalize_host(raw)
        if host:
            return host, settings_file.name
    return "", ""


def load_optional_txt_setting(setting_key: str) -> str:
    """Read an optional value from the adjacent settings TXT without printing it."""
    candidates = [
        BASE_DIR / "tracker_settings.txt", Path.cwd() / "tracker_settings.txt",
        BASE_DIR / "site_settings.txt", Path.cwd() / "site_settings.txt",
        BASE_DIR / "bot_settings.txt", Path.cwd() / "bot_settings.txt",
    ]
    seen: set[str] = set()
    for settings_file in candidates:
        resolved = str(settings_file.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        if not settings_file.is_file():
            continue
        try:
            lines = settings_file.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip().lower() == setting_key.strip().lower() and value.strip():
                return value.strip().strip('"').strip("'")
    return ""



class RuntimeSettingsWindow:
    """Compact live dashboard and runtime controls for the current run."""

    LABELS = {
        "auto_play": "BOT",
        "auto_next_game": "NEW MATCH",
        "auto_rematch": "REMATCH",
        "premove_enabled": "NORMAL PREMOVES",
        "low_clock_extra_premoves": "LAST 7–10s EXTRA PREMOVES",
        "clock_management": "CLOCK-BASED TIME MANAGEMENT",
    }
    BUTTON_LABELS = {
        "auto_play": "BOT",
        "auto_next_game": "NEW MATCH",
        "auto_rematch": "REMATCH",
        "premove_enabled": "PREMOVES",
        "low_clock_extra_premoves": "EXTRA PREMOVES · 7–10s",
        "clock_management": "CLOCK MANAGEMENT",
    }

    def __init__(self, args, driver, allowed_host, cfg, chosen_handle):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.args = args
        self.driver = driver
        self.allowed_host = allowed_host
        self.cfg = cfg
        self.chosen_handle = chosen_handle
        self.stop_requested = False
        self.buttons = {}
        self.settings = {
            key: bool(getattr(args, key))
            for key in self.LABELS
        }
        self.move_rows = {}
        self.move_order = []
        self.last_eval_white_cp = None
        self.latest_game_number = 1

        self.root = tk.Tk()
        self.root.title("Live Chess Dashboard — Runtime Controls")
        self.root.geometry("970x700")
        self.root.minsize(840, 600)
        self.root.resizable(True, True)
        self.root.configure(padx=10, pady=8, background="#f3f5f8")
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.option_add("*Font", ("Segoe UI", 9))

        header = tk.Frame(self.root, bg="#f3f5f8")
        header.pack(fill="x", pady=(0, 5))
        tk.Label(
            header, text="LIVE CHESS DASHBOARD",
            font=("Segoe UI", 15, "bold"), anchor="w",
            bg="#f3f5f8", fg="#172033",
        ).pack(fill="x")
        tk.Label(
            header,
            text="Compact runtime switches · live moves · evaluation · game stats",
            font=("Segoe UI", 9), anchor="w",
            bg="#f3f5f8", fg="#5b6472",
        ).pack(fill="x")

        controls = tk.LabelFrame(
            self.root, text="Runtime switches",
            padx=5, pady=4, bg="#f3f5f8", fg="#283548",
        )
        controls.pack(fill="x", pady=(0, 6))
        for column in range(3):
            controls.grid_columnconfigure(column, weight=1, uniform="switches")
        for index, key in enumerate(self.LABELS):
            button = tk.Button(
                controls,
                text="",
                font=("Segoe UI", 8, "bold"),
                anchor="center",
                padx=5,
                pady=3,
                bd=1,
                relief="raised",
                command=lambda setting=key: self.toggle(setting),
            )
            button.grid(
                row=index // 3, column=index % 3,
                sticky="ew", padx=3, pady=2,
            )
            self.buttons[key] = button
            self.refresh_button(key)

        overview = tk.LabelFrame(
            self.root, text="Live match overview",
            padx=8, pady=5, bg="#f3f5f8", fg="#283548",
        )
        overview.pack(fill="x", pady=(0, 6))

        self.game_stats = tk.StringVar(
            value="Game 1 | Moves: waiting | Turn: ? | Bot side: ? | Status: waiting"
        )
        tk.Label(
            overview, textvariable=self.game_stats,
            font=("Consolas", 9, "bold"), anchor="w",
            justify="left", bg="#f3f5f8", fg="#263248",
        ).pack(fill="x", pady=(0, 3))

        self.material_stats = tk.StringVar(
            value="Pieces: White ? / Black ?    Material: White ? / Black ?    Legal moves: ?"
        )
        tk.Label(
            overview, textvariable=self.material_stats,
            font=("Segoe UI", 9), anchor="w",
            justify="left", bg="#f3f5f8", fg="#5b6472",
        ).pack(fill="x", pady=(0, 3))

        self.clock_status = tk.StringVar(
            value="White --:--  |  Black --:--  |  Your clock: --:--"
        )
        tk.Label(
            overview, textvariable=self.clock_status,
            font=("Consolas", 9), justify="left",
            anchor="w", bg="#f3f5f8", fg="#263248",
        ).pack(fill="x", pady=(0, 4))

        self.evaluation_status = tk.StringVar(
            value="Evaluation: waiting for Stockfish analysis"
        )
        tk.Label(
            overview, textvariable=self.evaluation_status,
            font=("Segoe UI", 10, "bold"), anchor="w",
            bg="#f3f5f8", fg="#263248",
        ).pack(fill="x")
        self.eval_canvas = tk.Canvas(
            overview, height=25, bg="#e4e8ee",
            highlightthickness=1, highlightbackground="#c5ccd6",
        )
        self.eval_canvas.pack(fill="x", pady=(3, 1))
        self.eval_canvas.bind("<Configure>", lambda _event: self._draw_eval_bar())
        self._draw_eval_bar()

        move_frame = tk.LabelFrame(
            self.root, text="Live move history — scroll for earlier moves",
            padx=5, pady=4, bg="#f3f5f8", fg="#283548",
        )
        move_frame.pack(fill="both", expand=True, pady=(0, 5))

        columns = ("ply", "side", "move", "rank", "evaluation", "status")
        self.moves_tree = ttk.Treeview(
            move_frame, columns=columns, show="headings",
            height=10, selectmode="browse",
        )
        headings = {
            "ply": "Ply",
            "side": "Side",
            "move": "Move",
            "rank": "Move reference",
            "evaluation": "Eval (White POV)",
            "status": "Status",
        }
        widths = {
            "ply": 55, "side": 58, "move": 92,
            "rank": 115, "evaluation": 125, "status": 150,
        }
        for column in columns:
            self.moves_tree.heading(column, text=headings[column])
            self.moves_tree.column(
                column, width=widths[column],
                minwidth=48, anchor="center",
                stretch=column in {"move", "rank", "status"},
            )
        style = ttk.Style(self.root)
        try:
            style.configure("Treeview", rowheight=21, font=("Segoe UI", 9))
            style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        except Exception:
            pass
        scrollbar = ttk.Scrollbar(
            move_frame, orient="vertical", command=self.moves_tree.yview
        )
        self.moves_tree.configure(yscrollcommand=scrollbar.set)
        self.moves_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        tk.Label(
            self.root,
            text="Rank is Stockfish MultiPV reference; opening-book choices show BOOK #. Opponent moves have no engine rank.",
            font=("Segoe UI", 8), anchor="w",
            bg="#f3f5f8", fg="#5b6472",
        ).pack(fill="x", pady=(0, 3))

        self.status = tk.StringVar(
            value="Ready. Public competitive hosts remain blocked by blocked_platforms.txt."
        )
        tk.Label(
            self.root, textvariable=self.status,
            font=("Segoe UI", 8), justify="left",
            anchor="w", wraplength=930,
            bg="#f3f5f8", fg="#475569",
        ).pack(fill="x")

    def enabled(self, key):
        return bool(self.settings.get(key, False))

    def refresh_button(self, key):
        enabled = self.enabled(key)
        self.buttons[key].configure(
            text=f"{self.BUTTON_LABELS[key]}: {'ON' if enabled else 'OFF'}",
            bg="#16794b" if enabled else "#5b6472",
            fg="white",
            activebackground="#12613d" if enabled else "#434b57",
            activeforeground="white",
        )

    def toggle(self, key):
        self.settings[key] = not self.settings[key]
        setattr(self.args, key, self.settings[key])
        self.refresh_button(key)
        state = "ON" if self.settings[key] else "OFF"
        print(f"[SETTINGS] {self.LABELS[key]} -> {state}.", flush=True)
        if key in {"auto_next_game", "auto_rematch"}:
            self.sync_game_actions()
        if key == "auto_play" and self.settings[key]:
            self.status.set("Bot enabled. It will start after required engine checks pass.")
        elif key == "auto_play":
            self.status.set("Bot is OFF; the live reader and dashboard remain active.")

    def set_enabled(self, key, value):
        self.settings[key] = bool(value)
        setattr(self.args, key, bool(value))
        self.refresh_button(key)

    def sync_game_actions(self):
        try:
            if self.chosen_handle not in self.driver.window_handles:
                self.status.set("Selected browser tab is unavailable; game actions were not changed.")
                return
            self.driver.switch_to.window(self.chosen_handle)
            check_target(self.driver.current_url, self.allowed_host)
            result = self.driver.execute_script(
                "return window.__ownedSiteGameActions "
                "? window.__ownedSiteGameActions.setConfig(arguments[0]) "
                ": {installed:false};",
                {
                    "autoNextGame": self.enabled("auto_next_game"),
                    "autoRematch": self.enabled("auto_rematch"),
                },
            )
            if isinstance(result, dict) and result.get("installed"):
                self.status.set("Game-action toggles applied to the selected allowed site.")
            else:
                self.status.set("Game-action handler is not ready; check the console log.")
        except Exception:
            self.status.set("Could not update game-action handler; site details were not logged.")

    @staticmethod
    def _format_clock(value):
        if value is None:
            return "--:--"
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError):
            return "--:--"
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def _format_eval(cp):
        if cp is None:
            return "—"
        try:
            value = float(cp)
        except (TypeError, ValueError):
            return "—"
        if value >= 99000:
            return "White mate"
        if value <= -99000:
            return "Black mate"
        if abs(value) <= 15:
            return "Equal 0.00"
        return f"White +{value / 100.0:.2f}" if value > 0 else f"Black +{abs(value) / 100.0:.2f}"

    @staticmethod
    def _format_eval_cell(cp):
        if cp is None:
            return "—"
        try:
            value = float(cp)
        except (TypeError, ValueError):
            return "—"
        if value >= 99000:
            return "W MATE"
        if value <= -99000:
            return "B MATE"
        if abs(value) <= 15:
            return "0.00 (equal)"
        return f"W {value / 100.0:+.2f}" if value > 0 else f"B +{abs(value) / 100.0:.2f}"

    def update_clock_status(
        self, white_clock, black_clock, base_seconds, increment_seconds,
        low_clock_threshold, bot_color=None
    ):
        base = "?" if base_seconds is None else f"{float(base_seconds):.0f}s"
        increment = f"{float(increment_seconds or 0.0):.2f}s"
        side = (bot_color or "?").capitalize()
        own = white_clock if bot_color == "white" else black_clock if bot_color == "black" else None
        self.clock_status.set(
            f"White {self._format_clock(white_clock)}  |  "
            f"Black {self._format_clock(black_clock)}  |  "
            f"Your clock ({side}): {self._format_clock(own)}\n"
            f"Base time: {base}  |  Increment: {increment}  |  "
            f"Low-clock threshold: {float(low_clock_threshold):.1f}s"
        )

    def update_game_status(self, game_number, board, bot_color=None, game_result=None):
        """Refresh turn, progress, material and terminal-state summary."""
        if board is None:
            return
        try:
            self.latest_game_number = int(game_number)
            white_pieces = [p for p in board.piece_map().values() if p.color]
            black_pieces = [p for p in board.piece_map().values() if not p.color]
            values = {
                1: 1, 2: 3, 3: 3, 4: 5, 5: 9, 6: 0
            }
            white_material = sum(values.get(p.piece_type, 0) for p in white_pieces)
            black_material = sum(values.get(p.piece_type, 0) for p in black_pieces)
            try:
                legal_count = board.legal_moves.count()
            except Exception:
                legal_count = "?"
            result_text = str(game_result or "").strip()
            if result_text and result_text.lower() not in {
                "*", "live", "ongoing", "in progress", "playing", "none"
            }:
                state = f"FINISHED · {result_text[:24]}"
            elif board.is_checkmate():
                winner = "Black wins" if board.turn else "White wins"
                state = f"CHECKMATE · {winner}"
            elif board.is_stalemate():
                state = "DRAW · STALEMATE"
            elif board.is_insufficient_material():
                state = "DRAW · MATERIAL"
            elif board.is_game_over(claim_draw=False):
                state = "GAME OVER"
            elif board.is_check():
                state = "LIVE · CHECK"
            else:
                state = "LIVE"

            turn = "White" if board.turn else "Black"
            bot_side = (bot_color or "?").capitalize()
            self.game_stats.set(
                f"Game {game_number}  |  Plies: {board.ply()}  |  "
                f"Move: {board.fullmove_number}  |  Turn: {turn}  |  "
                f"Bot side: {bot_side}  |  {state}"
            )
            self.material_stats.set(
                f"Pieces W/B: {len(white_pieces)}/{len(black_pieces)}  |  "
                f"Material W/B: {white_material}/{black_material} points  |  "
                f"Legal moves now: {legal_count}"
            )
        except Exception:
            pass

    def update_evaluation(self, eval_white_cp, source="Stockfish"):
        try:
            self.last_eval_white_cp = (
                None if eval_white_cp is None else float(eval_white_cp)
            )
            if self.last_eval_white_cp is None:
                self.evaluation_status.set("Evaluation: waiting for Stockfish analysis")
            else:
                self.evaluation_status.set(
                    f"{self._format_eval(self.last_eval_white_cp)}  ·  Source: {source}"
                )
            self._draw_eval_bar()
        except Exception:
            pass

    def _draw_eval_bar(self):
        canvas = getattr(self, "eval_canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
            width = max(60, int(canvas.winfo_width()))
            height = max(22, int(canvas.winfo_height()))
            middle = width / 2.0
            canvas.create_rectangle(
                1, 2, width - 1, height - 2,
                fill="#e4e8ee", outline="#c5ccd6",
            )
            cp = self.last_eval_white_cp
            if cp is not None:
                if abs(cp) <= 15:
                    span = 7
                    color = "#718096"
                    canvas.create_rectangle(
                        middle - span, 4, middle + span, height - 4,
                        fill=color, outline=color,
                    )
                else:
                    scale = min(1.0, abs(cp) / 800.0)
                    span = max(3.0, scale * max(3.0, middle - 5.0))
                    color = "#2563eb" if cp > 0 else "#d97706"
                    left = middle if cp > 0 else middle - span
                    right = middle + span if cp > 0 else middle
                    canvas.create_rectangle(
                        left, 4, right, height - 4,
                        fill=color, outline=color,
                    )
            canvas.create_line(
                middle, 2, middle, height - 2,
                fill="#344054", width=2,
            )
            canvas.create_text(
                8, height / 2, text="BLACK",
                anchor="w", fill="#344054",
                font=("Segoe UI", 8, "bold"),
            )
            canvas.create_text(
                width - 8, height / 2, text="WHITE",
                anchor="e", fill="#344054",
                font=("Segoe UI", 8, "bold"),
            )
        except Exception:
            pass

    def _put_move(self, game_number, ply, side, san, rank=None,
                  eval_white_cp=None, status="Observed"):
        try:
            game_number = int(game_number)
            ply = int(ply)
        except (TypeError, ValueError):
            return
        key = (game_number, ply)
        row = self.move_rows.get(key)
        is_new = row is None
        if is_new:
            row = {
                "iid": f"game{game_number}_ply{ply}",
                "game": game_number,
                "ply": ply,
                "side": side or "?",
                "move": san or "?",
                "rank": rank or "—",
                "eval": eval_white_cp,
                "status": status or "Observed",
            }
            self.move_rows[key] = row
            self.move_order.insert(0, key)
        else:
            if side:
                row["side"] = side
            if san:
                row["move"] = san
            if rank not in (None, "", "—"):
                row["rank"] = rank
            if eval_white_cp is not None:
                row["eval"] = eval_white_cp
            if status:
                row["status"] = status

        values = (
            row["ply"],
            str(row["side"]).upper(),
            row["move"],
            row["rank"],
            self._format_eval_cell(row["eval"]),
            row["status"],
        )
        iid = row["iid"]
        if self.moves_tree.exists(iid):
            self.moves_tree.item(iid, values=values)
        else:
            self.moves_tree.insert("", "end", iid=iid, values=values)
        if is_new:
            self.moves_tree.move(iid, "", 0)

        while len(self.move_order) > 200:
            old_key = self.move_order.pop()
            old = self.move_rows.pop(old_key, None)
            if old and self.moves_tree.exists(old["iid"]):
                self.moves_tree.delete(old["iid"])

    def record_selected_move(
        self, game_number, ply, side, san, rank, eval_white_cp=None, status="Selected"
    ):
        self._put_move(
            game_number, ply, side, san, rank=rank,
            eval_white_cp=eval_white_cp, status=status,
        )

    def record_observed_move(self, game_number, ply, move, eval_white_cp=None):
        if not isinstance(move, dict):
            return
        self._put_move(
            game_number, ply, move.get("side"), move.get("san"),
            rank=None, eval_white_cp=eval_white_cp, status="On board",
        )

    def update_move_status(self, game_number, ply, status):
        key = (int(game_number), int(ply))
        row = self.move_rows.get(key)
        if row is not None:
            row["status"] = str(status)
            if self.moves_tree.exists(row["iid"]):
                self.moves_tree.set(row["iid"], "status", row["status"])

    def request_close(self):
        if self.messagebox.askyesno(
            "Stop reader?",
            "Close this window and stop the live reader/bot? Select No to keep controls open.",
            parent=self.root,
        ):
            self.stop_requested = True
            try:
                self.root.destroy()
            except self.tk.TclError:
                pass

    def pump(self):
        if self.stop_requested:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            self.stop_requested = True

    def responsive_sleep(self, seconds):
        deadline = time.monotonic() + max(0.0, float(seconds))
        while not self.stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.pump()
            time.sleep(min(0.025, max(0.001, remaining)))


def configure_optional_bot_elo(engine, setting_value=None):
    """Apply an explicitly configured Stockfish Elo limit for a labeled AI difficulty.

    No setting means no change: the default engine strength is preserved.
    This is a game-product difficulty control, not a move-timing or identity
    simulation feature.
    """
    raw = (
        str(setting_value).strip()
        if setting_value is not None
        else load_optional_txt_setting("bot_elo")
    )
    if not raw:
        return None

    try:
        elo = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("bot_elo must be a whole-number Stockfish Elo value.") from exc

    options = getattr(engine, "options", {})
    elo_option = options.get("UCI_Elo") if hasattr(options, "get") else None
    strength_option = options.get("UCI_LimitStrength") if hasattr(options, "get") else None
    if elo_option is None or strength_option is None:
        raise RuntimeError(
            "The configured Stockfish build does not support UCI_Elo difficulty limits."
        )

    try:
        min_elo = int(elo_option.min)
        max_elo = int(elo_option.max)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("Stockfish did not expose a valid UCI_Elo range.") from exc
    if not min_elo <= elo <= max_elo:
        raise RuntimeError(
            f"bot_elo must be between {min_elo} and {max_elo} for this Stockfish build."
        )

    engine.configure({"UCI_LimitStrength": True, "UCI_Elo": elo})
    return elo


def prepare_bot_components(args, allowed_host, installed):
    """Initialize the optional bot only for the configured, non-blocked site."""
    if chess is None:
        raise RuntimeError(
            "Autoplay requires python-chess. Install with: python -m pip install python-chess"
        )
    if is_public_competitive_host(allowed_host):
        raise RuntimeError("Autoplay is disabled for public competitive chess platforms.")
    if not installed.get("boardFound") or not installed.get("gameApiFound"):
        raise RuntimeError("Autoplay needs the site's exposed board game API.")
    try:
        import chesskida_strategy as strategy_module
        from gm_book import GMBook
    except ImportError as exc:
        raise RuntimeError(
            "ChessKida strategy files are missing. Pull the current working branch and install requirements."
        ) from exc

    configured_engine_path = (
        args.stockfish_path.strip()
        or load_optional_txt_setting("stockfish_path")
        or "stockfish.exe"
    )
    engine_path = Path(configured_engine_path).expanduser()
    if not engine_path.is_absolute():
        engine_path = (BASE_DIR / engine_path).resolve()
    if not engine_path.is_file():
        raise RuntimeError(
            "Stockfish executable was not found. Set stockfish_path in the TXT settings or use --stockfish-path."
        )
    engine = None
    try:
        engine = chess.engine.SimpleEngine.popen_uci(str(engine_path))
        engine.configure({"Threads": 2, "Hash": 128})
        configured_elo = configure_optional_bot_elo(engine)
    except RuntimeError:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        raise
    except Exception as exc:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        raise RuntimeError("Stockfish could not be started or configured. Check the configured executable.") from exc

    book = GMBook(BASE_DIR / "Ultimate_GM_Bullet.bin")
    if book.load():
        print("[BOOK] Opening book loaded.", flush=True)
    else:
        print("[BOOK] Opening book unavailable; using Stockfish strategy.", flush=True)
    print("[AUTO] ChessKida move-selection behavior enabled for the configured, non-blocked test site.", flush=True)
    if args.move_input == "cursor":
        print("[INPUT] Cursor drag mode enabled for the configured test board; public competitive hosts remain blocked.", flush=True)
    else:
        print("[INPUT] Direct game API mode enabled.", flush=True)
    engine_status = (
        f"[ENGINE] Threads=2 | Hash=128 MB | depth=12 | "
        f"MultiPV={strategy_module.TRAINING_MULTI_PV} | time={max(0.005, min(float(args.engine_time), 5.0)):.3f}s"
    )
    if configured_elo is not None:
        engine_status += f" | AI difficulty Elo={configured_elo}"
    print(f"{engine_status}.", flush=True)
    return engine, book, strategy_module


def host_for(url: str | None) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return ""


@lru_cache(maxsize=1)
def load_blocked_platforms() -> frozenset[str]:
    """Load blocked hostnames from the adjacent TXT file without logging entries."""
    if not BLOCKED_PLATFORMS_FILE.is_file():
        raise RuntimeError("Blocked-platform TXT configuration is missing.")
    try:
        lines = BLOCKED_PLATFORMS_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RuntimeError("Blocked-platform TXT configuration cannot be read.") from exc

    blocked: set[str] = set()
    for raw_line in lines:
        entry = raw_line.strip()
        if not entry or entry.startswith("#"):
            continue
        normalized = normalize_host(entry)
        if normalized:
            blocked.add(normalized)
    return frozenset(blocked)


def is_public_competitive_host(host: str) -> bool:
    """Check the TXT-configured blocklist, including subdomains."""
    normalized_host = normalize_host(host)
    if not normalized_host:
        return False
    return any(
        normalized_host == blocked or normalized_host.endswith("." + blocked)
        for blocked in load_blocked_platforms()
    )


def target_matches(url: str | None, allowed_host: str) -> bool:
    """Internal allowlist check. Values are never included in user-facing output."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = host_for(url)
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if is_public_competitive_host(host):
        return False
    return host == allowed_host or host.endswith("." + allowed_host)


def check_target(url: str | None, allowed_host: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("The selected browser tab is no longer available.")
    if not target_matches(url, allowed_host):
        raise RuntimeError("The selected browser tab no longer matches the saved site settings.")


JS_DETECT_VERIFICATION = r"""
const body = String(document.body?.innerText || '').slice(0, 12000).toLowerCase();
const title = String(document.title || '').toLowerCase();
const challengeText = /verify (that )?you are human|confirm (that )?you are not a robot|complete (the )?(security check|captcha)|security verification required|checking your browser|checking if the site connection is secure|human verification|required to verify|captcha required|please verify you are human|press and hold to confirm/.test(body + " " + title);
function isVisible(el) {
  if (!el) return false;
  const r = el.getBoundingClientRect();
  const style = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && style.display !== 'none' &&
    style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
}
const widgetSelectors = [
  '.g-recaptcha', '.h-captcha', '.cf-turnstile', '[data-sitekey]',
  '#challenge-form', '#cf-challenge-running', 'input[name*="captcha" i]',
  'iframe[src*="recaptcha" i]', 'iframe[src*="hcaptcha" i]',
  'iframe[src*="challenges.cloudflare.com" i]', 'iframe[title*="captcha" i]'
];
let captchaWidget = false;
for (const selector of widgetSelectors) {
  try {
    if (Array.from(document.querySelectorAll(selector)).some(isVisible)) {
      captchaWidget = true;
      break;
    }
  } catch (_) {}
}
return {challengeText, captchaWidget, detected: challengeText || captchaWidget};
""";


def wait_for_manual_verification(driver, allowed_host: str, cfg=None) -> bool:
    """Pause on an apparent challenge for manual completion; never solve it automatically."""
    state = driver.execute_script(JS_DETECT_VERIFICATION)
    if not isinstance(state, dict) or not state.get("detected"):
        return False

    print(
        "[CAPTCHA] Verification/challenge detected. Automation is paused; "
        "complete it manually in the Chrome window.",
        flush=True,
    )
    while True:
        try:
            input("[CAPTCHA] After you finish verification, press Enter to resume: ")
        except EOFError as exc:
            raise RuntimeError(
                "Manual verification is pending, but the console cannot accept input."
            ) from exc

        check_target(driver.current_url, allowed_host)
        state = driver.execute_script(JS_DETECT_VERIFICATION)
        if not isinstance(state, dict):
            raise RuntimeError("Could not confirm the verification page state.")
        # A visible CAPTCHA widget may remain on screen after being solved.
        # If explicit challenge text is still present, ask for another check.
        if state.get("challengeText"):
            print(
                "[CAPTCHA] Challenge text is still present. Finish the verification "
                "or wait for the page to complete, then press Enter again.",
                flush=True,
            )
            continue
        break

    if cfg is not None:
        reader_ready = driver.execute_script(
            "return Boolean(window.__liveGameApiReader && "
            "typeof window.__liveGameApiReader.current === 'function');"
        )
        if not reader_ready:
            installed = driver.execute_script(JS_INSTALL_OBSERVER, cfg)
            print(
                f"[CAPTCHA] Page observer refreshed | board={bool(installed.get('boardFound'))} "
                f"| game API={bool(installed.get('gameApiFound'))}.",
                flush=True,
            )
    print("[CAPTCHA] Manual verification step complete; re-checking game state.", flush=True)
    return True


def configured_uc_start_url(allowed_host: str) -> str:
    """Resolve a UC-mode entry URL while constraining it to the configured host."""
    raw_url = (
        load_optional_txt_setting("site_url").strip()
        or load_optional_txt_setting("url").strip()
    )
    if not raw_url:
        scheme = "https"
        if allowed_host == "localhost" or allowed_host.endswith((".localhost", ".local", ".test")):
            scheme = "http"
        else:
            try:
                address = ipaddress.ip_address(allowed_host)
                if address.is_private or address.is_loopback:
                    scheme = "http"
            except ValueError:
                pass
        raw_url = f"{scheme}://{allowed_host}"
    elif "://" not in raw_url:
        try_host = normalize_host(raw_url)
        scheme = "https"
        if try_host == "localhost" or (try_host and try_host.endswith((".localhost", ".local", ".test"))):
            scheme = "http"
        else:
            try:
                address = ipaddress.ip_address(try_host)
                if address.is_private or address.is_loopback:
                    scheme = "http"
            except (ValueError, TypeError):
                pass
        raw_url = f"{scheme}://{raw_url}"

    if not target_matches(raw_url, allowed_host):
        raise RuntimeError(
            "UC mode entry URL does not match the configured authorized site."
        )
    return raw_url


JS_INSTALL_OBSERVER = r"""
const cfg = arguments[0];
if (window.__liveGameApiReader?.observer) {
  try { window.__liveGameApiReader.observer.disconnect(); } catch (_) {}
}

let rootCache = [document];
let rootCacheAt = 0;
function rootsDeep(root = document, force = false) {
  const now = Date.now();
  if (root === document && !force && now - rootCacheAt < 1000) return rootCache;
  const roots = [root];
  for (let i = 0; i < roots.length; i++) {
    let nodes = [];
    try { nodes = roots[i].querySelectorAll('*'); } catch (_) {}
    for (const el of nodes) {
      if (el.shadowRoot) roots.push(el.shadowRoot); // open shadow roots
    }
  }
  if (root === document) { rootCache = roots; rootCacheAt = now; }
  return roots;
}
function queryDeep(selector) {
  if (!selector) return null;
  for (const root of rootsDeep()) {
    try {
      const found = root.querySelector(selector);
      if (found) return found;
    } catch (_) {}
  }
  return null;
}
function queryAllDeep(selector) {
  if (!selector) return [];
  const out = [], seen = new Set();
  for (const root of rootsDeep()) {
    try {
      for (const el of root.querySelectorAll(selector)) {
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
    } catch (_) {}
  }
  return out;
}
function queryAllWithinDeep(root, selector) {
  if (!root || !selector) return [];
  const roots = [root];
  if (root.shadowRoot) roots.push(root.shadowRoot);
  const out = [], seen = new Set();
  for (let i = 0; i < roots.length; i++) {
    const current = roots[i];
    try {
      if (current.matches && current.matches(selector) && !seen.has(current)) {
        seen.add(current);
        out.push(current);
      }
    } catch (_) {}
    try {
      for (const el of current.querySelectorAll(selector)) {
        if (!seen.has(el)) { seen.add(el); out.push(el); }
      }
      for (const el of current.querySelectorAll('*')) {
        if (el.shadowRoot) roots.push(el.shadowRoot);
      }
    } catch (_) {}
  }
  return out;
}
function deepElementFromPoint(x, y) {
  let node = document.elementFromPoint(x, y);
  const seen = new Set();
  while (node && node.shadowRoot && !seen.has(node)) {
    seen.add(node);
    let inner = null;
    try { inner = node.shadowRoot.elementFromPoint(x, y); } catch (_) {}
    if (!inner || inner === node) break;
    node = inner;
  }
  return node;
}
function pieceColorFromNode(start, board) {
  let node = start;
  for (let depth = 0; node && depth < 12; depth++) {
    const classes = String(node.className?.baseVal || node.className || '');
    const tokens = classes.split(/\s+/).filter(Boolean);
    if (tokens.some(token => /^w[prnbqk]$/.test(token))) return 'white';
    if (tokens.some(token => /^b[prnbqk]$/.test(token))) return 'black';
    for (const attr of ['data-color', 'data-piece-color', 'piece-color']) {
      const token = String(node.getAttribute?.(attr) || '').trim().toLowerCase();
      if (['white', 'w'].includes(token)) return 'white';
      if (['black', 'b'].includes(token)) return 'black';
    }
    if (node === board) break;
    node = node.parentElement || node.getRootNode?.().host || null;
  }
  return '';
}
function callMaybe(obj, name) {
  try { return obj && typeof obj[name] === 'function' ? obj[name]() : null; }
  catch (_) { return null; }
}
function readText(selector) {
  if (!selector) return '';
  const el = queryDeep(selector);
  if (!el) return '';
  return String(
    el.innerText ||
    el.textContent ||
    el.getAttribute?.('aria-label') ||
    el.getAttribute?.('title') ||
    ''
  ).replace(/\u00a0/g, ' ').trim();
}
function clockValueFromNode(node) {
  if (!node) return '';
  const values = [
    node.getAttribute?.('data-time'),
    node.getAttribute?.('data-remaining-time'),
    node.getAttribute?.('aria-valuetext'),
    node.getAttribute?.('aria-label'),
    node.getAttribute?.('title'),
    node.innerText,
    node.textContent
  ].filter(value => value != null && String(value).trim() !== '');
  for (const raw of values) {
    const value = String(raw).replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    const clockMatch = value.match(/\d{1,3}:[0-5]?\d(?:[.,]\d+)?/);
    if (clockMatch) return clockMatch[0].replace(',', '.');
    const secondsMatch = value.match(/(?:^|\D)(\d{1,3}(?:[.,]\d+)?)(?:\s*(?:s|sec|secs|second|seconds))?(?:$|\D)/i);
    if (secondsMatch) return secondsMatch[1].replace(',', '.');
  }
  return '';
}
function readClockText(selector, side) {
  const selectors = [
    '.clock-component.clock-' + side,
    '.clock-' + side + '.clock-component',
    selector,
    '[class~="clock-' + side + '"]',
    '[data-clock="' + side + '"]',
    '#' + side + '-clock',
    '.' + side + '.clock'
  ].filter(Boolean);
  const seen = new Set();
  for (const candidateSelector of selectors) {
    for (const clock of queryAllDeep(candidateSelector)) {
      if (seen.has(clock)) continue;
      seen.add(clock);
      const style = getComputedStyle(clock);
      const rect = clock.getBoundingClientRect?.();
      const visible = !!rect && rect.width > 0 && rect.height > 0 &&
        style.display !== 'none' && style.visibility !== 'hidden' &&
        Number(style.opacity || 1) > 0;
      if (!visible) continue;
      const inner = queryAllWithinDeep(
        clock,
        '.clock-time, .clock-time-monospace, .clock-digital, [data-time], [data-remaining-time], [aria-valuetext], time'
      );
      for (const node of inner) {
        if (node === clock) continue;
        const value = clockValueFromNode(node);
        if (value) return value;
      }
      const ownValue = clockValueFromNode(clock);
      if (ownValue) return ownValue;
    }
  }
  return '';
}
function getBoard() {
  return queryDeep(cfg.boardSelector) || queryDeep('chess-board') ||
         queryDeep('wc-chess-board') || queryDeep('#board-single');
}
function extractSan(value) {
  let text = String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  // Filter results, move clocks, and clock-only annotations.
  if (/^(?:1-0|0-1|1\/2-1\/2|\*)$/.test(text)) return '';
  if (/^(?:\d+(?:\.\d+)?s|\d+:\d{2}(?:\.\d+)?|\d{1,2}:\d{2})$/i.test(text)) return '';
  // Some move-list nodes include their move number before the SAN.
  text = text.replace(/^\d+\.(?:\.\.)?\s*/, '').trim();
  if (!text || /^\d+\.(?:\.\.)?$/.test(text)) return '';
  const sanPattern = /^(?:O-O-O|O-O|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=?[QRBN])?[+#]?)$/;
  if (sanPattern.test(text)) return text;
  // If a node contains move SAN plus a clock annotation, keep the SAN token only.
  for (const token0 of text.split(/[\s,]+/)) {
    const token = token0.replace(/^\d+\.(?:\.\.)?/, '').replace(/[()]/g, '');
    if (sanPattern.test(token)) return token;
  }
  return '';
}
function getMoves() {
  // Try each selector independently and choose the largest coherent SAN-node
  // set. Merging a move node with an ancestor move-list container can duplicate
  // its first SAN and corrupt the replay, so selector sets must not be mixed.
  const selectors = ['.white.node, .black.node', cfg.moveListSelector, cfg.movesSelector];
  let best = [];
  for (const selector of selectors) {
    const seen = new Set();
    const candidate = [];
    for (const el of queryAllDeep(selector)) {
      if (seen.has(el)) continue;
      seen.add(el);
      const san = extractSan(el.innerText || el.textContent || '');
      if (!san) continue;
      const ply = String(el.getAttribute(cfg.plyAttribute) || '').trim();
      candidate.push({ply, text: san, order: candidate.length});
    }
    if (candidate.length > best.length) best = candidate;
  }
  if (best.length && best.every(item => /^\d+$/.test(item.ply))) {
    best.sort((a, b) => Number(a.ply) - Number(b.ply) || a.order - b.order);
  }
  return best.map((item, index) => ({
    ply: item.ply || String(index + 1),
    text: item.text
  }));
}
function getLegalMoves(game) {
  // Read-only adaptation of the supplied userscript's getLegalMoves() use.
  // Serialize only stable fields; don't return page-owned JS objects.
  const raw = callMaybe(game, 'getLegalMoves');
  if (!Array.isArray(raw)) return [];
  return raw.map(move => ({
    from: move && move.from != null ? String(move.from) : '',
    to: move && move.to != null ? String(move.to) : '',
    promotion: move && move.promotion != null ? String(move.promotion) : '',
    san: move && move.san != null ? String(move.san) : ''
  })).filter(move => move.from && move.to);
}
function getApiMethods(game) {
  if (!game) return [];
  return [
    'getFEN', 'getTurn', 'getPlayingAs', 'getLegalMoves',
    'queuePremoveUci', 'setPremoveUci', 'queuePremove', 'setPremove', 'premove'
  ].filter(name => typeof game[name] === 'function');
}
function detectBottomSide(board) {
  if (!board) return {side: 'unknown', source: 'board-not-found'};

  const orientationAttrs = [
    board.getAttribute('data-orientation'),
    board.getAttribute('data-board-orientation'),
    board.getAttribute('orientation')
  ].filter(Boolean).map(v => String(v).trim().toLowerCase());
  for (const value of orientationAttrs) {
    if (value === 'white' || value === 'black') {
      return {side: value, source: 'board-orientation-attribute'};
    }
  }

  // Find rank labels by their actual on-screen vertical position, not DOM order.
  for (const coordinates of queryAllWithinDeep(board, '.coordinates')) {
    let yRank1 = null, yRank8 = null;
    for (const child of Array.from(coordinates.children || [])) {
      const label = String(child.innerText || child.textContent || '').trim();
      if (label !== '1' && label !== '8') continue;
      const r = child.getBoundingClientRect?.();
      if (!r || !r.width || !r.height) continue;
      const y = r.top + r.height / 2;
      if (label === '1') yRank1 = y;
      if (label === '8') yRank8 = y;
    }
    if (yRank1 !== null && yRank8 !== null && Math.abs(yRank1 - yRank8) > 20) {
      return {
        side: yRank1 > yRank8 ? 'white' : 'black',
        source: 'rank-label-screen-position'
      };
    }
  }

  // Inspect board descendants: the orientation class may not be on the host.
  const orientationNodes = queryAllWithinDeep(
    board, '.flipped, .orientation-black, .black-bottom, .orientation-white, .white-bottom'
  );
  for (const node of orientationNodes) {
    const tokens = String(node.className?.baseVal || node.className || '').toLowerCase().split(/\s+/);
    if (tokens.some(token => ['flipped', 'orientation-black', 'black-bottom'].includes(token))) {
      return {side: 'black', source: 'board-descendant-class'};
    }
    if (tokens.some(token => ['orientation-white', 'white-bottom'].includes(token))) {
      return {side: 'white', source: 'board-descendant-class'};
    }
  }

  const hostTokens = String(board.className?.baseVal || board.className || '').toLowerCase().split(/\s+/);
  if (hostTokens.some(token => ['flipped', 'orientation-black', 'black-bottom'].includes(token))) {
    return {side: 'black', source: 'board-class-fallback'};
  }
  if (hostTokens.some(token => ['orientation-white', 'white-bottom'].includes(token))) {
    return {side: 'white', source: 'board-class-fallback'};
  }
  return {side: 'unknown', source: 'orientation-not-exposed'};
}
function normalizeColorToken(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  const token = String(value).trim().toLowerCase();
  if (['white', 'w', 'light', 'white-side', 'white_side'].includes(token)) return 'white';
  if (['black', 'b', 'dark', 'black-side', 'black_side'].includes(token)) return 'black';
  return null;
}
function normalizeTurnColor(value) {
  const named = normalizeColorToken(value);
  if (named) return named;
  // Observed game API maps turn=1 to FEN 'w' and turn=2 to FEN 'b'.
  const token = String(value == null ? '' : value).trim().toLowerCase();
  if (token === '1') return 'white';
  if (token === '2') return 'black';
  return null;
}
function normalizePlayerText(value) {
  return String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim().toLocaleLowerCase().replace(/^@/, '');
}
function selectorNameSide(name, selector, color) {
  if (!name || !selector) return false;
  for (const el of queryAllDeep(selector)) {
    const text = normalizePlayerText(el.innerText || el.textContent || '');
    if (text && text.includes(name)) return true;
  }
  return false;
}
function describePlayerNameContext(start) {
  const path = [];
  let node = start;
  for (let depth = 0; node && depth <= 6; depth++, node = node.parentElement || node.getRootNode?.().host) {
    const tag = String(node.tagName || 'node').toLowerCase();
    const id = String(node.id || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 60);
    const classes = String(node.className?.baseVal || node.className || '')
      .split(/\s+/).filter(Boolean)
      .map(value => value.replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 45))
      .filter(Boolean).slice(0, 6);
    const attrs = [];
    for (const name of ['data-player-color', 'data-color', 'data-side', 'color', 'aria-label', 'title']) {
      const value = node.getAttribute?.(name);
      if (value && ['data-player-color', 'data-color', 'data-side', 'color'].includes(name)) {
        attrs.push(name + '=' + String(value).slice(0, 20));
      }
    }
    path.push(tag + (id ? '#' + id : '') + (classes.length ? '.' + classes.join('.') : '') +
      (attrs.length ? '[' + attrs.join(',') + ']' : ''));
  }
  return path.join(' <- ');
}
function sideEvidenceFromNode(start) {
  let node = start;
  for (let depth = 0; node && depth <= 7; depth++, node = node.parentElement || node.getRootNode?.().host) {
    for (const attr of ['data-player-color', 'data-color', 'data-side', 'color']) {
      const side = normalizeColorToken(node.getAttribute?.(attr));
      if (side) return {color: side, source: 'player-name-ancestor-attribute'};
    }
    const whiteSelector = cfg.whitePlayerSelector;
    const blackSelector = cfg.blackPlayerSelector;
    let whiteMatch = false, blackMatch = false;
    try {
      whiteMatch = !!whiteSelector && node.matches(whiteSelector);
      blackMatch = !!blackSelector && node.matches(blackSelector);
    } catch (_) {}
    if (whiteMatch !== blackMatch) {
      return {color: whiteMatch ? 'white' : 'black', source: 'configured-player-container'};
    }
    const clues = [
      String(node.id || ''),
      String(node.className?.baseVal || node.className || ''),
      String(node.getAttribute?.('aria-label') || ''),
      String(node.getAttribute?.('title') || '')
    ].join(' ').toLowerCase();
    // Only trust explicit player-container labels. A generic ancestor class like
    // ".white" can describe a piece, theme, or stale layout and caused the
    // username matcher to label Black games as White.
    const whiteHint = /white[-_ ]player|player[-_ ]white|whiteplayer|playerwhite|white[-_ ]player[-_ ](container|name|info)/.test(clues);
    const blackHint = /black[-_ ]player|player[-_ ]black|blackplayer|playerblack|black[-_ ]player[-_ ](container|name|info)/.test(clues);
    if (whiteHint !== blackHint) {
      return {color: whiteHint ? 'white' : 'black', source: 'explicit-player-ancestor-label'};
    }
  }
  return null;
}
function sideEvidenceFromPlayerPanelClock(start) {
  // Determine side from the clock inside the nearest container for this exact
  // username. Ignore outer wrappers that contain both players' clocks.
  let node = start;
  for (
    let depth = 0;
    node && depth <= 9;
    depth++,
    node = node.parentElement || node.getRootNode?.().host
  ) {
    let clocks = [];
    try {
      clocks = Array.from(node.querySelectorAll?.('.clock-component') || []);
    } catch (_) {}
    if (clocks.length !== 1) continue;

    const clock = clocks[0];
    const isWhite = !!clock.classList?.contains('clock-white');
    const isBlack = !!clock.classList?.contains('clock-black');
    if (isWhite !== isBlack) {
      return {
        color: isWhite ? 'white' : 'black',
        source: 'matched-player-panel-clock-class'
      };
    }
  }
  return null;
}
function detectConfiguredPlayerColor(configuredName) {
  const wanted = normalizePlayerText(configuredName);
  if (!wanted) return {color: null, source: 'player-name-not-configured', matched: false};

  const whiteByContainer = selectorNameSide(wanted, cfg.whitePlayerSelector, 'white');
  const blackByContainer = selectorNameSide(wanted, cfg.blackPlayerSelector, 'black');
  if (whiteByContainer !== blackByContainer) {
    return {
      color: whiteByContainer ? 'white' : 'black',
      source: 'configured-player-container',
      matched: true
    };
  }
  if (whiteByContainer && blackByContainer) {
    return {color: null, source: 'player-name-matched-both-sides', matched: true};
  }

  const matches = [];
  let exactNameFound = false;
  let nameContext = '';
  for (const el of queryAllDeep('*')) {
    const text = normalizePlayerText(el.innerText || el.textContent || '');
    if (text !== wanted) continue;
    exactNameFound = true;
    if (!nameContext) nameContext = describePlayerNameContext(el);
    const evidence = sideEvidenceFromPlayerPanelClock(el) || sideEvidenceFromNode(el);
    if (evidence && !matches.some(item => item.color === evidence.color)) {
      matches.push(evidence);
    }
  }
  if (matches.length === 1) {
    return {color: matches[0].color, source: matches[0].source, matched: true, debugPath: nameContext};
  }
  if (matches.length > 1) {
    return {color: null, source: 'player-name-matched-conflicting-elements', matched: true, debugPath: nameContext};
  }
  if (exactNameFound) {
    return {color: null, source: 'player-name-found-side-unknown', matched: true, debugPath: nameContext};
  }
  return {color: null, source: 'player-name-not-found', matched: false, debugPath: ''};
}
function snapshot() {
  const board = getBoard();
  const game = board && board.game;
  const orientation = detectBottomSide(board);
  let fen = callMaybe(game, 'getFEN');
  if (typeof fen !== 'string' || !fen.trim()) {
    const fenEl = queryDeep(cfg.fenSelector);
    fen = fenEl ? (fenEl.getAttribute(cfg.fenAttribute) || '') : '';
  }
  const moves = getMoves();
  const legalMoves = getLegalMoves(game);
  const turn = callMaybe(game, 'getTurn');
  const playingAs = callMaybe(game, 'getPlayingAs');
  const turnColor = normalizeTurnColor(turn);
  const playingAsColor = normalizeColorToken(playingAs);
  const playerNameSide = detectConfiguredPlayerColor(cfg.playerName);
  // Raw values can differ by API representation (for example "w" vs "white").
  // Compare normalized colors only; unknown values stay unknown, not guessed.
  const isMyTurn = !turnColor || !playingAsColor
    ? null
    : (turnColor === playingAsColor);
  return {
    boardFound: !!board,
    gameApiFound: !!(game && (typeof game.getFEN === 'function' || typeof game.getTurn === 'function')),
    apiMethods: getApiMethods(game),
    fen: typeof fen === 'string' ? fen.trim() : '',
    turn,
    turnColor,
    playingAs,
    playingAsColor,
    playerNameColor: playerNameSide.color,
    playerNameColorSource: playerNameSide.source,
    playerNameMatched: playerNameSide.matched,
    playerNameDebugPath: playerNameSide.debugPath || '',
    isMyTurn,
    bottomSide: orientation.side,
    bottomSideSource: orientation.source,
    moveCount: moves.length,
    moves,
    legalMoveCount: legalMoves.length,
    legalMoves,
    whiteClock: readClockText(cfg.whiteClockSelector, 'white'),
    blackClock: readClockText(cfg.blackClockSelector, 'black'),
    result: readText(cfg.resultSelector)
  };
}
const state = {queue: [], lastSignature: null, observer: null, pollTimer: null, pollIntervalMs: 50, lastError: null, snapshot};
function refresh(forceRoots = false) {
  try {
    if (forceRoots) rootsDeep(document, true);
    const value = snapshot();
    const signature = JSON.stringify(value);
    if (signature === state.lastSignature) return;
    state.lastSignature = signature;
    state.queue.push(value);
    if (state.queue.length > 10000) state.queue.splice(0, state.queue.length - 10000);
  } catch (e) { state.lastError = String(e); }
}
state.observer = new MutationObserver(() => refresh(false));
state.observer.observe(document.documentElement, {
  subtree: true, childList: true, characterData: true, attributes: true,
  attributeFilter: ['class', 'style', 'title', 'aria-label', 'aria-valuetext', 'aria-valuenow', 'data-clock', 'data-remaining-time', 'data-fen', 'data-ply', 'data-time', 'data-result', 'data-turn']
});
window.__liveGameApiReader = {
  observer: state.observer,
  queue: state.queue,
  status: () => ({lastError: state.lastError, pending: state.queue.length}),
  drain: () => state.queue.splice(0, state.queue.length),
  current: () => state.snapshot(),
  setFastPolling: enabled => {
    const nextIntervalMs = enabled ? 20 : 50;
    if (state.pollTimer && state.pollIntervalMs === nextIntervalMs) {
      return {changed: false, intervalMs: nextIntervalMs};
    }
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollIntervalMs = nextIntervalMs;
    state.pollTimer = setInterval(() => refresh(false), nextIntervalMs);
    refresh(true);
    return {changed: true, intervalMs: nextIntervalMs};
  },
  dragTarget: (uci, expectedFen, expectedColor, allowPremove = false) => {
    const board = getBoard();
    const game = board && board.game;
    if (!board || !game || typeof game.getFEN !== 'function') {
      return {ok: false, reason: 'board-game-api-unavailable'};
    }
    const currentFen = String(callMaybe(game, 'getFEN') || '').trim();
    if (!currentFen || currentFen !== String(expectedFen || '').trim()) {
      return {ok: false, reason: 'position-changed-before-drag'};
    }
    const wantedColor = String(expectedColor || '').trim().toLowerCase();
    if (wantedColor !== 'white' && wantedColor !== 'black') {
      return {ok: false, reason: 'player-color-unconfirmed'};
    }
    const fenFields = currentFen.split(/\s+/);
    const fenTurn = fenFields[1] === 'w' ? 'white' : (fenFields[1] === 'b' ? 'black' : '');
    const isPremove = fenTurn !== wantedColor;
    if (isPremove && !allowPremove) {
      return {ok: false, reason: 'not-configured-player-turn'};
    }
    if (!isPremove && allowPremove) {
      return {ok: false, reason: 'premove-no-longer-needed'};
    }
    const text = String(uci || '').toLowerCase();
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(text)) {
      return {ok: false, reason: 'invalid-uci'};
    }
    const from = text.slice(0, 2), to = text.slice(2, 4), promo = text.slice(4, 5);
    // Independently verify the source piece color from the authoritative FEN.
    const fenRanks = String(fenFields[0] || '').split('/');
    const sourceFile = from.charCodeAt(0) - 97;
    const sourceRank = Number(from[1]);
    let sourcePiece = '';
    if (fenRanks.length === 8 && sourceFile >= 0 && sourceFile < 8 && sourceRank >= 1 && sourceRank <= 8) {
      const row = fenRanks[8 - sourceRank];
      let file = 0;
      for (const token of row) {
        if (/^[1-8]$/.test(token)) {
          file += Number(token);
        } else {
          if (file === sourceFile) { sourcePiece = token; break; }
          file += 1;
        }
      }
    }
    if (!sourcePiece || !/^[prnbqkPRNBQK]$/.test(sourcePiece)) {
      return {ok: false, reason: 'source-square-empty-in-fen'};
    }
    const sourceColor = sourcePiece === sourcePiece.toUpperCase() ? 'white' : 'black';
    if (sourceColor !== wantedColor) {
      return {ok: false, reason: 'source-piece-is-not-configured-player'};
    }
    const rect = board.getBoundingClientRect();
    const orientation = detectBottomSide(board);
    if (!rect.width || !rect.height || Math.abs(rect.width - rect.height) > Math.max(8, rect.width * 0.08)) {
      return {ok: false, reason: 'board-geometry-not-square'};
    }
    if (!['white', 'black'].includes(orientation.side)) {
      return {ok: false, reason: 'board-orientation-unknown'};
    }

    // Verify the visually rendered source square before issuing a mouse drag.
    // A wrong orientation must not send the cursor onto an opponent's piece.
    const boardFile = orientation.side === 'white' ? sourceFile : 7 - sourceFile;
    const boardRow = orientation.side === 'white' ? 8 - sourceRank : sourceRank - 1;
    const sourceX = rect.left + (boardFile + 0.5) * rect.width / 8;
    const sourceY = rect.top + (boardRow + 0.5) * rect.height / 8;
    const renderedSource = deepElementFromPoint(sourceX, sourceY);
    const renderedColor = pieceColorFromNode(renderedSource, board);
    if (!renderedColor) {
      return {ok: false, reason: 'ui-source-piece-color-unreadable', bottomSide: orientation.side};
    }
    if (renderedColor !== sourceColor) {
      return {
        ok: false,
        reason: 'ui-source-piece-color-mismatch',
        expectedColor: sourceColor,
        detectedColor: renderedColor,
        bottomSide: orientation.side
      };
    }
    return {
      ok: true,
      boardElement: board,
      width: rect.width,
      height: rect.height,
      bottomSide: orientation.side,
      fenTurn,
      sourceColor,
      sourcePiece,
      premoveOnly: isPremove
    };
  },
  premoveStatus: () => {
    const board = getBoard();
    const game = board && board.game;
    const sources = [game, board].filter(Boolean);
    const looksPending = value => {
      if (value === true) return true;
      if (typeof value === 'string') return value.trim().length > 0;
      if (value && typeof value === 'object') {
        return Boolean(value.uci || value.from && value.to || value.source && value.target);
      }
      return false;
    };
    for (const source of sources) {
      for (const name of ['getPremove', 'getPendingPremove', 'getQueuedPremove']) {
        try {
          if (typeof source[name] === 'function') {
            const value = source[name]();
            if (looksPending(value)) return {pending: true, source: name};
          }
        } catch (_) {}
      }
      for (const name of ['pendingPremove', 'queuedPremove', 'premove']) {
        try {
          if (looksPending(source[name])) return {pending: true, source: name};
        } catch (_) {}
      }
    }
    if (board) {
      for (const selector of ['[data-premove]', '.premove', '[class*="premove"]', '[class*="pre-move"]']) {
        for (const el of queryAllWithinDeep(board, selector)) {
          try {
            const rect = el.getBoundingClientRect();
            const tokens = String(el.className?.baseVal || el.className || '');
            if (rect.width > 0 && rect.height > 0 && /premove|pre-move/i.test(tokens + ' ' + el.getAttribute('data-premove'))) {
              return {pending: true, source: 'visible-board-marker'};
            }
          } catch (_) {}
        }
      }
    }
    return {pending: false, source: 'not-exposed'};
  },
  queuePremove: (uci, expectedFen, expectedColor) => {
    const board = getBoard();
    const game = board && board.game;
    if (!board || !game || typeof game.getFEN !== 'function') {
      return {ok: false, reason: 'board-game-api-unavailable'};
    }
    const currentFen = String(callMaybe(game, 'getFEN') || '').trim();
    if (!currentFen || currentFen !== String(expectedFen || '').trim()) {
      return {ok: false, reason: 'position-changed-before-premove'};
    }
    const wantedColor = String(expectedColor || '').trim().toLowerCase();
    if (!['white', 'black'].includes(wantedColor)) {
      return {ok: false, reason: 'player-color-unconfirmed'};
    }
    const turnField = currentFen.split(/\s+/)[1];
    const fenTurn = turnField === 'w' ? 'white' : (turnField === 'b' ? 'black' : '');
    if (fenTurn === wantedColor) {
      return {ok: false, reason: 'premove-no-longer-needed'};
    }
    const text = String(uci || '').toLowerCase();
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(text)) {
      return {ok: false, reason: 'invalid-uci'};
    }
    const from = text.slice(0, 2), to = text.slice(2, 4), promotion = text.slice(4, 5);
    const ranks = String(currentFen.split(/\s+/)[0] || '').split('/');
    const fileWanted = from.charCodeAt(0) - 97, rankWanted = Number(from[1]);
    let piece = '', file = 0;
    const row = ranks[8 - rankWanted];
    if (row && fileWanted >= 0 && fileWanted < 8 && rankWanted >= 1 && rankWanted <= 8) {
      for (const token of row) {
        if (/^[1-8]$/.test(token)) file += Number(token);
        else {
          if (file === fileWanted) { piece = token; break; }
          file += 1;
        }
      }
    }
    if (!piece || !/^[prnbqkPRNBQK]$/.test(piece)) return {ok:false, reason:'source-square-empty-in-fen'};
    const sourceColor = piece === piece.toUpperCase() ? 'white' : 'black';
    if (sourceColor !== wantedColor) return {ok:false, reason:'source-piece-is-not-configured-player'};
    const request = {uci: text, from, to, promotion: promotion || null};
    const targets = [game, board];
    for (const target of targets) {
      for (const name of ['queuePremoveUci', 'setPremoveUci']) {
        try {
          if (typeof target[name] === 'function') {
            const result = target[name](text);
            if (result === false) return {ok:false, reason:'premove-api-rejected'};
            return {ok:true, source:name, confirmed:true};
          }
        } catch (_) {}
      }
      for (const name of ['queuePremove', 'setPremove', 'premove']) {
        try {
          if (typeof target[name] !== 'function') continue;
          let result;
          try { result = target[name](request); }
          catch (_) { result = target[name](text); }
          if (result === false) return {ok:false, reason:'premove-api-rejected'};
          return {ok:true, source:name, confirmed:true};
        } catch (_) {}
      }
    }
    return {ok:false, reason:'premove-api-unavailable'};
  },
  playUci: (uci, expectedFen, expectedColor) => {
    // Use only the configured site's exposed legal-move/game API.
    const board = getBoard();
    const game = board && board.game;
    if (!game || typeof game.getFEN !== 'function' ||
        typeof game.getLegalMoves !== 'function' || typeof game.move !== 'function') {
      return {ok: false, reason: 'game-move-api-unavailable'};
    }
    const currentFen = String(callMaybe(game, 'getFEN') || '').trim();
    if (!currentFen || currentFen !== String(expectedFen || '').trim()) {
      return {ok: false, reason: 'position-changed-before-submit'};
    }
    const wantedColor = normalizeColorToken(expectedColor);
    const fenTurn = currentFen.split(/\s+/)[1] === 'w' ? 'white' :
      (currentFen.split(/\s+/)[1] === 'b' ? 'black' : null);
    if (wantedColor && fenTurn !== wantedColor) {
      return {ok: false, reason: 'not-configured-player-turn'};
    }
    const text = String(uci || '').toLowerCase();
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(text)) {
      return {ok: false, reason: 'invalid-uci'};
    }
    const from = text.slice(0, 2), to = text.slice(2, 4), promo = text.slice(4, 5);
    const raw = callMaybe(game, 'getLegalMoves');
    if (!Array.isArray(raw)) return {ok: false, reason: 'legal-moves-unavailable'};
    const candidates = raw.filter(m => m && String(m.from || '').toLowerCase() === from &&
      String(m.to || '').toLowerCase() === to);
    const promotionCode = value => {
      const v = String(value == null ? '' : value).toLowerCase();
      if (['q', 'queen'].includes(v)) return 'q';
      if (['r', 'rook'].includes(v)) return 'r';
      if (['b', 'bishop'].includes(v)) return 'b';
      if (['n', 'knight'].includes(v)) return 'n';
      return '';
    };
    let move = null;
    if (promo) {
      move = candidates.find(m => promotionCode(m.promotion) === promo);
      if (!move) return {ok: false, reason: 'promotion-choice-not-exposed'};
    } else {
      move = candidates.find(m => !promotionCode(m.promotion));
      if (!move && candidates.length === 1) move = candidates[0];
      if (!move) return {ok: false, reason: 'legal-move-not-found'};
    }
    try {
      const returned = game.move({...move, promotion: promo || 'false', animate: false, userGenerated: true});
      if (returned === false) return {ok: false, reason: 'game-api-rejected-move'};
      return {ok: true, uci: text};
    } catch (e) {
      return {ok: false, reason: 'game-api-error'};
    }
  },
  stop: () => {
    try { state.observer.disconnect(); } catch (_) {}
    if (state.pollTimer) clearInterval(state.pollTimer);
  }
};
// Crucial fallback: changes to a page's JS game object don't necessarily cause
// a mutation in document/light DOM, and MutationObserver doesn't cross shadow roots.
// Poll the exposed state at a modest interval; the queue de-duplicates unchanged snapshots.
refresh(true);
state.pollTimer = setInterval(() => refresh(false), 50);
const first = snapshot();
return {
  ok: true,
  boardFound: first.boardFound,
  gameApiFound: first.gameApiFound,
  fen: first.fen,
  turn: first.turn,
  playingAs: first.playingAs,
  bottomSide: first.bottomSide,
  bottomSideSource: first.bottomSideSource,
  moveCount: first.moveCount,
  legalMoveCount: first.legalMoveCount,
  apiMethods: first.apiMethods
};
"""


def infer_move_between_fens(previous_fen: str, current_fen: str) -> dict[str, str] | None:
    """Infer one legal SAN/UCI move from consecutive authoritative FEN snapshots.

    Compares piece placement, side to move, castling rights and en-passant square.
    Move counters may vary between website implementations, so they are not used
    as the sole criterion.
    """
    if chess is None or not previous_fen or not current_fen:
        return None
    try:
        before = chess.Board(previous_fen)
        target = chess.Board(current_fen)
    except (ValueError, IndexError):
        return None

    for move in list(before.legal_moves):
        san = before.san(move)
        after = before.copy(stack=False)
        after.push(move)
        if _same_chess_position(after, target):
            return {
                "san": san,
                "uci": move.uci(),
                "side": "white" if before.turn == chess.WHITE else "black",
                "move_number": str(before.fullmove_number),
            }
    return None


def _same_chess_position(left, right) -> bool:
    """Compare legal position state, ignoring non-capturable en-passant targets."""
    def effective_ep_square(board):
        # python-chess can retain the passed-over square after e.g. e2-e4 while
        # standard FEN commonly writes '-'. That square affects legal moves only
        # when an en-passant capture is actually legal.
        return board.ep_square if board.has_legal_en_passant() else None

    return (
        left.board_fen() == right.board_fen()
        and left.turn == right.turn
        and left.castling_rights == right.castling_rights
        and effective_ep_square(left) == effective_ep_square(right)
    )


def uci_move_reaches_fen(expected_fen: str, current_fen: str, uci: str) -> bool:
    """Confirm a move is present in the current position, allowing one fast reply."""
    if chess is None or not expected_fen or not current_fen or not uci:
        return False
    try:
        before = chess.Board(expected_fen)
        target = chess.Board(current_fen)
        move = chess.Move.from_uci(str(uci))
        if move not in before.legal_moves:
            return False
        before.push(move)
        if _same_chess_position(before, target):
            return True
        # In fast games the opponent may reply before the next verification sample.
        for reply in list(before.legal_moves):
            after_reply = before.copy(stack=False)
            after_reply.push(reply)
            if _same_chess_position(after_reply, target):
                return True
    except (ValueError, IndexError, TypeError):
        return False
    return False


def reconstruct_history_from_dom_moves(
    dom_moves: object, current_fen: str
) -> list[dict[str, str]] | None:
    """Rebuild a complete game from DOM SAN only if it validates against live FEN."""
    if chess is None or not isinstance(dom_moves, list) or not dom_moves or not current_fen:
        return None
    try:
        target = chess.Board(current_fen)
    except (ValueError, IndexError):
        return None
    board = chess.Board()
    history: list[dict[str, str]] = []
    for item in dom_moves:
        if not isinstance(item, dict):
            return None
        san_text = str(item.get("text") or item.get("san") or "").strip()
        if not san_text:
            return None
        try:
            move = board.parse_san(san_text)
        except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
            return None
        entry = {
            "san": board.san(move),
            "uci": move.uci(),
            "side": "white" if board.turn == chess.WHITE else "black",
            "move_number": str(board.fullmove_number),
            "game_ply": str(board.ply() + 1),
        }
        history.append(entry)
        board.push(move)
    if board.ply() != target.ply() or not _same_chess_position(board, target):
        return None
    return history


def format_move_history(history: list[dict[str, str]], limit: int = 16) -> str:
    """Format reconstructed SAN moves for a compact console preview."""
    chunks: list[str] = []
    for item in history[-limit:]:
        number = item.get("move_number", "")
        san = item.get("san", "")
        side = item.get("side", "")
        if side == "white":
            chunks.append(f"{number}. {san}")
        else:
            if chunks and chunks[-1].startswith(f"{number}. "):
                chunks[-1] += f" {san}"
            else:
                chunks.append(f"{number}... {san}")
    return " ".join(chunks)


def is_start_position(fen: str) -> bool:
    if chess is None or not fen:
        return False
    try:
        board = chess.Board(fen)
        return (
            board.board_fen() == chess.Board().board_fen()
            and board.turn == chess.WHITE
            and board.fullmove_number == 1
            and board.halfmove_clock == 0
        )
    except (ValueError, IndexError):
        return False



JS_INSTALL_AUTO_NEXT_GAME = r"""
const initial = arguments[0] || {};
return (() => {
  try { window.__ownedSiteGameActions?.observer?.disconnect?.(); } catch (_) {}
  let handled = null, timer = null;
  const settings = {
    autoNextGame: !!initial.autoNextGame,
    autoRematch: !!initial.autoRematch
  };
  const allowedHost = String(initial.allowedHost || "").toLowerCase().replace(/^www\./, "");
  function allowedPage() {
    const currentHost = String(location.hostname || "").toLowerCase().replace(/^www\./, "");
    return !!allowedHost && (location.protocol === "http:" || location.protocol === "https:") &&
      (currentHost === allowedHost || currentHost.endsWith("." + allowedHost));
  }
  const visible = e => !!e && e.isConnected && e.getClientRects().length > 0 &&
    getComputedStyle(e).display !== "none" && getComputedStyle(e).visibility !== "hidden" &&
    Number(getComputedStyle(e).opacity || 1) > 0;
  const label = e => String(e?.innerText || e?.textContent || e?.getAttribute?.("aria-label") || "")
    .replace(/\s+/g, " ").trim();
  const terminalText = /game\s*over|checkmate|stalemate|draw|wins?|won|resign(?:ed|ation)?|timeout|time\s*out|flag\s*fall|1-0|0-1|1\/2-1\/2/i;
  const buttonSelector = "button,[role='button'],input[type='button'],input[type='submit']";
  const usable = e => visible(e) && !e.disabled &&
    e.getAttribute("aria-disabled") !== "true";
  const isUsableNewButton = e => usable(e) &&
    /^New(?:\s|$)/i.test(label(e)) && !/\bRematch\b/i.test(label(e));
  const isUsableRematchButton = e => usable(e) && /\bRematch\b/i.test(label(e));
  function findGameOverRoot() {
    const explicitSelectors = [
      ".game-over-modal-shell-container",
      "[data-game-over='true']",
      "[aria-label*='game over' i]",
      "[class*='game-over' i]",
      "[class*='gameover' i]",
      "[class*='result-modal' i]"
    ];
    const genericSelectors = ["[role='dialog']", "[aria-modal='true']"];
    const roots = [];
    for (const selector of [...explicitSelectors, ...genericSelectors]) {
      try {
        for (const el of document.querySelectorAll(selector)) {
          if (visible(el) && !roots.includes(el)) roots.push(el);
        }
      } catch (_) {}
    }
    try {
      const resultSelector = String(initial.resultSelector || "#game-result, [data-result]");
      for (const el of document.querySelectorAll(resultSelector)) {
        if (visible(el) && !roots.includes(el)) roots.push(el);
      }
    } catch (_) {}
    for (const root of roots) {
      const txt = label(root);
      const explicitGameOver = explicitSelectors.some(selector => {
        try { return root.matches(selector); } catch (_) { return false; }
      });
      if (!explicitGameOver && !terminalText.test(txt)) continue;
      if (root.querySelectorAll(buttonSelector).length > 0) return root;
      if (terminalText.test(txt) && [...document.querySelectorAll(buttonSelector)].some(
        e => isUsableNewButton(e) || isUsableRematchButton(e)
      )) return document.body;
    }
    return null;
  }
  function findActionButton(root) {
    if (!root || (!settings.autoNextGame && !settings.autoRematch)) return null;
    const localButtons = [...root.querySelectorAll(buttonSelector)];
    const globalButtons = [...document.querySelectorAll(buttonSelector)];
    const pool = localButtons.length ? localButtons : globalButtons;
    const rematch = pool.filter(isUsableRematchButton);
    const newGame = pool.filter(isUsableNewButton);
    if (settings.autoRematch && rematch.length) return rematch[0];
    if (settings.autoNextGame && newGame.length) {
      return newGame.find(e => /^New\s+1\s+min$/i.test(label(e))) || newGame[0];
    }
    if (settings.autoNextGame) {
      const fallback = globalButtons.filter(isUsableNewButton);
      return fallback.find(e => /^New\s+1\s+min$/i.test(label(e))) || fallback[0] || null;
    }
    return null;
  }
  function check() {
    if (!allowedPage()) {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      handled = null;
      return;
    }
    if (!settings.autoNextGame && !settings.autoRematch) {
      if (timer !== null) clearTimeout(timer);
      timer = null;
      handled = null;
      return;
    }
    const root = findGameOverRoot();
    if (!root) {
      handled = null;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      return;
    }
    if (handled === root || timer !== null) return;
    if (!findActionButton(root)) return;
    handled = root;
    const delay = 2000 + Math.floor(Math.random() * 3001);
    console.info("[AUTO GAME] Action scheduled in " + (delay / 1000).toFixed(2) + "s.");
    timer = setTimeout(() => {
      timer = null;
      if (!allowedPage() || findGameOverRoot() !== root || !visible(root)) return;
      const button = findActionButton(root);
      if (!button) {
        handled = null;
        console.warn("[AUTO GAME] No enabled action matching current toggles; no click performed.");
        return;
      }
      const action = isUsableRematchButton(button) ? "Rematch" : "New Match";
      console.info("[AUTO GAME] Clicking enabled " + action + " button.");
      button.click();
    }, delay);
  }
  const observer = new MutationObserver(check);
  observer.observe(document.documentElement || document.body, {
    childList: true, subtree: true, characterData: true, attributes: true
  });
  window.__ownedSiteGameActions = {
    observer,
    setConfig(next) {
      if (next && typeof next === "object") {
        settings.autoNextGame = !!next.autoNextGame;
        settings.autoRematch = !!next.autoRematch;
      }
      handled = null;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      check();
      return {
        installed: true,
        autoNextGame: settings.autoNextGame,
        autoRematch: settings.autoRematch
      };
    },
    stop() {
      try { observer.disconnect(); } catch (_) {}
      if (timer !== null) clearTimeout(timer);
      timer = null;
      handled = null;
    }
  };
  check();
  return {installed: true, autoNextGame: settings.autoNextGame, autoRematch: settings.autoRematch};
})();
""";

_CLOCK_MMSS_RE = re.compile(r"^(\d+):([0-5]?\d)(?:\.(\d+))?$")
_CLOCK_HHMMSS_RE = re.compile(r"^(\d+):([0-5]?\d):([0-5]?\d)(?:\.(\d+))?$")
_CLOCK_SECONDS_SUFFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:s|sec|secs|second|seconds)$", re.IGNORECASE)


def parse_clock_seconds(raw_value: object) -> float | None:
    """Parse visible clocks like 1:00, 00:09.8, 9.8s, or 9 seconds."""
    text = re.sub(r"\s+", "", str(raw_value or "")).replace(",", ".")
    if not text:
        return None
    # Some clock components expose accessibility text rather than only digits.
    suffix_match = _CLOCK_SECONDS_SUFFIX_RE.fullmatch(text)
    if suffix_match:
        return max(0.0, float(suffix_match.group(1)))
    match = _CLOCK_HHMMSS_RE.fullmatch(text)
    if match:
        hours, minutes, seconds, fraction = match.groups()
        decimal = float(f"0.{fraction}") if fraction else 0.0
        return max(0.0, int(hours) * 3600 + int(minutes) * 60 + int(seconds) + decimal)
    match = _CLOCK_MMSS_RE.fullmatch(text)
    if match:
        minutes, seconds, fraction = match.groups()
        decimal = float(f"0.{fraction}") if fraction else 0.0
        return max(0.0, int(minutes) * 60 + int(seconds) + decimal)
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return max(0.0, float(text))
    return None


def is_low_clock_mode(
    remaining_seconds: float | None,
    threshold_seconds: float = 10.0,
    force_fast_mode: bool = False,
) -> bool:
    """Central clock-mode predicate shared by all runtime paths."""
    if remaining_seconds is None:
        return False
    try:
        remaining = max(0.0, float(remaining_seconds))
        threshold = min(10.0, max(5.0, float(threshold_seconds)))
    except (TypeError, ValueError):
        return False
    return bool(force_fast_mode or remaining <= threshold)


def calculate_engine_search_limit(
    base_seconds: float,
    remaining_seconds: float | None,
    initial_seconds: float | None,
    increment_seconds: float,
    board,
    opponent_seconds: float | None = None,
    low_clock_trigger_seconds: float = 10.0,
    force_fast_mode: bool = False,
) -> float:
    """Cap engine calculation time using one shared clock-mode boundary."""
    base = max(0.005, min(float(base_seconds), 5.0))
    if remaining_seconds is None:
        # Unknown clock: conservative short search rather than spending the
        # normal configured engine time without knowing the available reserve.
        return max(0.005, min(base, 0.050))

    remaining = max(0.0, float(remaining_seconds))
    if is_low_clock_mode(
        remaining, low_clock_trigger_seconds, force_fast_mode
    ):
        return max(0.001, min(base, 0.035, remaining * 0.004))
    initial = max(1.0, float(initial_seconds or remaining or 1.0))
    increment = max(0.0, min(float(increment_seconds or 0.0), 60.0))

    # Retain a clock reserve for submitting the move and state confirmation.
    reserve = min(3.0, max(0.25, remaining * 0.08))
    usable = max(0.0, remaining - reserve)
    if usable <= 0.01:
        return max(0.005, min(base, max(0.005, remaining * 0.20)))

    # Estimate the remaining decision count from the observed base clock.
    if initial <= 60:
        expected_moves = 35.0
    elif initial <= 180:
        expected_moves = 40.0
    else:
        expected_moves = 45.0
    fraction = min(1.0, max(0.08, remaining / initial))
    expected_moves = max(8.0, expected_moves * (fraction ** 0.5))
    per_move_clock = usable / expected_moves + increment

    # Spend a small fraction of the available move budget on calculation.
    # The position can raise this only within the caller's configured cap.
    factor = 1.0
    try:
        if board.is_check() or board.legal_moves.count() >= 28:
            factor = 1.20
        elif len(board.piece_map()) <= 10:
            factor = 0.80
    except Exception:
        pass
    opponent_remaining = None
    try:
        if opponent_seconds is not None:
            opponent_remaining = max(0.0, float(opponent_seconds))
    except (TypeError, ValueError):
        opponent_remaining = None

    clock_margin = (
        remaining - opponent_remaining
        if opponent_remaining is not None else None
    )
    normal_search_fraction = random.uniform(0.14, 0.28)
    if clock_margin is not None and clock_margin < 0.0:
        deficit_threshold = max(2.0, min(12.0, initial * 0.10))
        deficit_scale = min(1.0, abs(clock_margin) / deficit_threshold)
        faster_search_fraction = random.uniform(0.08, 0.18)
        search_fraction = (
            normal_search_fraction
            + (faster_search_fraction - normal_search_fraction) * deficit_scale
        )
    else:
        search_fraction = normal_search_fraction
    clock_cap = max(0.005, per_move_clock * search_fraction * factor)
    return max(0.005, min(base, clock_cap))


def _perform_cursor_drag_once(
    driver, uci: str, expected_fen: str, expected_color: str | None,
    allow_premove: bool = False, low_clock_mode: bool = False,
) -> dict[str, object]:
    """Drag a legal move on the configured local/test board.

    Each move chooses new points inside the source and destination squares.
    Normal mode uses varied pointer travel and pre-delay; low-clock mode collapses
    the gesture to a single 5-12 ms segment with no intentional pre-delay.
    Premove gestures are best-effort and use explicit queue signals when exposed.
    """
    try:
        target = driver.execute_script(
            "return window.__liveGameApiReader ? "
            "window.__liveGameApiReader.dragTarget(arguments[0], arguments[1], arguments[2], arguments[3]) : "
            "{ok:false, reason:'reader-not-installed'};",
            uci, expected_fen, expected_color, allow_premove,
        )
    except WebDriverException:
        return {"ok": False, "reason": "drag-target-unavailable"}

    if not isinstance(target, dict) or not target.get("ok"):
        return target if isinstance(target, dict) else {"ok": False, "reason": "invalid-drag-target"}

    text = str(uci or "").lower()
    if len(text) == 5:
        # Cursor promotion UI differs by site. Use only the configured site's
        # already-exposed legal-move API as a verified fallback for promotions.
        return perform_api_move(
            driver, text, expected_fen, expected_color,
            low_clock_mode=low_clock_mode, source="promotion-api-fallback",
        )

    board_element = target.get("boardElement")
    try:
        width = float(target["width"])
        height = float(target["height"])
        bottom_side = str(target["bottomSide"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "reason": "invalid-board-geometry"}
    if board_element is None or width <= 0 or height <= 0:
        return {"ok": False, "reason": "invalid-board-element"}

    rng = random.SystemRandom()
    square_w = width / 8.0
    square_h = height / 8.0

    def square_point(square: str) -> tuple[float, float]:
        file_index = ord(square[0]) - ord("a")
        rank_number = int(square[1])
        if bottom_side == "white":
            col, row = file_index, 8 - rank_number
        else:
            col, row = 7 - file_index, rank_number - 1

        # New independent target for every source and destination click.
        # Keep the pointer within the square, away from its borders.
        jitter_x = rng.uniform(-0.22, 0.22)
        jitter_y = rng.uniform(-0.22, 0.22)
        return (
            (col + 0.5 + jitter_x) * square_w,
            (row + 0.5 + jitter_y) * square_h,
        )

    sx, sy = square_point(text[:2])
    dx, dy = square_point(text[2:4])

    # Build a shallow quadratic curve using a randomized perpendicular control
    # point; source and destination remain inside their selected squares.
    vx, vy = dx - sx, dy - sy
    distance = max(1.0, (vx * vx + vy * vy) ** 0.5)
    normal_x, normal_y = -vy / distance, vx / distance
    curve_sign = rng.choice((-1.0, 1.0))
    curve_size = rng.uniform(0.035, 0.085) * min(width, height) * curve_sign
    control_x = (sx + dx) / 2.0 + normal_x * curve_size
    control_y = (sy + dy) / 2.0 + normal_y * curve_size
    control_x = min(max(2.0, control_x), width - 2.0)
    control_y = min(max(2.0, control_y), height - 2.0)

    waypoint_count = rng.randint(2, 3) if low_clock_mode else rng.randint(2, 4)
    total_drag_ms = rng.randint(8, 18) if low_clock_mode else rng.randint(8, 95)
    pre_delay_ms = 0 if low_clock_mode else rng.randint(0, 25)
    base_duration, extra_ms = divmod(total_drag_ms, waypoint_count)
    segment_durations = [base_duration] * waypoint_count
    if extra_ms:
        for index in rng.sample(range(waypoint_count), extra_ms):
            segment_durations[index] += 1

    path_points: list[tuple[float, float]] = []
    for index in range(1, waypoint_count + 1):
        t = index / waypoint_count
        one_minus_t = 1.0 - t
        px = (
            one_minus_t * one_minus_t * sx
            + 2.0 * one_minus_t * t * control_x
            + t * t * dx
        )
        py = (
            one_minus_t * one_minus_t * sy
            + 2.0 * one_minus_t * t * control_y
            + t * t * dy
        )
        if index < waypoint_count:
            px += rng.uniform(-2.0, 2.0)
            py += rng.uniform(-2.0, 2.0)
            px = min(max(2.0, px), width - 2.0)
            py = min(max(2.0, py), height - 2.0)
        else:
            # Make sure the mouse button is released exactly at the chosen target.
            px, py = dx, dy
        path_points.append((px, py))

    source_offset = (round(sx - width / 2.0), round(sy - height / 2.0))
    hover_ms = 0 if low_clock_mode else rng.randint(8, 25)

    try:
        # Use W3C pointer actions with per-segment timing. Low-clock mode
        # removes optional positioning/hover waits and shortens the drag.
        if pre_delay_ms:
            time.sleep(pre_delay_ms / 1000.0)

        actions = ActionChains(driver)
        pointer = actions.w3c_actions.pointer_action
        pointer.source.create_pointer_move(
            origin=board_element,
            duration=0 if low_clock_mode else rng.randint(15, 35),
            x=source_offset[0],
            y=source_offset[1],
        )
        pointer.pause(hover_ms / 1000.0)
        pointer.pointer_down()
        # Per-move hold after mouse-down; even fast mode samples a fresh delay.
        pointer.pause(
            rng.uniform(0.001, 0.0035) if low_clock_mode else rng.uniform(0.002, 0.012)
        )

        for point_index, ((px, py), segment_ms) in enumerate(zip(path_points, segment_durations)):
            pointer.source.create_pointer_move(
                origin=board_element,
                duration=segment_ms,
                x=round(px - width / 2.0),
                y=round(py - height / 2.0),
            )
            if point_index < len(path_points) - 1:
                pointer.pause(
                    rng.uniform(0.0003, 0.0015)
                    if low_clock_mode else rng.uniform(0.001, 0.008)
                )

        pointer.pointer_up()
        actions.perform()
    except (WebDriverException, ValueError, TypeError, AttributeError) as exc:
        # Never leave the mouse button pressed if a pointer action fails.
        try:
            ActionChains(driver).release().perform()
        except Exception:
            pass
        return {
            "ok": False,
            "reason": "cursor-action-failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
            "drag_ms": total_drag_ms,
            "pre_delay_ms": pre_delay_ms,
        }

    # Confirm the UI accepted this drag before reporting success.
    deadline = time.monotonic() + (
        0.12 if low_clock_mode else (0.45 if allow_premove else 0.90)
    )
    while time.monotonic() < deadline:
        try:
            current_fen = driver.execute_script(
                "return window.__liveGameApiReader ? "
                "window.__liveGameApiReader.current().fen : '';"
            )
        except WebDriverException:
            current_fen = ""
        if current_fen and uci_move_reaches_fen(expected_fen, str(current_fen).strip(), text):
            return {
                "ok": True,
                "confirmed": True,
                "uci": text,
                "drag_ms": total_drag_ms,
                "pre_delay_ms": pre_delay_ms,
                "waypoints": waypoint_count,
                "premove_applied": bool(allow_premove),
            }
        if allow_premove:
            try:
                status = driver.execute_script(
                    "return window.__liveGameApiReader ? "
                    "window.__liveGameApiReader.premoveStatus() : {pending:false,source:'reader-not-installed'};"
                )
                if isinstance(status, dict) and status.get("pending"):
                    return {
                        "ok": True, "uci": text, "premove_attempted": True,
                        "premove_confirmed": True, "premove_source": status.get("source", ""),
                        "drag_ms": total_drag_ms, "pre_delay_ms": pre_delay_ms,
                        "waypoints": waypoint_count,
                    }
            except WebDriverException:
                pass
        time.sleep(0.003 if low_clock_mode else 0.015)

    if allow_premove:
        return {
            "ok": True, "uci": text, "premove_attempted": True,
            "premove_confirmed": False, "premove_source": "not-exposed",
            "drag_ms": total_drag_ms, "pre_delay_ms": pre_delay_ms,
            "waypoints": waypoint_count,
        }
    return {
        "ok": False,
        "reason": "cursor-drag-not-confirmed",
        "drag_ms": total_drag_ms,
        "pre_delay_ms": pre_delay_ms,
        "waypoints": waypoint_count,
    }


def perform_cursor_drag(
    driver, uci: str, expected_fen: str, expected_color: str | None,
    allow_premove: bool = False, low_clock_mode: bool = False,
) -> dict[str, object]:
    """Perform one guarded retry only when the first drag left the board unchanged."""
    first = _perform_cursor_drag_once(
        driver, uci, expected_fen, expected_color,
        allow_premove=allow_premove, low_clock_mode=low_clock_mode,
    )
    if first.get("ok") or allow_premove:
        # Unconfirmed premove gestures are never retried: the site might have
        # queued the move without exposing that state.
        return first
    reason = str(first.get("reason") or "")
    if reason not in {"cursor-drag-not-confirmed", "cursor-action-failed"}:
        return first
    try:
        current_fen = driver.execute_script(
            "return window.__liveGameApiReader ? "
            "window.__liveGameApiReader.current().fen : '';"
        )
    except WebDriverException:
        return first
    # Never replay a stale move if the first gesture or an opponent action
    # already changed the board.
    if not current_fen or str(current_fen).strip() != expected_fen.strip():
        return first
    retry = _perform_cursor_drag_once(
        driver, uci, expected_fen, expected_color,
        allow_premove=False, low_clock_mode=low_clock_mode,
    )
    retry["retry_attempted"] = True
    if retry.get("ok"):
        retry["retry_after"] = reason
        return retry
    retry["first_failure"] = reason
    return retry


def perform_api_move(
    driver, uci: str, expected_fen: str, expected_color: str | None,
    *, low_clock_mode: bool = False, source: str = "api",
) -> dict[str, object]:
    """Submit through the configured site's legal-move API and confirm the chosen move."""
    try:
        response = driver.execute_script(
            "return window.__liveGameApiReader ? "
            "window.__liveGameApiReader.playUci(arguments[0], arguments[1], arguments[2]) : "
            "{ok:false, reason:'reader-not-installed'};",
            uci, expected_fen, expected_color,
        )
    except WebDriverException:
        return {"ok": False, "reason": "api-submit-unavailable", "source": source}
    if not isinstance(response, dict) or not response.get("ok"):
        return response if isinstance(response, dict) else {
            "ok": False, "reason": "invalid-api-submit-response", "source": source
        }

    # API acceptance alone is not a confirmed move. Verify the selected UCI
    # is reflected in the authoritative board, allowing one instant opponent reply.
    deadline = time.monotonic() + (0.12 if low_clock_mode else 0.50)
    while True:
        try:
            current = driver.execute_script(
                "return window.__liveGameApiReader ? "
                "window.__liveGameApiReader.current().fen : '';"
            )
        except WebDriverException:
            current = ""
        if current and uci_move_reaches_fen(expected_fen, str(current).strip(), uci):
            return {
                **response, "ok": True, "confirmed": True,
                "uci": str(uci).lower(), "source": source,
            }
        if time.monotonic() >= deadline:
            break
        time.sleep(0.003 if low_clock_mode else 0.010)
    return {
        "ok": False,
        "reason": "api-move-not-confirmed",
        "api_accepted": True,
        "uci": str(uci).lower(),
        "source": source,
    }


def perform_api_premove(
    driver, uci: str, expected_fen: str, expected_color: str | None
) -> dict[str, object]:
    """Queue a premove only through an explicitly exposed site premove API."""
    try:
        result = driver.execute_script(
            "return window.__liveGameApiReader ? "
            "window.__liveGameApiReader.queuePremove(arguments[0], arguments[1], arguments[2]) : "
            "{ok:false, reason:'reader-not-installed'};",
            uci, expected_fen, expected_color,
        )
    except WebDriverException:
        return {"ok": False, "reason": "premove-api-unavailable"}
    return result if isinstance(result, dict) else {"ok": False, "reason": "invalid-premove-response"}


PREMOVE_OPENING_FULLMOVE_MIN = 5
PREMOVE_OPENING_FULLMOVE_MAX = 8
PREMOVE_ENDGAME_MAX_PIECES = 10
PREMOVE_MIN_BOT_EVAL_CP = -25
PREMOVE_MAX_EVAL_DROP_CP = 40
PREMOVE_OPENING_MIN_BOT_EVAL_CP = -50
PREMOVE_OPENING_MAX_EVAL_DROP_CP = 60
PREMOVE_MATE_REPLY_MULTIPV = 3
PREMOVE_MATE_FOLLOWUP_MULTIPV = 8
# Middlegame recaptures: accept a reasonably broad reply set, not every legal blunder.
# Rank is zero-indexed, so 4 means the top five replies; 175cp is a permissive gap.
PREMOVE_LIKELY_REPLY_MAX_RANK = 4
PREMOVE_LIKELY_REPLY_MAX_CP_GAP = 175


def premove_phase_allowed(
    board, opening_fullmove_max: int | None = None
) -> tuple[bool, str]:
    """Only permit premoves in the sampled opening window or genuine low-piece endgame."""
    if chess is None or board is None:
        return False, "board-unavailable"
    try:
        requested_limit = (
            PREMOVE_OPENING_FULLMOVE_MAX
            if opening_fullmove_max is None else int(opening_fullmove_max)
        )
        opening_limit = max(
            PREMOVE_OPENING_FULLMOVE_MIN,
            min(PREMOVE_OPENING_FULLMOVE_MAX, requested_limit),
        )
        if int(board.fullmove_number) <= opening_limit:
            return True, "opening"
        if len(board.piece_map()) <= PREMOVE_ENDGAME_MAX_PIECES:
            return True, "endgame"
    except (AttributeError, TypeError, ValueError):
        return False, "phase-unavailable"
    return False, "outside-opening-endgame"


def _premove_score_for_bot(info, bot_color: str) -> int | None:
    """Return centipawns from our side's POV; missing scores fail closed."""
    if not isinstance(info, dict) or chess is None:
        return None
    raw_score = info.get("score")
    if raw_score is None:
        return None
    try:
        bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
        if hasattr(raw_score, "pov"):
            raw_score = raw_score.pov(bot_turn)
        if hasattr(raw_score, "score"):
            value = raw_score.score(mate_score=100000)
            return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _single_premove_info(engine, board, limit, root_moves=None):
    """Normalize python-chess analyse() output for a single-PV check."""
    result = engine.analyse(
        board,
        limit,
        multipv=1,
        root_moves=root_moves,
    )
    return result[0] if isinstance(result, list) and result else (
        None if isinstance(result, list) else result
    )


def _build_middle_game_premove_scenarios(board, bot_color: str):
    """Find (a) moves legal after every opponent reply and (b) safe-shape recaptures."""
    if chess is None or board is None:
        return [], set(), []
    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    opponent_replies = list(board.legal_moves)
    if not opponent_replies:
        return [], set(), []

    response_boards = []
    universal_moves = None
    capture_groups = {}

    for reply in opponent_replies:
        after_reply = board.copy(stack=True)
        after_reply.push(reply)
        response_boards.append((reply, after_reply))

        # A universal premove must still be legal after every legal opponent move.
        legal_uci = {
            move.uci()
            for move in after_reply.legal_moves
            if move.promotion is None and not after_reply.is_castling(move)
        }
        universal_moves = (
            legal_uci if universal_moves is None else universal_moves & legal_uci
        )

        # A conditional recapture only triggers if an opposing capture lands on
        # a square currently occupied by one of our pieces.
        if board.is_capture(reply):
            target_piece = board.piece_at(reply.to_square)
            if target_piece is not None and target_piece.color == bot_turn:
                capture_groups.setdefault(reply.to_square, []).append((reply, after_reply))

    recapture_options = []
    piece_values = {
        chess.PAWN: 100,
        chess.KNIGHT: 320,
        chess.BISHOP: 330,
        chess.ROOK: 500,
        chess.QUEEN: 900,
        chess.KING: 20000,
    }
    for target_square, outcomes in capture_groups.items():
        common_recaptures = None
        first_position = outcomes[0][1]
        for _reply, after_capture in outcomes:
            possible = {
                move.uci()
                for move in after_capture.legal_moves
                if (
                    move.to_square == target_square
                    and after_capture.is_capture(move)
                    and move.promotion is None
                    and not after_capture.is_castling(move)
                )
            }
            if not possible:
                common_recaptures = set()
                break
            common_recaptures = (
                possible
                if common_recaptures is None
                else common_recaptures & possible
            )
            if not common_recaptures:
                break

        if common_recaptures:
            target_piece = board.piece_at(target_square)
            recapture_options.append({
                "target_square": target_square,
                "piece_value": piece_values.get(
                    target_piece.piece_type if target_piece else chess.PAWN, 0
                ),
                "outcomes": outcomes,
                "candidate_uci": common_recaptures,
                "candidate_moves": [
                    move for move in first_position.legal_moves
                    if move.uci() in common_recaptures
                ],
            })

    recapture_options.sort(
        key=lambda group: (
            -group["piece_value"],
            -len(group["outcomes"]),
            chess.square_name(group["target_square"]),
        )
    )
    return response_boards, (universal_moves or set()), recapture_options


def _candidate_safe_across_outcomes(
    engine, outcomes, candidate, bot_color: str, search_time: float,
    baseline_cache: dict[str, int | None],
) -> tuple[bool, int | None, str]:
    """Test a candidate against every relevant reply, failing closed on missing scores."""
    worst_eval = 100000
    limit = chess.engine.Limit(depth=8, time=search_time)
    candidate_uci = candidate.uci()

    for reply, after_reply in outcomes:
        legal_move = next(
            (move for move in after_reply.legal_moves if move.uci() == candidate_uci),
            None,
        )
        if legal_move is None:
            return False, None, "candidate-not-legal-after-every-reply"

        reply_key = reply.uci()
        if reply_key not in baseline_cache:
            baseline_info = _single_premove_info(engine, after_reply, limit)
            baseline_cache[reply_key] = _premove_score_for_bot(baseline_info, bot_color)
        best_eval = baseline_cache[reply_key]
        if best_eval is None:
            return False, None, "baseline-evaluation-unavailable"

        selected_info = _single_premove_info(
            engine, after_reply, limit, root_moves=[legal_move]
        )
        selected_eval = _premove_score_for_bot(selected_info, bot_color)
        if selected_eval is None:
            return False, None, "candidate-evaluation-unavailable"
        if selected_eval < PREMOVE_MIN_BOT_EVAL_CP:
            return False, selected_eval, "candidate-can-leave-bot-losing"
        if best_eval - selected_eval > PREMOVE_MAX_EVAL_DROP_CP:
            return False, selected_eval, "candidate-reduces-evaluation-too-much"
        worst_eval = min(worst_eval, selected_eval)

    return True, (worst_eval if worst_eval != 100000 else None), "all-replies-safe"


def choose_middle_game_premove(
    engine, board, strategy_module, bot_color: str, args,
) -> tuple[object | None, object | None, str]:
    """Premove only for robust recaptures or one move safe after every legal reply."""
    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    if board.turn == bot_turn:
        return None, None, "bot-turn-already-active"

    # Build the legality shapes first. Most middlegame positions return here
    # without extra Stockfish searches or any cursor/API action.
    response_boards, universal_uci, recapture_options = (
        _build_middle_game_premove_scenarios(board, bot_color)
    )
    if not response_boards:
        return None, None, "no-opponent-replies"
    if not universal_uci and not recapture_options:
        return None, None, "no-universal-safe-move-or-recapture-pattern"

    search_time = max(0.003, min(float(args.engine_time), 0.008))
    current_lines = engine.analyse(
        board,
        chess.engine.Limit(depth=8, time=search_time),
        multipv=min(PREMOVE_LIKELY_REPLY_MAX_RANK + 1, len(response_boards)),
    )
    if not isinstance(current_lines, list):
        current_lines = [current_lines]
    current_lines = [
        info for info in current_lines
        if isinstance(info, dict) and info.get("pv")
        and info["pv"][0] in board.legal_moves
    ]
    if not current_lines:
        return None, None, "current-position-analysis-unavailable"
    current_eval = _premove_score_for_bot(current_lines[0], bot_color)
    if current_eval is None:
        return None, None, "current-evaluation-unavailable"
    if current_eval < PREMOVE_MIN_BOT_EVAL_CP:
        return None, None, "bot-position-already-losing"

    # Estimate whether the opponent capture is plausible. This is deliberately
    # permissive: top-five replies within 1.75 pawns remain eligible. It only
    # filters speculative recaptures; universal-safe premoves are still checked
    # below. A candidate recapture must still pass the all-outcomes safety test.
    reply_scores = []
    for reply_rank, info in enumerate(current_lines[: PREMOVE_LIKELY_REPLY_MAX_RANK + 1]):
        pv = info.get("pv") if isinstance(info, dict) else None
        score_obj = info.get("score") if isinstance(info, dict) else None
        if not pv or score_obj is None:
            continue
        try:
            opponent_score = score_obj.pov(board.turn).score(mate_score=100000)
        except (AttributeError, TypeError, ValueError):
            opponent_score = None
        if opponent_score is None:
            continue
        reply_scores.append((reply_rank, pv[0].uci(), int(opponent_score)))

    plausible_reply_uci = set()
    if reply_scores:
        best_opponent_score = max(score for _rank, _uci, score in reply_scores)
        plausible_reply_uci = {
            uci
            for rank, uci, score in reply_scores
            if (
                rank <= PREMOVE_LIKELY_REPLY_MAX_RANK
                and best_opponent_score - score <= PREMOVE_LIKELY_REPLY_MAX_CP_GAP
            )
        }
    elif current_lines and current_lines[0].get("pv"):
        # Preserve the most likely predicted reply if MultiPV scores are partial.
        plausible_reply_uci.add(current_lines[0]["pv"][0].uci())

    likely_recapture_options = [
        group
        for group in recapture_options
        if any(reply.uci() in plausible_reply_uci for reply, _after in group["outcomes"])
    ]

    baseline_cache: dict[str, int | None] = {}

    # 1) Midgame recapture: choose one destination currently occupied by our
    # piece. The premove can only become legal when that piece is captured;
    # check every legal capture to this square and reject unsafe recaptures.
    for group in likely_recapture_options:
        outcomes = group["outcomes"]
        common_uci = group["candidate_uci"]
        first_capture_board = outcomes[0][1]
        infos = engine.analyse(
            first_capture_board,
            chess.engine.Limit(depth=8, time=search_time),
            multipv=min(5, max(1, len(common_uci))),
        )
        if not isinstance(infos, list):
            infos = [infos]
        ordered = []
        seen = set()
        for info in infos:
            pv = info.get("pv") if isinstance(info, dict) else None
            if pv and pv[0].uci() in common_uci and pv[0].uci() not in seen:
                ordered.append(pv[0])
                seen.add(pv[0].uci())
        for move in group["candidate_moves"]:
            if move.uci() in common_uci and move.uci() not in seen:
                ordered.append(move)
                seen.add(move.uci())

        # Test a small ranked set of recaptures; an unsafe top candidate should
        # not prevent another candidate from passing the material/eval guards.
        for candidate in ordered[:3]:
            if any(
                strategy_module.candidate_hangs_own_piece(after_capture, candidate)
                for _reply, after_capture in outcomes
            ):
                continue
            safe, worst_eval, reason = _candidate_safe_across_outcomes(
                engine, outcomes, candidate, bot_color, search_time, baseline_cache
            )
            if safe and worst_eval is not None:
                target_name = chess.square_name(group["target_square"])
                return (
                    candidate,
                    outcomes[0][0],
                    f"midgame-safe-recapture target={target_name} worst={worst_eval / 100:+.2f}",
                )
            # This candidate failed; test the next ranked recapture for the same target.
            continue

    # 2) Universal-safe move: the exact same move must be legal after every
    # possible opponent move, then pass engine checks in every resulting position.
    predicted_reply = current_lines[0]["pv"][0]
    predicted_board = next(
        (position for reply, position in response_boards if reply == predicted_reply),
        None,
    )
    if predicted_board is None or not universal_uci:
        return None, None, "no-universal-safe-move-or-safe-recapture"

    own_lines = engine.analyse(
        predicted_board,
        chess.engine.Limit(depth=8, time=search_time),
        multipv=min(
            int(getattr(strategy_module, "TRAINING_MULTI_PV", 15)),
            max(1, predicted_board.legal_moves.count()),
        ),
    )
    if not isinstance(own_lines, list):
        own_lines = [own_lines]
    candidates = []
    seen = set()
    for info in own_lines:
        pv = info.get("pv") if isinstance(info, dict) else None
        if not pv:
            continue
        move = pv[0]
        if (
            move.uci() in universal_uci
            and move.uci() not in seen
            and move.promotion is None
            and not predicted_board.is_castling(move)
        ):
            candidates.append(move)
            seen.add(move.uci())

    for candidate in candidates[:4]:
        if any(
            strategy_module.candidate_hangs_own_piece(after_reply, candidate)
            for _reply, after_reply in response_boards
        ):
            continue
        safe, worst_eval, _reason = _candidate_safe_across_outcomes(
            engine, response_boards, candidate, bot_color, search_time, baseline_cache
        )
        if safe and worst_eval is not None:
            return (
                candidate,
                predicted_reply,
                f"midgame-universal-safe worst={worst_eval / 100:+.2f}",
            )

    return None, None, "no-midgame-premove-passed-all-reply-checks"


PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP = 50


def choose_low_clock_conversion_premove(
    engine, board, strategy_module, bot_color: str, args,
) -> tuple[object | None, object | None, str]:
    """Fast, safety-checked capture/promotion premove for a favorable low-clock endgame."""
    if chess is None or engine is None or strategy_module is None or board is None:
        return None, None, "low-clock-prerequisites-unavailable"

    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    if board.turn == bot_turn or board.is_game_over(claim_draw=True):
        return None, None, "not-opponent-turn-or-game-over"

    try:
        opponent_movable_pieces = len({move.from_square for move in board.legal_moves})
        bot_non_king_count = sum(
            1 for piece in board.piece_map().values()
            if piece.color == bot_turn and piece.piece_type != chess.KING
        )
        opponent_non_king_count = sum(
            1 for piece in board.piece_map().values()
            if piece.color != bot_turn and piece.piece_type != chess.KING
        )
        bot_material = strategy_module.side_material(board, bot_turn)
        opponent_material = strategy_module.side_material(board, not bot_turn)
    except Exception:
        return None, None, "low-clock-position-metrics-unavailable"

    if not (1 <= opponent_movable_pieces <= 3):
        return None, None, f"opponent-has-{opponent_movable_pieces}-movable-pieces"
    if bot_non_king_count <= opponent_non_king_count or bot_material <= opponent_material:
        return None, None, "material-advantage-not-confirmed"

    search_time = max(0.003, min(float(args.engine_time), 0.006))
    try:
        reply_count = max(1, min(3, board.legal_moves.count()))
        opponent_lines = engine.analyse(
            board,
            chess.engine.Limit(depth=8, time=search_time),
            multipv=reply_count,
        )
        if not isinstance(opponent_lines, list):
            opponent_lines = [opponent_lines]
        opponent_lines = [
            info for info in opponent_lines
            if isinstance(info, dict) and info.get("pv")
            and info["pv"][0] in board.legal_moves
        ]
        if not opponent_lines:
            return None, None, "low-clock-opponent-reply-unavailable"

        predicted_reply = opponent_lines[0]["pv"][0]
        current_eval = _premove_score_for_bot(opponent_lines[0], bot_color)
        if current_eval is None or current_eval < PREMOVE_MIN_BOT_EVAL_CP:
            return None, predicted_reply, "low-clock-position-not-safely-winning"

        hypothetical = board.copy(stack=True)
        hypothetical.push(predicted_reply)
        if hypothetical.turn != bot_turn:
            return None, predicted_reply, "low-clock-hypothetical-turn-mismatch"

        own_lines = engine.analyse(
            hypothetical,
            chess.engine.Limit(depth=8, time=search_time),
            multipv=min(8, max(1, hypothetical.legal_moves.count())),
        )
        if not isinstance(own_lines, list):
            own_lines = [own_lines]
        own_lines = [
            info for info in own_lines
            if isinstance(info, dict) and info.get("pv")
            and info["pv"][0] in hypothetical.legal_moves
        ]
        if not own_lines:
            # Stockfish gave no usable continuation. Continue to the legal-move
            # fallback below rather than abandoning this low-clock opportunity.
            own_lines = []

        best_after_reply_eval = _premove_score_for_bot(own_lines[0], bot_color) if own_lines else None
        if best_after_reply_eval is None or best_after_reply_eval < PREMOVE_MIN_BOT_EVAL_CP:
            return None, predicted_reply, "low-clock-best-follow-up-not-safe"

        # If the current short analysis already sees mate, select the shortest
        # mate line it found, randomizing only exact-distance ties.
        mate_options = []
        for info in own_lines:
            score = info.get("score")
            pv = info.get("pv") or []
            if not score or not pv:
                continue
            try:
                mate_distance = score.pov(bot_turn).mate()
            except (AttributeError, TypeError, ValueError):
                mate_distance = None
            if mate_distance is not None and mate_distance > 0:
                mate_options.append((int(mate_distance), info["pv"][0], info))
        if mate_options:
            fastest_distance = min(item[0] for item in mate_options)
            fastest = [item for item in mate_options if item[0] == fastest_distance]
            mate_distance, move, info = random.choice(fastest)
            return (
                move,
                predicted_reply,
                f"low-clock-fast-mate-premove M{mate_distance} after likely reply",
            )

        # Enumerate actual tactical moves so promotions/captures are not limited
        # to those that happened to land inside the first MultiPV slice.
        info_by_uci = {
            info["pv"][0].uci(): info
            for info in own_lines if info.get("pv")
        }
        tactical = []
        piece_values = {
            chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330,
            chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 20000,
        }
        allow_promotion = getattr(args, "move_input", "api") != "cursor"
        for move in hypothetical.legal_moves:
            if move.promotion is not None and not allow_promotion:
                continue
            is_promotion = move.promotion is not None
            is_capture = hypothetical.is_capture(move)
            if not (is_promotion or is_capture or hypothetical.gives_check(move)):
                continue
            victim = hypothetical.piece_at(move.to_square)
            if hypothetical.is_en_passant(move):
                victim_value = piece_values[chess.PAWN]
            else:
                victim_value = piece_values.get(victim.piece_type, 0) if victim else 0
            promotion_value = piece_values.get(move.promotion, 0) if move.promotion else 0
            tactical.append((
                0 if is_promotion else (1 if is_capture else 2),
                -promotion_value if is_promotion else -victim_value,
                move,
            ))
        tactical.sort(key=lambda item: (item[0], item[1], item[2].uci()))

        # Cap extra one-move checks so a low-clock decision cannot turn into a
        # long search. A move is accepted only if it stays winning enough.
        checked = set()
        for _kind, _value, move in tactical[:6]:
            if strategy_module.candidate_hangs_own_piece(hypothetical, move):
                continue
            uci = move.uci()
            if uci in checked:
                continue
            checked.add(uci)
            info = info_by_uci.get(uci)
            if info is None:
                info = _single_premove_info(
                    engine,
                    hypothetical,
                    chess.engine.Limit(depth=7, time=search_time),
                    root_moves=[move],
                )
            selected_eval = _premove_score_for_bot(info, bot_color)
            if selected_eval is None or selected_eval < PREMOVE_MIN_BOT_EVAL_CP:
                continue
            if best_after_reply_eval - selected_eval > PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP:
                continue
            tactical_type = (
                "promotion" if move.promotion is not None
                else ("capture" if hypothetical.is_capture(move) else "check")
            )
            return (
                move,
                predicted_reply,
                f"low-clock-{tactical_type}-priority | eval={selected_eval / 100:+.2f} "
                f"| opponent-movable={opponent_movable_pieces}",
            )

        return None, predicted_reply, "no-low-clock-tactic-passed-evaluation-guard"
    except (chess.engine.EngineError, ValueError, RuntimeError, TypeError):
        return None, None, "low-clock-premove-analysis-failed"



def choose_low_clock_likely_reply_premove(
    engine, board, bot_color: str, args, strategy_module=None,
) -> tuple[object | None, object | None, str]:
    """Fast low-clock fallback: best continuation after the most likely reply.

    This speculative fallback runs only on the configured authorized test site.
    It is less restrictive than the universal-safe middlegame path but still
    requires a real legal reply and a legal, submittable continuation.
    """
    if chess is None or engine is None or board is None or strategy_module is None:
        return None, None, "low-clock-fallback-prerequisites-unavailable"
    if not callable(getattr(strategy_module, "candidate_hangs_own_piece", None)):
        return None, None, "low-clock-fallback-safety-check-unavailable"

    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    if board.turn == bot_turn or board.is_game_over(claim_draw=True):
        return None, None, "low-clock-fallback-not-opponent-turn-or-game-over"

    try:
        reply_count = board.legal_moves.count()
        if reply_count <= 0:
            return None, None, "low-clock-fallback-no-legal-reply"

        search_time = max(0.002, min(float(args.engine_time), 0.005))
        opponent_lines = engine.analyse(
            board,
            chess.engine.Limit(depth=7, time=search_time),
            multipv=min(3, reply_count),
        )
        if not isinstance(opponent_lines, list):
            opponent_lines = [opponent_lines]
        opponent_lines = [
            info for info in opponent_lines
            if isinstance(info, dict) and info.get("pv")
            and info["pv"][0] in board.legal_moves
        ]
        if not opponent_lines:
            return None, None, "low-clock-fallback-opponent-reply-unavailable"

        predicted_reply = opponent_lines[0]["pv"][0]
        hypothetical = board.copy(stack=True)
        hypothetical.push(predicted_reply)
        if hypothetical.turn != bot_turn or hypothetical.is_game_over(claim_draw=True):
            return None, predicted_reply, "low-clock-fallback-no-follow-up-after-predicted-reply"

        own_lines = engine.analyse(
            hypothetical,
            chess.engine.Limit(depth=7, time=search_time),
            multipv=min(8, max(1, hypothetical.legal_moves.count())),
        )
        if not isinstance(own_lines, list):
            own_lines = [own_lines]
        candidates = []
        seen = set()
        for info in own_lines:
            pv = info.get("pv") if isinstance(info, dict) else None
            if not pv:
                continue
            move = pv[0]
            if move not in hypothetical.legal_moves or move.uci() in seen:
                continue
            if move.promotion is not None and getattr(args, "move_input", "api") == "cursor":
                continue
            # Reject an immediate, uncompensated material loss after this premove.
            if strategy_module.candidate_hangs_own_piece(hypothetical, move):
                continue
            candidates.append((move, info))
            seen.add(move.uci())

        # Never fall back to a merely-legal move when the engine could not
        # score it. Late-clock premoves still must pass the material guard above
        # and a tight evaluation-drop threshold; otherwise use normal-turn logic.
        best_after_reply_eval = _premove_score_for_bot(own_lines[0], bot_color) if own_lines else None
        if best_after_reply_eval is None:
            return None, predicted_reply, "low-clock-follow-up-evaluation-unavailable"
        if best_after_reply_eval < PREMOVE_MIN_BOT_EVAL_CP:
            return None, predicted_reply, "low-clock-position-too-losing-for-premove"

        guarded_candidates = []
        for move, info in candidates:
            selected_eval = _premove_score_for_bot(info, bot_color)
            if selected_eval is None or selected_eval < PREMOVE_MIN_BOT_EVAL_CP:
                continue
            if best_after_reply_eval - selected_eval > PREMOVE_LOW_CLOCK_MAX_EVAL_DROP_CP:
                continue
            guarded_candidates.append((move, info, selected_eval))
        if not guarded_candidates:
            return None, predicted_reply, "low-clock-candidates-failed-material-or-evaluation-guards"

        selected, selected_info, selected_eval = guarded_candidates[0]
        reply_san = board.san(predicted_reply)
        eval_text = f" eval={selected_eval / 100:+.2f}" if selected_eval is not None else ""
        return (
            selected,
            predicted_reply,
            f"low-clock-likely-reply-fallback reply={reply_san} rank=1{eval_text}",
        )
    except (chess.engine.EngineError, ValueError, RuntimeError, TypeError, AttributeError):
        return None, None, "low-clock-fallback-analysis-failed"


def _premove_mate_distance_for_bot(info, bot_color: str) -> int | None:
    """Return a mate distance from the bot's perspective when Stockfish reports one."""
    if not isinstance(info, dict) or chess is None:
        return None
    score = info.get("score")
    if score is None:
        return None
    try:
        bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
        if hasattr(score, "pov"):
            score = score.pov(bot_turn)
        mate_distance = score.mate() if hasattr(score, "mate") else None
        return int(mate_distance) if mate_distance is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def choose_mate_phase_premove(
    engine, board, bot_color: str, args,
) -> tuple[object | None, object | None, str]:
    """Queue a mating continuation when a forced mate is already visible.

    The premove is based on the engine's most likely opponent reply, matching
    the opening/low-clock premove contract. Do not return a speculative move
    unless Stockfish still reports a forced mate for the bot after that reply.
    """
    if chess is None or engine is None or board is None:
        return None, None, "mate-premove-prerequisites-unavailable"

    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    if board.turn == bot_turn or board.is_game_over(claim_draw=True):
        return None, None, "mate-premove-not-opponent-turn-or-game-over"

    try:
        reply_count = board.legal_moves.count()
        if reply_count <= 0:
            return None, None, "mate-premove-no-legal-opponent-reply"

        search_time = max(0.003, min(float(args.engine_time), 0.010))
        current_lines = engine.analyse(
            board,
            chess.engine.Limit(depth=8, time=search_time),
            multipv=min(PREMOVE_MATE_REPLY_MULTIPV, reply_count),
        )
        if not isinstance(current_lines, list):
            current_lines = [current_lines]
        current_lines = [
            info for info in current_lines
            if isinstance(info, dict)
            and info.get("pv")
            and info["pv"][0] in board.legal_moves
        ]
        if not current_lines:
            return None, None, "mate-premove-no-reply-line"

        current_mate = _premove_mate_distance_for_bot(current_lines[0], bot_color)
        if current_mate is None or current_mate <= 0:
            return None, None, "no-forced-mate-premove-found"

        predicted_reply = current_lines[0]["pv"][0]
        hypothetical = board.copy(stack=True)
        hypothetical.push(predicted_reply)
        if hypothetical.turn != bot_turn or hypothetical.is_game_over(claim_draw=True):
            return None, predicted_reply, "mate-premove-no-follow-up-after-reply"

        own_lines = engine.analyse(
            hypothetical,
            chess.engine.Limit(depth=9, time=search_time),
            multipv=min(PREMOVE_MATE_FOLLOWUP_MULTIPV, max(1, hypothetical.legal_moves.count())),
        )
        if not isinstance(own_lines, list):
            own_lines = [own_lines]
        own_lines = [
            info for info in own_lines
            if isinstance(info, dict)
            and info.get("pv")
            and info["pv"][0] in hypothetical.legal_moves
        ]
        if not own_lines:
            return None, predicted_reply, "mate-premove-no-mating-follow-up"

        mate_candidates = []
        cursor_input = getattr(args, "move_input", "api") == "cursor"
        for rank, info in enumerate(own_lines):
            move = info["pv"][0]
            if move.promotion is not None and cursor_input:
                continue
            after_move = hypothetical.copy(stack=True)
            after_move.push(move)
            mate_distance = 0 if after_move.is_checkmate() else _premove_mate_distance_for_bot(info, bot_color)
            if mate_distance is None or mate_distance < 0:
                continue
            mate_candidates.append((mate_distance, rank, move))

        if not mate_candidates:
            return None, predicted_reply, "mate-premove-no-submittable-mating-line"

        fastest_distance = min(item[0] for item in mate_candidates)
        fastest = [item for item in mate_candidates if item[0] == fastest_distance]
        _distance, _rank, selected = random.choice(fastest)
        reply_san = board.san(predicted_reply)
        mate_label = "M0" if fastest_distance == 0 else f"M{fastest_distance}"
        return (
            selected,
            predicted_reply,
            f"mate-phase-premove {mate_label} after predicted reply {reply_san}",
        )
    except (chess.engine.EngineError, ValueError, RuntimeError, TypeError, AttributeError):
        return None, None, "mate-premove-analysis-failed"


class _PremoveDeadlineExceeded(RuntimeError):
    """Raised when premove analysis would overrun its monotonic deadline."""


class _DeadlineBoundEngine:
    """Proxy that caps each premove engine search to the remaining time budget."""

    def __init__(self, engine, deadline: float):
        self._engine = engine
        self._deadline = float(deadline)

    def __getattr__(self, name):
        return getattr(self._engine, name)

    def analyse(self, board, limit, *args, **kwargs):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0.012:
            raise _PremoveDeadlineExceeded("Premove analysis deadline elapsed.")
        # Reserve a little room for UCI response and Python/Selenium hand-off.
        allowed_time = remaining - 0.008
        requested_time = getattr(limit, "time", None)
        if requested_time is not None:
            allowed_time = min(allowed_time, max(0.0, float(requested_time)))
        if allowed_time < 0.005:
            raise _PremoveDeadlineExceeded("Premove analysis budget is too small.")
        bounded_limit = chess.engine.Limit(
            depth=getattr(limit, "depth", None),
            time=allowed_time,
        )
        result = self._engine.analyse(board, bounded_limit, *args, **kwargs)
        if time.monotonic() > self._deadline:
            raise _PremoveDeadlineExceeded("Premove engine result arrived after deadline.")
        return result


def choose_premove_candidate(
    engine, board, strategy_module, bot_color: str, args, book=None,
    opponent_match_history: list[float] | None = None,
    low_clock_mode: bool = False,
    analysis_deadline: float | None = None,
) -> tuple[object | None, object | None, str]:
    """Choose a premove using existing policies, bounded by an analysis deadline."""
    if chess is None or engine is None or strategy_module is None or board is None:
        return None, None, "premove-prerequisites-unavailable"
    if board.is_game_over(claim_draw=True):
        return None, None, "game-over"

    bot_turn = chess.WHITE if bot_color == "white" else chess.BLACK
    if board.turn == bot_turn:
        return None, None, "bot-turn-already-active"

    if analysis_deadline is not None:
        if time.monotonic() >= float(analysis_deadline):
            return None, None, "premove-analysis-deadline-exceeded"
        engine = _DeadlineBoundEngine(engine, float(analysis_deadline))

    if low_clock_mode:
        low_clock_move, low_clock_reply, low_clock_reason = choose_low_clock_conversion_premove(
            engine, board, strategy_module, bot_color, args
        )
        if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
            return None, low_clock_reply, "premove-analysis-deadline-exceeded"
        if low_clock_move is not None:
            return low_clock_move, low_clock_reply, low_clock_reason
        fallback_move, fallback_reply, fallback_reason = choose_low_clock_likely_reply_premove(
            engine, board, bot_color, args, strategy_module=strategy_module
        )
        if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
            return None, fallback_reply or low_clock_reply, "premove-analysis-deadline-exceeded"
        if fallback_move is not None:
            return fallback_move, fallback_reply, fallback_reason
        return None, fallback_reply or low_clock_reply, f"{low_clock_reason}; {fallback_reason}"

    mate_move, mate_reply, mate_reason = choose_mate_phase_premove(
        engine, board, bot_color, args
    )
    if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
        return None, mate_reply, "premove-analysis-deadline-exceeded"
    if mate_move is not None:
        return mate_move, mate_reply, mate_reason

    allowed, phase = premove_phase_allowed(
        board,
        getattr(args, "opening_premove_fullmove_max", PREMOVE_OPENING_FULLMOVE_MAX),
    )
    if not allowed:
        middle_move, middle_reply, middle_reason = choose_middle_game_premove(
            engine, board, strategy_module, bot_color, args
        )
        if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
            return None, middle_reply, "premove-analysis-deadline-exceeded"
        return middle_move, middle_reply, middle_reason

    search_time = max(0.010, min(float(args.engine_time), 0.050))
    try:
        # Preserve the previous opening/endgame policy, with its existing
        # loss/evaluation-drop checks.
        opponent_lines = engine.analyse(
            board, chess.engine.Limit(depth=8, time=search_time), multipv=3
        )
        if not isinstance(opponent_lines, list):
            opponent_lines = [opponent_lines]
        opponent_lines = [
            info for info in opponent_lines
            if isinstance(info, dict) and info.get("pv")
            and info["pv"][0] in board.legal_moves
        ]
        if not opponent_lines:
            return None, None, "opponent-reply-not-found"
        phase_min_eval = (
            PREMOVE_OPENING_MIN_BOT_EVAL_CP
            if phase == "opening"
            else PREMOVE_MIN_BOT_EVAL_CP
        )
        phase_max_eval_drop = (
            PREMOVE_OPENING_MAX_EVAL_DROP_CP
            if phase == "opening"
            else PREMOVE_MAX_EVAL_DROP_CP
        )

        current_eval = _premove_score_for_bot(opponent_lines[0], bot_color)
        if current_eval is None:
            return None, None, "current-evaluation-unavailable"
        if current_eval < phase_min_eval:
            return None, None, f"{phase}-position-too-losing-for-premove"

        predicted_reply = opponent_lines[0]["pv"][0]
        hypothetical = board.copy(stack=True)
        hypothetical.push(predicted_reply)
        if hypothetical.turn != bot_turn:
            return None, predicted_reply, "hypothetical-turn-mismatch"

        own_lines = engine.analyse(
            hypothetical,
            chess.engine.Limit(depth=10, time=search_time),
            multipv=min(12, max(1, len(hypothetical.legal_moves))),
        )
        if not isinstance(own_lines, list):
            own_lines = [own_lines]
        own_lines = [
            info for info in own_lines
            if isinstance(info, dict) and info.get("pv")
            and info["pv"][0] in hypothetical.legal_moves
        ]
        if not own_lines:
            return None, predicted_reply, "premove-candidate-not-found"

        best_after_reply_eval = _premove_score_for_bot(own_lines[0], bot_color)
        if best_after_reply_eval is None:
            return None, predicted_reply, "reply-evaluation-unavailable"
        if best_after_reply_eval < phase_min_eval:
            return None, predicted_reply, f"{phase}-reply-too-losing-for-premove"

        # Do not call the opening book here: premove forecasting must not
        # consume/change the normal turn's existing randomized book choice.
        # Try candidate lines in Stockfish order so a poor first premove does
        # not prevent a later safe opening continuation from being queued.
        cursor_input = getattr(args, "move_input", "api") == "cursor"
        safe_candidates = []
        # Check a manageable top subset with a slightly longer, more stable search.
        for candidate_info in own_lines[:8]:
            if (
                analysis_deadline is not None
                and time.monotonic() >= float(analysis_deadline) - 0.012
            ):
                return None, predicted_reply, "premove-analysis-deadline-exceeded"
            selected = candidate_info["pv"][0]
            if selected.promotion is not None and cursor_input:
                continue
            # A bounded eval drop alone can miss a one-move hanging piece.
            if strategy_module.candidate_hangs_own_piece(hypothetical, selected):
                continue

            after_selected = hypothetical.copy(stack=True)
            after_selected.push(selected)
            final_info = engine.analyse(
                after_selected,
                chess.engine.Limit(depth=8, time=search_time),
                multipv=1,
            )
            if isinstance(final_info, list):
                final_info = final_info[0] if final_info else None
            selected_eval = _premove_score_for_bot(final_info, bot_color)
            if selected_eval is None:
                continue
            if selected_eval < phase_min_eval:
                continue
            if best_after_reply_eval - selected_eval > phase_max_eval_drop:
                continue

            safe_candidates.append((selected, selected_eval))

        if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
            return None, predicted_reply, "premove-analysis-deadline-exceeded"
        if not safe_candidates:
            return None, predicted_reply, f"{phase}-no-candidate-passed-evaluation-guard"

        # Each item passed legality, hanging-piece, and evaluation guards.
        # Weighted random choice avoids always queuing the first MultiPV line.
        best_safe_eval = max(score for _move, score in safe_candidates)
        candidate_weights = [
            max(0.15, 1.0 - (best_safe_eval - score) / 150.0)
            for _move, score in safe_candidates
        ]
        selected, selected_eval = random.choices(
            safe_candidates, weights=candidate_weights, k=1
        )[0]
        return (
            selected, predicted_reply,
            f"{phase}-safe-eval={selected_eval / 100:+.2f}; "
            f"random-safe-pool={len(safe_candidates)}",
        )
    except (chess.engine.EngineError, ValueError, RuntimeError, TypeError):
        if analysis_deadline is not None and time.monotonic() >= float(analysis_deadline):
            return None, None, "premove-analysis-deadline-exceeded"
        return None, None, "premove-analysis-failed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach to Chrome and read live chess DOM/API state; output hides site identifiers."
    )
    parser.add_argument("--debugger-address", default="127.0.0.1:9223")
    parser.add_argument(
        "--browser-mode", choices=("attach", "uc"), default=None,
        help="Browser startup mode: attach to the existing debugging Chrome session by default; use uc only to launch a separate browser.",
    )
    parser.add_argument(
        "--allowed-domain", default="",
        help="Optional override; otherwise load it from a neighbouring TXT settings file.",
    )
    parser.add_argument("--board-selector", default="chess-board")
    parser.add_argument("--fen-selector", default="")
    parser.add_argument("--fen-attribute", default="data-fen")
    parser.add_argument("--moves-selector", default="[data-ply], .white.node, .black.node")
    parser.add_argument("--ply-attribute", default="data-ply")
    parser.add_argument("--time-attribute", default="data-time")  # kept for CLI compatibility
    parser.add_argument("--move-list-selector", default="vertical-move-list, wc-simple-move-list")
    parser.add_argument("--white-clock-selector", default=".clock-component.clock-white")
    parser.add_argument("--black-clock-selector", default=".clock-component.clock-black")
    parser.add_argument("--result-selector", default="#game-result, [data-result]")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--queue-read-interval", type=float, default=0.2)
    auto_group = parser.add_mutually_exclusive_group()
    auto_group.add_argument(
        "--auto-play", dest="auto_play", action="store_true",
        help="Enable Stockfish autoplay on the exact configured authorized site (public chess hosts are blocked).",
    )
    auto_group.add_argument(
        "--no-auto-play", dest="auto_play", action="store_false",
        help="Disable autoplay even if auto_play=true is present in TXT settings.",
    )
    parser.set_defaults(auto_play=None)
    parser.add_argument("--stockfish-path", default="", help="Stockfish executable path; can also be set as stockfish_path=... in TXT settings.")
    parser.add_argument("--engine-time", type=float, default=0.75, help="Maximum Stockfish search time per move in seconds; effective time is adapted to the remaining clock.")
    parser.add_argument("--move-input", choices=("api", "cursor"), default=None, help="Move submission method; can also be set with move_input=api|cursor in tracker_settings.txt.")
    premove_group = parser.add_mutually_exclusive_group()
    premove_group.add_argument("--premove", dest="premove_enabled", action="store_true", help="Try best-effort premoves on the configured authorized site.")
    premove_group.add_argument("--no-premove", dest="premove_enabled", action="store_false", help="Disable premove attempts.")
    parser.set_defaults(premove_enabled=None)
    next_game_group = parser.add_mutually_exclusive_group()
    next_game_group.add_argument(
        "--auto-next-game", dest="auto_next_game", action="store_true",
        help="On the configured authorized test site, click a New-game button 2-5 seconds after game over.",
    )
    next_game_group.add_argument(
        "--no-auto-next-game", dest="auto_next_game", action="store_false",
        help="Disable automatic next-game clicks.",
    )
    parser.set_defaults(auto_next_game=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        blocked_platforms = load_blocked_platforms()
    except RuntimeError:
        print("[CONFIG ERROR] Blocked-platform TXT file is missing or unreadable.", file=sys.stderr, flush=True)
        print("Place blocked_platforms.txt beside this script and retry.", file=sys.stderr, flush=True)
        return 2
    print(f"[CONFIG] Blocked platform list loaded ({len(blocked_platforms)} entries).", flush=True)

    if args.browser_mode is None:
        args.browser_mode = "attach"
    if args.browser_mode not in {"attach", "uc"}:
        print("[CONFIG ERROR] browser_mode must be attach or uc.", file=sys.stderr, flush=True)
        return 2
    print(f"[CONFIG] Browser mode: {args.browser_mode}.", flush=True)

    if args.auto_play is None:
        auto_setting = load_optional_txt_setting("auto_play").strip().lower()
        args.auto_play = auto_setting in {"1", "true", "yes", "on", "enabled"}
    if args.move_input is None:
        args.move_input = load_optional_txt_setting("move_input").strip().lower() or "api"
    if args.move_input not in {"api", "cursor"}:
        print("[CONFIG ERROR] move_input must be api or cursor.", file=sys.stderr, flush=True)
        return 2
    if args.premove_enabled is None:
        raw_premove = load_optional_txt_setting("premove_enabled").strip().lower()
        args.premove_enabled = raw_premove in {"1", "true", "yes", "on", "enabled"} if raw_premove else True
    print(f"[CONFIG] Premove attempts: {'enabled' if args.premove_enabled else 'disabled'}.", flush=True)
    if args.auto_next_game is None:
        next_setting = load_optional_txt_setting("auto_next_game").strip().lower()
        args.auto_next_game = (
            next_setting in {"1", "true", "yes", "on", "enabled"}
            if next_setting else True
        )
    print(f"[CONFIG] Automatic new match: {'enabled' if args.auto_next_game else 'disabled'}.", flush=True)
    raw_rematch_setting = load_optional_txt_setting("auto_rematch").strip().lower()
    args.auto_rematch = raw_rematch_setting in {"1", "true", "yes", "on", "enabled"}
    raw_extra_premove_setting = load_optional_txt_setting("low_clock_extra_premoves").strip().lower()
    args.low_clock_extra_premoves = (
        raw_extra_premove_setting in {"1", "true", "yes", "on", "enabled"}
        if raw_extra_premove_setting else True
    )
    raw_clock_management_setting = load_optional_txt_setting("clock_management").strip().lower()
    args.clock_management = (
        raw_clock_management_setting in {"1", "true", "yes", "on", "enabled"}
        if raw_clock_management_setting else True
    )
    print(f"[CONFIG] Automatic rematch: {'enabled' if args.auto_rematch else 'disabled'}.", flush=True)
    print(
        f"[CONFIG] Low-clock extra premoves: {'enabled' if args.low_clock_extra_premoves else 'disabled'} "
        "for the extra same-position retry in the sampled 7–10s window.",
        flush=True,
    )
    print(f"[CONFIG] Clock-based time management: {'enabled' if args.clock_management else 'disabled'}.", flush=True)
    if args.allowed_domain.strip():
        allowed_host = normalize_host(args.allowed_domain)
        settings_source = "command-line settings"
    else:
        allowed_host, source_name = load_allowed_host_from_txt()
        settings_source = f"{source_name} TXT file" if source_name else "TXT settings"
    if not allowed_host:
        print("[CONFIG ERROR] Site settings could not be loaded from TXT.", file=sys.stderr)
        print("Add site_domain=your-hostname to tracker_settings.txt.", file=sys.stderr)
        print("The configured hostname is intentionally hidden in terminal output.", file=sys.stderr)
        return 2
    configured_player_name = load_optional_txt_setting("player_name").strip()
    configured_bot_color_raw = load_optional_txt_setting("bot_color")
    configured_bot_color = normalize_chess_color(configured_bot_color_raw)
    if args.auto_play and configured_bot_color_raw and configured_bot_color is None:
        print("[CONFIG ERROR] bot_color must be exactly white or black.", file=sys.stderr, flush=True)
        return 2

    # The configured username identifies the player's side per game. A fixed
    # bot_color must not override this when the player alternates between White/Black.
    if configured_player_name and configured_bot_color:
        print(
            "[CONFIG] player_name is set; ignoring fixed bot_color so the player's "
            "side can be detected independently for each game.",
            flush=True,
        )
        configured_bot_color = None

    if args.auto_play and configured_bot_color:
        print(f"[CONFIG] Fallback bot color loaded: {configured_bot_color}.", flush=True)
    if args.auto_play and configured_player_name:
        print("[CONFIG] Player-name side detection enabled; configured name is hidden.", flush=True)
    print(f"[CONFIG] Settings loaded from the {settings_source}.", flush=True)

    # Attach mode is unchanged; UC mode launches a dedicated SeleniumBase-managed browser.
    driver = None
    if args.browser_mode == "uc":
        if SeleniumBaseDriver is None:
            print(
                "[CONFIG ERROR] SeleniumBase is missing. Install dependencies with: "
                "python -m pip install -r requirements.txt",
                file=sys.stderr, flush=True,
            )
            return 2
        try:
            start_url = configured_uc_start_url(allowed_host)
        except RuntimeError as exc:
            print(f"[CONFIG ERROR] {exc}", file=sys.stderr, flush=True)
            return 2
        print("[CONNECT] Launching SeleniumBase UC Mode for the configured authorized site...", flush=True)
        try:
            driver = SeleniumBaseDriver(uc=True)
            if hasattr(driver, "uc_open_with_reconnect"):
                driver.uc_open_with_reconnect(start_url, reconnect_time=2)
            else:
                driver.get(start_url)
        except Exception:
            try:
                if driver is not None:
                    driver.quit()
            except Exception:
                pass
            print(
                "[CONNECT ERROR] SeleniumBase UC Mode could not launch the browser. "
                "Update Chrome and install the project requirements.",
                file=sys.stderr, flush=True,
            )
            return 2
        print("[CONNECT] SeleniumBase UC browser launched.", flush=True)
        print(
            "[UC] Navigate to your authorized test-game page and sign in if needed. "
            "If verification appears, complete it manually in the browser.",
            flush=True,
        )
        try:
            input("[UC] Press Enter when the correct test page is ready: ")
            wait_for_manual_verification(driver, allowed_host)
        except (RuntimeError, WebDriverException):
            try:
                driver.quit()
            except Exception:
                pass
            print("[UC] Site verification/setup did not complete; stopping safely.", file=sys.stderr, flush=True)
            return 2
    else:
        # Don't echo the local debugger address or any page URL.
        print("[CONNECT] Checking the local Chrome DevTools endpoint...", flush=True)
        debugger_url = f"http://{args.debugger_address}/json/version"
        try:
            with urlopen(debugger_url, timeout=3) as response:
                debugger_info = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:
            print("[CONNECT ERROR] Chrome DevTools is not responding on the configured local port.", file=sys.stderr, flush=True)
            print("Launch the separate debugging Chrome window, open your test page, then retry.", file=sys.stderr, flush=True)
            return 2
        browser_version = str(debugger_info.get("Browser", "Chrome"))
        print(f"[CONNECT] DevTools is online ({browser_version}).", flush=True)

        options = webdriver.ChromeOptions()
        options.debugger_address = args.debugger_address
        print("[CONNECT] Attaching Selenium to the existing Chrome session...", flush=True)
        try:
            driver = webdriver.Chrome(options=options)
        except WebDriverException:
            print("[CONNECT ERROR] Selenium could not attach to Chrome. Update Selenium and check ChromeDriver compatibility.", file=sys.stderr, flush=True)
            return 2
        print("[CONNECT] Selenium attached.", flush=True)

    cfg = {
        "boardSelector": args.board_selector,
        "fenSelector": args.fen_selector,
        "fenAttribute": args.fen_attribute,
        "movesSelector": args.moves_selector,
        "plyAttribute": args.ply_attribute,
        "timeAttribute": args.time_attribute,
        "moveListSelector": args.move_list_selector,
        "whiteClockSelector": (
            load_optional_txt_setting("white_clock_selector") or args.white_clock_selector
        ),
        "blackClockSelector": (
            load_optional_txt_setting("black_clock_selector") or args.black_clock_selector
        ),
        "resultSelector": args.result_selector,
        "playerName": configured_player_name,
        "whitePlayerSelector": (
            load_optional_txt_setting("white_player_selector")
            or ".white.player, .player.white, [data-color='white'], [data-side='white'], "
               "[data-player-color='white'], #white-player, .white-player"
        ),
        "blackPlayerSelector": (
            load_optional_txt_setting("black_player_selector")
            or ".black.player, .player.black, [data-color='black'], [data-side='black'], "
               "[data-player-color='black'], #black-player, .black-player"
        ),
    }

    chosen_handle = None
    engine = None
    book = None
    strategy_module = None
    last_auto_attempt_fen = ""
    move_retry_gate = MoveAttemptRetryGate()
    last_premove_attempt_fen = ""
    last_low_clock_premove_fen = ""
    last_premove_gate_report_fen = ""
    strategy_board = None
    strategy_board_fen = ""
    active_bot_color = configured_bot_color
    side_resolver = PlayerSideResolver(
        configured_bot_color,
        player_name_enabled=bool(configured_player_name),
    )
    last_side_warning_fen = ""
    previous_eval_white_cp = None
    next_opponent_best_cp = None
    pending_opponent_best_cp = None
    pending_opponent_color = None
    opponent_match_history: list[float] = []
    opponent_pressure = False
    # If the site exposes the clock from game start, estimate the base time
    # from its first valid reading. Optional TXT keys can make this explicit:
    # time_control_seconds=60 and clock_increment_seconds=0.
    configured_time_control = load_optional_txt_setting("time_control_seconds")
    try:
        configured_time_control_seconds = (
            max(0.0, float(configured_time_control))
            if configured_time_control else None
        )
    except ValueError:
        configured_time_control_seconds = None
    # Detect each match from its live start clocks. The TXT value is a fallback
    # only when attaching to a game already in progress.
    initial_game_clock_seconds = None
    raw_increment_setting = load_optional_txt_setting("clock_increment_seconds").strip()
    raw_increment_fixed = load_optional_txt_setting("clock_increment_fixed").strip().lower()
    clock_increment_is_configured = raw_increment_fixed in {"1", "true", "yes", "on", "enabled"}
    try:
        clock_increment_seconds = max(0.0, float(raw_increment_setting or 0.0))
    except ValueError:
        clock_increment_seconds = 0.0
    # Increment is auto-detected by default so one TXT value cannot bias 3+2,
    # 1+1, 1-minute, and 10-second games alike. Set clock_increment_fixed=true
    # to deliberately pin clock_increment_seconds.
    if not clock_increment_is_configured:
        clock_increment_seconds = 0.0
    clock_increment_samples = []
    last_seen_clock_by_side = {"white": None, "black": None}
    clock_source_reported = False
    low_clock_trigger_seconds = random.uniform(7.0, 10.0)
    # Sample one opening premove window per game, then hold it steady until next game.
    args.opening_premove_fullmove_max = random.randint(
        PREMOVE_OPENING_FULLMOVE_MIN, PREMOVE_OPENING_FULLMOVE_MAX
    )
    print(
        f"[PREMOVE] Opening premove window sampled: up to "
        f"{args.opening_premove_fullmove_max} full moves for this game.",
        flush=True,
    )
    short_control_fast_mode = False
    print(
        f"[LOW CLOCK] Random late-clock threshold for this game: <= {low_clock_trigger_seconds:.2f}s.",
        flush=True,
    )
    try:
        handles = driver.window_handles
        candidates: list[tuple[int, str]] = []
        print("[TABS] Browser tabs (site details hidden):", flush=True)
        for i, handle in enumerate(handles):
            try:
                driver.switch_to.window(handle)
                url = driver.current_url
            except WebDriverException:
                print(f"  {i}: unavailable", flush=True)
                continue
            matched = target_matches(url, allowed_host)
            print(f"  {i}: {'matches saved settings' if matched else 'not a matching tab'}", flush=True)
            if matched:
                candidates.append((i, handle))

        if not candidates:
            raise RuntimeError("No eligible tab matches the saved settings. Check that the authorized test site is open in the debugging Chrome window.")

        allowed_indexes = {i for i, _ in candidates}
        print("[TABS] Select the matching test-game tab number:", flush=True)
        while True:
            raw = input("> ").strip()
            if raw.isdigit() and int(raw) in allowed_indexes:
                chosen_handle = next(h for i, h in candidates if i == int(raw))
                break
            print(f"Choose one of: {sorted(allowed_indexes)}", flush=True)

        driver.switch_to.window(chosen_handle)
        check_target(driver.current_url, allowed_host)
        installed = driver.execute_script(JS_INSTALL_OBSERVER, cfg)
        next_state = driver.execute_script(
            JS_INSTALL_AUTO_NEXT_GAME,
            {
                **cfg,
                "autoNextGame": args.auto_next_game,
                "autoRematch": args.auto_rematch,
                "allowedHost": allowed_host,
            },
        )
        if not isinstance(next_state, dict) or not next_state.get("installed"):
            raise RuntimeError("Could not install the game-action handler.")
        print("[AUTO GAME] Live action handler installed; New Match and Rematch can be toggled in the settings window.", flush=True)
        print("[CONNECTED] Selected browser tab.", flush=True)
        print(
            f"[BOARD] found={installed.get('boardFound')} | "
            f"game API={installed.get('gameApiFound')} | "
            f"turn={installed.get('turn')} | playingAs={installed.get('playingAs')} | "
            f"bottomSide={installed.get('bottomSide', 'unknown')} | "
            f"isMyTurn={installed.get('isMyTurn')} | "
            f"legalMoves={installed.get('legalMoveCount', 0)}",
            flush=True,
        )
        api_methods = installed.get("apiMethods") or []
        if api_methods:
            print("[API] Available game API methods: " + ", ".join(api_methods), flush=True)
        print("[FEN]", installed.get("fen") or "not exposed through game API/configured attribute", flush=True)
        if not installed.get("boardFound"):
            print("[NOTE] Board selector did not match; adjust --board-selector.", flush=True)
        elif not installed.get("gameApiFound"):
            print("[NOTE] Board found, but the game API methods are not accessible.", flush=True)

        if args.auto_play:
            engine, book, strategy_module = prepare_bot_components(args, allowed_host, installed)

        csv_path = Path(args.csv).expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not csv_path.exists() or csv_path.stat().st_size == 0
        previous_fen = ""
        last_dashboard_eval_fen = ""
        reconstructed_history: list[dict[str, str]] = []
        game_number = 1
        if chess is None:
            print("[NOTE] Full SAN move reconstruction needs python-chess: python -m pip install python-chess", flush=True)

        with csv_path.open("a", newline="", encoding="utf-8-sig") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            print("[MODE] Live state reader active (DOM observer + 200ms state fallback); Ctrl+C to stop.", flush=True)
            last_console_signature = None
            last_name_debug_signature = None
            last_clock_signature = None
            last_fast_polling_state = None
            low_clock_polling_active = False
            settings_window = None
            try:
                settings_window = RuntimeSettingsWindow(
                    args, driver, allowed_host, cfg, chosen_handle
                )
                print("[GUI] Live settings window opened. Switches apply without restarting.", flush=True)
            except Exception:
                print("[GUI] Runtime settings window could not be opened; continuing with TXT/CLI settings.", flush=True)

            while True:
                if settings_window is not None:
                    settings_window.pump()
                    if settings_window.stop_requested:
                        print("[STOP] Stop requested from runtime settings window.", flush=True)
                        break
                # If Bot was OFF at startup, initialize the engine only when it is switched ON.
                if args.auto_play and engine is None:
                    try:
                        if chosen_handle not in driver.window_handles:
                            raise RuntimeError("Selected browser tab is unavailable.")
                        driver.switch_to.window(chosen_handle)
                        check_target(driver.current_url, allowed_host)
                        current_install = driver.execute_script(
                            "return window.__liveGameApiReader ? window.__liveGameApiReader.current() : {};"
                        ) or {}
                        engine, book, strategy_module = prepare_bot_components(
                            args, allowed_host,
                            {
                                "boardFound": bool(current_install.get("boardFound")),
                                "gameApiFound": bool(current_install.get("gameApiFound")),
                            },
                        )
                    except (RuntimeError, WebDriverException):
                        print("[AUTO] Bot could not be enabled; check Stockfish, strategy files, and the allowed-site board API.", flush=True)
                        args.auto_play = False
                        if settings_window is not None:
                            settings_window.set_enabled("auto_play", False)
                # Closing the chosen tab often yields current_url=None in ChromeDriver.
                if chosen_handle not in driver.window_handles:
                    print("[STOP] Selected browser tab was closed. No URL was logged.", flush=True)
                    break
                try:
                    current_url = driver.current_url
                except WebDriverException:
                    print("[STOP] Browser tab is unavailable or DevTools disconnected. No URL was logged.", flush=True)
                    break
                if not current_url:
                    print("[STOP] Selected browser tab no longer has a web page. No URL was logged.", flush=True)
                    break
                if not target_matches(current_url, allowed_host):
                    print("[STOP] Selected tab left the configured site. Site details were not logged.", flush=True)
                    break
                try:
                    events = driver.execute_script(
                        "return window.__liveGameApiReader ? window.__liveGameApiReader.drain() : [];"
                    )
                except WebDriverException:
                    print("[STOP] Browser tab closed or DevTools disconnected. No URL was logged.", flush=True)
                    break

                # If the queue is quiet, re-read the authoritative state when a retry is due.
                if (
                    not events
                    and move_retry_gate.fen
                    and move_retry_gate.should_attempt(move_retry_gate.fen)
                ):
                    try:
                        retry_snapshot = driver.execute_script(
                            "return window.__liveGameApiReader ? "
                            "window.__liveGameApiReader.current() : null;"
                        )
                    except WebDriverException:
                        retry_snapshot = None
                    if (
                        isinstance(retry_snapshot, dict)
                        and str(retry_snapshot.get("fen") or "").strip() == move_retry_gate.fen
                    ):
                        events = [retry_snapshot]

                for event in events or []:
                    if settings_window is not None:
                        settings_window.pump()
                        if settings_window.stop_requested:
                            break
                    fen = str(event.get("fen") or "").strip()
                    dom_moves = event.get("moves") or []
                    api_bot_color, api_color_conflict = normalize_api_bot_color(
                        event.get("playingAsColor"), event.get("playingAs")
                    )
                    player_name_color = normalize_chess_color(event.get("playerNameColor"))
                    active_bot_color = side_resolver.resolve(api_bot_color, player_name_color)
                    white_clock_seconds = parse_clock_seconds(event.get("whiteClock"))
                    black_clock_seconds = parse_clock_seconds(event.get("blackClock"))
                    if active_bot_color == "white":
                        own_clock_for_poll = white_clock_seconds
                    elif active_bot_color == "black":
                        own_clock_for_poll = black_clock_seconds
                    else:
                        own_clock_for_poll = None
                    low_clock_polling_active = (
                        own_clock_for_poll is not None
                        and is_low_clock_mode(
                            own_clock_for_poll, low_clock_trigger_seconds, short_control_fast_mode
                        )
                        and (args.clock_management or args.premove_enabled)
                    )
                    if low_clock_polling_active != last_fast_polling_state:
                        try:
                            poll_state = driver.execute_script(
                                "return window.__liveGameApiReader ? "
                                "window.__liveGameApiReader.setFastPolling(arguments[0]) : null;",
                                low_clock_polling_active,
                            )
                            if isinstance(poll_state, dict):
                                print(
                                    f"[POLLING] {'fast' if low_clock_polling_active else 'normal'} "
                                    f"clock-state polling ({poll_state.get('intervalMs', '?')} ms).",
                                    flush=True,
                                )
                        except WebDriverException:
                            pass
                        last_fast_polling_state = low_clock_polling_active
                    clock_signature = (
                        str(event.get("whiteClock") or ""),
                        str(event.get("blackClock") or ""),
                        white_clock_seconds,
                        black_clock_seconds,
                    )
                    if clock_signature != last_clock_signature:
                        last_clock_signature = clock_signature
                        if active_bot_color == "white":
                            own_clock_for_log = white_clock_seconds
                        elif active_bot_color == "black":
                            own_clock_for_log = black_clock_seconds
                        else:
                            own_clock_for_log = None
                        fmt_clock = lambda value: "unreadable" if value is None else f"{value:.2f}s"
                        print(
                            f"[CLOCK READ] white raw={clock_signature[0]!r} -> {fmt_clock(white_clock_seconds)} | "
                            f"black raw={clock_signature[1]!r} -> {fmt_clock(black_clock_seconds)} | "
                            f"own={fmt_clock(own_clock_for_log)}",
                            flush=True,
                        )
                    observed_clocks = [
                        value for value in (white_clock_seconds, black_clock_seconds)
                        if value is not None
                    ]
                    is_new_game_snapshot = bool(
                        fen and previous_fen and fen != previous_fen
                        and is_start_position(fen)
                        and not is_start_position(previous_fen)
                    )
                    is_initial_start_snapshot = bool(
                        fen and not previous_fen and is_start_position(fen)
                    )
                    if is_new_game_snapshot or is_initial_start_snapshot:
                        last_seen_clock_by_side = {"white": None, "black": None}
                        clock_increment_samples = []
                        short_control_fast_mode = False
                        if not clock_increment_is_configured:
                            clock_increment_seconds = 0.0
                    if is_new_game_snapshot:
                        low_clock_trigger_seconds = random.uniform(7.0, 10.0)
                        args.opening_premove_fullmove_max = random.randint(
                            PREMOVE_OPENING_FULLMOVE_MIN, PREMOVE_OPENING_FULLMOVE_MAX
                        )
                        print(
                            f"[PREMOVE] New game opening window sampled: up to "
                            f"{args.opening_premove_fullmove_max} full moves.",
                            flush=True,
                        )
                        print(
                            f"[LOW CLOCK] New game threshold sampled: <= {low_clock_trigger_seconds:.2f}s.",
                            flush=True,
                        )
                        initial_game_clock_seconds = None
                        clock_source_reported = False
                    if (
                        not clock_increment_is_configured
                        and not (is_new_game_snapshot or is_initial_start_snapshot)
                        and fen and previous_fen and fen != previous_fen
                    ):
                        moved_side = None
                        try:
                            previous_turn = previous_fen.split()[1]
                            moved_side = "white" if previous_turn == "w" else "black" if previous_turn == "b" else None
                        except (IndexError, AttributeError):
                            moved_side = None
                        for side_name, current_clock in (
                            ("white", white_clock_seconds),
                            ("black", black_clock_seconds),
                        ):
                            previous_clock = last_seen_clock_by_side.get(side_name)
                            if (
                                side_name == moved_side
                                and previous_clock is not None
                                and current_clock is not None
                            ):
                                clock_gain = float(current_clock) - float(previous_clock)
                                if 0.35 <= clock_gain <= 10.0:
                                    clock_increment_samples.append(clock_gain)
                                    clock_increment_samples = clock_increment_samples[-9:]
                                    ordered_samples = sorted(clock_increment_samples)
                                    middle = len(ordered_samples) // 2
                                    if len(ordered_samples) % 2:
                                        clock_increment_seconds = ordered_samples[middle]
                                    else:
                                        clock_increment_seconds = (
                                            ordered_samples[middle - 1] + ordered_samples[middle]
                                        ) / 2.0
                            if current_clock is not None:
                                last_seen_clock_by_side[side_name] = float(current_clock)
                    if initial_game_clock_seconds is None and observed_clocks:
                        if is_start_position(fen):
                            initial_game_clock_seconds = max(observed_clocks)
                        elif configured_time_control_seconds is not None:
                            # Only trust a user-specified base time when attaching mid-game.
                            initial_game_clock_seconds = configured_time_control_seconds
                    # In a 10-second control, fast mode must start at the first
                    # move instead of waiting for a random 7-10s threshold.
                    if initial_game_clock_seconds is not None and not clock_source_reported:
                        short_control_fast_mode = initial_game_clock_seconds <= 10.0
                        if short_control_fast_mode:
                            low_clock_trigger_seconds = 10.0
                            print(
                                "[CLOCK MODE] Short time control detected (<=10s): "
                                "fast move handling and extra-premove attempts enabled from the start.",
                                flush=True,
                            )
                    if not is_new_game_snapshot:
                        for side_name, current_clock in (
                            ("white", white_clock_seconds),
                            ("black", black_clock_seconds),
                        ):
                            if current_clock is not None and last_seen_clock_by_side.get(side_name) is None:
                                last_seen_clock_by_side[side_name] = float(current_clock)
                    if settings_window is not None:
                        settings_window.update_clock_status(
                            white_clock_seconds,
                            black_clock_seconds,
                            initial_game_clock_seconds,
                            clock_increment_seconds,
                            low_clock_trigger_seconds,
                            bot_color=active_bot_color,
                        )
                    if observed_clocks and not clock_source_reported:
                        if initial_game_clock_seconds is None:
                            print(
                                "[CLOCK] Base time=unknown (attached mid-game; set time_control_seconds in TXT "
                                "to enable a reliable clock profile); "
                                f"increment={clock_increment_seconds:.2f}s.",
                                flush=True,
                            )
                        else:
                            source_note = (
                                "configured fallback (game already in progress)"
                                if configured_time_control_seconds is not None and not is_start_position(fen)
                                else "start-position clock reading"
                            )
                            print(
                                f"[CLOCK] Base time={initial_game_clock_seconds:.2f}s via {source_note}; "
                                f"increment={clock_increment_seconds:.2f}s.",
                                flush=True,
                            )
                        clock_source_reported = True
                    last_move: dict[str, str] | None = None
                    note = "DOM/API state change; identifiers omitted"

                    if fen and fen != previous_fen:
                        if previous_fen:
                            last_move = infer_move_between_fens(previous_fen, fen)
                            if last_move:
                                try:
                                    last_move["game_ply"] = str(chess.Board(fen).ply())
                                except (ValueError, IndexError):
                                    last_move["game_ply"] = str(len(reconstructed_history) + 1)
                                reconstructed_history.append(last_move)
                                if settings_window is not None:
                                    try:
                                        observed_ply = (
                                            chess.Board(fen).ply()
                                            if chess is not None and fen
                                            else len(reconstructed_history)
                                        )
                                        settings_window.record_observed_move(
                                            game_number,
                                            observed_ply,
                                            last_move,
                                            eval_white_cp=settings_window.last_eval_white_cp,
                                        )
                                    except Exception:
                                        pass
                                note = "Move reconstructed from consecutive FEN states"
                            elif is_start_position(fen) and not is_start_position(previous_fen):
                                game_number += 1
                                reconstructed_history = []
                                note = "New game detected; move history reset"
                            elif chess is not None:
                                note = "Position changed but transition could not be inferred as one legal move"
                        previous_fen = fen

                    # Repair skipped FEN transitions when the page exposes a complete
                    # SAN move list. A history is accepted only if replaying every move
                    # from the initial position exactly matches the authoritative FEN.
                    if fen and dom_moves and chess is not None:
                        dom_history = reconstruct_history_from_dom_moves(dom_moves, fen)
                        if dom_history and dom_history != reconstructed_history:
                            reconstructed_history = dom_history
                            last_move = dict(dom_history[-1])
                            note = "Move history reconciled from legal DOM SAN list"
                            print(
                                f"[HISTORY SYNC] Reconciled {len(dom_history)} plies against current FEN.",
                                flush=True,
                            )
                            if settings_window is not None:
                                for history_item in dom_history:
                                    try:
                                        settings_window.record_observed_move(
                                            game_number,
                                            int(history_item["game_ply"]),
                                            history_item,
                                            eval_white_cp=settings_window.last_eval_white_cp,
                                        )
                                    except Exception:
                                        pass

                    # Keep a persistent python-chess board so the ChessKida strategy
                    # sees the same move_stack/history state as the original bot.
                    if chess is not None and fen:
                        try:
                            if strategy_board is None:
                                strategy_board = chess.Board(fen)
                                strategy_board_fen = fen
                            elif fen != strategy_board_fen:
                                strategy_transition = infer_move_between_fens(strategy_board_fen, fen)
                                was_start = is_start_position(strategy_board_fen)
                                is_new_start = is_start_position(fen) and not was_start

                                if is_new_start:
                                    strategy_board = chess.Board(fen)
                                    opponent_match_history.clear()
                                    next_opponent_best_cp = None
                                    pending_opponent_best_cp = None
                                    pending_opponent_color = None
                                    previous_eval_white_cp = None
                                    opponent_pressure = False
                                    last_auto_attempt_fen = ""
                                    last_premove_attempt_fen = ""
                                    last_low_clock_premove_fen = ""
                                    last_premove_gate_report_fen = ""
                                    side_resolver.reset_for_new_game()
                                    active_bot_color = side_resolver.resolve(api_bot_color, player_name_color)
                                elif strategy_transition is not None:
                                    strategy_move = chess.Move.from_uci(strategy_transition["uci"])
                                    if strategy_move in strategy_board.legal_moves:
                                        strategy_board.push(strategy_move)
                                    else:
                                        strategy_board = chess.Board(fen)
                                        opponent_match_history.clear()
                                        next_opponent_best_cp = None
                                        pending_opponent_best_cp = None
                                        pending_opponent_color = None
                                        previous_eval_white_cp = None
                                        opponent_pressure = False
                                else:
                                    # Recover synchronization if a page skipped a FEN state.
                                    strategy_board = chess.Board(fen)
                                    opponent_match_history.clear()
                                    next_opponent_best_cp = None
                                    pending_opponent_best_cp = None
                                    pending_opponent_color = None
                                    previous_eval_white_cp = None
                                    opponent_pressure = False

                                strategy_board_fen = fen

                                # Hold the expected score for the opponent's reply.
                                # Quality is calculated after the resulting position is evaluated;
                                # an equally good alternate move is not scored as zero.
                                bot_side_for_score = active_bot_color or api_bot_color
                                if (
                                    strategy_transition is not None
                                    and bot_side_for_score in {"white", "black"}
                                    and strategy_transition.get("side") != bot_side_for_score
                                    and next_opponent_best_cp is not None
                                ):
                                    pending_opponent_best_cp = float(next_opponent_best_cp)
                                    pending_opponent_color = strategy_transition.get("side")
                                    next_opponent_best_cp = None
                        except (ValueError, IndexError):
                            strategy_board = None
                            strategy_board_fen = ""

                    if settings_window is not None and chess is not None and fen:
                        try:
                            dashboard_board = chess.Board(fen)
                            settings_window.update_game_status(
                                game_number,
                                dashboard_board,
                                bot_color=active_bot_color,
                                game_result=event.get("result"),
                            )

                            # Refresh the advantage bar after an opponent position
                            # change too, without requesting another 100-line MultiPV.
                            # The quick one-line probe is skipped in very low-clock
                            # situations so the next move remains responsive.
                            bot_color_value = (
                                chess.WHITE if active_bot_color == "white"
                                else chess.BLACK if active_bot_color == "black"
                                else None
                            )
                            bot_to_move = (
                                args.auto_play
                                and bot_color_value is not None
                                and dashboard_board.turn == bot_color_value
                            )
                            if (
                                engine is not None
                                and fen != last_dashboard_eval_fen
                                and not bot_to_move
                            ):
                                bot_clock_for_dashboard = (
                                    white_clock_seconds if active_bot_color == "white"
                                    else black_clock_seconds if active_bot_color == "black"
                                    else None
                                )
                                safe_to_probe = (
                                    not short_control_fast_mode
                                    and (
                                        bot_clock_for_dashboard is None
                                        or bot_clock_for_dashboard > low_clock_trigger_seconds
                                    )
                                )
                                if safe_to_probe:
                                    try:
                                        quick_limit = max(
                                            0.008,
                                            min(0.045, float(args.engine_time) * 0.15),
                                        )
                                        quick_info = engine.analyse(
                                            dashboard_board,
                                            chess.engine.Limit(time=quick_limit),
                                            multipv=1,
                                        )
                                        if isinstance(quick_info, list):
                                            quick_info = quick_info[0] if quick_info else None
                                        if isinstance(quick_info, dict):
                                            quick_score = quick_info.get("score")
                                            if quick_score is not None:
                                                quick_eval_white_cp = strategy_module.score_to_cp(
                                                    quick_score.pov(chess.WHITE)
                                                ) if strategy_module is not None else quick_score.pov(chess.WHITE).score(
                                                    mate_score=100000
                                                )
                                                settings_window.update_evaluation(
                                                    quick_eval_white_cp,
                                                    source="Live position quick eval",
                                                )
                                                if last_move is not None:
                                                    settings_window.record_observed_move(
                                                        game_number,
                                                        dashboard_board.ply(),
                                                        last_move,
                                                        eval_white_cp=quick_eval_white_cp,
                                                    )
                                    except Exception:
                                        pass
                                last_dashboard_eval_fen = fen
                        except (ValueError, IndexError):
                            pass

                    history_copy = [dict(item) for item in reconstructed_history]
                    writer.writerow({
                        "timestamp_utc": now_utc(),
                        "board_found": event.get("boardFound", False),
                        "game_api_found": event.get("gameApiFound", False),
                        "fen": fen,
                        "turn": event.get("turn", ""),
                        "api_methods": json.dumps(event.get("apiMethods") or [], ensure_ascii=False, separators=(",", ":")),
                        "playing_as": event.get("playingAs", ""),
                        "is_my_turn": event.get("isMyTurn", ""),
                        "bottom_side": event.get("bottomSide", "unknown"),
                        "bottom_side_source": event.get("bottomSideSource", "unknown"),
                        "game_number": game_number,
                        "move_count": len(history_copy),
                        "last_move_san": last_move.get("san", "") if last_move else "",
                        "last_move_uci": last_move.get("uci", "") if last_move else "",
                        "history_source": "FEN-diff/python-chess" if chess is not None else "FEN snapshots only",
                        "moves_json": json.dumps(history_copy, ensure_ascii=False, separators=(",", ":")),
                        "dom_moves_json": json.dumps(dom_moves, ensure_ascii=False, separators=(",", ":")),
                        "legal_move_count": int(event.get("legalMoveCount", 0) or 0),
                        "legal_moves_json": json.dumps(event.get("legalMoves") or [], ensure_ascii=False, separators=(",", ":")),
                        "white_clock": event.get("whiteClock", ""),
                        "black_clock": event.get("blackClock", ""),
                        "result": event.get("result", ""),
                        "note": note,
                    })
                    output_file.flush()

                    # Base console reporting on actual FEN transitions and the
                    # reconstructed history, not the site's unreliable getLegalMoves list.
                    print_signature = json.dumps({
                        "fen": fen,
                        "turn": event.get("turn"),
                        "turnColor": event.get("turnColor"),
                        "playingAs": event.get("playingAs"),
                        "playingAsColor": event.get("playingAsColor"),
                        "playerNameColor": event.get("playerNameColor"),
                        "playerNameColorSource": event.get("playerNameColorSource"),
                        "playerNameMatched": event.get("playerNameMatched"),
                        "playerNameDebugPath": event.get("playerNameDebugPath"),
                        "isMyTurn": event.get("isMyTurn"),
                        "bottomSide": event.get("bottomSide", "unknown"),
                        "historyLen": len(history_copy),
                        "lastMove": last_move,
                        "result": event.get("result", ""),
                        "boardFound": event.get("boardFound", False),
                    }, ensure_ascii=False, sort_keys=True)
                    if print_signature != last_console_signature:
                        last_console_signature = print_signature
                        print(
                            f"[LIVE] turn={event.get('turn')} (normalized={event.get('turnColor')}) | "
                            f"player={event.get('playingAsColor') or 'unknown'} | "
                            f"nameSide={event.get('playerNameColor') or 'unknown'} "
                            f"({event.get('playerNameColorSource') or 'no-evidence'}) | "
                            f"botSide={active_bot_color or 'unconfirmed'} | "
                            f"ply={(chess.Board(fen).ply() if chess is not None and fen else len(history_copy))} | "
                            f"FEN={'OK' if fen else 'missing'} | "
                            f"API={'OK' if event.get('gameApiFound') else 'missing'} | "
                            f"bottomSide={event.get('bottomSide', 'unknown')} (orientation only) | "
                            f"isMyTurn={event.get('isMyTurn')}",
                            flush=True,
                        )
                        name_debug_signature = (
                            event.get("playerNameColor"),
                            event.get("playerNameColorSource"),
                            event.get("playerNameMatched"),
                        )
                        if (
                            load_optional_txt_setting("player_name").strip()
                            and name_debug_signature != last_name_debug_signature
                        ):
                            last_name_debug_signature = name_debug_signature
                            if event.get("playerNameColor") in {"white", "black"}:
                                print(
                                    f"[NAME DEBUG] Configured player name resolved to "
                                    f"{event.get('playerNameColor')} via "
                                    f"{event.get('playerNameColorSource')}.",
                                    flush=True,
                                )
                            elif event.get("playerNameMatched"):
                                print(
                                    "[NAME DEBUG] Player name text was found, but its side is unknown. "
                                    "Set white_player_selector and black_player_selector to the exact "
                                    "containers that include each player's name.",
                                    flush=True,
                                )
                                if event.get("playerNameDebugPath"):
                                    print(
                                        "[NAME DOM] " + str(event.get("playerNameDebugPath")),
                                        flush=True,
                                    )
                            else:
                                print(
                                    "[NAME DEBUG] Exact player_name text was not found. "
                                    "Check the spelling and visible username text in tracker_settings.txt.",
                                    flush=True,
                                )
                        if fen:
                            print("       ", fen, flush=True)
                        if last_move:
                            print(
                                f"        Detected move: {last_move.get('move_number')}. "
                                f"{last_move.get('san')} ({last_move.get('uci')})",
                                flush=True,
                            )
                        if history_copy:
                            print("        Move history:", format_move_history(history_copy), flush=True)
                        if event.get("result"):
                            print("        Result detected.", flush=True)

                    # Autoplay side detection must use the player-color API or an
                    # explicit bot_color setting. Board orientation is presentation only.
                    if args.auto_play and engine is not None and strategy_module is not None and fen and strategy_board is not None:
                        api_my_turn = event.get("isMyTurn")
                        api_bot_color, autoplay_api_color_conflict = normalize_api_bot_color(
                            event.get("playingAsColor"), event.get("playingAs")
                        )
                        player_name_color = normalize_chess_color(event.get("playerNameColor"))
                        # The resolver above already selected the side once for this
                        # snapshot. The explicit API is authoritative; player-name
                        # evidence is a stable two-snapshot fallback only.
                        color_conflict = bool(
                            autoplay_api_color_conflict
                            or (
                                configured_bot_color
                                and api_bot_color
                                and api_bot_color != configured_bot_color
                            )
                            or (
                                configured_player_name
                                and api_bot_color
                                and player_name_color
                                and api_bot_color != player_name_color
                            )
                            or (
                                active_bot_color
                                and api_bot_color
                                and api_bot_color != active_bot_color
                            )
                        )

                        if fen != last_auto_attempt_fen or move_retry_gate.should_attempt(fen):
                            engine_board = strategy_board.copy(stack=True)
                            side_to_move = (
                                "white" if engine_board.turn == chess.WHITE else "black"
                            )
                            api_turn_color = normalize_chess_color(event.get("turnColor"))
                            turn_conflict = bool(
                                api_turn_color and api_turn_color != side_to_move
                            )
                            side_matches_bot = bool(
                                active_bot_color and side_to_move == active_bot_color
                            )

                            # Never treat bottomSide as the bot color. Unknown or
                            # contradictory color data must stop move submission.
                            our_turn = (
                                side_matches_bot
                                and not color_conflict
                                and not turn_conflict
                                and api_my_turn is not False
                            )
                            if api_my_turn is True and not side_matches_bot:
                                turn_conflict = True
                                our_turn = False

                            if (
                                (active_bot_color is None or color_conflict or turn_conflict)
                                and fen != last_side_warning_fen
                            ):
                                last_side_warning_fen = fen
                                if active_bot_color is None:
                                    print(
                                        "[AUTO SAFETY] Bot color is unconfirmed. "
                                        "Set bot_color=white or bot_color=black in tracker_settings.txt, "
                                        "or wait for stable player-name/getPlayingAs side evidence. No move submitted.",
                                        flush=True,
                                    )
                                elif color_conflict:
                                    print(
                                        "[AUTO SAFETY] Player-side sources conflict. "
                                        "Move generation paused; verify player-name selectors and bot_color in TXT settings.",
                                        flush=True,
                                    )
                                else:
                                    print(
                                        "[AUTO SAFETY] API turn conflicts with FEN side-to-move. "
                                        "Move generation paused for this position.",
                                        flush=True,
                                    )

                            if our_turn and not engine_board.is_game_over(claim_draw=True):
                                move_cycle_started_at = time.monotonic()
                                if not move_retry_gate.begin(fen, move_cycle_started_at):
                                    continue
                                last_auto_attempt_fen = fen
                                if move_retry_gate.attempts > 1:
                                    print(
                                        f"[AUTO RETRY] Same position retry "
                                        f"{move_retry_gate.attempts}/{move_retry_gate.MAX_ATTEMPTS}.",
                                        flush=True,
                                    )
                                try:
                                    engine_result = []
                                    selected_info = None
                                    selection_meta = None
                                    remaining_clock_seconds = (
                                        white_clock_seconds
                                        if engine_board.turn == chess.WHITE
                                        else black_clock_seconds
                                    )
                                    opponent_clock_seconds = (
                                        black_clock_seconds
                                        if engine_board.turn == chess.WHITE
                                        else white_clock_seconds
                                    )
                                    low_clock_mode = is_low_clock_mode(
                                        remaining_clock_seconds,
                                        low_clock_trigger_seconds,
                                        short_control_fast_mode,
                                    )
                                    low_clock_total_budget_seconds = (
                                        random.uniform(0.025, 0.045)
                                        if (
                                            low_clock_mode
                                            and remaining_clock_seconds is not None
                                            and opponent_clock_seconds is not None
                                            and remaining_clock_seconds <= opponent_clock_seconds
                                        )
                                        else random.uniform(0.030, 0.055)
                                        if low_clock_mode else None
                                    )
                                    if low_clock_mode:
                                        print(
                                            f"[LOW CLOCK] Active | own={remaining_clock_seconds:.2f}s "
                                            f"| opponent={opponent_clock_seconds if opponent_clock_seconds is not None else 'unknown'}s "
                                            f"| threshold={low_clock_trigger_seconds:.2f}s "
                                            f"| total move budget={low_clock_total_budget_seconds:.3f}s.",
                                            flush=True,
                                        )
                                    book_choice = book.choose(engine_board) if book is not None and not low_clock_mode else None

                                    if book_choice is not None:
                                        pending_opponent_best_cp = None
                                        pending_opponent_color = None
                                        chosen_move = book_choice["move"]
                                        best_info = None
                                        selection_meta = {
                                            "rank": int(book_choice["rank"]) - 1,
                                            "current_cp": 0,
                                            "selected_cp": 0,
                                            "reason": "GM BOOK | weighted opening-book choice",
                                            "source": "GM_BOOK",
                                            "book_entries": int(book_choice.get("entries", 0)),
                                            "book_weight": float(book_choice.get("weight", 0.0)),
                                        }
                                    else:
                                        limit_seconds = calculate_engine_search_limit(
                                            base_seconds=args.engine_time,
                                            remaining_seconds=remaining_clock_seconds,
                                            initial_seconds=initial_game_clock_seconds,
                                            increment_seconds=clock_increment_seconds,
                                            board=engine_board,
                                            opponent_seconds=opponent_clock_seconds,
                                            low_clock_trigger_seconds=low_clock_trigger_seconds,
                                            force_fast_mode=short_control_fast_mode,
                                        )
                                        if remaining_clock_seconds is None:
                                            print(
                                                f"[ENGINE CLOCK] Remaining clock unavailable; "
                                                f"using configured cap={limit_seconds:.3f}s.",
                                                flush=True,
                                            )
                                        else:
                                            print(
                                                f"[ENGINE CLOCK] remaining={remaining_clock_seconds:.2f}s | "
                                                f"search_limit={limit_seconds:.3f}s.",
                                                flush=True,
                                            )
                                        engine_result = engine.analyse(
                                            engine_board,
                                            chess.engine.Limit(
                                                depth=8 if low_clock_mode else 12,
                                                time=limit_seconds,
                                            ),
                                            multipv=(
                                                min(3, strategy_module.TRAINING_MULTI_PV)
                                                if low_clock_mode
                                                else strategy_module.TRAINING_MULTI_PV
                                            ),
                                        )
                                        if not isinstance(engine_result, list):
                                            engine_result = [engine_result]
                                        best_info = engine_result[0] if engine_result else None
                                        if pending_opponent_best_cp is not None:
                                            try:
                                                # Compare evaluations with the same shallow search limits.
                                                quality_info = None
                                                if not low_clock_mode:
                                                    quality_info = engine.analyse(
                                                        engine_board,
                                                        chess.engine.Limit(
                                                            depth=int(getattr(strategy_module, "ANALYSIS_DEPTH", 8)),
                                                            time=max(0.005, min(
                                                                float(getattr(strategy_module, "ANALYSIS_TIME", 0.010)),
                                                                0.025,
                                                            )),
                                                        ),
                                                    )
                                                score_obj = (
                                                    quality_info.get("score")
                                                    if isinstance(quality_info, dict) else None
                                                )
                                                if (
                                                    score_obj is not None
                                                    and pending_opponent_color in {"white", "black"}
                                                ):
                                                    opponent_chess_color = (
                                                        chess.WHITE if pending_opponent_color == "white"
                                                        else chess.BLACK
                                                    )
                                                    played_position_cp = strategy_module.score_to_cp(
                                                        score_obj.pov(opponent_chess_color)
                                                    )
                                                    estimated_cpl = strategy_module.opponent_move_centipawn_loss(
                                                        pending_opponent_best_cp, played_position_cp
                                                    )
                                                    if estimated_cpl is not None:
                                                        move_quality = strategy_module.opponent_quality_from_centipawn_loss(
                                                            estimated_cpl
                                                        )
                                                        if move_quality is not None:
                                                            opponent_match_history.append(float(move_quality))
                                                            opponent_pressure = move_quality >= 85.0
                                                            print(
                                                                f"[OPPONENT QUALITY] comparable-search CPL={estimated_cpl:.0f}cp "
                                                                f"| score={move_quality:.1f}/100.",
                                                                flush=True,
                                                            )
                                            except (chess.engine.EngineError, ValueError, TypeError, AttributeError, RuntimeError):
                                                print("[OPPONENT QUALITY] Sample skipped; comparable evaluation unavailable.", flush=True)
                                            finally:
                                                pending_opponent_best_cp = None
                                                pending_opponent_color = None
                                        if settings_window is not None and best_info is not None:
                                            try:
                                                best_score = best_info.get("score")
                                                if best_score is not None:
                                                    current_eval_white_cp = strategy_module.score_to_cp(
                                                        best_score.pov(chess.WHITE)
                                                    )
                                                    settings_window.update_evaluation(
                                                        current_eval_white_cp, source="Stockfish"
                                                    )
                                            except Exception:
                                                pass

                                        opponent_accuracy, opponent_sample_count = strategy_module.opponent_recent_accuracy(
                                            opponent_match_history
                                        )
                                        print(
                                            f"[ENGINE POV] analysis_side={active_bot_color} | "
                                            f"fen_side_to_move={side_to_move}.",
                                            flush=True,
                                        )
                                        chosen_move, selected_info, selection_meta = strategy_module.choose_stockfish_move(
                                            engine_board,
                                            engine_result,
                                            previous_eval_white_cp=previous_eval_white_cp,
                                            opponent_accuracy=opponent_accuracy,
                                            opponent_sample_count=opponent_sample_count,
                                            opponent_pressure=opponent_pressure,
                                            engine=engine,
                                            bot_color=(active_bot_color == "white"),
                                            low_clock_mode=low_clock_mode,
                                        )
                                        opponent_pressure = False

                                    if chosen_move is None or chosen_move not in engine_board.legal_moves:
                                        raise RuntimeError("ChessKida selector did not return a legal move.")

                                    chosen_san = engine_board.san(chosen_move)
                                    rank = selection_meta.get("rank") if isinstance(selection_meta, dict) else None
                                    rank_text = f"#{int(rank) + 1}" if rank is not None else "FREE"
                                    reason = str(selection_meta.get("reason", "strategy selection")) if isinstance(selection_meta, dict) else "strategy selection"
                                    pending_ply = engine_board.ply() + 1
                                    selected_side = "white" if engine_board.turn == chess.WHITE else "black"
                                    selected_eval_white_cp = None
                                    selection_source = (
                                        str(selection_meta.get("source", ""))
                                        if isinstance(selection_meta, dict) else ""
                                    )
                                    if selection_source != "GM_BOOK" and isinstance(selected_info, dict):
                                        try:
                                            selected_score = selected_info.get("score")
                                            if selected_score is not None:
                                                selected_eval_white_cp = strategy_module.score_to_cp(
                                                    selected_score.pov(chess.WHITE)
                                                )
                                        except Exception:
                                            selected_eval_white_cp = None
                                    if (
                                        selected_eval_white_cp is None
                                        and selection_source != "GM_BOOK"
                                        and isinstance(selection_meta, dict)
                                    ):
                                        try:
                                            selected_cp = selection_meta.get("selected_cp")
                                            if selected_cp is not None:
                                                selected_eval_white_cp = (
                                                    int(selected_cp)
                                                    if engine_board.turn == chess.WHITE
                                                    else -int(selected_cp)
                                                )
                                        except (TypeError, ValueError):
                                            pass
                                    dashboard_rank = (
                                        f"BOOK #{int(rank) + 1}"
                                        if selection_source == "GM_BOOK" and rank is not None
                                        else rank_text
                                    )
                                    if settings_window is not None:
                                        settings_window.record_selected_move(
                                            game_number, pending_ply, selected_side,
                                            chosen_san, dashboard_rank,
                                            eval_white_cp=selected_eval_white_cp,
                                            status="Selected",
                                        )
                                    print(f"[STRATEGY] selected={chosen_san} | rank={rank_text} | {reason}", flush=True)

                                    if low_clock_mode:
                                        wait_for_manual_verification(driver, allowed_host, cfg)

                                    natural_delay = strategy_module.natural_bot_move_delay(
                                        engine_board,
                                        chosen_move,
                                        engine_result=engine_result,
                                        selection_meta=selection_meta,
                                        thinking_enabled=strategy_module.BOT_THINKING_ENABLED_DEFAULT,
                                        previous_eval_white_cp=previous_eval_white_cp,
                                        initial_time_seconds=initial_game_clock_seconds,
                                        remaining_time_seconds=remaining_clock_seconds,
                                        opponent_time_seconds=opponent_clock_seconds,
                                        increment_seconds=clock_increment_seconds,
                                        is_opening_book_move=(
                                            isinstance(selection_meta, dict)
                                            and selection_meta.get("source") == "GM_BOOK"
                                        ),
                                        low_clock_trigger_seconds=low_clock_trigger_seconds,
                                        low_clock_total_budget_seconds=low_clock_total_budget_seconds,
                                        low_clock_mode=low_clock_mode,
                                        elapsed_seconds_before_delay=time.monotonic() - move_cycle_started_at,
                                        submission_reserve_seconds=(
                                            0.075 if args.move_input == "cursor" else 0.035
                                        ),
                                    )
                                    if natural_delay > 0:
                                        mode_text = "LOW_CLOCK" if low_clock_mode else "OWN_CLOCK"
                                        print(
                                            f"[BOT TIMING] delay={natural_delay:.3f}s | mode={mode_text}"
                                            f" | own_clock={remaining_clock_seconds if remaining_clock_seconds is not None else 'unknown'}s.",
                                            flush=True,
                                        )
                                        if settings_window is not None:
                                            settings_window.responsive_sleep(natural_delay)
                                        else:
                                            time.sleep(natural_delay)

                                    if not low_clock_mode:
                                        wait_for_manual_verification(driver, allowed_host, cfg)
                                    if settings_window is not None:
                                        settings_window.pump()
                                    if not args.auto_play:
                                        submit = {"ok": False, "reason": "bot-disabled-in-settings"}
                                    elif args.move_input == "cursor":
                                        submit = perform_cursor_drag(
                                            driver, chosen_move.uci(), fen, active_bot_color,
                                            low_clock_mode=low_clock_mode,
                                        )
                                    else:
                                        submit = perform_api_move(
                                            driver, chosen_move.uci(), fen, active_bot_color,
                                            low_clock_mode=low_clock_mode,
                                        )
                                    if isinstance(submit, dict) and submit.get("ok"):
                                        move_retry_gate.finish(submit)
                                        if low_clock_mode:
                                            move_elapsed = time.monotonic() - move_cycle_started_at
                                            print(
                                                f"[MOVE BUDGET] target={low_clock_total_budget_seconds:.3f}s "
                                                f"end-to-end={move_elapsed:.3f}s "
                                                f"overrun={max(0.0, move_elapsed - low_clock_total_budget_seconds):.3f}s.",
                                                flush=True,
                                            )
                                        if args.move_input == "cursor":
                                            print(f"[CURSOR] Drag accepted | travel={int(submit.get('drag_ms', 0))} ms | pre-delay={int(submit.get('pre_delay_ms', 0))} ms | waypoints={int(submit.get('waypoints', 0))}.", flush=True)
                                        print(f"[AUTO] Submitted selected move: {chosen_san} ({chosen_move.uci()}).", flush=True)
                                        if settings_window is not None:
                                            settings_window.update_move_status(
                                                game_number, pending_ply, "Submitted"
                                            )

                                        after_board = engine_board.copy(stack=True)
                                        after_board.push(chosen_move)
                                        if low_clock_mode:
                                            analysis_state = None
                                            print(
                                                "[LOW CLOCK] Skipped nonessential post-move analysis to keep the next event/premove responsive.",
                                                flush=True,
                                            )
                                        else:
                                            try:
                                                analysis_state = strategy_module.build_analysis(
                                                    engine,
                                                    engine_board,
                                                    after_board,
                                                    chosen_move,
                                                    best_info=best_info,
                                                )
                                            except Exception:
                                                analysis_state = None

                                        if analysis_state:
                                            previous_eval_white_cp = analysis_state.get("eval_cp")
                                            if settings_window is not None:
                                                settings_window.update_evaluation(
                                                    previous_eval_white_cp,
                                                    source="After-move analysis",
                                                )

                                            next_opponent_best_cp = analysis_state.get("next_reply_eval_cp")
                                        else:
                                            previous_eval_white_cp = None
                                            next_opponent_best_cp = None
                                    else:
                                        move_retry_gate.finish(
                                            submit if isinstance(submit, dict)
                                            else {"ok": False, "reason": "invalid-submit-result"}
                                        )
                                        reason = submit.get("reason", "move-submit-failed") if isinstance(submit, dict) else "move-submit-failed"
                                        detail = ""
                                        if args.move_input == "cursor" and isinstance(submit, dict) and submit.get("error"):
                                            detail = f" | {submit.get('error_type', 'Error')}: {str(submit.get('error'))[:240]}"
                                        elif args.move_input == "cursor" and isinstance(submit, dict) and submit.get("drag_ms") is not None:
                                            detail = (
                                                f" | travel={int(submit.get('drag_ms', 0))} ms"
                                                f" | pre-delay={int(submit.get('pre_delay_ms', 0))} ms"
                                            )
                                        print(f"[AUTO] Move was not confirmed ({reason}){detail}.", flush=True)
                                        if reason == "position-changed-before-drag":
                                            print(
                                                "[AUTO SAFETY] Board changed after selection; stale move was not retried.",
                                                flush=True,
                                            )
                                        if settings_window is not None:
                                            settings_window.update_move_status(
                                                game_number, pending_ply,
                                                f"Failed: {str(reason)[:28]}",
                                            )
                                except (chess.engine.EngineError, ValueError, WebDriverException, RuntimeError):
                                    move_retry_gate.finish_exception()
                                    print("[AUTO] Strategy calculation/submission failed; bounded retry remains available if no move was accepted.", flush=True)
                            elif (
                                args.auto_play
                                and args.premove_enabled
                                and active_bot_color in {"white", "black"}
                                and not side_matches_bot
                                and not color_conflict
                                and not turn_conflict
                                and (api_my_turn is not True or api_turn_color == side_to_move)
                                and (
                                    fen != last_premove_attempt_fen
                                    or (
                                        args.low_clock_extra_premoves
                                        and fen != last_low_clock_premove_fen
                                        and (
                                            (active_bot_color == "white" and white_clock_seconds is not None and white_clock_seconds <= low_clock_trigger_seconds)
                                            or (active_bot_color == "black" and black_clock_seconds is not None and black_clock_seconds <= low_clock_trigger_seconds)
                                        )
                                    )
                                )
                                and not engine_board.is_game_over(claim_draw=True)
                            ):
                                # A premove is speculative: analyze the likely opponent reply,
                                # then queue a move legal after that predicted reply. If the
                                # site cannot queue/confirm it, ordinary-turn logic still runs.
                                last_premove_attempt_fen = fen
                                bot_clock_for_premove = (
                                    white_clock_seconds if active_bot_color == "white"
                                    else black_clock_seconds
                                )
                                opponent_clock_for_premove = (
                                    black_clock_seconds if active_bot_color == "white"
                                    else white_clock_seconds
                                )
                                low_clock_premove_mode = is_low_clock_mode(
                                    bot_clock_for_premove,
                                    low_clock_trigger_seconds,
                                    short_control_fast_mode,
                                )
                                low_clock_premove_budget_seconds = (
                                    random.uniform(0.025, 0.045)
                                    if (
                                        low_clock_premove_mode
                                        and bot_clock_for_premove is not None
                                        and opponent_clock_for_premove is not None
                                        and bot_clock_for_premove <= opponent_clock_for_premove
                                    )
                                    else random.uniform(0.030, 0.055)
                                    if low_clock_premove_mode else None
                                )
                                if low_clock_premove_mode:
                                    last_low_clock_premove_fen = fen
                                    print(
                                        f"[PREMOVE] Random-threshold low-clock priority active "
                                        f"({bot_clock_for_premove:.2f}s <= {low_clock_trigger_seconds:.2f}s); "
                                        f"opponent={opponent_clock_for_premove if opponent_clock_for_premove is not None else 'unknown'}s; "
                                        f"optional-wait target={low_clock_premove_budget_seconds:.3f}s, "
                                        "checking safe capture/promotion conversion first.",
                                        flush=True,
                                    )
                                try:
                                    premove_cycle_started_at = time.monotonic()
                                    premove_analysis_budget_seconds = (
                                        random.uniform(0.10, 0.18)
                                        if low_clock_premove_mode
                                        else random.uniform(0.45, 0.75)
                                    )
                                    premove_analysis_deadline = (
                                        premove_cycle_started_at + premove_analysis_budget_seconds
                                    )
                                    print(
                                        f"[PREMOVE CHECK] Candidate analysis started | "
                                        f"deadline={premove_analysis_budget_seconds:.3f}s.",
                                        flush=True,
                                    )
                                    premove_move, predicted_reply, premove_source = choose_premove_candidate(
                                        engine,
                                        engine_board,
                                        strategy_module,
                                        active_bot_color,
                                        args,
                                        book=book,
                                        opponent_match_history=opponent_match_history,
                                        low_clock_mode=low_clock_premove_mode,
                                        analysis_deadline=premove_analysis_deadline,
                                    )
                                    premove_analysis_elapsed = time.monotonic() - premove_cycle_started_at
                                    if (
                                        premove_move is not None
                                        and time.monotonic() >= premove_analysis_deadline - 0.012
                                    ):
                                        premove_move = None
                                        premove_source = "premove-analysis-deadline-exceeded"
                                    print("[PREMOVE CHECK] candidate=" + (premove_move.uci() if premove_move is not None else "none") + " | reason=" + str(premove_source) + " | analysis=" + format(premove_analysis_elapsed, ".3f") + "s", flush=True)
                                    if premove_move is None:
                                        print(
                                            f"[PREMOVE] Skipped ({premove_source}); normal turn handling remains enabled.",
                                            flush=True,
                                        )
                                    elif len(premove_move.uci()) == 5 and args.move_input == "cursor":
                                        print(
                                            "[PREMOVE] Skipped promotion candidate; this board needs a site-specific promotion handler.",
                                            flush=True,
                                        )
                                    else:
                                        predicted_san = engine_board.san(predicted_reply) if predicted_reply else "unknown"
                                        chosen_premove_uci = premove_move.uci()
                                        wait_for_manual_verification(driver, allowed_host, cfg)
                                        if low_clock_premove_mode:
                                            reserve = 0.075 if args.move_input == "cursor" else 0.035
                                            elapsed = time.monotonic() - premove_cycle_started_at
                                            wait_remaining = max(
                                                0.0,
                                                low_clock_premove_budget_seconds - elapsed - reserve,
                                            )
                                            print(
                                                f"[PREMOVE TIMING] optional-wait-target={low_clock_premove_budget_seconds:.3f}s "
                                                f"elapsed={elapsed:.3f}s reserve={reserve:.3f}s "
                                                f"wait={wait_remaining:.3f}s.",
                                                flush=True,
                                            )
                                            if wait_remaining > 0:
                                                if settings_window is not None:
                                                    settings_window.responsive_sleep(wait_remaining)
                                                else:
                                                    time.sleep(wait_remaining)
                                        if settings_window is not None:
                                            settings_window.pump()
                                        if not args.auto_play or not args.premove_enabled:
                                            premove_submit = {"ok": False, "reason": "bot-or-extra-premove-disabled-in-settings"}
                                        elif args.move_input == "cursor":
                                            premove_submit = perform_cursor_drag(
                                                driver,
                                                chosen_premove_uci,
                                                fen,
                                                active_bot_color,
                                                allow_premove=True,
                                                low_clock_mode=low_clock_premove_mode,
                                            )
                                        else:
                                            premove_submit = perform_api_premove(
                                                driver,
                                                chosen_premove_uci,
                                                fen,
                                                active_bot_color,
                                            )
                                        if isinstance(premove_submit, dict) and premove_submit.get("ok"):
                                            if not low_clock_premove_mode:
                                                # A normal-mode queue already exists; avoid a duplicate retry for this FEN.
                                                last_low_clock_premove_fen = fen
                                            if low_clock_premove_mode:
                                                premove_elapsed = time.monotonic() - premove_cycle_started_at
                                                print(
                                                    f"[PREMOVE BUDGET] optional-wait-target={low_clock_premove_budget_seconds:.3f}s "
                                                    f"end-to-end={premove_elapsed:.3f}s "
                                                    f"overrun={max(0.0, premove_elapsed - low_clock_premove_budget_seconds):.3f}s.",
                                                    flush=True,
                                                )
                                            if premove_submit.get("premove_confirmed"):
                                                confirmation = f"confirmed via {premove_submit.get('premove_source', 'site status')}"
                                            elif premove_submit.get("confirmed"):
                                                confirmation = f"accepted by {premove_submit.get('source', 'site premove API')}"
                                            else:
                                                confirmation = "gesture sent; site queue status is not exposed, normal-turn fallback remains enabled"
                                            print(
                                                f"[PREMOVE] {engine_board.san(premove_move) if premove_move in engine_board.legal_moves else premove_move.uci()} "
                                                f"queued-attempt after predicted reply {predicted_san} | {confirmation}.",
                                                flush=True,
                                            )
                                        else:
                                            reason = premove_submit.get("reason", "premove-submit-failed") if isinstance(premove_submit, dict) else "premove-submit-failed"
                                            print(
                                                f"[PREMOVE] Not queued ({reason}); normal turn handling remains enabled.",
                                                flush=True,
                                            )
                                except (chess.engine.EngineError, ValueError, WebDriverException, RuntimeError, TypeError):
                                    print("[PREMOVE] Candidate calculation/submission failed; normal turn handling remains enabled.", flush=True)
                            else:
                                if fen and fen != last_premove_gate_report_fen:
                                    last_premove_gate_report_fen = fen
                                    if not args.auto_play:
                                        gate_reason = "autoplay-disabled"
                                    elif not args.premove_enabled:
                                        gate_reason = "premove-disabled"
                                    elif active_bot_color not in {"white", "black"}:
                                        gate_reason = "bot-side-unverified"
                                    elif side_matches_bot:
                                        gate_reason = "it-is-the-bot-turn"
                                    elif color_conflict:
                                        gate_reason = "player-side-conflict"
                                    elif turn_conflict:
                                        gate_reason = "FEN-turn-conflict"
                                    elif api_my_turn is True and api_turn_color != side_to_move:
                                        gate_reason = "API-my-turn-signal-does-not-match-FEN-turn"
                                    elif engine_board.is_game_over(claim_draw=True):
                                        gate_reason = "game-over"
                                    elif fen == last_premove_attempt_fen:
                                        gate_reason = "already-attempted-this-position"
                                    else:
                                        gate_reason = "premove-trigger-conditions-not-met"
                                    print("[PREMOVE GATE] Not attempted: " + gate_reason + ".", flush=True)

                if low_clock_polling_active:
                    if settings_window is not None:
                        settings_window.responsive_sleep(0.005)
                    else:
                        time.sleep(0.005)
                else:
                    if settings_window is not None:
                        settings_window.responsive_sleep(max(0.05, args.queue_read_interval))
                    else:
                        time.sleep(max(0.05, args.queue_read_interval))

    except KeyboardInterrupt:
        print("\n[STOP] Reader stopped.", flush=True)
    except (RuntimeError, WebDriverException):
        print("[ERROR] Browser session or site-tab selection failed. Site details were not logged.", file=sys.stderr, flush=True)
        return 2
    finally:
        if book is not None:
            try:
                book.close()
            except Exception:
                pass
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        try:
            if driver and chosen_handle and chosen_handle in driver.window_handles:
                driver.switch_to.window(chosen_handle)
                driver.execute_script("window.__liveGameApiReader?.stop?.(); window.__ownedSiteGameActions?.stop?.(); window.__ownedSiteAutoNextGame?.stop?.()")
        except Exception:
            pass
        if args.browser_mode == "uc" and driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())