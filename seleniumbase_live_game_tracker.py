"""Private-output live chess DOM/API reader with optional owned-site autoplay.

Attaches to an existing Chrome debugging session, reads the exposed game API,
reconstructs moves from FEN changes, and writes a local CSV. Optional Stockfish
autoplay is available on the exact configured site unless its hostname is listed
in blocked_platforms.txt. Blocked hosts are configured in that text file rather
than hard-coded in the Python source. URLs and page titles are never stored in the CSV.
"""
from __future__ import annotations

import argparse
import csv
import json
import ipaddress
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
except ImportError as exc:
    raise SystemExit(
        "Missing Selenium. Install with: python -m pip install -U selenium"
    ) from exc

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


def normalize_host(value: str) -> str:
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


def host_for(url: str | None) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")
    except ValueError:
        return ""


@lru_cache(maxsize=1)
def load_blocked_platforms() -> frozenset[str]:
    """Read blocked hostnames from the neighbouring blocked_platforms.txt file."""
    if not BLOCKED_PLATFORMS_FILE.is_file():
        return frozenset()

    blocked: set[str] = set()
    try:
        lines = BLOCKED_PLATFORMS_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise RuntimeError("Could not read blocked_platforms.txt.") from exc

    for raw_line in lines:
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        host = normalize_host(value)
        if host:
            blocked.add(host)
    return frozenset(blocked)


def is_blocked_platform(host: str) -> bool:
    """Return True only for hosts listed in blocked_platforms.txt (including subdomains)."""
    normalized = normalize_host(host)
    if not normalized:
        return False
    return any(
        normalized == blocked or normalized.endswith("." + blocked)
        for blocked in load_blocked_platforms()
    )


