"""Small, responsive live chess-coach overlay for the existing Selenium project.

Run the existing tracker first, then run this companion script in a second
terminal. It attaches to the existing local Chrome DevTools page, requires the
tracker's read-only window.__liveGameApiReader, and renders a minimizable panel.
It does not import, patch, or modify the gameplay script.

Only this new file is needed. python-chess and websocket-client are expected to
be available from the existing project dependencies. Stockfish analysis is
optional and read-only: the observed move is analysed as a forced root move;
this file never chooses or submits a chess move.
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

try:
    import chess
    import chess.engine
except ImportError as exc:
    raise SystemExit(
        "Missing python-chess. From the project terminal run: "
        "python -m pip install python-chess"
    ) from exc

try:
    import websocket
except ImportError as exc:
    raise SystemExit(
        "Missing websocket-client. From the project terminal run: "
        "python -m pip install websocket-client"
    ) from exc


BASE_DIR = Path(__file__).resolve().parent
BLOCKED_FILE = BASE_DIR / "blocked_platforms.txt"
POLL_SECONDS = 0.25
DEFAULT_ENGINE_SECONDS = 0.14
MAX_FEED_LINES = 8


def _normalize_host(value: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        return ""
    value = value.removeprefix("*.")
    parsed = urlsplit(value if "://" in value else "//" + value)
    host = (parsed.hostname or "").rstrip(".").removeprefix("www.")
    if not host:
        return ""
    labels = host.split(".")
    public_suffixes = {
        "com", "org", "net", "edu", "gov", "mil", "int", "in", "uk", "io",
        "dev", "app", "au", "ca", "de", "fr", "jp", "us", "co.uk",
        "org.uk", "ac.uk", "gov.uk", "co.in", "org.in", "net.in", "gen.in",
        "firm.in", "ac.in", "edu.in", "gov.in", "com.au", "net.au",
        "org.au", "co.jp", "co.nz",
    }
    if host == "localhost" or host.endswith((".localhost", ".local", ".test")):
        return host
    if len(labels) < 2 or host in public_suffixes or any(not part for part in labels):
        return ""
    return host


def _settings_candidates() -> list[Path]:
    candidates = [
        BASE_DIR / "tracker_settings.txt",
        Path.cwd() / "tracker_settings.txt",
        BASE_DIR / "site_settings.txt",
        Path.cwd() / "site_settings.txt",
        BASE_DIR / "bot_settings.txt",
        Path.cwd() / "bot_settings.txt",
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path.resolve())
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _load_settings() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in _settings_candidates():
        if not path.is_file():
            continue
        try:
            rows = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for raw in rows:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip().lower(), value.strip())
    return values


def _load_blocked_hosts() -> set[str]:
    if not BLOCKED_FILE.is_file():
        raise RuntimeError("blocked_platforms.txt is missing; refusing to attach.")
    blocked: set[str] = set()
    for raw in BLOCKED_FILE.read_text(encoding="utf-8-sig").splitlines():
        item = raw.strip()
        if not item or item.startswith("#"):
            continue
        normalized = _normalize_host(item)
        if normalized:
            blocked.add(normalized)
    return blocked


def _is_blocked(host: str, blocked: set[str]) -> bool:
    normalized = _normalize_host(host)
    return any(
        normalized == entry or normalized.endswith("." + entry)
        for entry in blocked
    )


def _host_allowed(url: str, allowed_host: str, blocked: set[str]) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = _normalize_host(parsed.hostname or "")
    return (
        parsed.scheme in {"http", "https"}
        and bool(host)
        and not _is_blocked(host, blocked)
        and (host == allowed_host or host.endswith("." + allowed_host))
    )


def _find_authorized_page(port: int, allowed_host: str, blocked: set[str]) -> dict:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    try:
        with urlopen(endpoint, timeout=2.0) as response:
            targets = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        raise RuntimeError(
            f"Cannot read Chrome DevTools on 127.0.0.1:{port}. "
            "Start the existing debugging Chrome window first."
        ) from exc

    if not isinstance(targets, list):
        raise RuntimeError("Chrome DevTools returned an unexpected target list.")

    matches = [
        target for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and isinstance(target.get("webSocketDebuggerUrl"), str)
        and target["webSocketDebuggerUrl"].startswith("ws")
        and _host_allowed(str(target.get("url", "")), allowed_host, blocked)
    ]
    if not matches:
        raise RuntimeError(
            "No open browser tab matched the configured authorized site. "
            "Open your own test site in the debugging Chrome window."
        )
    return matches[-1]


class _CDPPage:
    """Tiny synchronous Chrome DevTools Protocol client for one local tab."""

    def __init__(self, target: dict):
        self.socket = websocket.create_connection(
            target["webSocketDebuggerUrl"],
            timeout=2.0,
            suppress_origin=True,
        )
        self.socket.settimeout(2.0)
        self._sequence = 0

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass

    def call(self, method: str, params: dict | None = None, timeout: float = 2.0) -> dict:
        self._sequence += 1
        request_id = self._sequence
        self.socket.settimeout(timeout)
        request = {"id": request_id, "method": method, "params": params or {}}
        self.socket.send(json.dumps(request))
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            message = json.loads(self.socket.recv())
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError("Chrome DevTools command failed.")
            return message.get("result") or {}
        raise TimeoutError("Chrome DevTools command timed out.")

    def evaluate(self, expression: str, timeout: float = 2.0):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": False,
            },
            timeout=timeout,
        )
        remote = result.get("result") or {}
        if result.get("exceptionDetails"):
            raise RuntimeError("Read-only browser script evaluation failed.")
        return remote.get("value")


_STATE_EXPRESSION = r"""(() => {
  const api = window.__liveGameApiReader;
  if (!api || typeof api.current !== 'function') {
    return {attached: false, fen: '', moves: []};
  }
  try {
    const value = api.current() || {};
    const moves = Array.isArray(value.moves)
      ? value.moves.map((item, index) => ({
          ply: String(item && item.ply != null ? item.ply : index + 1),
          text: String(item && item.text != null ? item.text : '')
        })).filter(item => item.text)
      : [];
    return {
      attached: true,
      boardFound: Boolean(value.boardFound),
      gameApiFound: Boolean(value.gameApiFound),
      fen: String(value.fen || ''),
      moves,
      bottomSide: String(value.bottomSide || 'unknown'),
      playingAsColor: String(value.playingAsColor || ''),
      playerNameColor: String(value.playerNameColor || ''),
      turnColor: String(value.turnColor || ''),
      whiteClock: String(value.whiteClock || ''),
      blackClock: String(value.blackClock || ''),
      result: String(value.result || '')
    };
  } catch (_) {
    return {attached: false, fen: '', moves: [], readError: true};
  }
})()"""


_INSTALL_OVERLAY = r"""(() => {
  if (window.__liveChessCoachPanel) return true;

  const host = document.createElement('div');
  host.id = 'live-chess-coach-panel-host';
  host.style.cssText =
    'position:fixed;right:12px;bottom:12px;z-index:2147483646;' +
    'width:min(326px,calc(100vw - 20px));max-width:calc(100vw - 20px);' +
    'font:13px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;' +
    'color-scheme:dark;pointer-events:auto;';
  const shadow = host.attachShadow({mode: 'open'});
  shadow.innerHTML = `
    <style>
      :host { all: initial; }
      * { box-sizing: border-box; }
      .panel {
        width: 100%; max-height: min(45vh, 390px);
        display:flex; flex-direction:column; overflow:hidden;
        background:rgba(18,24,34,.96); color:#edf3fa;
        border:1px solid rgba(180,200,225,.24); border-radius:12px;
        box-shadow:0 8px 30px rgba(0,0,0,.35); backdrop-filter:blur(10px);
        resize:both; min-width:210px; min-height:0;
      }
      .head {
        min-height:36px; display:flex; align-items:center; gap:7px;
        padding:6px 8px 6px 11px; cursor:move; touch-action:none;
        border-bottom:1px solid rgba(180,200,225,.16);
        user-select:none; flex:0 0 auto;
      }
      .brand { font-weight:750; letter-spacing:.2px; white-space:nowrap; }
      .live {
        display:inline-block;width:7px;height:7px;border-radius:100%;
        background:#65d6ad; box-shadow:0 0 0 3px rgba(101,214,173,.12);
      }
      .status {
        min-width:0; flex:1; color:#aebdd0; font-size:10.5px;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
      }
      button {
        font:inherit; color:#eaf2fc; background:#293444;
        border:1px solid rgba(200,215,235,.18); border-radius:6px;
        min-width:26px; height:24px; padding:0 6px; cursor:pointer;
      }
      button:hover { background:#3b4a5f; }
      .body { overflow:auto; padding:8px; min-height:0; }
      .banner {
        font-size:10px; color:#9eacc0; margin:0 0 7px;
        display:flex; justify-content:space-between; gap:8px;
      }
      .feed { display:flex; flex-direction:column; gap:6px; }
      .entry {
        border-left:3px solid #62cdb2; border-radius:5px;
        background:rgba(255,255,255,.045); padding:6px 8px;
        overflow-wrap:anywhere;
      }
      .entry.opponent { border-left-color:#f0bb62; }
      .entry.warning { border-left-color:#ff737d; background:rgba(255,115,125,.09); }
      .entry.info { border-left-color:#83b8ff; }
      .tag { display:block; font-size:9px; font-weight:800; letter-spacing:.55px;
        color:#9ee8d2; margin:0 0 2px; text-transform:uppercase; }
      .opponent .tag { color:#ffd18a; }
      .warning .tag { color:#ff9ca3; }
      .info .tag { color:#a9ceff; }
      .message { white-space:pre-wrap; color:#eef3f9; font-size:12px; }
      .foot {
        display:flex;align-items:center;justify-content:space-between;gap:8px;
        font-size:10px;color:#8e9db0;margin-top:7px;
      }
      :host(.collapsed) .body { display:none; }
      :host(.collapsed) .panel { width:auto; max-height:none; resize:none; }
      :host(.collapsed) .status, :host(.collapsed) #arrows { display:none; }
      .chip {
        display:none; border:1px solid rgba(180,200,225,.28);
        border-radius:999px;background:#17202d;color:#f1f6fd;
        box-shadow:0 4px 18px rgba(0,0,0,.3);padding:8px 12px;
        font-weight:750;cursor:pointer;
      }
      :host(.hidden) .panel { display:none; }
      :host(.hidden) .chip { display:inline-flex; }
      @media (max-width:600px) {
        .panel { max-height:min(36vh,300px); }
        .head { min-height:34px; }
        .body { padding:7px; }
        .message { font-size:11.5px; }
        .panel { resize:none; }
      }
      @media (prefers-reduced-motion:no-preference) {
        .panel, .chip { transition:opacity .12s ease; }
      }
    </style>
    <section class="panel" id="panel">
      <div class="head" id="head">
        <span class="live"></span>
        <span class="brand">Live Coach</span>
        <span class="status" id="status">Waiting for tracker…</span>
        <button id="arrows" type="button" title="Show/hide arrows">↗</button>
        <button id="min" type="button" title="Minimize panel">−</button>
        <button id="hide" type="button" title="Shrink to floating tab">×</button>
      </div>
      <div class="body" id="body">
        <div class="banner"><span id="mode">READ-ONLY ANALYSIS</span><span id="clock"></span></div>
        <div class="feed" id="feed"></div>
        <div class="foot">
          <span id="footnote">Only the observed move is explained.</span>
          <button id="clear" type="button" title="Clear arrows">Clear arrows</button>
        </div>
      </div>
    </section>
    <button class="chip" id="restore" type="button">♟ Live Coach</button>
  `;
  const svgNs = 'http://www.w3.org/2000/svg';
  const arrowsSvg = document.createElementNS(svgNs, 'svg');
  arrowsSvg.id = 'live-chess-coach-arrows';
  arrowsSvg.style.cssText =
    'position:fixed;inset:0;width:100vw;height:100vh;z-index:2147483645;' +
    'pointer-events:none;overflow:visible;';
  document.documentElement.appendChild(arrowsSvg);
  (document.body || document.documentElement).appendChild(host);

  const $ = selector => shadow.querySelector(selector);
  const state = {arrows: [], highlights: [], visuals: true, collapsed: false, hidden: false, orientation:'white'};

  function findBoard() {
    const selectors = ['wc-chess-board', 'chess-board', '#board-single', '[data-board]'];
    const roots = [document];
    for (let i = 0; i < roots.length && i < 80; i++) {
      const root = roots[i];
      for (const selector of selectors) {
        try {
          const found = root.querySelector(selector);
          if (found) return found;
        } catch (_) {}
      }
      try {
        for (const element of root.querySelectorAll('*')) {
          if (element.shadowRoot) roots.push(element.shadowRoot);
          if (roots.length >= 80) break;
        }
      } catch (_) {}
    }
    return null;
  }

  function squareCenter(square, rect, orientation) {
    if (!/^[a-h][1-8]$/.test(square)) return null;
    const file = square.charCodeAt(0) - 97;
    const rank = Number(square[1]);
    let col, row;
    if (orientation === 'black') {
      col = 7 - file;
      row = rank - 1;
    } else {
      col = file;
      row = 8 - rank;
    }
    return {
      x: rect.left + (col + .5) * rect.width / 8,
      y: rect.top + (row + .5) * rect.height / 8,
      x0: rect.left + col * rect.width / 8,
      y0: rect.top + row * rect.height / 8,
      w: rect.width / 8,
      h: rect.height / 8
    };
  }

  function node(tag, attrs) {
    const element = document.createElementNS(svgNs, tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      element.setAttribute(key, String(value));
    }
    return element;
  }

  function redrawArrows() {
    arrowsSvg.replaceChildren();
    if (!state.visuals || (!state.arrows.length && !state.highlights.length)) return;
    const board = findBoard();
    if (!board) return;
    const rect = board.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 20) return;
    arrowsSvg.setAttribute('viewBox', `0 0 ${window.innerWidth} ${window.innerHeight}`);

    const defs = node('defs', {});
    const colors = {played:'#38d9b0', reply:'#ffc766', danger:'#ff6577'};
    for (const [name, color] of Object.entries(colors)) {
      const marker = node('marker', {
        id:'coach-arrow-' + name, markerWidth:7, markerHeight:7,
        refX:5.5, refY:3.5, orient:'auto', markerUnits:'strokeWidth'
      });
      marker.appendChild(node('path', {d:'M0,0 L7,3.5 L0,7 z', fill:color}));
      defs.appendChild(marker);
    }
    arrowsSvg.appendChild(defs);

    for (const item of state.highlights) {
      const center = squareCenter(String(item.square || ''), rect, state.orientation);
      if (!center) continue;
      const color = colors[item.kind] || colors.played;
      arrowsSvg.appendChild(node('rect', {
        x:center.x0 + 1, y:center.y0 + 1, width:center.w - 2, height:center.h - 2,
        rx:3, fill:color, 'fill-opacity':'.16', stroke:color, 'stroke-width':2.5
      }));
    }

    for (const item of state.arrows) {
      const from = squareCenter(String(item.from || ''), rect, state.orientation);
      const to = squareCenter(String(item.to || ''), rect, state.orientation);
      if (!from || !to) continue;
      const kind = colors[item.kind] ? item.kind : 'played';
      const dx = to.x - from.x, dy = to.y - from.y;
      const length = Math.max(1, Math.hypot(dx, dy));
      const shrink = Math.min(12, length * .16);
      const ex = to.x - dx / length * shrink;
      const ey = to.y - dy / length * shrink;
      arrowsSvg.appendChild(node('line', {
        x1:from.x, y1:from.y, x2:ex, y2:ey,
        stroke:colors[kind], 'stroke-width':Math.max(4, Math.min(9, rect.width / 47)),
        'stroke-linecap':'round', 'stroke-opacity':'.88',
        'marker-end':'url(#coach-arrow-' + kind + ')'
      }));
    }
  }

  function renderMessages(messages) {
    const feed = $('#feed');
    feed.replaceChildren();
    for (const item of (Array.isArray(messages) ? messages : []).slice(-8)) {
      const entry = document.createElement('div');
      const kind = ['ours','opponent','warning','info'].includes(item.kind) ? item.kind : 'info';
      entry.className = 'entry ' + (kind === 'ours' ? '' : kind);
      const tag = document.createElement('span');
      tag.className = 'tag';
      tag.textContent = item.label || (
        kind === 'ours' ? 'BOT MOVE' :
        kind === 'opponent' ? 'OPPONENT MOVE' :
        kind === 'warning' ? 'TACTICAL WARNING' : 'COACH'
      );
      const message = document.createElement('div');
      message.className = 'message';
      message.textContent = String(item.text || '');
      entry.append(tag, message);
      feed.appendChild(entry);
    }
  }

  function update(payload) {
    if (!payload || typeof payload !== 'object') return false;
    if (payload.status != null) $('#status').textContent = String(payload.status);
    if (payload.clock != null) $('#clock').textContent = String(payload.clock);
    if (payload.mode != null) $('#mode').textContent = String(payload.mode);
    if (payload.footnote != null) $('#footnote').textContent = String(payload.footnote);
    if (Array.isArray(payload.messages)) renderMessages(payload.messages);
    if (payload.orientation === 'white' || payload.orientation === 'black') {
      state.orientation = payload.orientation;
    }
    if (Array.isArray(payload.arrows)) state.arrows = payload.arrows;
    if (Array.isArray(payload.highlights)) state.highlights = payload.highlights;
    if (payload.clearVisuals) {
      state.arrows = [];
      state.highlights = [];
    }
    redrawArrows();
    return true;
  }

  $('#arrows').addEventListener('click', event => {
    event.stopPropagation();
    state.visuals = !state.visuals;
    $('#arrows').textContent = state.visuals ? '↗' : '↘';
    redrawArrows();
  });
  $('#min').addEventListener('click', event => {
    event.stopPropagation();
    state.collapsed = !state.collapsed;
    host.classList.toggle('collapsed', state.collapsed);
    $('#min').textContent = state.collapsed ? '+' : '−';
    if (state.collapsed) {
      host.style.width = 'max-content';
      $('#panel').style.width = 'fit-content';
      $('#panel').style.resize = 'none';
    } else {
      host.style.width = 'min(326px,calc(100vw - 20px))';
      $('#panel').style.width = '100%';
      $('#panel').style.resize = '';
    }
  });
  $('#hide').addEventListener('click', event => {
    event.stopPropagation();
    host.classList.add('hidden');
    host.style.width = 'max-content';
    state.hidden = true;
  });
  $('#restore').addEventListener('click', () => {
    host.classList.remove('hidden');
    host.style.width = 'min(326px,calc(100vw - 20px))';
    state.hidden = false;
  });
  $('#clear').addEventListener('click', event => {
    event.stopPropagation();
    state.arrows = [];
    state.highlights = [];
    redrawArrows();
  });

  let drag = null;
  $('#head').addEventListener('pointerdown', event => {
    if (event.target.closest('button')) return;
    const rect = host.getBoundingClientRect();
    drag = {x:event.clientX, y:event.clientY, left:rect.left, top:rect.top};
    host.style.left = rect.left + 'px';
    host.style.top = rect.top + 'px';
    host.style.right = 'auto';
    host.style.bottom = 'auto';
    try { $('#head').setPointerCapture(event.pointerId); } catch (_) {}
  });
  $('#head').addEventListener('pointermove', event => {
    if (!drag) return;
    const left = Math.max(4, Math.min(window.innerWidth - 110, drag.left + event.clientX - drag.x));
    const top = Math.max(4, Math.min(window.innerHeight - 42, drag.top + event.clientY - drag.y));
    host.style.left = left + 'px';
    host.style.top = top + 'px';
  });
  const stopDrag = () => { drag = null; };
  $('#head').addEventListener('pointerup', stopDrag);
  $('#head').addEventListener('pointercancel', stopDrag);
  window.addEventListener('resize', redrawArrows);

  window.__liveChessCoachPanel = {update, redraw: redrawArrows};
  return true;
})()"""


def _ui_update(page: _CDPPage, payload: dict) -> None:
    expression = (
        "(() => { const p = window.__liveChessCoachPanel; "
        "return p && typeof p.update === 'function' ? p.update("
        + json.dumps(payload, ensure_ascii=False)
        + ") : false; })()"
    )
    page.evaluate(expression, timeout=1.5)


def _normalise_move_items(raw) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    result = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        result.append({
            "ply": str(item.get("ply") or index + 1),
            "text": text,
        })
    return result


def _move_list_signature(items: list[dict[str, str]]) -> tuple[str, ...]:
    return tuple(item["text"] for item in items)


def _replay(items: list[dict[str, str]]) -> tuple[chess.Board, list[dict]]:
    board = chess.Board()
    records: list[dict] = []
    for index, item in enumerate(items):
        token = item["text"]
        try:
            move = board.parse_san(token)
        except (ValueError, chess.InvalidMoveError, chess.IllegalMoveError):
            try:
                move = chess.Move.from_uci(token)
            except (ValueError, chess.InvalidMoveError):
                break
            if move not in board.legal_moves:
                break
        mover = board.turn
        before = board.copy(stack=False)
        try:
            san = board.san(move)
            board.push(move)
        except (ValueError, AssertionError):
            break
        records.append({
            "index": index,
            "move": move,
            "san": san,
            "mover": mover,
            "before": before,
            "after": board.copy(stack=False),
        })
    return board, records


def _find_move_to_fen(board: chess.Board, target_fen: str) -> chess.Move | None:
    """Conservative one-ply fallback for sites with no usable SAN history."""
    if not target_fen:
        return None
    try:
        target = chess.Board(target_fen)
    except ValueError:
        return None

    target_key = target.fen().split()[:3]
    matches = []
    for move in list(board.legal_moves):
        candidate = board.copy(stack=False)
        candidate.push(move)
        if candidate.fen().split()[:3] == target_key:
            matches.append(move)
    return matches[0] if len(matches) == 1 else None


def _square_name(square: chess.Square) -> str:
    return chess.square_name(square)


def _describe_score(
    info: dict | None,
    perspective: chess.Color,
    perspective_label: str = "bot",
) -> str:
    if not isinstance(info, dict):
        return "Engine evaluation unavailable."
    score_obj = info.get("score")
    if score_obj is None:
        return "Engine evaluation unavailable."
    try:
        score = score_obj.pov(perspective)
        mate = score.mate()
    except (AttributeError, TypeError, ValueError):
        return "Engine evaluation unavailable."

    if mate is not None:
        other_side = "opponent" if perspective_label == "bot" else "doosri side"
        if mate > 0:
            return f"Is line mein engine {perspective_label} ke liye mate in {mate} dikhata hai."
        if mate < 0:
            return f"Is line mein engine {other_side} ke liye mate in {abs(mate)} dikhata hai."
        return "Engine ne position ko mate position score kiya hai."

    try:
        cp = score.score()
    except (AttributeError, TypeError, ValueError):
        cp = None
    if cp is None:
        return "Engine evaluation unavailable."

    pawns = cp / 100.0
    other_side = "opponent" if perspective_label == "bot" else "doosri side"
    if pawns >= 1.2:
        return f"Is played-move line mein {perspective_label} ko lagbhag +{pawns:.1f} pawn ka faayda hai."
    if pawns <= -1.2:
        return f"Is played-move line mein {other_side} ko lagbhag {abs(pawns):.1f} pawn ka faayda hai."
    if pawns >= 0.35:
        return f"Is played-move line mein {perspective_label} ko halka faayda hai (lagbhag +{pawns:.1f})."
    if pawns <= -0.35:
        return f"Is played-move line mein {other_side} ko halka faayda hai (lagbhag {pawns:.1f})."
    return "Is played-move line mein position lagbhag balanced dikh rahi hai."


def _line_san(board: chess.Board, pv: list[chess.Move], max_plies: int = 5) -> str:
    position = board.copy(stack=False)
    parts = []
    for move in pv[:max_plies]:
        if move not in position.legal_moves:
            break
        try:
            parts.append(position.san(move))
            position.push(move)
        except (ValueError, AssertionError):
            break
    return " ".join(parts)


def _move_commentary(
    before: chess.Board,
    move: chess.Move,
    bot_color: chess.Color | None,
    engine,
    engine_seconds: float,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Explain the already-observed move. Never returns a replacement move."""
    mover = before.turn
    is_bot_move = bot_color is not None and mover == bot_color
    side_label = (
        "Hamari move" if is_bot_move
        else "Opponent ki move" if bot_color is not None
        else "Board par move"
    )
    san = before.san(move)
    source, target = _square_name(move.from_square), _square_name(move.to_square)
    lines: list[dict[str, str]] = [{
        "kind": "ours" if is_bot_move else "opponent" if bot_color is not None else "info",
        "label": "BOT MOVE" if is_bot_move else "OPPONENT MOVE" if bot_color is not None else "OBSERVED MOVE",
        "text": f"{side_label}: {san} ({source}→{target}).",
    }]

    squares = [{"square": target, "kind": "played"}]
    arrows = [{"from": source, "to": target, "kind": "played"}]

    if before.is_capture(move):
        captured = before.piece_at(move.to_square)
        if before.is_en_passant(move):
            captured = chess.Piece(chess.PAWN, not before.turn)
        if captured:
            label = captured.symbol().upper()
            captured_side = (
                "opponent ka" if is_bot_move
                else "hamara" if bot_color is not None
                else "doosri side ka"
            )
            lines.append({
                "kind": "info",
                "label": "MATERIAL",
                "text": f"Yeh capture hai: move se {captured_side} {label} piece capture hua.",
            })
    if before.gives_check(move):
        responder = (
            "opponent" if is_bot_move
            else "hamari side" if bot_color is not None
            else "doosri side"
        )
        lines.append({
            "kind": "info",
            "label": "TACTICAL IDEA",
            "text": f"Is move se check diya gaya hai; {responder} ko pehle check ka legal jawab dena hoga.",
        })

    if engine is None:
        lines.append({
            "kind": "info",
            "label": "ENGINE",
            "text": "Stockfish executable nahi mila. Move aur board history dikh rahe hain, lekin predicted line/evaluation available nahi.",
        })
        return lines, arrows, squares

    try:
        info = engine.analyse(
            before,
            chess.engine.Limit(time=max(0.04, min(float(engine_seconds), 0.30)), depth=12),
            root_moves=[move],
        )
    except (chess.engine.EngineError, OSError, ValueError, RuntimeError):
        lines.append({
            "kind": "info",
            "label": "ENGINE",
            "text": "Is move ki engine line is baar available nahi hui.",
        })
        return lines, arrows, squares

    pv = info.get("pv") if isinstance(info, dict) else None
    pv = list(pv) if isinstance(pv, (list, tuple)) else []
    if pv and pv[0] != move:
        pv = []

    if pv:
        line_text = _line_san(before, pv, max_plies=5)
        lines.append({
            "kind": "info",
            "label": "IS MOVE KI CALCULATED LINE",
            "text": line_text or "Line notation available nahi hui.",
        })

        after = before.copy(stack=False)
        after.push(move)
        if len(pv) >= 2 and pv[1] in after.legal_moves:
            reply = pv[1]
            reply_san = after.san(reply)
            reply_from = _square_name(reply.from_square)
            reply_to = _square_name(reply.to_square)
            arrows.append({"from": reply_from, "to": reply_to, "kind": "reply"})
            squares.append({"square": reply_to, "kind": "reply"})

            response_side = (
                "opponent ka likely jawab" if is_bot_move
                else "hamari possible response" if bot_color is not None
                else "agla likely move"
            )
            if after.gives_check(reply):
                lines.append({
                    "kind": "warning",
                    "label": "CHECK WARNING",
                    "text": f"Engine ki line mein {response_side} {reply_san} check hai.",
                })
            elif after.is_capture(reply):
                captured = after.piece_at(reply.to_square)
                if after.is_en_passant(reply):
                    captured = chess.Piece(chess.PAWN, not after.turn)
                if captured:
                    piece_name = {
                        chess.PAWN: "pawn", chess.KNIGHT: "knight",
                        chess.BISHOP: "bishop", chess.ROOK: "rook",
                        chess.QUEEN: "queen", chess.KING: "king",
                    }.get(captured.piece_type, "piece")
                    capture_description = (
                        f"Opponent ka jawab {reply_san} hamara {piece_name} capture karta hai."
                        if is_bot_move else
                        f"Hamari line mein {reply_san} se opponent ka {piece_name} capture ho sakta hai."
                        if bot_color is not None else
                        f"Is line mein {reply_san} ek {piece_name} capture karta hai."
                    )
                    lines.append({
                        "kind": "warning" if is_bot_move else "info",
                        "label": "CAPTURE WARNING" if is_bot_move else "TACTICAL REPLY",
                        "text": capture_description,
                    })
            response_label = (
                "OPPONENT KA LIKELY REPLY" if is_bot_move
                else "HAMARI POSSIBLE RESPONSE" if bot_color is not None
                else "NEXT LIKELY MOVE"
            )
            lines.append({
                "kind": "info",
                "label": response_label,
                "text": f"Is line mein agla move {reply_san} ({reply_from}→{reply_to}) hai. Yeh engine prediction hai, guarantee nahi.",
            })

    perspective = bot_color if bot_color is not None else mover
    perspective_label = "bot" if bot_color is not None else "move khelne wali side"
    lines.append({
        "kind": "info",
        "label": "POSITION ESTIMATE",
        "text": _describe_score(info, perspective, perspective_label),
    })
    if isinstance(info, dict):
        try:
            score = info.get("score")
            mate = score.pov(perspective).mate() if score is not None else None
        except (AttributeError, TypeError, ValueError):
            mate = None
        if mate is not None and bot_color is not None and mate < 0:
            lines.append({
                "kind": "warning",
                "label": "MATE WARNING",
                "text": "Engine ki is short variation mein bot ke khilaaf mate score hai. Ye warning isi played move ki line ke liye hai.",
            })
    return lines, arrows, squares


