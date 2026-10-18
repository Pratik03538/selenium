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
let suppress = false;
let overlay = null;
let overlayHost = null;
let originalHostPosition = '';
let changedHostPosition = false;
const svgNS = 'http://www.w3.org/2000/svg';
const boardSelectors = Array.from(new Set([
  selector, 'chess-board', 'wc-chess-board', '#board-single'
].filter(Boolean)));
function matchesBoard(node) {
  if (!node || typeof node.matches !== 'function') return false;
  return boardSelectors.some(sel => {
    try { return node.matches(sel); } catch (_) { return false; }
  });
}
function findBoard(root = document) {
  // Match the same board fallback order used by the existing tracker, and
  // search open shadow roots because many chess boards are web components.
  const roots = [root];
  const visitedRoots = new Set();
  while (roots.length) {
    const current = roots.shift();
    if (!current || visitedRoots.has(current)) continue;
    visitedRoots.add(current);
    for (const sel of boardSelectors) {
      try {
        for (const candidate of current.querySelectorAll(sel)) {
          const r = candidate.getBoundingClientRect();
          if (r.width > 8 && r.height > 8) return candidate;
        }
      } catch (_) {}
    }
    let elements = [];
    try { elements = current.querySelectorAll('*'); } catch (_) {}
    for (const el of elements) {
      if (el.shadowRoot && !visitedRoots.has(el.shadowRoot)) roots.push(el.shadowRoot);
    }
  }
  return null;
}
function resolveBottomSide(board, state = {}) {
  const apiSide = String(state.bottomSide || '').trim().toLowerCase();
  if (apiSide === 'white' || apiSide === 'black') return apiSide;
  let node = board;
  for (let depth=0; node && depth<6; depth++) {
    for (const attr of ['data-orientation', 'data-board-orientation', 'orientation']) {
      const value = String(node.getAttribute?.(attr) || '').trim().toLowerCase();
      if (value === 'white' || value === 'black') return value;
    }
    const tokens = String(node.className?.baseVal || node.className || '').toLowerCase().split(/\s+/);
    if (tokens.some(t => ['flipped','orientation-black','black-bottom'].includes(t))) return 'black';
    if (tokens.some(t => ['orientation-white','white-bottom'].includes(t))) return 'white';
    node = node.parentElement || node.getRootNode?.().host || null;
  }
  const roots = [board];
  if (board?.shadowRoot) roots.push(board.shadowRoot);
  const visited = new Set();
  for (let i=0; i<roots.length; i++) {
    const root = roots[i];
    if (!root || visited.has(root)) continue;
    visited.add(root);
    try {
      for (const el of root.querySelectorAll('*')) if (el.shadowRoot) roots.push(el.shadowRoot);
      for (const coords of root.querySelectorAll('.coordinates')) {
        let y1 = null, y8 = null;
        for (const child of Array.from(coords.children || [])) {
          const label = String(child.innerText || child.textContent || '').trim();
          if (label !== '1' && label !== '8') continue;
          const r = child.getBoundingClientRect?.();
          if (!r || !r.width || !r.height) continue;
          const y = r.top + r.height/2;
          if (label === '1') y1 = y;
          if (label === '8') y8 = y;
        }
        if (y1 !== null && y8 !== null && Math.abs(y1-y8) > 20) return y1 > y8 ? 'white' : 'black';
      }
    } catch (_) {}
  }
  return '';
}
function clearLabels() {
  if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
  overlay = null;
  overlayHost = null;
  changedHostPosition = false;
}
function ensureOverlay() {
  const board = findBoard();
  if (!board) return {ok:false, reason:'board-element-not-found'};
  const rect = board.getBoundingClientRect();
  if (rect.width < 8 || rect.height < 8) return {ok:false, reason:'board-has-no-visible-size'};
  if (!overlay || !overlay.isConnected || overlayHost !== board) {
    clearLabels();
    overlayHost = board;
    overlay = document.createElementNS(svgNS, 'svg');
    overlay.setAttribute('data-variation-line-labels', 'true');
    // A viewport-fixed SVG above the page works for open/closed web-component
    // shadow roots without changing any existing board DOM or CSS layout.
    overlay.style.position = 'fixed';
    overlay.style.left = rect.left + 'px';
    overlay.style.top = rect.top + 'px';
    overlay.style.margin = '0';
    overlay.style.padding = '0';
    overlay.style.width = rect.width + 'px';
    overlay.style.height = rect.height + 'px';
    overlay.style.overflow = 'hidden';
    overlay.style.pointerEvents = 'none';
    overlay.style.zIndex = '2147483647';
    (document.body || document.documentElement).appendChild(overlay);
  }
  overlay.style.left = rect.left + 'px';
  overlay.style.top = rect.top + 'px';
  overlay.style.width = rect.width + 'px';
  overlay.style.height = rect.height + 'px';
  overlay.setAttribute('viewBox', '0 0 ' + rect.width + ' ' + rect.height);
  overlay.setAttribute('width', String(rect.width));
  overlay.setAttribute('height', String(rect.height));
  return {ok:true, board, rect, svg:overlay};
}
function drawLabels(items, details = [], statusText = '') {
  const setup = ensureOverlay();
  if (!setup || !setup.ok) return {ok:false, reason:setup?.reason || 'board-label-layer-unavailable'};
  const {board, rect, svg} = setup;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const state = api.current() || {};
  const side = resolveBottomSide(board, state);
  if (!['white','black'].includes(side) && Array.isArray(items) && items.length) {
    return {ok:false, reason:'board-orientation-unknown'};
  }
  const make = (tag, attrs, textValue) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    if (textValue !== undefined) node.textContent = String(textValue);
    return node;
  };
  const squarePoint = (square) => {
    const file = square.charCodeAt(0)-97;
    const rank = Number(square[1]);
    const boardFile = side === 'white' ? file : 7-file;
    const boardRow = side === 'white' ? 8-rank : rank-1;
    return {x:(boardFile+0.5)*rect.width/8, y:(boardRow+0.5)*rect.height/8};
  };
  const palette = ['#1f2937','#374151','#4b5563','#111827'];
  for (const item of (Array.isArray(items) ? items : [])) {
    const uci = String(item.uci || '');
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) continue;
    const a = squarePoint(uci.slice(0,2)), b = squarePoint(uci.slice(2,4));
    const x = (a.x+b.x)/2, y = (a.y+b.y)/2;
    const radius = Math.max(11, Math.min(rect.width,rect.height)/34);
    const group = make('g', {'data-ply':item.number || ''});
    group.appendChild(make('circle',{cx:x,cy:y,r:radius,fill:palette[(Number(item.number||1)-1)%palette.length],stroke:'#ffffff','stroke-width':1.5,opacity:0.96}));
    group.appendChild(make('text',{x:x,y:y+4,'text-anchor':'middle','font-family':'Segoe UI, Arial, sans-serif','font-size':Math.max(10,radius*1.05),'font-weight':'700',fill:'#ffffff'},item.number || ''));
    const shortEval = String(item.eval || '');
    if (shortEval) {
      const labelX = Math.min(rect.width-5, Math.max(5, x+radius+3));
      const labelY = Math.min(rect.height-5, Math.max(15, y-radius-2));
      const textWidth = Math.min(92, Math.max(34, shortEval.length*6.5+10));
      const left = labelX + textWidth > rect.width ? Math.max(2,labelX-textWidth-radius*2) : labelX;
      group.appendChild(make('rect',{x:left,y:labelY-12,width:textWidth,height:20,rx:5,fill:'#111827',stroke:'#ffffff','stroke-width':0.8,opacity:0.94}));
      group.appendChild(make('text',{x:left+textWidth/2,y:labelY+2,'text-anchor':'middle','font-family':'Segoe UI, Arial, sans-serif','font-size':10,'font-weight':'600',fill:'#ffffff'},shortEval));
    }
    svg.appendChild(group);
  }

  // Compact results panel is part of the board's own SVG layer, not a separate window.
  const lines = Array.isArray(details) ? details : [];
  const pad = 6;
  const rowHeight = Math.max(11, Math.min(16, rect.height / 34));
  const rowCount = Math.ceil(Math.min(lines.length, 16) / 2);
  const panelHeight = lines.length
    ? Math.min(rect.height * 0.42, 24 + rowCount * rowHeight + 7)
    : 0;
  if (lines.length) {
    const panelY = rect.height - panelHeight - 3;
    svg.appendChild(make('rect', {
      x:3, y:panelY, width:rect.width-6, height:panelHeight,
      rx:5, fill:'#111827', 'fill-opacity':0.93, stroke:'#f9fafb',
      'stroke-width':0.8, 'stroke-opacity':0.85
    }));
    const title = String(statusText || 'HYPOTHETICAL LINE').slice(0, 90);
    svg.appendChild(make('text', {
      x:pad+2, y:panelY+13, 'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(9, Math.min(11, rect.width/42)),
      'font-weight':'700', fill:'#ffffff'
    }, title));
    const colWidth = (rect.width - 2*pad - 8) / 2;
    for (let i = 0; i < Math.min(lines.length, 16); i++) {
      const row = Math.floor(i/2);
      const col = i%2;
      const value = String(lines[i] || '').slice(0, 58);
      svg.appendChild(make('text', {
        x:pad+col*(colWidth+8), y:panelY+25+row*rowHeight,
        'font-family':'Consolas, monospace',
        'font-size':Math.max(8, Math.min(10, rect.width/48)),
        'font-weight':i===0?'700':'500',
        fill:'#f9fafb'
      }, value));
    }
  } else if (statusText) {
    const label = String(statusText).slice(0, 95);
    const fontSize = Math.max(10, Math.min(13, rect.width/38));
    const textWidth = Math.min(rect.width-8, Math.max(160, label.length*fontSize*0.58+18));
    svg.appendChild(make('rect', {
      x:4, y:4, width:textWidth, height:fontSize+12,
      rx:5, fill:'#111827', 'fill-opacity':0.93,
      stroke:'#f9fafb', 'stroke-width':0.8
    }));
    svg.appendChild(make('text', {
      x:12, y:fontSize+8, 'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':fontSize, 'font-weight':'700', fill:'#ffffff'
    }, label));
  }
  return {ok:true, count:svg.childNodes.length, boardFound:true};
}
function drawSuggestion(item) {
  const setup = ensureOverlay();
  if (!setup || !setup.ok) return {ok:false, reason:setup?.reason || 'board-label-layer-unavailable'};
  const {board, rect, svg} = setup;
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const state = api.current() || {};
  const side = resolveBottomSide(board, state);
  if (!['white','black'].includes(side)) return {ok:false, reason:'board-orientation-unknown'};

  const uci = String(item?.uci || '').toLowerCase();
  if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) return {ok:false, reason:'invalid-suggested-move'};
  const make = (tag, attrs, textValue) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    if (textValue !== undefined) node.textContent = String(textValue);
    return node;
  };
  const squarePoint = (square) => {
    const file = square.charCodeAt(0)-97;
    const rank = Number(square[1]);
    const boardFile = side === 'white' ? file : 7-file;
    const boardRow = side === 'white' ? 8-rank : rank-1;
    return {x:(boardFile+0.5)*rect.width/8, y:(boardRow+0.5)*rect.height/8};
  };
  const a = squarePoint(uci.slice(0,2));
  const b = squarePoint(uci.slice(2,4));
  const dx = b.x-a.x, dy = b.y-a.y;
  const length = Math.max(1, Math.hypot(dx,dy));
  const ux = dx/length, uy = dy/length;
  const inset = Math.min(rect.width,rect.height)/19;
  const x2 = b.x-ux*inset, y2 = b.y-uy*inset;
  const opacity = 0.28; // intentionally faint so the position remains visible.
  const markerId = 'variation-next-arrow-head';

  const defs = make('defs');
  defs.appendChild(make('marker', {
    id:markerId, markerWidth:9, markerHeight:9,
    refX:7, refY:4.5, orient:'auto',
    markerUnits:'userSpaceOnUse'
  }));
  defs.firstChild.appendChild(make('path',{d:'M0,0 L9,4.5 L0,9 z',fill:'#2563eb',opacity}));
  svg.appendChild(defs);

  // Soft translucent line only: it does not intercept mouse/right-click input.
  svg.appendChild(make('line', {
    x1:a.x, y1:a.y, x2, y2,
    stroke:'#2563eb', 'stroke-width':Math.max(4,rect.width/90),
    'stroke-linecap':'round', 'stroke-dasharray':'5 5',
    opacity, 'marker-end':'url(#'+markerId+')'
  }));
  svg.appendChild(make('circle',{
    cx:a.x,cy:a.y,r:Math.max(5,rect.width/100),
    fill:'#2563eb',opacity:opacity*0.8
  }));

  // A small translucent tag confirms the move and evaluation without listing
  // the rest of the PV; the next move is only calculated after the user traces it.
  const tagText = String(item?.san || uci);
  const evalText = String(item?.eval || '');
  const deltaText = String(item?.delta || '');
  const statusText = String(item?.title || 'NEXT MOVE');
  const noteText = String(item?.note || '');
  const tagWidth = Math.min(rect.width-10, Math.max(70, (tagText.length+evalText.length+4)*6.2));
  const tagHeight = Math.min(70, Math.max(noteText ? 57 : 43, rect.height*0.14));
  let tagX = b.x + 7;
  if (tagX+tagWidth > rect.width-3) tagX = b.x-tagWidth-7;
  tagX = Math.max(3,tagX);
  let tagY = b.y-tagHeight-7;
  if (tagY < 3) tagY = Math.min(rect.height-tagHeight-3,b.y+7);
  const group = make('g', {'data-suggested-move':uci});
  group.appendChild(make('rect',{
    x:tagX,y:tagY,width:tagWidth,height:tagHeight,rx:5,
    fill:'#dbeafe','fill-opacity':0.83,
    stroke:'#2563eb','stroke-width':1,opacity:0.93
  }));
  group.appendChild(make('text',{
    x:tagX+6,y:tagY+12,'font-family':'Segoe UI, Arial, sans-serif',
    'font-size':Math.max(9,Math.min(11,rect.width/42)),
    'font-weight':'700',fill:'#1e3a8a'
  },statusText.slice(0,60)));
  group.appendChild(make('text',{
    x:tagX+6,y:tagY+25,'font-family':'Segoe UI, Arial, sans-serif',
    'font-size':Math.max(10,Math.min(13,rect.width/36)),
    'font-weight':'700',fill:'#1e40af'
  },tagText+' · '+evalText));
  group.appendChild(make('text',{
    x:tagX+6,y:tagY+37,'font-family':'Segoe UI, Arial, sans-serif',
    'font-size':Math.max(8,Math.min(10,rect.width/48)),
    'font-weight':'600',fill:'#1e3a8a'
  },deltaText.slice(0,60)));
  if (noteText) {
    group.appendChild(make('text',{
      x:tagX+6,y:tagY+49,'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(8,Math.min(10,rect.width/48)),
      'font-weight':'600',fill:'#1e3a8a'
    },noteText.slice(0,60)));
  }
  svg.appendChild(group);
  return {ok:true, uci, boardFound:true};
}
function matchingBoard(event) {
  const path = typeof event.composedPath === 'function' ? event.composedPath() : [];
  for (const node of path) {
    if (matchesBoard(node)) return node;
  }
  // Some wrappers omit the custom-element host from composedPath; fall back to
  // hit-testing and climb through parent/shadow-host boundaries.
  let node = null;
  try { node = document.elementFromPoint(event.clientX, event.clientY); } catch (_) {}
  for (let depth=0; node && depth<18; depth++) {
    if (matchesBoard(node)) return node;
    node = node.parentElement || node.getRootNode?.().host || null;
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
function isRightButton(event) {
  return event.button === 2 || event.which === 3;
}
function onDown(event) {
  if (suppress) { down = null; return; }
  if (!isRightButton(event)) return;
  const board = matchingBoard(event);
  if (!board) { down = null; return; }
  const state = api.current() || {};
  const side = resolveBottomSide(board, state);
  if (!['white','black'].includes(side)) {
    down = null;
    drawLabels([], [], 'Board orientation not detected · check board settings');
    return;
  }
  const rect = board.getBoundingClientRect();
  const from = squareAt(board, event.clientX, event.clientY, side);
  if (!from) { down = null; return; }
  down = {
    board, from, fen: String(state.fen || ''), side,
    rect: {left:rect.left,top:rect.top,width:rect.width,height:rect.height,bottom_side:side}
  };
}
function onUp(event) {
  if (suppress) { down = null; return; }
  if (!down || !isRightButton(event)) return;
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
  drawLabels([], [], 'Arrow detected: ' + saved.from + ' → ' + to + ' · analyzing…');
}
function onContextMenu(event) {
  if (matchingBoard(event)) event.preventDefault();
}
window.addEventListener('pointerdown', onDown, true);
window.addEventListener('pointerup', onUp, true);
window.addEventListener('contextmenu', onContextMenu, true);
window.__variationLineAnalyzer = {
  queue,
  drawLabels,
  drawSuggestion,
  clearLabels,
  diagnostics:() => {
    const found = findBoard();
    const rect = found?.getBoundingClientRect?.();
    return {
      boardFound:!!found, boardTag:found?.tagName || '',
      boardWidth:rect?.width || 0, boardHeight:rect?.height || 0,
      bottomSide:resolveBottomSide(found, api.current() || {}) || 'unknown',
      selectors:boardSelectors
    };
  },
  get suppress() { return suppress; },
  set suppress(value) { suppress = !!value; },
  stop:() => {
    window.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('pointerup', onUp, true);
    window.removeEventListener('contextmenu', onContextMenu, true);
    clearLabels();
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


def format_white_score(score) -> tuple[str, float]:
    """Format a Stockfish score from White's perspective."""
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


def eval_white(engine, board: chess.Board, seconds: float) -> tuple[str, float]:
    # Stockfish cannot analyse an already-terminal position on some builds.
    if board.is_checkmate():
        winner = not board.turn
        side = "White" if winner == chess.WHITE else "Black"
        value = 100000.0 if winner == chess.WHITE else -100000.0
        return f"Mate for {side}", value
    if board.is_game_over(claim_draw=True):
        return "Draw", 0.0
    info = engine.analyse(board, chess.engine.Limit(time=seconds))
    return format_white_score(info["score"].pov(chess.WHITE))


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


def set_listener_suppressed(driver, value: bool) -> None:
    try:
        driver.execute_script(
            "if (window.__variationLineAnalyzer) window.__variationLineAnalyzer.suppress = arguments[0];",
            bool(value),
        )
    except Exception:
        pass


def clear_generated(driver, generated: list[tuple[str, dict]]) -> None:
    if not generated:
        return
    cdp = tracker._direct_cdp_page_for_driver(driver)
    if cdp is None:
        generated.clear()
        return
    # Suppress our own synthetic native-arrow gestures so they are not re-read
    # as if the user had drawn a new hypothetical line.
    set_listener_suppressed(driver, True)
    try:
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
    finally:
        set_listener_suppressed(driver, False)
    generated.clear()


def set_board_status(driver, message: str) -> None:
    """Render operational status directly on the board instead of opening a window."""
    try:
        result = driver.execute_script(
            "return window.__variationLineAnalyzer?.drawLabels?.([], [], arguments[0]) || {ok:false};",
            str(message),
        )
        reason = ""
        if isinstance(result, dict) and not result.get("ok"):
            reason = str(result.get("reason") or "unknown")
        previous = getattr(driver, "_variation_last_display_warning", None)
        current = (reason, str(message))
        if reason and current != previous:
            # Avoid flooding the terminal from the 120 ms polling loop.
            prior_reason = previous[0] if isinstance(previous, tuple) else None
            if reason != prior_reason:
                print(f"[BOARD DISPLAY] Status could not be drawn on the board ({reason}).", flush=True)
        try:
            setattr(driver, "_variation_last_display_warning", current if reason else None)
        except Exception:
            pass
    except Exception as exc:
        previous = getattr(driver, "_variation_last_display_warning", None)
        current = ("exception", type(exc).__name__)
        if current != previous:
            print(f"[BOARD DISPLAY] Could not update board status ({type(exc).__name__}).", flush=True)
        try:
            setattr(driver, "_variation_last_display_warning", current)
        except Exception:
            pass


def short_eval(value: str) -> str:
    return (
        value.replace("White +", "W+")
        .replace("Black +", "B+")
        .replace("Equal (", "=")
        .replace(")", "")
    )


def calculate_next_hint(
    engine,
    board: chess.Board,
    search_time: float,
    step_number: int,
    max_steps: int,
) -> tuple[dict | None, str]:
    """Return exactly one best move for the current virtual position."""
    if board.is_game_over(claim_draw=True):
        outcome = board.outcome(claim_draw=True)
        if outcome and outcome.winner is not None:
            return None, ("Checkmate · White wins" if outcome.winner == chess.WHITE
                          else "Checkmate · Black wins")
        return None, "Draw · no further moves"

    try:
        info = engine.analyse(board, chess.engine.Limit(time=search_time))
        pv = list(info.get("pv") or [])
        if not pv:
            return None, "Stockfish returned no legal continuation"
        move = pv[0]
        if move not in board.legal_moves:
            return None, "Stockfish returned an invalid move"

        root_eval, root_value = format_white_score(info["score"].pov(chess.WHITE))
        san = board.san(move)
        side = "White" if board.turn == chess.WHITE else "Black"
        move_prefix = (
            f"{board.fullmove_number}."
            if board.turn == chess.WHITE
            else f"{board.fullmove_number}..."
        )
        note = capture_description(board, move)
        after = board.copy(stack=False)
        after.push(move)
        if after.is_checkmate():
            note = (note + " · " if note else "") + "CHECKMATE"
        elif after.is_check():
            note = (note + " · " if note else "") + "CHECK"
        elif after.is_stalemate():
            note = (note + " · " if note else "") + "STALEMATE"

        after_eval, after_value = eval_white(engine, after, search_time)
        delta = delta_text(root_value, after_value)
        capture_short = ""
        if "captures" in note:
            piece_name = next(
                (p for p in ("queen", "rook", "bishop", "knight", "pawn") if p in note),
                "",
            )
            captured_side = "W" if "captures White " in note else ("B" if "captures Black " in note else "")
            letter = {"queen": "Q", "rook": "R", "bishop": "B", "knight": "N", "pawn": "P"}.get(piece_name, "")
            if letter:
                capture_short = f"captures {captured_side}{letter}"

        return {
            "uci": move.uci(),
            "san": f"{move_prefix}{san}",
            "side": side,
            "eval": short_eval(after_eval),
            "root_eval": short_eval(root_eval),
            "delta": delta,
            "note": note,
            "capture": capture_short,
            "title": f"NEXT {step_number}/{max_steps} · {side}",
            "step": step_number,
        }, ""
    except Exception as exc:
        return None, f"Stockfish error · {type(exc).__name__}"


def show_hint(driver, hint: dict | None, message: str = "") -> bool:
    """Display one faint candidate arrow and compact eval on the same board."""
    try:
        if hint is None:
            result = driver.execute_script(
                "return window.__variationLineAnalyzer?.drawLabels?.([], [], arguments[0]) || {ok:false};",
                message or "No suggested continuation",
            )
        else:
            result = driver.execute_script(
                "return window.__variationLineAnalyzer?.drawSuggestion?.(arguments[0]) || {ok:false};",
                hint,
            )
        if isinstance(result, dict) and result.get("ok"):
            return True
        reason = result.get("reason", "unknown") if isinstance(result, dict) else "invalid-response"
        print(f"[BOARD DISPLAY] Could not render the single move hint ({reason}).", flush=True)
        return False
    except Exception as exc:
        print(f"[BOARD DISPLAY] Hint display failed ({type(exc).__name__}).", flush=True)
        return False


def remove_unconfirmed_native_arrow(driver, event: dict) -> None:
    """Toggle off an accidental/mismatched native arrow without changing the position."""
    try:
        uci = str(event.get("from", "")) + str(event.get("to", ""))
        geom = event.get("geometry") or {}
        cdp = tracker._direct_cdp_page_for_driver(driver)
        if cdp is None or len(uci) < 4:
            return
        set_listener_suppressed(driver, True)
        tracker._dispatch_native_board_arrow(
            cdp, uci,
            left=float(geom["left"]), top=float(geom["top"]),
            width=float(geom["width"]), height=float(geom["height"]),
            bottom_side=str(geom["bottom_side"]),
        )
    except Exception:
        pass
    finally:
        set_listener_suppressed(driver, False)

def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze a manually drawn hypothetical line on the configured authorized chess board.")
    parser.add_argument("--debugger-address", default="127.0.0.1:9223")
    parser.add_argument("--allowed-domain", default="")
    parser.add_argument("--board-selector", default="chess-board")
    parser.add_argument("--stockfish-path", default="")
    parser.add_argument("--plies", type=int, default=4, help="Maximum sequential suggestions before stopping (default 4, maximum 4).")
    parser.add_argument("--search-time", type=float, default=0.20, help="Stockfish search time per position in seconds (0.05-2.0).")
    args = parser.parse_args()
    args.plies = max(1, min(4, int(args.plies)))
    args.search_time = max(0.05, min(2.0, float(args.search_time)))
    driver = None
    engine = None

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
        # Reuse the main tracker's reader if it is already installed. Do not
        # replace/disconnect the running tracker observer from this add-on.
        existing_state = driver.execute_script(
            "return window.__liveGameApiReader?.current?.() || null;"
        )
        if isinstance(existing_state, dict) and (
            existing_state.get("fen") or existing_state.get("boardFound")
            or existing_state.get("gameApiFound")
        ):
            installed = {"boardFound": True, "gameApiFound": True}
        else:
            installed = driver.execute_script(tracker.JS_INSTALL_OBSERVER, cfg)
        if not isinstance(installed, dict) or not installed.get("boardFound") or not installed.get("gameApiFound"):
            print("[BOARD ERROR] Board/game API was not detected. Configure the board selector and exposed board game API first.", file=sys.stderr)
            return 2
        watch_result = driver.execute_script(INSTALL_LINE_WATCHER, args.board_selector)
        if not isinstance(watch_result, dict) or not watch_result.get("ok"):
            print("[BOARD ERROR] Could not install the right-click line listener.", file=sys.stderr)
            return 2
        try:
            diagnostics = driver.execute_script(
                "return window.__variationLineAnalyzer?.diagnostics?.() || {};"
            ) or {}
            print(
                "[BOARD] Listener installed | "
                f"boardFound={diagnostics.get('boardFound')} | "
                f"tag={diagnostics.get('boardTag') or 'unknown'} | "
                f"size={diagnostics.get('boardWidth', 0)}x{diagnostics.get('boardHeight', 0)} | "
                f"orientation={diagnostics.get('bottomSide', 'unknown')}",
                flush=True,
            )
        except Exception:
            pass

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

        generated: list[tuple[str, dict]] = []
        initial = driver.execute_script(
            "return window.__liveGameApiReader?.current?.() || {};"
        ) or {}
        last_fen = str(initial.get("fen") or "").strip()
        if not last_fen:
            print("[BOARD ERROR] The authorized test board did not expose a usable FEN.", file=sys.stderr, flush=True)
            driver.execute_script("window.__variationLineAnalyzer?.stop?.();")
            engine.quit()
            return 2
        try:
            analysis_board = chess.Board(last_fen)
        except ValueError:
            print("[BOARD ERROR] The board FEN is invalid; no hint was drawn.", file=sys.stderr, flush=True)
            driver.execute_script("window.__variationLineAnalyzer?.stop?.();")
            engine.quit()
            return 2

        accepted_steps = 0
        pending_hint: dict | None = None

        def show_next_step() -> None:
            nonlocal pending_hint
            if accepted_steps >= args.plies:
                pending_hint = None
                show_hint(
                    driver, None,
                    f"Preview complete · {accepted_steps}/{args.plies} moves confirmed · restart analyzer to begin again"
                )
                print(f"[DONE] Sequential preview finished after {accepted_steps} moves.", flush=True)
                return

            hint, reason = calculate_next_hint(
                engine, analysis_board, args.search_time,
                accepted_steps + 1, args.plies,
            )
            if hint is None:
                pending_hint = None
                show_hint(driver, None, reason or "No further legal move")
                print(f"[END] {reason or 'No further legal move'}.", flush=True)
                return

            pending_hint = hint
            if show_hint(driver, pending_hint):
                note = f" · {hint['note']}" if hint.get("note") else ""
                print(
                    f"[HINT] {hint['title']} · {hint['san']} · "
                    f"after={hint['eval']} · {hint['delta']}{note}",
                    flush=True,
                )
                print("[HINT] Trace this same faint arrow with right-click drag to reveal only the next move.", flush=True)

        show_next_step()
        print("[READY] One faint move is shown at a time; no move is played or premoved.", flush=True)
        print("[READY] Draw the exact shown arrow to confirm it. Up to four plies total.", flush=True)
        print("[READY] Press Ctrl+C in this terminal to stop.", flush=True)

        try:
            while True:
                try:
                    state = driver.execute_script(
                        "return window.__liveGameApiReader?.current?.() || {};"
                    ) or {}
                    current_fen = str(state.get("fen") or "").strip()

                    # If the real board is played/reset, discard the hypothetical
                    # continuation and start one fresh single-move hint.
                    if current_fen and current_fen != last_fen:
                        clear_generated(driver, generated)
                        try:
                            driver.execute_script("window.__variationLineAnalyzer?.clearLabels?.();")
                            analysis_board = chess.Board(current_fen)
                        except Exception:
                            set_board_status(driver, "Board changed · could not reset analysis position")
                            time.sleep(0.12)
                            continue
                        last_fen = current_fen
                        accepted_steps = 0
                        pending_hint = None
                        show_next_step()

                    queued = driver.execute_script(
                        "return window.__variationLineAnalyzer ? "
                        "window.__variationLineAnalyzer.queue.splice(0) : [];"
                    ) or []
                    if not isinstance(queued, list):
                        queued = []

                    for event in queued:
                        if not isinstance(event, dict):
                            continue
                        event_fen = str(event.get("fen") or "").strip()
                        if current_fen and event_fen and event_fen != current_fen:
                            continue

                        event_uci = (
                            str(event.get("from") or "").lower()
                            + str(event.get("to") or "").lower()
                        )
                        if pending_hint is not None:
                            expected_uci = str(pending_hint.get("uci") or "").lower()
                            # Promotion can be drawn as a four-square arrow, so compare
                            # the source and destination while preserving the engine's
                            # selected promotion piece in the virtual analysis board.
                            if event_uci[:4] == expected_uci[:4]:
                                try:
                                    accepted_move = chess.Move.from_uci(expected_uci)
                                    if accepted_move not in analysis_board.legal_moves:
                                        raise ValueError("hint is no longer legal")
                                    accepted_san = analysis_board.san(accepted_move)
                                    analysis_board.push(accepted_move)
                                    accepted_steps += 1
                                    pending_hint = None
                                    try:
                                        driver.execute_script("window.__variationLineAnalyzer?.clearLabels?.();")
                                    except Exception:
                                        pass
                                    print(
                                        f"[CONFIRMED] {accepted_san} · "
                                        f"{accepted_steps}/{args.plies}",
                                        flush=True,
                                    )
                                    show_next_step()
                                except Exception as exc:
                                    print(f"[STEP ERROR] Could not advance virtual position ({type(exc).__name__}).", flush=True)
                                    show_next_step()
                            else:
                                # Remove the mistaken solid board annotation and keep
                                # the one translucent suggestion waiting for confirmation.
                                remove_unconfirmed_native_arrow(driver, event)
                                show_hint(pending_hint)
                                print(
                                    f"[HINT] Still waiting for {pending_hint.get('san')}; "
                                    f"'{event_uci}' did not match the faint arrow.",
                                    flush=True,
                                )
                        else:
                            remove_unconfirmed_native_arrow(driver, event)
                            show_hint(
                                driver, None,
                                f"Preview ended · {accepted_steps}/{args.plies} moves · restart analyzer to begin again"
                            )
                except tracker.WebDriverException as exc:
                    print(f"[BROWSER ERROR] Board read failed ({type(exc).__name__}).", file=sys.stderr, flush=True)
                time.sleep(0.12)
        finally:
            try:
                clear_generated(driver, generated)
            except Exception:
                pass
            try:
                driver.execute_script("window.__variationLineAnalyzer?.stop?.();")
            except Exception:
                pass
            try:
                driver.execute_script("window.__variationLineAnalyzer?.clearLabels?.();")
            except Exception:
                pass
            try:
                engine.quit()
            except Exception:
                pass
        return 0
    except KeyboardInterrupt:
        print("\n[STOP] Analyzer stopped.")
        return 0
    except Exception as exc:
        if driver is not None:
            try:
                driver.execute_script("window.__variationLineAnalyzer?.stop?.()")
            except Exception:
                pass
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
        print(f"[ERROR] Analyzer failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