def target_matches(url: str | None, allowed_host: str) -> bool:
    """Match the configured host, excluding hosts listed in blocked_platforms.txt."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = host_for(url)
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if is_blocked_platform(host):
        return False
    return host == allowed_host or host.endswith("." + allowed_host)


def check_target(url: str | None, allowed_host: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise RuntimeError("The selected browser tab is no longer available.")
    if not target_matches(url, allowed_host):
        raise RuntimeError("The selected browser tab no longer matches the saved site settings.")


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
function callMaybe(obj, name) {
  try { return obj && typeof obj[name] === 'function' ? obj[name]() : null; }
  catch (_) { return null; }
}
function readText(selector) {
  if (!selector) return '';
  const el = queryDeep(selector);
  return el ? String(el.innerText || el.textContent || '').trim() : '';
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
  // Adapted from the supplied userscript's move-list lookup: prefer actual
  // SAN move nodes from the page move list before generic data-* nodes.
  const selectors = ['.white.node, .black.node', cfg.moveListSelector, cfg.movesSelector];
  const found = new Map();
  for (const selector of selectors) {
    for (const el of queryAllDeep(selector)) {
      const san = extractSan(el.innerText || el.textContent || '');
      if (!san) continue;
      const ply = el.getAttribute(cfg.plyAttribute) || '';
      const key = ply ? `${ply}:${san}` : san;
      if (!found.has(key)) {
        found.set(key, {ply: ply || String(found.size + 1), text: san});
      }
    }
    if (found.size) break;
  }
  return Array.from(found.values());
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
  return ['getFEN', 'getTurn', 'getPlayingAs', 'getLegalMoves']
    .filter(name => typeof game[name] === 'function');
}
function detectBottomSide(board) {
  if (!board) return {side: 'unknown', source: 'board-not-found'};

  // Prefer explicit semantic orientation attributes when present.
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

  // IMPORTANT: coordinate labels are more direct evidence than a generic
  // `flipped` CSS class, which some board implementations reuse for styling.
  // This follows the supplied userscript's coordinate-based approach.
  for (const coordinates of queryAllDeep('.coordinates')) {
    const children = Array.from(coordinates.children || []);
    const labels = children.map(el => String(el.innerText || el.textContent || '').trim());
    if (labels.length && labels[0] === '1') {
      return {side: 'white', source: 'coordinate-label'};
    }
    if (labels.length && labels[0] === '8') {
      return {side: 'black', source: 'coordinate-label'};
    }
  }

  // Use class-based clues only when coordinate labels are absent.
  const classText = String(board.className?.baseVal || board.className || '').toLowerCase();
  if (/(^|\s)(flipped|orientation-black|black-bottom)(\s|$)/.test(classText)) {
    return {side: 'black', source: 'board-class-fallback'};
  }
  if (/(^|\s)(orientation-white|white-bottom)(\s|$)/.test(classText)) {
    return {side: 'white', source: 'board-class-fallback'};
  }

  return {side: 'unknown', source: 'orientation-not-exposed'};
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
  const isMyTurn = turn == null || playingAs == null
    ? null
    : (String(turn) === String(playingAs));
  return {
    boardFound: !!board,
    gameApiFound: !!(game && (typeof game.getFEN === 'function' || typeof game.getTurn === 'function')),
    apiMethods: getApiMethods(game),
    fen: typeof fen === 'string' ? fen.trim() : '',
    turn,
    playingAs,
    isMyTurn,
    bottomSide: orientation.side,
    bottomSideSource: orientation.source,
    moveCount: moves.length,
    moves,
    legalMoveCount: legalMoves.length,
    legalMoves,
    whiteClock: readText(cfg.whiteClockSelector),
    blackClock: readText(cfg.blackClockSelector),
    result: readText(cfg.resultSelector)
  };
}
const state = {queue: [], lastSignature: null, observer: null, pollTimer: null, lastError: null, snapshot};
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
  attributeFilter: ['class', 'aria-label', 'data-fen', 'data-ply', 'data-time', 'data-result', 'data-turn']
});
window.__liveGameApiReader = {
  observer: state.observer,
  queue: state.queue,
  status: () => ({lastError: state.lastError, pending: state.queue.length}),
  drain: () => state.queue.splice(0, state.queue.length),
  current: () => state.snapshot(),
  playUci: (uci, expectedFen) => {
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
state.pollTimer = setInterval(() => refresh(false), 200);
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
        if (
            after.board_fen() == target.board_fen()
            and after.turn == target.turn
            and after.castling_rights == target.castling_rights
            and after.ep_square == target.ep_square
        ):
            return {
                "san": san,
                "uci": move.uci(),
                "side": "white" if before.turn == chess.WHITE else "black",
                "move_number": str(before.fullmove_number),
            }
    return None


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach to Chrome and read live chess DOM/API state; output hides site identifiers."
    )
    parser.add_argument("--debugger-address", default="127.0.0.1:9223")
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
    parser.add_argument("--white-clock-selector", default=".white.clock, #white-clock")
    parser.add_argument("--black-clock-selector", default=".black.clock, #black-clock")
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
    parser.add_argument("--engine-time", type=float, default=0.20, help="Stockfish time budget per move, seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.auto_play is None:
        auto_setting = load_optional_txt_setting("auto_play").strip().lower()
        args.auto_play = auto_setting in {"1", "true", "yes", "on", "enabled"}
    if args.allowed_domain.strip():
        allowed_host = normalize_host(args.allowed_domain)
        settings_source = "command-line settings"
    else:
        allowed_host, source_name = load_allowed_host_from_txt()
        settings_source = f"{source_name} TXT file" if source_name else "TXT settings"
    if not BLOCKED_PLATFORMS_FILE.is_file():
        print("[CONFIG ERROR] blocked_platforms.txt is missing.", file=sys.stderr)
        print("Restore the file beside this script before starting the tracker.", file=sys.stderr)
        return 2
    try:
        blocked_platforms = load_blocked_platforms()
    except RuntimeError:
        print("[CONFIG ERROR] Could not read blocked_platforms.txt.", file=sys.stderr)
        return 2
    if not allowed_host:
        print("[CONFIG ERROR] Site settings could not be loaded from TXT.", file=sys.stderr)
        print("Add site_domain=your-hostname to tracker_settings.txt.", file=sys.stderr)
        print("The configured hostname is intentionally hidden in terminal output.", file=sys.stderr)
        return 2
    print(f"[CONFIG] Settings loaded from the {settings_source}.", flush=True)
    print(f"[CONFIG] Blocked platform list loaded ({len(blocked_platforms)} hostnames).", flush=True)

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
        "whiteClockSelector": args.white_clock_selector,
        "blackClockSelector": args.black_clock_selector,
        "resultSelector": args.result_selector,
    }

    chosen_handle = None
    engine = None
    last_auto_attempt_fen = ""
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
            if chess is None:
                raise RuntimeError("Autoplay requires python-chess. Install with: python -m pip install python-chess")
            if is_blocked_platform(allowed_host):
                raise RuntimeError("Autoplay is disabled for platforms listed in blocked_platforms.txt.")
            if not installed.get("boardFound") or not installed.get("gameApiFound"):
                raise RuntimeError("Autoplay needs the site's exposed board game API.")
            configured_engine_path = args.stockfish_path.strip() or load_optional_txt_setting("stockfish_path") or "stockfish.exe"
            engine_path = Path(configured_engine_path).expanduser()
            if not engine_path.is_absolute():
                engine_path = (BASE_DIR / engine_path).resolve()
            if not engine_path.is_file():
                raise RuntimeError("Stockfish executable was not found. Set stockfish_path in the TXT settings or use --stockfish-path.")
            try:
                engine = chess.engine.SimpleEngine.popen_uci(str(engine_path))
            except Exception as exc:
                raise RuntimeError("Stockfish could not be started. Check the configured executable.") from exc
            print("[AUTO] Stockfish autoplay enabled for the configured authorized site.", flush=True)
            print(f"[AUTO] Playing the detected bottom side; engine budget={max(0.05, args.engine_time):.2f}s.", flush=True)

        csv_path = Path(args.csv).expanduser().resolve()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not csv_path.exists() or csv_path.stat().st_size == 0
        previous_fen = ""
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
            while True:
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

                for event in events or []:
                    fen = str(event.get("fen") or "").strip()
                    dom_moves = event.get("moves") or []
                    last_move: dict[str, str] | None = None
                    note = "DOM/API state change; identifiers omitted"

                    if fen and fen != previous_fen:
                        if previous_fen:
                            last_move = infer_move_between_fens(previous_fen, fen)
                            if last_move:
                                last_move["game_ply"] = str(len(reconstructed_history) + 1)
                                reconstructed_history.append(last_move)
                                note = "Move reconstructed from consecutive FEN states"
                            elif is_start_position(fen) and not is_start_position(previous_fen):
                                game_number += 1
                                reconstructed_history = []
                                note = "New game detected; move history reset"
                            elif chess is not None:
                                note = "Position changed but transition could not be inferred as one legal move"
                        previous_fen = fen

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
                        "playingAs": event.get("playingAs"),
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
                            f"[LIVE] turn={event.get('turn')} | ply={len(history_copy)} | "
                            f"FEN={'OK' if fen else 'missing'} | "
                            f"API={'OK' if event.get('gameApiFound') else 'missing'} | "
                            f"bottomSide={event.get('bottomSide', 'unknown')} | "
                            f"isMyTurn={event.get('isMyTurn')}",
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

                    # Autoplay operates only when explicitly enabled, the configured
                    # host has already passed the allowlist and public-platform block,
                    # and the detected bottom-side color is to move. Never click by
                    # coordinates; submit through the site's exposed legal-move API.
                    if args.auto_play and engine is not None and fen:
                        bottom = str(event.get("bottomSide") or "unknown").lower()
                        # Prefer the site's own turn-vs-playingAs comparison (the
                        # method used in the supplied userscript). Only fall back
                        # to bottom-side/FEN comparison when the API values are absent.
                        api_my_turn = event.get("isMyTurn")
                        if fen != last_auto_attempt_fen:
                            try:
                                engine_board = chess.Board(fen)
                            except (ValueError, IndexError):
                                engine_board = None
                            if engine_board is not None:
                                if api_my_turn is True:
                                    our_turn = True
                                elif api_my_turn is False:
                                    our_turn = False
                                else:
                                    our_turn = (
                                        bottom in {"white", "black"}
                                        and ((engine_board.turn == chess.WHITE and bottom == "white")
                                             or (engine_board.turn == chess.BLACK and bottom == "black"))
                                    )
                                if our_turn and not engine_board.is_game_over(claim_draw=True):
                                    last_auto_attempt_fen = fen
                                    try:
                                        limit_seconds = max(0.05, min(float(args.engine_time), 5.0))
                                        chosen_move = engine.play(engine_board, chess.engine.Limit(time=limit_seconds)).move
                                        if chosen_move is not None:
                                            submit = driver.execute_script(
                                                "return window.__liveGameApiReader ? window.__liveGameApiReader.playUci(arguments[0], arguments[1]) : {ok:false, reason:'reader-not-installed'};",
                                                chosen_move.uci(), fen,
                                            )
                                            if isinstance(submit, dict) and submit.get("ok"):
                                                try:
                                                    san = engine_board.san(chosen_move)
                                                except Exception:
                                                    san = chosen_move.uci()
                                                print(f"[AUTO] Submitted move for current player: {san} ({chosen_move.uci()}).", flush=True)
                                            else:
                                                reason = submit.get("reason", "move-submit-failed") if isinstance(submit, dict) else "move-submit-failed"
                                                print(f"[AUTO] Move was not submitted ({reason}).", flush=True)
                                    except (chess.engine.EngineError, ValueError, WebDriverException):
                                        print("[AUTO] Move calculation/submission failed; waiting for the next position.", flush=True)
                time.sleep(max(0.05, args.queue_read_interval))

    except KeyboardInterrupt:
        print("\n[STOP] Reader stopped.", flush=True)
    except RuntimeError as exc:
        # These messages are authored by this script and avoid exposing page URLs.
        print(f"[ERROR] {exc}", file=sys.stderr, flush=True)
        return 2
    except WebDriverException:
        print("[ERROR] Browser session or site-tab selection failed. Site details were not logged.", file=sys.stderr, flush=True)
        return 2
    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        try:
            if driver and chosen_handle and chosen_handle in driver.window_handles:
                driver.switch_to.window(chosen_handle)
                driver.execute_script("window.__liveGameApiReader?.stop?.()")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())