def _resolve_bot_color(state: dict, settings: dict[str, str]) -> chess.Color | None:
    # Prefer the same per-game side signals that the tracker observes. If a
    # player-name setting is configured, do not fall back to a fixed TXT color:
    # the player can be White in one game and Black in the next.
    for token in (state.get("playingAsColor"), state.get("playerNameColor")):
        normalized = str(token or "").strip().lower()
        if normalized in {"white", "w", "light"}:
            return chess.WHITE
        if normalized in {"black", "b", "dark"}:
            return chess.BLACK

    if settings.get("player_name", "").strip():
        return None

    normalized = str(settings.get("bot_color") or "").strip().lower()
    if normalized in {"white", "w", "light"}:
        return chess.WHITE
    if normalized in {"black", "b", "dark"}:
        return chess.BLACK
    return None


def _open_engine(settings: dict[str, str], override_path: str):
    candidates: list[Path] = []
    raw = override_path.strip() or settings.get("stockfish_path", "").strip()
    if raw:
        raw = raw.strip("\"'")
        expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
        candidates.append(expanded if expanded.is_absolute() else (Path.cwd() / expanded))
    candidates.extend([
        BASE_DIR / "stockfish.exe",
        BASE_DIR / "stockfish",
        BASE_DIR / "stockfish-windows-x86-64-avx2.exe",
        Path.cwd() / "stockfish.exe",
    ])

    discovered = shutil.which("stockfish.exe") or shutil.which("stockfish")
    if discovered:
        candidates.append(Path(discovered))

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_file():
            continue
        try:
            engine = chess.engine.SimpleEngine.popen_uci(str(candidate), timeout=3.0)
            try:
                engine.configure({"Threads": 1, "Hash": 32})
            except (chess.engine.EngineError, chess.engine.EngineTerminatedError):
                pass
            return engine
        except (chess.engine.EngineError, OSError, RuntimeError):
            continue
    return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show a small read-only live chess tutor overlay in the existing Chrome tab."
    )
    parser.add_argument("--debugger-port", type=int, default=9223)
    parser.add_argument("--allowed-domain", default="", help="Optional override; blocked_platforms.txt is still enforced.")
    parser.add_argument("--stockfish-path", default="", help="Optional Stockfish executable path.")
    parser.add_argument("--engine-time", type=float, default=DEFAULT_ENGINE_SECONDS,
                        help="Small per-move analysis budget (0.04–0.30 seconds).")
    parser.add_argument("--poll-interval", type=float, default=POLL_SECONDS)
    parser.add_argument("--no-engine", action="store_true", help="Show observed moves without Stockfish variations.")
    args = parser.parse_args()
    if not 1 <= args.debugger_port <= 65535:
        parser.error("--debugger-port must be between 1 and 65535")
    if args.poll_interval < 0.10 or args.poll_interval > 2.0:
        parser.error("--poll-interval must be between 0.10 and 2 seconds")
    if args.engine_time < 0.04 or args.engine_time > 0.30:
        parser.error("--engine-time must be between 0.04 and 0.30 seconds")
    return args


