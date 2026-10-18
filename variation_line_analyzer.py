"""Standalone hypothetical-line analyzer for the configured authorized chess board.

The existing seleniumbase_live_game_tracker.py is imported only for its established
Chrome attach, site allowlist, board observer, and native-arrow helpers. This file
does not modify the tracker or submit chess moves. It watches user-drawn right-click
arrows, treats a legal arrow as a hypothetical first move, and draws a Stockfish PV
as additional native board arrows while showing numbered SAN/evaluation/capture
details in a companion window.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import urlopen

try:
    import chess
    import chess.engine
except ImportError as exc:
    raise SystemExit("Missing python-chess. Install with: python -m pip install python-chess") from exc

try:
    import seleniumbase_live_game_tracker as tracker
except ImportError as exc:
    raise SystemExit("Keep this file beside seleniumbase_live_game_tracker.py and run it from the project venv.") from exc


def make_observer_config(board_selector: str) -> dict:
    return {
        "boardSelector": board_selector,
        "fenSelector": "",
        "fenAttribute": "data-fen",
        "movesSelector": "[data-ply], .white.node, .black.node",
        "plyAttribute": "data-ply",
        "timeAttribute": "data-time",
        "moveListSelector": "vertical-move-list, wc-simple-move-list",
        "whiteClockSelector": tracker.load_optional_txt_setting("white_clock_selector") or ".clock-component.clock-white",
        "blackClockSelector": tracker.load_optional_txt_setting("black_clock_selector") or ".clock-component.clock-black",
        "resultSelector": "#game-result, [data-result]",
        "playerName": tracker.load_optional_txt_setting("player_name"),
        "whitePlayerSelector": tracker.load_optional_txt_setting("white_player_selector") or ".white.player, .player.white, [data-color='white'], [data-side='white'], [data-player-color='white'], #white-player, .white-player",
        "blackPlayerSelector": tracker.load_optional_txt_setting("black_player_selector") or ".black.player, .player.black, [data-color='black'], [data-side='black'], [data-player-color='black'], #black-player, .black-player",
    }


INSTALL_LINE_WATCHER = r"""
const selector = String(arguments[0] || 'chess-board');
const api = window.__liveGameApiReader;
if (!api || typeof api.current !== 'function') {
  return {ok:false, reason:'tracker-board-observer-not-installed'};
}
if (window.__variationLineAnalyzer) {
  try { window.__variationLineAnalyzer.stop(); } catch (_) {}
}
const queue = [];
let down = null;
function matchingBoard(event) {
  const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
  for (const node of path) {
    try { if (node && node.matches && node.matches(selector)) return node; } catch (_) {}
  }
  return null;
}
function squareAt(board, x, y, side) {
  const r = board.getBoundingClientRect();
  if (!r.width || !r.height || Math.abs(r.width-r.height) > Math.max(8, r.width*0.08)) return null;
  if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) return null;
  const fileOnScreen = Math.max(0, Math.min(7, Math.floor((x-r.left)/(r.width/8))));
  const rowOnScreen = Math.max(0, Math.min(7, Math.floor((y-r.top)/(r.height/8))));
  const file = side === 'white' ? fileOnScreen : 7-fileOnScreen;
  const rank = side === 'white' ? 8-rowOnScreen : rowOnScreen+1;
  return String.fromCharCode(97+file) + String(rank);
}
function onDown(event) {
  if (event.button !== 2) return;
  const board = matchingBoard(event);
  if (!board) { down = null; return; }
  const state = api.current() || {};
  const side = String(state.bottomSide || '').toLowerCase();
  if (!['white','black'].includes(side)) { down = null; return; }
  const rect = board.getBoundingClientRect();
  const from = squareAt(board, event.clientX, event.clientY, side);
  if (!from) { down = null; return; }
  down = {
    board, from, fen: String(state.fen || ''), side,
    rect: {left:rect.left,top:rect.top,width:rect.width,height:rect.height,bottom_side:side}
  };
}
function onUp(event) {
  if (!down || event.button !== 2) return;
  const saved = down;
  down = null;
  const to = squareAt(saved.board, event.clientX, event.clientY, saved.side);
  if (!to || to === saved.from) return;
  const state = api.current() || {};
  const fen = String(state.fen || '');
  if (!fen || fen !== saved.fen) return;
  queue.push({
    from:saved.from, to, fen,
    geometry:saved.rect,
    bottomSide:saved.side,
    at:Date.now()
  });
  if (queue.length > 20) queue.splice(0, queue.length-20);
}
window.addEventListener('pointerdown', onDown, true);
window.addEventListener('pointerup', onUp, true);
window.__variationLineAnalyzer = {
  queue,
  stop:() => {
    window.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('pointerup', onUp, true);
    queue.length = 0;
    delete window.__variationLineAnalyzer;
  }
};
return {ok:true};
"""


PIECE_NAMES = {
    "p": "pawn", "n": "knight", "b": "bishop",
    "r": "rook", "q": "queen", "k": "king",
}


def eval_white(engine, board: chess.Board, seconds: float) -> tuple[str, float]:
    # Stockfish cannot analyse an already-terminal position on some builds.
    # Handle mate/draw states before asking the engine.
    if board.is_checkmate():
        winner = not board.turn
        side = "White" if winner == chess.WHITE else "Black"
        value = 100000.0 if winner == chess.WHITE else -100000.0
        return f"Mate for {side}", value
    if board.is_game_over(claim_draw=True):
        return "Draw", 0.0

    info = engine.analyse(board, chess.engine.Limit(time=seconds))
    score = info["score"].pov(chess.WHITE)
    mate = score.mate()
    if mate is not None:
        side = "White" if mate > 0 else "Black"
        value = (100000.0 if mate > 0 else -100000.0)
        value -= abs(mate) if mate > 0 else -abs(mate)
        return f"Mate for {side} in {abs(mate)}", value
    cp = score.score(mate_score=100000)
    value = (cp or 0) / 100.0
    if value > 0.04:
        label = f"White +{value:.2f}"
    elif value < -0.04:
        label = f"Black +{abs(value):.2f}"
    else:
        label = f"Equal ({value:+.2f})"
    return label, value


def delta_text(previous: float, current: float) -> str:
    delta = current - previous
    if abs(delta) < 0.04:
        return "Δ≈0.00"
    side = "White" if delta > 0 else "Black"
    return f"Δ {delta:+.2f} toward {side}"


def capture_description(board: chess.Board, move: chess.Move) -> str:
    captured = board.piece_at(move.to_square)
    if board.is_en_passant(move):
        captured_square = move.to_square - 8 if board.turn == chess.WHITE else move.to_square + 8
        captured = board.piece_at(captured_square)
    if captured:
        owner = "White" if captured.color == chess.WHITE else "Black"
        return f"captures {owner} {PIECE_NAMES[captured.symbol().lower()]}"
    if board.is_castling(move):
        return "castles"
    if move.promotion:
        return f"promotes to {PIECE_NAMES[chess.piece_symbol(move.promotion)]}"
    return ""


def move_label(board: chess.Board, move: chess.Move) -> str:
    san = board.san(move)
    turn = board.fullmove_number
    prefix = f"{turn}." if board.turn == chess.WHITE else f"{turn}..."
    note = capture_description(board, move)
    board.push(move)
    if board.is_checkmate():
        note = (note + "; " if note else "") + "CHECKMATE"
    elif board.is_check():
        note = (note + "; " if note else "") + "check"
    return prefix + san, note


def clear_generated(driver, generated: list[tuple[str, dict]]) -> None:
    if not generated:
        return
    cdp = tracker._direct_cdp_page_for_driver(driver)
    if cdp is None:
        generated.clear()
        return
    # Native arrows toggle off when the same right-drag is repeated.
    for uci, geom in reversed(generated):
        try:
            tracker._dispatch_native_board_arrow(
                cdp, uci,
                left=geom["left"], top=geom["top"],
                width=geom["width"], height=geom["height"],
                bottom_side=geom["bottom_side"],
            )
            time.sleep(0.025)
        except Exception:
            pass
    generated.clear()


def analyze_arrow(driver, engine, event: dict, max_plies: int, search_time: float,
                  generated: list[tuple[str, dict]], text_widget, status_var) -> None:
    from_sq, to_sq = str(event.get("from", "")), str(event.get("to", ""))
    fen = str(event.get("fen", ""))
    geom = event.get("geometry") or {}
    try:
        board = chess.Board(fen)
    except Exception:
        status_var.set("Current board FEN could not be read.")
        return
    clear_generated(driver, generated)
    uci = from_sq + to_sq
    move = chess.Move.from_uci(uci)
    if move not in board.legal_moves:
        # Promotion arrows need a promotion suffix; queen is the conventional default.
        if len(uci) == 4 and board.piece_at(move.from_square) and board.piece_at(move.from_square).piece_type == chess.PAWN and chess.square_rank(move.to_square) in (0, 7):
            move = chess.Move.from_uci(uci + "q")
        if move not in board.legal_moves:
            status_var.set(f"{uci}: not a legal move in the current position.")
            text_widget.insert("end", f"\n{from_sq} → {to_sq}: not legal in the current position.\n")
            text_widget.see("end")
            return
    try:
        starting_eval, start_score = eval_white(engine, board, search_time)
        line_board = board.copy()
        line_board.push(move)
        if line_board.is_game_over(claim_draw=True):
            pv = []
        else:
            first_info = engine.analyse(line_board, chess.engine.Limit(time=search_time))
            pv = list(first_info.get("pv") or [])
        line = [move] + pv[:max(0, max_plies-1)]
    except Exception as exc:
        status_var.set(f"Engine analysis failed ({type(exc).__name__}).")
        return

    report = [
        f"\nHypothetical move: {from_sq} → {to_sq}",
        f"Position before move: {starting_eval}",
        f"Continuation: up to {len(line)} plies (one ply = one player's move).",
        "The first arrow is yours; numbered native arrows below show the engine continuation.",
        ""
    ]
    eval_board = board.copy()
    previous_score = start_score
    cdp = tracker._direct_cdp_page_for_driver(driver)
    drawn_count = 0
    for index, current_move in enumerate(line, start=1):
        if current_move not in eval_board.legal_moves:
            break
        before = eval_board.copy()
        label, note = move_label(eval_board, current_move)
        current_eval, current_score = eval_white(engine, eval_board, search_time)
        step_delta = delta_text(previous_score, current_score)
        line_item = f"{index}. {label} | {current_eval} | {step_delta}"
        if note:
            line_item += f" | {note}"
        report.append(line_item)
        previous_score = current_score

        # The user already drew the first move. Add only the continuation as
        # native right-click arrows, without submitting or premoving any move.
        if index > 1 and cdp is not None and isinstance(geom, dict):
            try:
                result = tracker._dispatch_native_board_arrow(
                    cdp, current_move.uci(),
                    left=float(geom["left"]), top=float(geom["top"]),
                    width=float(geom["width"]), height=float(geom["height"]),
                    bottom_side=str(geom["bottom_side"]),
                )
                if result.get("ok"):
                    generated.append((current_move.uci(), dict(geom)))
                    drawn_count += 1
                    time.sleep(0.035)
            except Exception:
                pass

        if eval_board.is_checkmate():
            break
    report.extend([
        "",
        "Eval convention: positive favors White; negative favors Black.",
        "Arrows are the board's native right-click annotations. The numbered move/eval list is in this window.",
        f"Native continuation arrows drawn: {drawn_count}",
        ""
    ])
    text_widget.insert("end", "\n".join(report) + "\n")
    text_widget.see("end")
    status_var.set(f"Analyzed {from_sq} → {to_sq}; {drawn_count} continuation arrows drawn.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a manually drawn hypothetical line on the configured authorized chess board.")
    parser.add_argument("--debugger-address", default="127.0.0.1:9223")
    parser.add_argument("--allowed-domain", default="")
    parser.add_argument("--board-selector", default="chess-board")
    parser.add_argument("--stockfish-path", default="")
    parser.add_argument("--plies", type=int, default=8, help="Total half-moves shown, including your drawn first move (2-16).")
    parser.add_argument("--search-time", type=float, default=0.20, help="Stockfish search time per evaluated position in seconds (0.05-2.0).")
    args = parser.parse_args()
    args.plies = max(2, min(16, int(args.plies)))
    args.search_time = max(0.05, min(2.0, float(args.search_time)))

    try:
        blocked = tracker.load_blocked_platforms()
        allowed_host = tracker.normalize_host(args.allowed_domain) if args.allowed_domain else tracker.load_allowed_host_from_txt()[0]
        if not allowed_host:
            print("[CONFIG ERROR] Add site_domain=your-authorized-test-host to tracker_settings.txt.", file=sys.stderr)
            return 2
        if tracker.is_public_competitive_host(allowed_host):
            print("[BLOCKED] Public competitive chess platforms are not supported.", file=sys.stderr)
            return 2
        with urlopen(f"http://{args.debugger_address}/json/version", timeout=3.0) as response:
            browser_info = json.loads(response.read().decode("utf-8", errors="replace"))
        browser_version = str(browser_info.get("Browser", "Chrome"))
        tracker.wake_configured_debugger_tab(args.debugger_address, allowed_host)
        driver_path, resolution_error = tracker.resolve_chromedriver_with_timeout(
            timeout_seconds=12.0, browser_version=browser_version
        )
        if resolution_error or not driver_path:
            print(f"[CONNECT ERROR] Compatible ChromeDriver unavailable ({resolution_error or 'not found'}).", file=sys.stderr)
            return 2
        options = tracker.webdriver.ChromeOptions()
        options.debugger_address = args.debugger_address
        driver, attach_error = tracker.attach_to_chrome_with_timeout(options, driver_path, timeout_seconds=30.0)
        if attach_error or driver is None:
            print(f"[CONNECT ERROR] Could not attach to Chrome ({attach_error or 'unknown error'}).", file=sys.stderr)
            return 2

        candidates = []
        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                if tracker.target_matches(driver.current_url, allowed_host):
                    candidates.append((handle, driver.current_url))
            except tracker.WebDriverException:
                pass
        if not candidates:
            print("[TAB ERROR] Open the configured authorized test board in the existing debugging Chrome session.", file=sys.stderr)
            return 2
        if len(candidates) == 1:
            chosen_handle = candidates[0][0]
        else:
            print("Matching authorized tabs:")
            for idx, (_, url) in enumerate(candidates):
                print(f"  {idx}: {tracker.host_for(url)}")
            while True:
                choice = input("Choose tab number: ").strip()
                if choice.isdigit() and int(choice) < len(candidates):
                    chosen_handle = candidates[int(choice)][0]
                    break
        driver.switch_to.window(chosen_handle)
        tracker.check_target(driver.current_url, allowed_host)
        cfg = make_observer_config(args.board_selector)
        installed = driver.execute_script(tracker.JS_INSTALL_OBSERVER, cfg)
        if not isinstance(installed, dict) or not installed.get("boardFound") or not installed.get("gameApiFound"):
            print("[BOARD ERROR] Board/game API was not detected. Configure the board selector and exposed board game API first.", file=sys.stderr)
            return 2
        watch_result = driver.execute_script(INSTALL_LINE_WATCHER, args.board_selector)
        if not isinstance(watch_result, dict) or not watch_result.get("ok"):
            print("[BOARD ERROR] Could not install the right-click line listener.", file=sys.stderr)
            return 2

        configured_path = args.stockfish_path.strip() or tracker.load_optional_txt_setting("stockfish_path") or "stockfish.exe"
        engine_path = Path(configured_path).expanduser()
        if not engine_path.is_absolute():
            engine_path = (tracker.BASE_DIR / engine_path).resolve()
        if not engine_path.is_file():
            print("[ENGINE ERROR] Stockfish executable not found; set stockfish_path in tracker_settings.txt.", file=sys.stderr)
            driver.execute_script("window.__variationLineAnalyzer?.stop?.()")
            return 2
        engine = chess.engine.SimpleEngine.popen_uci(str(engine_path))
        engine.configure({"Threads": 2, "Hash": 128})

        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("Variation Line Analyzer — Authorized Study/Test Board")
        root.geometry("650x560")
        root.minsize(520, 380)
        status = tk.StringVar(value="Ready. Right-click drag an arrow on the board to analyze that hypothetical move.")
        ttk.Label(root, textvariable=status, wraplength=610).pack(fill="x", padx=10, pady=(10, 5))
        controls = ttk.Frame(root)
        controls.pack(fill="x", padx=10, pady=(0, 6))
        output = tk.Text(root, wrap="word", font=("Consolas", 10), height=22)
        scroll = ttk.Scrollbar(root, orient="vertical", command=output.yview)
        output.configure(yscrollcommand=scroll.set)
        output.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scroll.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))

        generated: list[tuple[str, dict]] = []
        last_fen = str((driver.execute_script("return window.__liveGameApiReader.current()") or {}).get("fen") or "")
        busy = False

        def clear_lines() -> None:
            clear_generated(driver, generated)
            status.set("Continuation arrows drawn by this analyzer were cleared.")

        ttk.Button(controls, text="Clear continuation arrows", command=clear_lines).pack(side="left")
        ttk.Label(controls, text=f"Max plies: {args.plies}  |  Search: {args.search_time:.2f}s/position").pack(side="left", padx=12)

        def poll() -> None:
            nonlocal last_fen, busy
            if busy:
                root.after(100, poll)
                return
            try:
                state = driver.execute_script("return window.__liveGameApiReader ? window.__liveGameApiReader.current() : {};") or {}
                current_fen = str(state.get("fen") or "")
                if current_fen and last_fen and current_fen != last_fen:
                    clear_generated(driver, generated)
                    status.set("Board position changed; old continuation arrows cleared.")
                if current_fen:
                    last_fen = current_fen
                queued = driver.execute_script("return window.__variationLineAnalyzer ? window.__variationLineAnalyzer.queue.splice(0) : [];") or []
                for event in queued:
                    busy = True
                    try:
                        analyze_arrow(driver, engine, event, args.plies, args.search_time, generated, output, status)
                    finally:
                        busy = False
            except Exception as exc:
                status.set(f"Listener paused: {type(exc).__name__}")
            root.after(120, poll)

        def close_app() -> None:
            try:
                clear_generated(driver, generated)
                driver.execute_script("window.__variationLineAnalyzer?.stop?.()")
            except Exception:
                pass
            try:
                engine.quit()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

        root.protocol("WM_DELETE_WINDOW", close_app)
        output.insert("end", "Ready. Draw a legal right-click arrow on the authorized board.\n")
        output.insert("end", "The arrow is treated as a hypothetical first move; no move is submitted.\n")
        output.insert("end", "Evaluation is from White's perspective. Captures, checks, and mate are called out per ply.\n\n")
        root.after(120, poll)
        root.mainloop()
        return 0
    except KeyboardInterrupt:
        print("\n[STOP] Analyzer stopped.")
        return 0
    except Exception as exc:
        print(f"[ERROR] Analyzer failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