def _clock_seconds(value: object) -> float | None:
    """Parse the site's visible clock without assuming its exact presentation."""
    raw = str(value or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        if ":" in raw:
            parts = raw.split(":")
            if len(parts) == 2:
                return max(0.0, float(parts[0]) * 60.0 + float(parts[1]))
            if len(parts) == 3:
                return max(
                    0.0,
                    float(parts[0]) * 3600.0
                    + float(parts[1]) * 60.0
                    + float(parts[2]),
                )
        return max(0.0, float(raw.removesuffix("s").strip()))
    except (TypeError, ValueError):
        return None


def _side_clock_gap(state: dict, bot_color: str | None) -> tuple[float | None, float | None]:
    if bot_color not in {"white", "black"}:
        return None, None
    white = _clock_seconds(state.get("whiteClock"))
    black = _clock_seconds(state.get("blackClock"))
    if white is None or black is None:
        return None, None
    own = white if bot_color == "white" else black
    opponent = black if bot_color == "white" else white
    return own, own - opponent


def _launch_wrapped_bot() -> int:
    """Launch the original tracker and tutor its already-selected move pre-submit.

    All patching is done in this new process and this new file. The tracker source
    and strategy source stay unchanged; the wrappers run only immediately before
    the original submit function and always delegate to that exact function.
    """
    parser = argparse.ArgumentParser(
        description="Launch the existing tracker with a read-only live tutor overlay."
    )
    parser.add_argument("--launch-bot", action="store_true")
    parser.add_argument("--explain-pause", type=float, default=0.45,
                        help="Maximum explanation pause in seconds; clock-gap guard can shorten it.")
    parser.add_argument("--clock-margin", type=float, default=0.75,
                        help="Minimum own-clock lead to preserve before adding an explanation pause.")
    parser.add_argument("--coach-engine-time", type=float, default=0.12,
                        help="Maximum Stockfish analysis time for the already-selected move.")
    parser.add_argument("--coach-stockfish-path", default="",
                        help="Optional Stockfish path for tutor analysis (does not choose the bot move).")
    parser.add_argument("--coach-no-engine", action="store_true",
                        help="Show the move arrow and brief commentary without an extra Stockfish search.")
    options, tracker_args = parser.parse_known_args(sys.argv[1:])
    if not options.launch_bot:
        parser.error("Use --launch-bot to start the tracker with pre-move commentary.")
    if not 0.0 <= options.explain_pause <= 1.5:
        parser.error("--explain-pause must be between 0 and 1.5 seconds.")
    if not 0.0 <= options.clock_margin <= 10.0:
        parser.error("--clock-margin must be between 0 and 10 seconds.")
    if not 0.04 <= options.coach_engine_time <= 0.25:
        parser.error("--coach-engine-time must be between 0.04 and 0.25 seconds.")

    if tracker_args and tracker_args[0] == "--":
        tracker_args = tracker_args[1:]

    tracker_path = BASE_DIR / "seleniumbase_live_game_tracker.py"
    if not tracker_path.is_file():
        print("[COACH ERROR] Existing tracker file is missing beside this new file.", file=sys.stderr)
        return 2

    settings = _load_settings()
    tracker_globals: dict = {}
    tutor_engine = None
    last_explanation_at = 0.0

    # Respect an explicitly passed Stockfish path to the existing tracker too.
    tracker_engine_override = ""
    for index, item in enumerate(tracker_args):
        if item == "--stockfish-path" and index + 1 < len(tracker_args):
            tracker_engine_override = tracker_args[index + 1]
        elif item.startswith("--stockfish-path="):
            tracker_engine_override = item.split("=", 1)[1]

    engine_override = options.coach_stockfish_path or tracker_engine_override

    def explain_before_submit(
        driver,
        uci: str,
        expected_fen: str,
        expected_color: str | None,
        *args,
        **kwargs,
    ) -> None:
        nonlocal tutor_engine, last_explanation_at
        if kwargs.get("allow_premove", False):
            # A premove is queued for a future position; its final tactical line
            # cannot be honestly explained as if it were already a legal move.
            return
        if kwargs.get("low_clock_mode", False):
            # In emergency mode do not add an engine search or an intentional pause.
            return
        try:
            board = chess.Board(str(expected_fen))
            move = chess.Move.from_uci(str(uci))
        except (ValueError, TypeError):
            return
        if move not in board.legal_moves:
            return

        try:
            cdp_factory = tracker_globals.get("_direct_cdp_page_for_driver")
            cdp = cdp_factory(driver) if callable(cdp_factory) else None
            if cdp is None:
                return
            state = cdp.evaluate(_STATE_EXPRESSION, timeout=0.6)
            state = state if isinstance(state, dict) else {}
            orientation = str(state.get("bottomSide") or "white")
            if orientation not in {"white", "black"}:
                orientation = "white"

            own_clock, gap = _side_clock_gap(state, expected_color)
            # When the own clock is not comfortably ahead, skip the extra search
            # entirely and render only the move/check/capture information.
            can_spend_analysis = (
                own_clock is None
                or (
                    own_clock > max(5.0, options.clock_margin + 2.0)
                    and (gap is None or gap > options.clock_margin + options.coach_engine_time + 0.1)
                )
            )
            active_engine = None
            if not options.coach_no_engine and can_spend_analysis:
                if tutor_engine is None:
                    tutor_engine = _open_engine(settings, engine_override)
                active_engine = tutor_engine

            lines, arrows, highlights = _move_commentary(
                board,
                move,
                chess.WHITE if expected_color == "white"
                else chess.BLACK if expected_color == "black"
                else None,
                active_engine,
                options.coach_engine_time,
            )
            _INSTALL_OVERLAY_RESULT = cdp.evaluate(_INSTALL_OVERLAY, timeout=0.8)
            payload = {
                "status": "Explaining selected move before execution",
                "mode": "SELECTED MOVE • READ-ONLY COACH",
                "clock": (
                    f"W {state.get('whiteClock') or '—'}  ·  B {state.get('blackClock') or '—'}"
                ),
                "footnote": "Short pre-move explanation. Existing move selection remains unchanged.",
                "messages": lines[-MAX_FEED_LINES:],
                "arrows": arrows,
                "highlights": highlights,
                "orientation": orientation,
            }
            _ui_update(cdp, payload)
            last_explanation_at = time.monotonic()

            # Re-read clocks after the bounded engine work and drawing. Pause only
            # while a measurable lead remains beyond the user's safety margin.
            if active_engine is None or options.explain_pause <= 0:
                return
            latest = cdp.evaluate(_STATE_EXPRESSION, timeout=0.5)
            if not isinstance(latest, dict):
                return
            _, latest_gap = _side_clock_gap(latest, expected_color)
            if latest_gap is None:
                return
            allowed_pause = min(
                options.explain_pause,
                max(0.0, latest_gap - options.clock_margin),
            )
            if allowed_pause > 0:
                time.sleep(allowed_pause)
        except Exception as exc:
            # Coaching failure must not replace, cancel, or alter the chosen move.
            print(
                f"[COACH] Pre-move explanation skipped ({type(exc).__name__}); "
                "continuing with the existing selected move.",
                flush=True,
            )

    try:
        tracker_globals = runpy.run_path(
            str(tracker_path),
            run_name="live_game_tracker_module",
        )
        # The original main() resolves these function names from its own global
        # namespace. Replacing only these call points here lets us explain the
        # same chosen UCI immediately before the original submission function.
        original_cursor = tracker_globals.get("perform_cursor_drag")
        original_api = tracker_globals.get("perform_api_move")
        if not callable(original_cursor) or not callable(original_api):
            raise RuntimeError("Tracker move-submit functions were not available.")

        def wrapped_cursor(driver, uci, expected_fen, expected_color,
                           allow_premove=False, low_clock_mode=False):
            if not allow_premove:
                explain_before_submit(
                    driver, uci, expected_fen, expected_color,
                    allow_premove=False, low_clock_mode=low_clock_mode,
                )
            return original_cursor(
                driver, uci, expected_fen, expected_color,
                allow_premove=allow_premove,
                low_clock_mode=low_clock_mode,
            )

        def wrapped_api(driver, uci, expected_fen, expected_color, *,
                        low_clock_mode=False, source="api"):
            if not low_clock_mode:
                explain_before_submit(
                    driver, uci, expected_fen, expected_color,
                    allow_premove=False, low_clock_mode=low_clock_mode,
                )
            return original_api(
                driver, uci, expected_fen, expected_color,
                low_clock_mode=low_clock_mode,
                source=source,
            )

        tracker_globals["perform_cursor_drag"] = wrapped_cursor
        tracker_globals["perform_api_move"] = wrapped_api

        original_argv = sys.argv[:]
        sys.argv = [str(tracker_path), *tracker_args]
        print("[COACH] Launching the original tracker with read-only pre-move explanations.")
        print("[COACH] No move selector is wrapped; the selected UCI is passed unchanged to the original submit function.")
        print("[COACH] Premove queues and low-clock emergency moves skip the intentional explanation wait.")
        try:
            return int(tracker_globals["main"]() or 0)
        finally:
            sys.argv = original_argv
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 0
    except Exception as exc:
        print(f"[COACH ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if tutor_engine is not None:
            try:
                tutor_engine.quit()
            except Exception:
                pass


def main() -> int:
    if "--launch-bot" in sys.argv[1:]:
        return _launch_wrapped_bot()
    args = _parse_args()
    settings = _load_settings()
    try:
        blocked = _load_blocked_hosts()
    except (RuntimeError, OSError) as exc:
        print(f"[COACH ERROR] {exc}", file=sys.stderr)
        return 2

    allowed_host = _normalize_host(args.allowed_domain) if args.allowed_domain else ""
    if not allowed_host:
        for key in ("site_domain", "allowed_domain", "domain", "site_url", "url"):
            allowed_host = _normalize_host(settings.get(key, ""))
            if allowed_host:
                break
    if not allowed_host:
        print("[COACH ERROR] Configured site host was not found in tracker_settings.txt.", file=sys.stderr)
        print("[COACH ERROR] No browser connection was made.", file=sys.stderr)
        return 2
    if _is_blocked(allowed_host, blocked):
        print("[COACH ERROR] Configured site matches the blocklist; no browser connection was made.", file=sys.stderr)
        return 2

    try:
        target = _find_authorized_page(args.debugger_port, allowed_host, blocked)
    except RuntimeError as exc:
        print(f"[COACH ERROR] {exc}", file=sys.stderr)
        return 2

    engine = None if args.no_engine else _open_engine(settings, args.stockfish_path)
    page = None
    try:
        page = _CDPPage(target)
        page.call("Runtime.enable")
        page.evaluate(_INSTALL_OVERLAY, timeout=2.0)
        _ui_update(page, {
            "status": "Waiting for live game reader…",
            "mode": "READ-ONLY • NO MOVE INPUT",
            "footnote": "Panel arrows are visual overlays; gameplay input is untouched.",
            "messages": [{
                "kind": "info",
                "label": "READY",
                "text": "Existing tracker ki read-only page state ka wait ho raha hai. Original bot ko alag terminal mein chalte rehne do.",
            }],
            "arrows": [],
            "highlights": [],
            "orientation": "white",
        })
        print("[COACH] Panel injected into the authorized debugging tab.")
        print("[COACH] Gameplay files unchanged. Press Ctrl+C to remove the overlay and exit.")
        print(f"[COACH] Stockfish analysis: {'enabled' if engine is not None else 'disabled (move text only)'}.")

        previous_entries: list[dict[str, str]] | None = None
        previous_fen = ""
        previous_board: chess.Board | None = None
        already_explained: set[tuple[str, str]] = set()
        feed: list[dict[str, str]] = []
        current_orientation = "white"
        last_status_push = 0.0
        last_overlay_check = 0.0

        while True:
            try:
                if time.monotonic() - last_overlay_check >= 1.5:
                    overlay_ready = bool(
                        page.evaluate("Boolean(window.__liveChessCoachPanel)", timeout=1.0)
                    )
                    if not overlay_ready:
                        page.evaluate(_INSTALL_OVERLAY, timeout=1.5)
                        _ui_update(page, {
                            "status": "Panel restored after page update",
                            "mode": "READ-ONLY • NO MOVE INPUT",
                            "footnote": "Overlay restored; gameplay input is untouched.",
                        })
                    last_overlay_check = time.monotonic()
                state = page.evaluate(_STATE_EXPRESSION, timeout=1.5)
            except Exception:
                _ui_update(page, {"status": "Reconnecting to tracker state…"})
                time.sleep(args.poll_interval)
                continue

            if not isinstance(state, dict) or not state.get("attached"):
                now = time.monotonic()
                if now - last_status_push > 1.5:
                    _ui_update(page, {
                        "status": "Start existing tracker first",
                        "mode": "WAITING",
                        "footnote": "No move is submitted by this panel.",
                    })
                    last_status_push = now
                time.sleep(args.poll_interval)
                continue

            fen = str(state.get("fen") or "").strip()
            entries = _normalise_move_items(state.get("moves"))
            current_orientation = (
                str(state.get("bottomSide") or "white")
                if str(state.get("bottomSide") or "white") in {"white", "black"}
                else "white"
            )
            bot_color = _resolve_bot_color(state, settings)
            clock = (
                f"W {state.get('whiteClock') or '—'}  ·  B {state.get('blackClock') or '—'}"
            )

            try:
                board_from_history, records = _replay(entries)
                history_ok = len(records) == len(entries)
            except Exception:
                board_from_history, records, history_ok = chess.Board(), [], False

            if previous_entries is None:
                previous_entries = entries
                previous_fen = fen
                previous_board = board_from_history if history_ok and entries else None
                if previous_board is None and fen:
                    try:
                        previous_board = chess.Board(fen)
                    except ValueError:
                        previous_board = None
                _ui_update(page, {
                    "status": "Connected • waiting for next move",
                    "mode": "READ-ONLY • NO MOVE INPUT",
                    "clock": clock,
                    "footnote": "Tutor explains moves after they appear on the board.",
                    "messages": [{
                        "kind": "info",
                        "label": "CONNECTED",
                        "text": "Live board history mil gayi. Agli move ke baad selected/observed move ki line aur predicted reply yahan aayenge.",
                    }],
                    "orientation": current_orientation,
                })
                last_status_push = time.monotonic()
                time.sleep(args.poll_interval)
                continue

            old_signature = _move_list_signature(previous_entries)
            new_signature = _move_list_signature(entries)
            new_records: list[dict] = []
            common = 0
            while (
                common < min(len(old_signature), len(new_signature))
                and old_signature[common] == new_signature[common]
            ):
                common += 1

            history_changed = old_signature != new_signature
            is_simple_append_or_last_correction = (
                history_changed
                and common >= max(0, len(old_signature) - 1)
                and len(new_signature) >= common
            )
            is_append = history_changed and (
                len(new_signature) >= len(old_signature)
                and common == len(old_signature)
            )

            if history_changed and history_ok and (is_append or is_simple_append_or_last_correction):
                new_records = [record for record in records if record["index"] >= common]
                if len(new_records) > 3:
                    new_records = new_records[-3:]
            elif history_changed and (
                len(entries) < len(previous_entries)
                or (common == 0 and bool(old_signature) and bool(new_signature))
            ):
                feed.clear()
                already_explained.clear()
                previous_board = board_from_history if history_ok and entries else None
                _ui_update(page, {
                    "status": "New game/history reset",
                    "mode": "READ-ONLY",
                    "messages": [{
                        "kind": "info",
                        "label": "NEW GAME",
                        "text": "Move history reset detect hua. Ab nayi game ki moves explain hongi.",
                    }],
                    "arrows": [],
                    "highlights": [],
                    "orientation": current_orientation,
                })
            elif not history_changed and fen and fen != previous_fen and previous_board is not None:
                inferred = _find_move_to_fen(previous_board, fen)
                if inferred is not None:
                    san = previous_board.san(inferred)
                    after = previous_board.copy(stack=False)
                    mover = after.turn
                    after.push(inferred)
                    new_records = [{
                        "index": len(entries),
                        "move": inferred,
                        "san": san,
                        "mover": mover,
                        "before": previous_board.copy(stack=False),
                        "after": after.copy(stack=False),
                    }]
                else:
                    _ui_update(page, {
                        "status": "Position changed • move text unavailable",
                        "mode": "READ-ONLY",
                        "footnote": "Waiting for the page to expose the move history.",
                    })

            for record in new_records:
                move = record["move"]
                before = record["before"]
                explanation_key = (before.fen(), move.uci())
                if explanation_key in already_explained:
                    continue
                already_explained.add(explanation_key)
                if len(already_explained) > 256:
                    already_explained = set(list(already_explained)[-128:])
                try:
                    lines, arrows, highlights = _move_commentary(
                        before,
                        move,
                        bot_color,
                        engine,
                        args.engine_time,
                    )
                except Exception:
                    lines = [{
                        "kind": "info",
                        "label": "OBSERVED MOVE",
                        "text": f"Move {record['san']} detect hui. Detailed analysis is unavailable for this move.",
                    }]
                    arrows = [{
                        "from": _square_name(move.from_square),
                        "to": _square_name(move.to_square),
                        "kind": "played",
                    }]
                    highlights = [{"square": _square_name(move.to_square), "kind": "played"}]

                feed.extend(lines)
                feed = feed[-MAX_FEED_LINES:]
                _ui_update(page, {
                    "status": "Bot move explained" if bot_color is not None and record["mover"] == bot_color else "Move explained",
                    "mode": "ANALYSIS OF THE MOVE ACTUALLY PLAYED",
                    "clock": clock,
                    "messages": feed,
                    "arrows": arrows,
                    "highlights": highlights,
                    "orientation": current_orientation,
                    "footnote": "Analysis is read-only. It never changes or submits a move.",
                })
                last_status_push = time.monotonic()

            if fen:
                try:
                    previous_board = board_from_history if history_ok and entries else chess.Board(fen)
                except ValueError:
                    pass
            previous_entries = entries
            previous_fen = fen
            if time.monotonic() - last_status_push > 1.5:
                _ui_update(page, {
                    "status": "Live • waiting for move",
                    "clock": clock,
                    "orientation": current_orientation,
                })
                last_status_push = time.monotonic()

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print("\n[COACH] Stopping panel.")
    except Exception as exc:
        print(f"[COACH ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        if page is not None:
            try:
                page.evaluate(
                    "(() => { try { window.__liveChessCoachPanel = null; "
                    "document.getElementById('live-chess-coach-panel-host')?.remove(); "
                    "document.getElementById('live-chess-coach-arrows')?.remove(); return true; } "
                    "catch (_) { return false; } })()",
                    timeout=1.0,
                )
            except Exception:
                pass
            page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
