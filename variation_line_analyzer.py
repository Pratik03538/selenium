"""Interactive line analyzer for an authorized study/test board.

Reuses the tracker's allowlist, Chrome attach, board reader, and native-arrow
helpers. When launched by the tracker, SHOW MOVE ON switches its existing BOT
off and shows hints; SHOW MOVE OFF switches the tracker BOT on. This analyzer
does not submit moves itself, and public competitive hosts remain blocked.
"""
from __future__ import annotations

import argparse
import json
import re
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


def publish_autoplay_mode(
    path: Path | None,
    enabled: bool,
    move_reference: dict | None = None,
) -> bool:
    """Atomically publish the BOT switch and latest SHOW MOVE reference to the tracker."""
    if path is None:
        return False
    payload = {"autoplay_enabled": bool(enabled)}
    if move_reference is not None:
        payload["move_reference"] = move_reference
    else:
        # Mode changes should not erase the last reference already published.
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("move_reference"), dict):
                payload["move_reference"] = previous["move_reference"]
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text(json.dumps(payload), encoding="utf-8")
        temp_path.replace(path)
        return True
    except OSError:
        try:
            temp_path.unlink()
        except OSError:
            pass
        return False


INSTALL_LINE_WATCHER = r"""
const selector = String(arguments[0] || 'chess-board');
const autoplayControlEnabled = Boolean(arguments[1]);
const api = window.__liveGameApiReader;
if (!api || typeof api.current !== 'function') {
  return {ok:false, reason:'tracker-board-observer-not-installed'};
}
if (window.__variationLineAnalyzer) {
  try { window.__variationLineAnalyzer.stop(); } catch (_) {}
}
const queue = [];
// Track only native arrows drawn through this listener. Repeating the same
// right-drag toggles that annotation off, which must reset the line queue.
const userArrows = new Set();
let down = null;
let suppress = false;
let turnHintEnabled = false;
let turnHintStatus = autoplayControlEnabled ? 'OFF · autoplay mode' : 'OFF · no move is played';
let turnControl = null;
let turnControlButton = null;
let turnControlStatus = null;
let turnHintOverlay = null;
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
function drawSuggestions(items, statusText = '') {
  const setup = ensureOverlay();
  if (!setup || !setup.ok) return {ok:false, reason:setup?.reason || 'board-label-layer-unavailable'};
  const {board, rect, svg} = setup;
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const state = api.current() || {};
  const side = resolveBottomSide(board, state);
  if (!['white','black'].includes(side)) return {ok:false, reason:'board-orientation-unknown'};

  const hints = (Array.isArray(items) ? items : []).slice(0, 2);
  if (!hints.length) return {ok:false, reason:'no-hints'};
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
  const colors = ['#2563eb', '#c2410c'];
  const defs = make('defs');
  hints.forEach((item, index) => {
    const color = colors[index % colors.length];
    const opacity = index === 0 ? 0.32 : 0.20;
    const markerId = 'variation-next-arrow-head-' + (index + 1);
    const marker = make('marker', {
      id:markerId, markerWidth:9, markerHeight:9,
      refX:7, refY:4.5, orient:'auto',
      markerUnits:'userSpaceOnUse'
    });
    marker.appendChild(make('path',{d:'M0,0 L9,4.5 L0,9 z',fill:color,opacity}));
    defs.appendChild(marker);
  });
  svg.appendChild(defs);

  for (let index=0; index<hints.length; index++) {
    const item = hints[index] || {};
    const uci = String(item.uci || '').toLowerCase();
    if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) continue;
    const color = colors[index % colors.length];
    const opacity = index === 0 ? 0.32 : 0.20;
    const a = squarePoint(uci.slice(0,2));
    const b = squarePoint(uci.slice(2,4));
    const dx = b.x-a.x, dy = b.y-a.y;
    const length = Math.max(1, Math.hypot(dx,dy));
    const ux = dx/length, uy = dy/length;
    const inset = Math.min(rect.width,rect.height)/19;
    const x2 = b.x-ux*inset, y2 = b.y-uy*inset;
    // Both arrows are deliberately faint; numbered badges identify the order.
    svg.appendChild(make('line', {
      x1:a.x, y1:a.y, x2, y2,
      stroke:color, 'stroke-width':Math.max(3,rect.width/110),
      'stroke-linecap':'round', 'stroke-dasharray':'5 5',
      opacity, 'marker-end':'url(#variation-next-arrow-head-'+(index+1)+')'
    }));
    const midX=(a.x+b.x)/2, midY=(a.y+b.y)/2;
    const badgeRadius=Math.max(8,Math.min(rect.width,rect.height)/48);
    svg.appendChild(make('circle',{
      cx:midX, cy:midY, r:badgeRadius, fill:color,
      stroke:'#ffffff','stroke-width':1.2,opacity:0.68
    }));
    svg.appendChild(make('text',{
      x:midX,y:midY+4,'text-anchor':'middle',
      'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(9,badgeRadius*1.05),
      'font-weight':'700',fill:'#ffffff',opacity:0.98
    },String(index+1)));

    const san = String(item.san || uci);
    const evalText = String(item.eval || '');
    const deltaText = String(item.delta || '');
    const noteText = String(item.capture || item.note || '');
    const titleText = String(item.title || item.side || (index===0?'Next':'Reply'));
    const tagWidth = Math.min(rect.width*0.48-4, Math.max(86,(san.length+evalText.length+5)*5.4));
    const tagHeight = noteText ? 42 : 32;
    let tagX = b.x + (index===0 ? 7 : -tagWidth-7);
    tagX = Math.max(3,Math.min(rect.width-tagWidth-3,tagX));
    let tagY = b.y-tagHeight-7+(index*10);
    if (tagY < 3) tagY = Math.min(rect.height-tagHeight-3,b.y+7+(index*10));
    const group = make('g', {'data-suggested-move':uci,'data-suggestion-index':index+1});
    group.appendChild(make('rect',{
      x:tagX,y:tagY,width:tagWidth,height:tagHeight,rx:4,
      fill:'#eff6ff','fill-opacity':0.82,
      stroke:color,'stroke-width':0.8,opacity:0.94
    }));
    group.appendChild(make('text',{
      x:tagX+5,y:tagY+11,'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(8,Math.min(10,rect.width/47)),
      'font-weight':'700',fill:color,opacity:0.98
    },String(index+1)+'. '+titleText));
    group.appendChild(make('text',{
      x:tagX+5,y:tagY+23,'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(9,Math.min(12,rect.width/39)),
      'font-weight':'700',fill:'#1e3a8a',opacity:0.98
    },san+' '+evalText));
    group.appendChild(make('text',{
      x:tagX+5,y:tagY+34,'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':Math.max(8,Math.min(9,rect.width/51)),
      'font-weight':'600',fill:'#1e3a8a',opacity:0.98
    },(deltaText+(noteText ? ' · '+noteText : '')).slice(0,70)));
    svg.appendChild(group);
  }

  if (statusText) {
    const label = String(statusText).slice(0, 100);
    const fontSize = Math.max(10, Math.min(13, rect.width/38));
    const textWidth = Math.min(rect.width-8, Math.max(150, label.length*fontSize*0.56+18));
    svg.appendChild(make('rect', {
      x:4, y:4, width:textWidth, height:fontSize+12,
      rx:5, fill:'#7f1d1d', 'fill-opacity':0.94,
      stroke:'#fecaca', 'stroke-width':0.8
    }));
    svg.appendChild(make('text', {
      x:12, y:fontSize+8, 'font-family':'Segoe UI, Arial, sans-serif',
      'font-size':fontSize, 'font-weight':'700', fill:'#ffffff', opacity:0.98
    }, label));
  }
  return {ok:true, count:hints.length, boardFound:true};
}
function refreshTurnControlText() {
  if (turnControlButton) {
    turnControlButton.textContent = 'SHOW MOVE: ' + (turnHintEnabled ? 'ON' : 'OFF');
    turnControlButton.style.background = turnHintEnabled ? '#166534' : '#334155';
    turnControlButton.style.borderColor = turnHintEnabled ? '#86efac' : '#94a3b8';
    turnControlButton.setAttribute('aria-pressed', turnHintEnabled ? 'true' : 'false');
  }
  if (turnControlStatus) {
    turnControlStatus.textContent = String(turnHintStatus || '');
  }
}
function setTurnHintStatus(message) {
  turnHintStatus = String(message || '');
  refreshTurnControlText();
  return {ok:true, status:turnHintStatus};
}
function positionTurnControl() {
  if (!turnControl) return;
  const board = findBoard();
  const rect = board?.getBoundingClientRect?.();
  const panelWidth = 142;
  let left = rect && rect.width ? rect.right + 10 : 12;
  // Keep the control adjacent to the board's right edge whenever the viewport allows it.
  if (left + panelWidth > window.innerWidth - 4) {
    left = Math.max(4, window.innerWidth - panelWidth - 4);
  }
  const top = rect && rect.height
    ? Math.max(8, Math.min(rect.top + 6, window.innerHeight - 92))
    : 12;
  turnControl.style.left = Math.round(left) + 'px';
  turnControl.style.top = Math.round(top) + 'px';
}
function ensureTurnControl() {
  if (!turnControl || !turnControl.isConnected) {
    turnControl = document.createElement('div');
    turnControl.setAttribute('data-variation-turn-control', 'true');
    turnControl.style.cssText = [
      'position:fixed','width:142px','box-sizing:border-box','padding:7px',
      'border:1px solid #cbd5e1','border-radius:8px','background:rgba(15,23,42,.98)',
      'box-shadow:0 4px 14px rgba(0,0,0,.28)','z-index:2147483647',
      'font-family:Segoe UI,Arial,sans-serif','pointer-events:auto'
    ].join(';');

    turnControlButton = document.createElement('button');
    turnControlButton.type = 'button';
    turnControlButton.style.cssText = [
      'width:100%','min-height:34px','border:1px solid #94a3b8','border-radius:6px',
      'color:#fff','font-weight:800','font-size:12px','letter-spacing:.2px',
      'cursor:pointer','padding:6px 4px','opacity:1'
    ].join(';');
    turnControlButton.addEventListener('click', () => {
      turnHintEnabled = !turnHintEnabled;
      turnHintStatus = turnHintEnabled
        ? (autoplayControlEnabled ? 'ON · move hints only · autoplay paused' : 'ON · checking whose turn it is')
        : (autoplayControlEnabled ? 'OFF · autoplay mode' : 'OFF · no move is played');
      if (!turnHintEnabled) clearTurnSuggestion();
      else clearTurnSuggestion();
      refreshTurnControlText();
    });

    turnControlStatus = document.createElement('div');
    turnControlStatus.style.cssText = [
      'margin-top:7px','color:#f8fafc','font-size:10px','line-height:1.35',
      'font-weight:650','overflow-wrap:anywhere','opacity:1'
    ].join(';');
    turnControlStatus.textContent = turnHintStatus;
    turnControl.appendChild(turnControlButton);
    turnControl.appendChild(turnControlStatus);
    (document.body || document.documentElement).appendChild(turnControl);
    refreshTurnControlText();
  }
  positionTurnControl();
  return {ok:true, enabled:turnHintEnabled};
}
function clearTurnSuggestion() {
  if (turnHintOverlay && turnHintOverlay.parentNode) {
    turnHintOverlay.parentNode.removeChild(turnHintOverlay);
  }
  turnHintOverlay = null;
  return {ok:true};
}
function drawTurnSuggestion(item) {
  const setup = ensureOverlay();
  if (!setup || !setup.ok) return {ok:false, reason:setup?.reason || 'board-element-not-found'};
  const {board, rect} = setup;
  const side = resolveBottomSide(board, api.current() || {});
  if (!['white','black'].includes(side)) return {ok:false, reason:'board-orientation-unknown'};
  const uci = String(item?.uci || '').toLowerCase();
  if (!/^[a-h][1-8][a-h][1-8][qrbn]?$/.test(uci)) return {ok:false, reason:'invalid-selected-move'};

  if (turnHintOverlay && turnHintOverlay.parentNode) {
    turnHintOverlay.parentNode.removeChild(turnHintOverlay);
  }
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('data-variation-turn-hint', 'true');
  svg.setAttribute('viewBox', '0 0 ' + rect.width + ' ' + rect.height);
  svg.setAttribute('width', String(rect.width));
  svg.setAttribute('height', String(rect.height));
  svg.style.cssText = [
    'position:fixed','left:'+rect.left+'px','top:'+rect.top+'px',
    'width:'+rect.width+'px','height:'+rect.height+'px','margin:0','padding:0',
    'overflow:hidden','pointer-events:none','z-index:2147483646'
  ].join(';');

  const make = (tag, attrs, textValue) => {
    const node = document.createElementNS(svgNS, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    if (textValue !== undefined) node.textContent = String(textValue);
    return node;
  };
  const point = square => {
    const file = square.charCodeAt(0)-97;
    const rank = Number(square[1]);
    const boardFile = side === 'white' ? file : 7-file;
    const boardRow = side === 'white' ? 8-rank : rank-1;
    return {x:(boardFile+0.5)*rect.width/8, y:(boardRow+0.5)*rect.height/8};
  };
  const a=point(uci.slice(0,2)), b=point(uci.slice(2,4));
  const dx=b.x-a.x, dy=b.y-a.y;
  const length=Math.max(1,Math.hypot(dx,dy));
  const ux=dx/length, uy=dy/length;
  const inset=Math.min(rect.width,rect.height)/19;
  const marker=make('marker',{id:'variation-turn-head',markerWidth:10,markerHeight:10,refX:8,refY:5,orient:'auto',markerUnits:'userSpaceOnUse'});
  marker.appendChild(make('path',{d:'M0,0 L10,5 L0,10 z',fill:'#16a34a',opacity:0.30}));
  const defs=make('defs');
  defs.appendChild(marker);
  svg.appendChild(defs);
  svg.appendChild(make('line',{
    x1:a.x,y1:a.y,x2:b.x-ux*inset,y2:b.y-uy*inset,
    stroke:'#15803d','stroke-width':Math.max(3,rect.width/125),
    'stroke-linecap':'round','stroke-dasharray':'5 5',
    opacity:0.30,'marker-end':'url(#variation-turn-head)'
  }));

  const text='TURN MOVE · '+String(item?.san || uci);
  const fontSize=Math.max(10,Math.min(12,rect.width/48));
  const boxWidth=Math.min(rect.width-8,Math.max(108,text.length*fontSize*0.58+16));
  let x=Math.min(rect.width-boxWidth-4,Math.max(4,b.x+7));
  let y=b.y+9;
  if (y+27>rect.height) y=Math.max(4,b.y-32);
  svg.appendChild(make('rect',{
    x,y,width:boxWidth,height:25,rx:5,
    fill:'#f0fdf4','fill-opacity':0.97,stroke:'#15803d','stroke-width':1.1,opacity:0.98
  }));
  svg.appendChild(make('text',{
    x:x+8,y:y+16,'font-family':'Segoe UI,Arial,sans-serif',
    'font-size':fontSize,'font-weight':'800',fill:'#14532d',opacity:0.99
  },text));
  (document.body || document.documentElement).appendChild(svg);
  turnHintOverlay=svg;
  positionTurnControl();
  return {ok:true, uci, san:String(item?.san || uci)};
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
  // Releasing outside the board is an interrupted gesture, not a queue reset.
  if (!to) return;
  const state = api.current() || {};
  const fen = String(state.fen || '');
  if (!fen || fen !== saved.fen) return;

  if (to === saved.from) {
    // In tracker-launched autoplay mode, a click on a piece requests its
    // control/attack map. SHOW MOVE keeps the existing clear-all gesture.
    if (autoplayControlEnabled && !turnHintEnabled) {
      queue.push({
        action:'attack_map',
        square:saved.from,
        fen,
        geometry:saved.rect,
        bottomSide:saved.side,
        at:Date.now()
      });
      if (queue.length > 20) queue.splice(0, queue.length-20);
      return;
    }
    // In manual SHOW MOVE mode, a right-click without a drag clears the queue.
    userArrows.clear();
    queue.push({action:'clear_all', fen, remainingArrows:0, at:Date.now()});
    if (queue.length > 20) queue.splice(0, queue.length-20);
    drawLabels([], [], 'Queue cleared · draw a new legal arrow to begin');
    return;
  }

  const uci = saved.from + to;
  const wasDrawnByUser = userArrows.has(uci);
  if (wasDrawnByUser) userArrows.delete(uci);
  else userArrows.add(uci);

  queue.push({
    action:wasDrawnByUser ? 'clear' : 'draw',
    from:saved.from, to, uci, fen,
    geometry:saved.rect,
    bottomSide:saved.side,
    remainingArrows:userArrows.size,
    at:Date.now()
  });
  if (queue.length > 20) queue.splice(0, queue.length-20);
  drawLabels(
    [], [],
    wasDrawnByUser
      ? 'Arrow toggled off · queue cleared'
      : 'Your arrow detected: ' + saved.from + ' → ' + to + ' · analyzing…'
  );
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
  drawSuggestions,
  clearLabels,
  resetTrackedArrows:() => { userArrows.clear(); return true; },
  getTurnHintState:() => ({enabled:turnHintEnabled,status:turnHintStatus}),
  setTurnHintStatus,
  drawTurnSuggestion,
  clearTurnSuggestion,
  ensureTurnControl,
  diagnostics:() => {
    const found = findBoard();
    const rect = found?.getBoundingClientRect?.();
    return {
      boardFound:!!found, boardTag:found?.tagName || '',
      boardLeft:rect?.left || 0, boardTop:rect?.top || 0,
      boardWidth:rect?.width || 0, boardHeight:rect?.height || 0,
      bottomSide:resolveBottomSide(found, api.current() || {}) || 'unknown',
      trackedUserArrows:userArrows.size,
      selectors:boardSelectors
    };
  },
  get suppress() { return suppress; },
  set suppress(value) { suppress = !!value; },
  stop:() => {
    window.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('pointerup', onUp, true);
    window.removeEventListener('contextmenu', onContextMenu, true);
    window.removeEventListener('resize', positionTurnControl, true);
    window.removeEventListener('scroll', positionTurnControl, true);
    clearLabels();
    clearTurnSuggestion();
    if (turnControl && turnControl.parentNode) turnControl.parentNode.removeChild(turnControl);
    turnControl = null;
    turnControlButton = null;
    turnControlStatus = null;
    queue.length = 0;
    userArrows.clear();
    delete window.__variationLineAnalyzer;
  }
};
ensureTurnControl();
window.addEventListener('resize', positionTurnControl, true);
window.addEventListener('scroll', positionTurnControl, true);
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


def eval_white(
    engine,
    board: chess.Board,
    seconds: float,
    strategy_module=None,
) -> tuple[str, float, int]:
    """Return a White-perspective label, display score and selector-compatible CP."""
    if board.is_checkmate():
        winner = not board.turn
        side = "White" if winner == chess.WHITE else "Black"
        value = 100000.0 if winner == chess.WHITE else -100000.0
        cp = 100000 if winner == chess.WHITE else -100000
        return f"Mate for {side}", value, cp
    if board.is_game_over(claim_draw=True):
        return "Draw", 0.0, 0

    info = engine.analyse(board, chess.engine.Limit(time=max(0.005, seconds)))
    score_white = info["score"].pov(chess.WHITE)
    label, value = format_white_score(score_white)
    if strategy_module is not None:
        cp = int(strategy_module.score_to_cp(score_white))
    else:
        cp = int(round(value * 100))
    return label, value, cp


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


def dispatch_native_annotation_arrow(
    cdp,
    uci: str,
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    bottom_side: str,
) -> dict:
    """Toggle a native annotation arrow without submitting or moving a chess piece."""
    text = str(uci or "").lower()
    if not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", text):
        return {"ok": False, "reason": "invalid-uci"}
    if (
        width <= 0 or height <= 0
        or abs(width - height) > max(8.0, width * 0.08)
        or bottom_side not in {"white", "black"}
    ):
        return {"ok": False, "reason": "invalid-board-geometry"}

    def square_point(square: str) -> tuple[float, float]:
        file_index = ord(square[0]) - ord("a")
        rank_number = int(square[1])
        board_file = file_index if bottom_side == "white" else 7 - file_index
        board_row = 8 - rank_number if bottom_side == "white" else rank_number - 1
        return (
            left + (board_file + 0.5) * width / 8.0,
            top + (board_row + 0.5) * height / 8.0,
        )

    sx, sy = square_point(text[:2])
    dx, dy = square_point(text[2:4])
    mx, my = (sx + dx) / 2.0, (sy + dy) / 2.0
    # A direct websocket is faster, but ChromeDriver's built-in CDP command
    # is a reliable fallback when a Chrome attach session does not expose the
    # page websocket. Keep this limited to native right-click annotation input.
    use_driver_cdp = callable(getattr(cdp, "execute_cdp_cmd", None))
    command_ids: set[int] = set()

    def send_mouse_event(params: dict) -> None:
        if use_driver_cdp:
            cdp.execute_cdp_cmd("Input.dispatchMouseEvent", params)
        else:
            command_ids.add(cdp.send("Input.dispatchMouseEvent", params))

    try:
        send_mouse_event({
            "type": "mouseMoved", "x": sx, "y": sy,
            "button": "none", "buttons": 0, "pointerType": "mouse",
        })
        send_mouse_event({
            "type": "mousePressed", "x": sx, "y": sy,
            "button": "right", "buttons": 2, "clickCount": 1,
            "pointerType": "mouse",
        })
        time.sleep(0.004)
        send_mouse_event({
            "type": "mouseMoved", "x": mx, "y": my,
            "button": "right", "buttons": 2, "pointerType": "mouse",
        })
        send_mouse_event({
            "type": "mouseMoved", "x": dx, "y": dy,
            "button": "right", "buttons": 2, "pointerType": "mouse",
        })
        send_mouse_event({
            "type": "mouseReleased", "x": dx, "y": dy,
            "button": "right", "buttons": 0, "clickCount": 1,
            "pointerType": "mouse",
        })
        if not use_driver_cdp:
            cdp.wait_for(command_ids, timeout=1.0)
    except Exception as exc:
        try:
            release = {
                "type": "mouseReleased", "button": "right",
                "buttons": 0, "pointerType": "mouse",
            }
            if use_driver_cdp:
                cdp.execute_cdp_cmd("Input.dispatchMouseEvent", release)
            else:
                cdp.send("Input.dispatchMouseEvent", release)
        except Exception:
            pass
        return {"ok": False, "reason": "native-arrow-gesture-failed", "error_type": type(exc).__name__}
    return {"ok": True, "uci": text}


def clear_generated(driver, generated: list[tuple[str, dict]]) -> None:
    if not generated:
        return
    cdp = tracker._direct_cdp_page_for_driver(driver)
    if cdp is None and callable(getattr(driver, "execute_cdp_cmd", None)):
        cdp = driver
    if cdp is None:
        generated.clear()
        return
    # Suppress our own synthetic native-arrow gestures so they are not re-read
    # as if the user had drawn a new hypothetical line.
    set_listener_suppressed(driver, True)
    try:
        for uci, geom in reversed(generated):
            try:
                dispatch_native_annotation_arrow(
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


def autoplay_eval_snapshot(engine, board: chess.Board, seconds: float) -> tuple[float, int | None]:
    """Return a quick White-perspective score and forced-mate distance."""
    if board.is_checkmate():
        winner = not board.turn
        return (100000.0 if winner == chess.WHITE else -100000.0), 0
    if board.is_game_over(claim_draw=True):
        return 0.0, None
    info = engine.analyse(board, chess.engine.Limit(time=max(0.01, float(seconds))))
    score_white = info["score"].pov(chess.WHITE)
    _label, value = format_white_score(score_white)
    return float(value), score_white.mate()


def classify_autoplay_transition(
    before_board: chess.Board | None,
    after_board: chess.Board,
    move: chess.Move | None,
    eval_delta_white: float | None = None,
    current_mate_distance: int | None = None,
) -> str:
    """Describe only captures, forcing moves, mates, promotions, or large eval swings."""
    reasons: list[str] = []
    if before_board is not None and move is not None and move in before_board.legal_moves:
        if before_board.is_capture(move):
            reasons.append("CAPTURE")
        if move.promotion is not None:
            reasons.append("PROMOTION")
        try:
            resulting = before_board.copy(stack=True)
            resulting.push(move)
            if resulting.is_checkmate():
                reasons.append("CHECKMATE")
            elif resulting.is_check():
                reasons.append("CHECK")
        except (ValueError, AssertionError):
            pass

    if after_board.is_checkmate() and "CHECKMATE" not in reasons:
        reasons.append("CHECKMATE")
    if current_mate_distance is not None and not after_board.is_checkmate():
        reasons.append(f"FORCED MATE IN {abs(int(current_mate_distance))}")
    if (
        eval_delta_white is not None
        and current_mate_distance is None
        and abs(float(eval_delta_white)) >= 1.25
    ):
        reasons.append(f"TACTICAL EVAL SWING {float(eval_delta_white):+.2f}")
    # Keep the explanation compact if multiple signals describe the same event.
    return " · ".join(dict.fromkeys(reasons))


def autoplay_preview_plies(reason: str) -> int:
    """Choose two arrows for routine moves and four for tactical moments."""
    if not str(reason or "").strip():
        return 0
    tactical_tokens = (
        "CAPTURE",
        "CHECK",
        "MATE",
        "PROMOTION",
        "TACTICAL EVAL SWING",
    )
    return 4 if any(token in str(reason).upper() for token in tactical_tokens) else 2


def piece_controlled_squares(board: chess.Board, square_name: str) -> list[str]:
    """Return geometric squares attacked/controlled by the piece on a square."""
    try:
        source = chess.parse_square(str(square_name or "").strip().lower())
    except ValueError:
        return []
    if board.piece_at(source) is None:
        return []
    return [chess.square_name(square) for square in sorted(board.attacks(source))]


def draw_native_piece_control_map(
    driver,
    board: chess.Board,
    source_square: str,
    geometry: dict,
    generated: list[tuple[str, dict]],
) -> list[str]:
    """Draw native right-drag arrows from a selected piece to its attacked squares."""
    try:
        source = chess.parse_square(str(source_square or "").strip().lower())
        piece = board.piece_at(source)
        if piece is None:
            print(f"[CONTROL MAP] No piece on {source_square}.", flush=True)
            return []
        geom = {
            "left": float(geometry.get("left", 0)),
            "top": float(geometry.get("top", 0)),
            "width": float(geometry.get("width", 0)),
            "height": float(geometry.get("height", 0)),
            "bottom_side": str(geometry.get("bottom_side", "")),
        }
        if (
            geom["width"] <= 8
            or geom["height"] <= 8
            or abs(geom["width"] - geom["height"]) > max(8.0, geom["width"] * 0.08)
            or geom["bottom_side"] not in {"white", "black"}
        ):
            print("[CONTROL MAP] Invalid board geometry; no arrows drawn.", flush=True)
            return []

        targets = piece_controlled_squares(board, source_square)
        if not targets:
            print(f"[CONTROL MAP] {piece.symbol()} on {source_square} controls no squares.", flush=True)
            return []
        cdp = tracker._direct_cdp_page_for_driver(driver)
        if cdp is None and callable(getattr(driver, "execute_cdp_cmd", None)):
            cdp = driver
        if cdp is None:
            print("[CONTROL MAP] Native arrow channel unavailable.", flush=True)
            return []

        shown: list[str] = []
        set_listener_suppressed(driver, True)
        try:
            for target in targets:
                uci = str(source_square).lower() + target
                result = dispatch_native_annotation_arrow(
                    cdp, uci,
                    left=geom["left"], top=geom["top"],
                    width=geom["width"], height=geom["height"],
                    bottom_side=geom["bottom_side"],
                )
                if result.get("ok"):
                    generated.append((uci, dict(geom)))
                    shown.append(target)
                    time.sleep(0.004)
                else:
                    print(
                        f"[CONTROL MAP] Could not draw {uci} ({result.get('reason', 'unknown')}).",
                        flush=True,
                    )
        finally:
            set_listener_suppressed(driver, False)
        print(
            f"[CONTROL MAP] {piece.symbol()} on {source_square}: "
            f"{len(shown)}/{len(targets)} controlled squares annotated.",
            flush=True,
        )
        return shown
    except Exception as exc:
        print(f"[CONTROL MAP] Drawing failed ({type(exc).__name__}).", flush=True)
        return []


def draw_native_autoplay_preview(
    driver,
    engine,
    board: chess.Board,
    search_seconds: float,
    max_plies: int,
    generated: list[tuple[str, dict]],
) -> list[str]:
    """Draw 2–4 future PV arrows with the board's native right-click arrow mechanism."""
    if board.is_game_over(claim_draw=True):
        return []
    try:
        geometry = driver.execute_script(
            "return window.__variationLineAnalyzer?.diagnostics?.() || {};"
        ) or {}
        if (
            not geometry.get("boardFound")
            or geometry.get("bottomSide") not in {"white", "black"}
        ):
            return []
        geom = {
            "left": float(geometry.get("boardLeft", 0)),
            "top": float(geometry.get("boardTop", 0)),
            "width": float(geometry.get("boardWidth", 0)),
            "height": float(geometry.get("boardHeight", 0)),
            "bottom_side": str(geometry.get("bottomSide")),
        }
        if (
            geom["width"] <= 8
            or geom["height"] <= 8
            or abs(geom["width"] - geom["height"]) > max(8.0, geom["width"] * 0.08)
        ):
            return []
        cdp = tracker._direct_cdp_page_for_driver(driver)
        if cdp is None and callable(getattr(driver, "execute_cdp_cmd", None)):
            cdp = driver
            print(
                "[AUTO ARROWS] Direct websocket unavailable; using ChromeDriver CDP for native arrows.",
                flush=True,
            )
        if cdp is None:
            print("[AUTO ARROWS] Native board channel is unavailable; no overlay fallback used.", flush=True)
            return []

        info = engine.analyse(
            board,
            chess.engine.Limit(time=max(0.02, min(float(search_seconds), 0.12))),
            multipv=1,
        )
        pv = info.get("pv") if isinstance(info, dict) else None
        if not isinstance(pv, list):
            return []
        virtual_board = board.copy(stack=True)
        arrows: list[tuple[str, str]] = []
        seen_uci: set[str] = set()
        limit = max(1, min(int(max_plies), 4))
        for move in pv:
            if move not in virtual_board.legal_moves:
                break
            san = virtual_board.san(move)
            prefix = (
                f"{virtual_board.fullmove_number}."
                if virtual_board.turn == chess.WHITE
                else f"{virtual_board.fullmove_number}..."
            )
            uci = move.uci()
            if uci not in seen_uci:
                seen_uci.add(uci)
                arrows.append((uci, prefix + san))
            virtual_board.push(move)
            if len(arrows) >= limit:
                break

        if not arrows:
            return []
        shown: list[str] = []
        set_listener_suppressed(driver, True)
        try:
            for uci, label in arrows:
                result = dispatch_native_annotation_arrow(
                    cdp, uci,
                    left=geom["left"], top=geom["top"],
                    width=geom["width"], height=geom["height"],
                    bottom_side=geom["bottom_side"],
                )
                if result.get("ok"):
                    generated.append((uci, dict(geom)))
                    shown.append(label)
                    time.sleep(0.012)
                else:
                    print(
                        f"[AUTO ARROWS] Could not draw native arrow for {label} "
                        f"({result.get('reason', 'unknown')}).",
                        flush=True,
                    )
        finally:
            set_listener_suppressed(driver, False)
        return shown
    except Exception as exc:
        print(f"[AUTO ARROWS] Native preview failed ({type(exc).__name__}); no overlay fallback used.", flush=True)
        return []


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


def select_strategy_move(
    engine,
    book,
    strategy_module,
    board: chess.Board,
    search_time: float,
    previous_eval_white_cp: int | None,
) -> tuple[chess.Move | None, dict | None, dict, str, float, int, str]:
    """Select one move using the project's ChessKida selector, not the raw top PV."""
    root_label = ""
    root_value = 0.0
    root_cp = 0

    # The main tracker uses this same weighted GM book before the ChessKida selector.
    if book is not None:
        try:
            book_choice = book.choose(board)
        except Exception:
            book_choice = None
        if isinstance(book_choice, dict) and book_choice.get("move") in board.legal_moves:
            try:
                root_label, root_value, root_cp = eval_white(
                    engine, board, min(search_time, 0.01), strategy_module
                )
            except Exception:
                root_label, root_value, root_cp = "", 0.0, 0
            metadata = {
                "rank": int(book_choice.get("rank", 1)) - 1,
                "source": "GM_BOOK",
                "reason": "GM BOOK | weighted opening-book choice",
                "selected_cp": root_cp,
            }
            return book_choice["move"], None, metadata, root_label, root_value, root_cp, ""

    try:
        result = engine.analyse(
            board,
            chess.engine.Limit(depth=8, time=max(0.005, search_time)),
            multipv=max(1, int(getattr(strategy_module, "TRAINING_MULTI_PV", 15))),
        )
        infos = result if isinstance(result, list) else [result]
        infos = [
            item for item in infos
            if isinstance(item, dict) and item.get("pv") and item.get("score") is not None
        ]
    except Exception as exc:
        return None, None, {}, root_label, root_value, root_cp, (
            f"Move selection analysis failed · {type(exc).__name__}"
        )

    if not infos:
        return None, None, {}, root_label, root_value, root_cp, (
            "No legal MultiPV candidates returned by strategy analysis"
        )

    try:
        root_score_white = infos[0]["score"].pov(chess.WHITE)
        root_label, root_value = format_white_score(root_score_white)
        root_cp = int(strategy_module.score_to_cp(root_score_white))
    except Exception:
        try:
            root_label, root_value, root_cp = eval_white(
                engine, board, min(search_time, 0.01), strategy_module
            )
        except Exception:
            return None, None, {}, "", 0.0, 0, "Current-position evaluation unavailable"

    try:
        selected_move, selected_info, metadata = strategy_module.choose_stockfish_move(
            board,
            infos,
            previous_eval_white_cp=previous_eval_white_cp,
            opponent_accuracy=None,
            opponent_sample_count=0,
            opponent_pressure=False,
            engine=engine,
            bot_color=board.turn,
            low_clock_mode=False,
        )
    except Exception as exc:
        return None, None, {}, root_label, root_value, root_cp, (
            f"ChessKida selector failed · {type(exc).__name__}"
        )

    if selected_move not in board.legal_moves:
        reason = metadata.get("reason", "selector returned no legal move") if isinstance(metadata, dict) else "selector returned no legal move"
        return None, None, metadata if isinstance(metadata, dict) else {}, root_label, root_value, root_cp, str(reason)

    return (
        selected_move,
        selected_info if isinstance(selected_info, dict) else None,
        metadata if isinstance(metadata, dict) else {},
        root_label,
        root_value,
        root_cp,
        "",
    )


def calculate_next_hints(
    engine,
    strategy_module,
    book,
    board: chess.Board,
    search_time: float,
    step_number: int,
    max_steps: int,
    previous_eval_white_cp: int | None = None,
    forced_first_hint: dict | None = None,
) -> tuple[list[dict], str]:
    """Calculate up to two moves by repeatedly applying the existing move selector."""
    if board.is_game_over(claim_draw=True):
        outcome = board.outcome(claim_draw=True)
        if outcome and outcome.winner is not None:
            return [], ("Checkmate · White wins" if outcome.winner == chess.WHITE
                        else "Checkmate · Black wins")
        return [], "Draw · no further moves"

    remaining_steps = 2 if max_steps <= 0 else max(0, max_steps - step_number + 1)
    moves_to_show = min(2, remaining_steps)
    if moves_to_show < 1:
        return [], "Preview complete"

    # Keep virtual history so strategy selection sees the same line that has
    # been traced so far, instead of treating every position as a fresh FEN.
    line_board = board.copy(stack=True)
    previous_display_value = None
    selector_previous_cp = previous_eval_white_cp
    hints: list[dict] = []

    for offset in range(moves_to_show):
        if line_board.is_game_over(claim_draw=True):
            break

        if offset == 0 and forced_first_hint:
            # The second arrow in the previously displayed pair is already part
            # of the current queue. Reuse it instead of random-selecting again.
            try:
                queued_move = chess.Move.from_uci(str(forced_first_hint.get("uci") or ""))
            except ValueError:
                queued_move = None
            if queued_move is not None and queued_move in line_board.legal_moves:
                move = queued_move
                selection_meta = {
                    "source": forced_first_hint.get("selection_source", "CHESSKIDA"),
                    "rank": forced_first_hint.get("selection_rank"),
                    "reason": forced_first_hint.get("selection_reason", "Queued ChessKida selection"),
                }
                _selected_info = None
                root_label, root_value, _root_cp = eval_white(
                    engine, line_board, min(search_time, 0.01), strategy_module
                )
            else:
                forced_first_hint = None

        if not (offset == 0 and forced_first_hint and queued_move in line_board.legal_moves):
            (
                move,
                _selected_info,
                selection_meta,
                root_label,
                root_value,
                _root_cp,
                reason,
            ) = select_strategy_move(
                engine,
                book,
                strategy_module,
                line_board,
                search_time,
                selector_previous_cp,
            )
            if move is None:
                if hints:
                    return hints, reason or "No further legal continuation"
                return [], reason or "ChessKida selector returned no legal continuation"

        if previous_display_value is None:
            previous_display_value = root_value

        san = line_board.san(move)
        side = "White" if line_board.turn == chess.WHITE else "Black"
        move_prefix = (
            f"{line_board.fullmove_number}."
            if line_board.turn == chess.WHITE
            else f"{line_board.fullmove_number}..."
        )
        note = capture_description(line_board, move)
        source = str(selection_meta.get("source", "CHESSKIDA"))
        selected_rank = selection_meta.get("rank")
        selection_reason = str(selection_meta.get("reason", "ChessKida move-selection policy"))

        line_board.push(move)
        after_eval, after_value, after_cp = eval_white(
            engine, line_board, min(search_time, 0.01), strategy_module
        )

        if line_board.is_checkmate():
            note = (note + " · " if note else "") + "CHECKMATE"
        elif line_board.is_check():
            note = (note + " · " if note else "") + "CHECK"
        elif line_board.is_stalemate():
            note = (note + " · " if note else "") + "STALEMATE"

        capture_short = ""
        if "captures" in note:
            piece_name = next(
                (p for p in ("queen", "rook", "bishop", "knight", "pawn") if p in note),
                "",
            )
            captured_side = (
                "W" if "captures White " in note
                else ("B" if "captures Black " in note else "")
            )
            letter = {
                "queen": "Q", "rook": "R", "bishop": "B",
                "knight": "N", "pawn": "P",
            }.get(piece_name, "")
            if letter:
                capture_short = f"captures {captured_side}{letter}"

        if "CHECKMATE" in note:
            display_eval = "MATE"
        elif "STALEMATE" in note:
            display_eval = "DRAW"
        else:
            display_eval = short_eval(after_eval)

        hints.append({
            "uci": move.uci(),
            "san": f"{move_prefix}{san}",
            "side": side,
            "eval": display_eval,
            "root_eval": short_eval(root_label),
            "eval_white_cp": after_cp,
            "delta": delta_text(previous_display_value, after_value),
            "note": note,
            "capture": capture_short,
            "title": f"MOVE {step_number + offset} · {side}",
            "step": step_number + offset,
            "selection_source": source,
            "selection_rank": selected_rank,
            "selection_reason": selection_reason,
        })
        previous_display_value = after_value
        selector_previous_cp = after_cp

        if line_board.is_game_over(claim_draw=True):
            break

    if not hints:
        return [], "No legal continuation found"
    return hints, ""


def show_hints(driver, hints: list[dict], message: str = "") -> bool:
    """Display at most two faint candidate arrows directly over the same board."""
    try:
        if not hints:
            result = driver.execute_script(
                "return window.__variationLineAnalyzer?.drawLabels?.([], [], arguments[0]) || {ok:false};",
                message or "No suggested continuation",
            )
        else:
            result = driver.execute_script(
                "return window.__variationLineAnalyzer?.drawSuggestions?.(arguments[0], arguments[1]) || {ok:false};",
                hints[:2], message,
            )
        if isinstance(result, dict) and result.get("ok"):
            return True
        reason = result.get("reason", "unknown") if isinstance(result, dict) else "invalid-response"
        print(f"[BOARD DISPLAY] Could not render the two-move preview ({reason}).", flush=True)
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
        if cdp is None and callable(getattr(driver, "execute_cdp_cmd", None)):
            cdp = driver
        if cdp is None or len(uci) < 4:
            return
        set_listener_suppressed(driver, True)
        dispatch_native_annotation_arrow(
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
    parser.add_argument("--window-handle", default="", help="Optional tracker-selected Chrome tab handle for automatic launch.")
    parser.add_argument("--autoplay-control-file", default="", help="Optional private IPC file used only when the tracker launches this companion.")
    parser.add_argument("--stockfish-path", default="")
    parser.add_argument("--plies", type=int, default=0, help="Optional maximum sequential plies; 0 means unlimited (default).")
    parser.add_argument("--search-time", type=float, default=0.05, help="Fast Stockfish search time per position in seconds (0.05-2.0).")
    args = parser.parse_args()
    mode_control_raw = str(args.autoplay_control_file or "").strip()
    mode_control_file = Path(mode_control_raw).expanduser().resolve() if mode_control_raw else None
    args.plies = max(0, int(args.plies))
    progress_limit = str(args.plies) if args.plies > 0 else "∞"
    args.search_time = max(0.05, min(2.0, float(args.search_time)))
    driver = None
    engine = None
    book = None
    strategy_module = None

    try:
        try:
            import chesskida_strategy as strategy_module
            from gm_book import GMBook
        except ImportError as exc:
            print(
                "[STRATEGY ERROR] ChessKida selector files are missing beside the tracker.",
                file=sys.stderr,
            )
            return 2

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
        requested_handle = str(args.window_handle or "").strip()
        requested_match = next(
            (handle for handle, _url in candidates if requested_handle and handle == requested_handle),
            None,
        )
        if requested_match is not None:
            chosen_handle = requested_match
        elif len(candidates) == 1:
            chosen_handle = candidates[0][0]
        elif requested_handle:
            print(
                "[TAB ERROR] Could not match the tracker-selected tab handle; "
                "automatic launch will not prompt for a different tab.",
                file=sys.stderr,
            )
            return 2
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
        watch_result = driver.execute_script(
            INSTALL_LINE_WATCHER, args.board_selector, mode_control_file is not None
        )
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

        book = GMBook(tracker.BASE_DIR / "Ultimate_GM_Bullet.bin")
        if book.load():
            print("[BOOK] Weighted GM opening book loaded for the same opening-selection behavior.", flush=True)
        else:
            print("[BOOK] GM opening book unavailable; continuing with the ChessKida selector.", flush=True)
            book = None

        # Match the tracker's side-resolution policy: when player_name is
        # configured, the player's detected side takes priority over fixed bot_color.
        configured_player_name = tracker.load_optional_txt_setting("player_name").strip()
        configured_turn_color = (
            None if configured_player_name
            else tracker.normalize_chess_color(
                tracker.load_optional_txt_setting("bot_color")
            )
        )
        accepted_steps = 0
        pending_hints: list[dict] = []
        branch_active = False
        awaiting_clear = False
        queued_followup_hint: dict | None = None
        line_previous_eval_white_cp: int | None = None
        turn_hint_was_enabled = False
        turn_hint_last_key = ""
        turn_hint_last_status = "OFF"
        last_published_autoplay_state: bool | None = None
        last_autoplay_eval_fen = ""
        last_autoplay_eval_value: float | None = None
        last_autoplay_mate_distance: int | None = None
        last_attack_map_square = ""
        off_hint_status = (
            "OFF · autoplay mode" if mode_control_file is not None
            else "OFF · no move is played"
        )

        def show_next_step() -> None:
            nonlocal pending_hints, awaiting_clear, queued_followup_hint
            if not branch_active:
                pending_hints = []
                show_hints(driver, [], "Queue empty · draw a legal arrow to begin")
                return

            if args.plies > 0 and accepted_steps >= args.plies:
                pending_hints = []
                queued_followup_hint = None
                awaiting_clear = True
                show_hints(
                    driver, [],
                    f"Preview complete · {accepted_steps}/{args.plies} plies · manually clear ALL your arrows to restart"
                )
                print(
                    f"[DONE] Preview finished after {accepted_steps} moves. "
                    "Your arrows are untouched; clear all of them manually to restart.",
                    flush=True,
                )
                return

            forced_hint = queued_followup_hint
            queued_followup_hint = None
            hints, reason = calculate_next_hints(
                engine, strategy_module, book, analysis_board, args.search_time,
                accepted_steps + 1, args.plies, line_previous_eval_white_cp,
                forced_hint,
            )
            if not hints:
                pending_hints = []
                awaiting_clear = bool(branch_active)
                show_hints(
                    driver, [],
                    (reason or "No further legal move") +
                    (" · manually clear all arrows to restart" if awaiting_clear else "")
                )
                print(
                    f"[END] {reason or 'No further legal move'}." +
                    (" Clear all native arrows manually to restart." if awaiting_clear else ""),
                    flush=True,
                )
                return

            pending_hints = hints[:2]
            if show_hints(driver, pending_hints, reason):
                for hint in pending_hints:
                    note = f" · {hint['note']}" if hint.get("note") else ""
                    print(
                        f"[HINT {hint['step']}] {hint['title']} · {hint['san']} · "
                        f"after={hint['eval']} · {hint['delta']} · "
                        f"selector={hint.get('selection_source', 'CHESSKIDA')} "
                        f"rank={hint.get('selection_rank', '—')} · "
                        f"{hint.get('selection_reason', 'ChessKida selection')}{note}",
                        flush=True,
                    )
                print(
                    "[HINT] Draw the first hint to continue; another legal arrow selects a branch. "
                    "Clear/toggle off an arrow to reset the queue.",
                    flush=True,
                )

        startup_turn_mode = driver.execute_script(
            "return window.__variationLineAnalyzer?.getTurnHintState?.() || {enabled:false};"
        ) or {}
        if mode_control_file is None or bool(startup_turn_mode.get("enabled")):
            show_hints(driver, [], "Queue empty · draw a legal arrow to begin")
        else:
            # Autoplay mode uses only the board's native arrows; do not leave a
            # translucent analyzer SVG/status layer on the board.
            driver.execute_script("window.__variationLineAnalyzer?.clearLabels?.();")
        if mode_control_file is not None:
            print(
                "[READY] Autoplay mode: right-click a piece without dragging to draw arrows "
                "to the squares it controls; repeat the same click to clear the map.",
                flush=True,
            )
            print(
                "[READY] SHOW MOVE mode keeps manual right-drag arrows. "
                "Attack-map arrows clear on the next board position.",
                flush=True,
            )
        else:
            print("[READY] No suggestions are shown until you draw a native right-click arrow.", flush=True)
            print("[READY] Your drawn arrow selects the starting move; the next two suggestions then appear.", flush=True)
            print("[READY] Drawing the first hinted move continues that line; clear ALL your arrows to return to the real board position.", flush=True)
            print("[READY] No move cap by default. Clear all your arrows to return to the real position and start a fresh queue.", flush=True)
        print("[READY] Press Ctrl+C in this terminal to stop.", flush=True)

        try:
            while True:
                try:
                    state = driver.execute_script(
                        "return window.__liveGameApiReader?.current?.() || {};"
                    ) or {}
                    current_fen = str(state.get("fen") or "").strip()
                    turn_hint_ui = driver.execute_script(
                        "return window.__variationLineAnalyzer?.getTurnHintState?.() || {enabled:false};"
                    ) or {}
                    turn_hint_enabled = bool(turn_hint_ui.get("enabled"))
                    autoplay_preview_mode = mode_control_file is not None and not turn_hint_enabled

                    # Native autoplay arrows are cleared immediately on entering SHOW MOVE,
                    # which leaves that mode available for the user's manual native arrows.
                    if not autoplay_preview_mode:
                        if generated:
                            clear_generated(driver, generated)
                        last_attack_map_square = ""
                        last_autoplay_eval_fen = ""
                        last_autoplay_eval_value = None
                        last_autoplay_mate_distance = None

                    # A real board move resets the hypothetical branch to the actual position.
                    # Only tracker-launched autoplay mode gets automatic tactical previews.
                    if current_fen and current_fen != last_fen:
                        previous_fen = last_fen
                        before_board = None
                        observed_move = None
                        if previous_fen:
                            try:
                                before_board = chess.Board(previous_fen)
                            except ValueError:
                                before_board = None
                            try:
                                transition = tracker.infer_move_between_fens(previous_fen, current_fen)
                                if transition and before_board is not None:
                                    candidate = chess.Move.from_uci(str(transition.get("uci") or ""))
                                    if candidate in before_board.legal_moves:
                                        observed_move = candidate
                            except (ValueError, TypeError):
                                observed_move = None

                        clear_generated(driver, generated)
                        last_attack_map_square = ""
                        try:
                            driver.execute_script("window.__variationLineAnalyzer?.clearLabels?.();")
                            next_board = chess.Board(current_fen)
                        except Exception:
                            set_board_status(driver, "Board changed · could not reset analysis position")
                            time.sleep(0.12)
                            continue

                        current_eval_value = None
                        current_mate_distance = None
                        eval_delta_white = None
                        autoplay_reason = ""
                        if autoplay_preview_mode:
                            # Static tactics still trigger if the quick engine scan fails.
                            autoplay_reason = classify_autoplay_transition(
                                before_board, next_board, observed_move,
                            )
                            try:
                                current_eval_value, current_mate_distance = autoplay_eval_snapshot(
                                    engine, next_board, min(args.search_time, 0.04)
                                )
                                if (
                                    last_autoplay_eval_fen == previous_fen
                                    and last_autoplay_eval_value is not None
                                ):
                                    eval_delta_white = current_eval_value - last_autoplay_eval_value
                                evaluated_reason = classify_autoplay_transition(
                                    before_board,
                                    next_board,
                                    observed_move,
                                    eval_delta_white,
                                    current_mate_distance,
                                )
                                if evaluated_reason:
                                    autoplay_reason = evaluated_reason
                            except Exception as exc:
                                print(
                                    f"[AUTO ARROWS] Quick tactical scan failed ({type(exc).__name__}); "
                                    "static capture/check/mate detection remains active.",
                                    flush=True,
                                )
                            # Show a short native coaching line after every confirmed move,
                            # even when the position has no special tactical trigger.
                            if observed_move is not None and not autoplay_reason:
                                autoplay_reason = "MOVE UPDATE"

                        analysis_board = next_board
                        driver.execute_script("window.__variationLineAnalyzer?.resetTrackedArrows?.();")
                        last_fen = current_fen
                        accepted_steps = 0
                        pending_hints = []
                        branch_active = False
                        awaiting_clear = False
                        queued_followup_hint = None
                        line_previous_eval_white_cp = None

                        if autoplay_preview_mode:
                            # Cache this position even if the quick evaluation failed, so
                            # the same board is not re-evaluated twice in one polling cycle.
                            last_autoplay_eval_fen = current_fen
                            last_autoplay_eval_value = current_eval_value
                            last_autoplay_mate_distance = current_mate_distance
                            if autoplay_reason:
                                if next_board.is_game_over(claim_draw=True):
                                    print(
                                        f"[AUTO ARROWS] {autoplay_reason} · position is terminal; "
                                        "no future move arrows available.",
                                        flush=True,
                                    )
                                else:
                                    max_preview_plies = autoplay_preview_plies(autoplay_reason)
                                    preview_labels = draw_native_autoplay_preview(
                                        driver, engine, next_board,
                                        min(args.search_time, 0.08),
                                        max_preview_plies, generated,
                                    )
                                    if preview_labels:
                                        print(
                                            f"[AUTO ARROWS] {autoplay_reason} | native board arrows: "
                                            + " → ".join(preview_labels),
                                            flush=True,
                                        )
                                    else:
                                        print(
                                            f"[AUTO ARROWS] {autoplay_reason} detected; "
                                            "native preview unavailable, no translucent fallback used.",
                                            flush=True,
                                        )
                        else:
                            last_autoplay_eval_fen = ""
                            last_autoplay_eval_value = None
                            last_autoplay_mate_distance = None
                            show_hints(
                                driver, [],
                                "Board position changed · queue reset · draw a new arrow",
                            )
                            print("[RESET] Board position changed; waiting for your next arrow.", flush=True)

                    # Establish a baseline the first time autoplay is enabled for the
                    # current position; switching modes itself must not draw arrows.
                    if (
                        autoplay_preview_mode
                        and current_fen
                        and current_fen != last_autoplay_eval_fen
                    ):
                        try:
                            baseline_board = chess.Board(current_fen)
                            last_autoplay_eval_value, last_autoplay_mate_distance = autoplay_eval_snapshot(
                                engine, baseline_board, min(args.search_time, 0.04)
                            )
                        except Exception:
                            last_autoplay_eval_value = None
                            last_autoplay_mate_distance = None
                        last_autoplay_eval_fen = current_fen
                    if mode_control_file is not None:
                        requested_autoplay = not turn_hint_enabled
                        if requested_autoplay != last_published_autoplay_state:
                            if publish_autoplay_mode(mode_control_file, requested_autoplay):
                                last_published_autoplay_state = requested_autoplay
                                print(
                                    "[MODE] SHOW MOVE: "
                                    + ("OFF -> tracker BOT ON." if requested_autoplay else "ON -> tracker BOT OFF."),
                                    flush=True,
                                )
                            else:
                                print("[MODE] Could not send the toggle state to the tracker.", flush=True)
                    if not turn_hint_enabled:
                        if turn_hint_was_enabled or turn_hint_last_status != "OFF":
                            driver.execute_script(
                                "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                "window.__variationLineAnalyzer?.setTurnHintStatus?.(arguments[0]);",
                                off_hint_status,
                            )
                        turn_hint_was_enabled = False
                        turn_hint_last_key = ""
                        turn_hint_last_status = "OFF"
                    else:
                        if not turn_hint_was_enabled:
                            turn_hint_last_key = ""
                            turn_hint_was_enabled = True
                        # FEN alone is not enough to cache a side-detection result:
                        # the tracker may expose playerNameColor/isMyTurn a little
                        # after the position snapshot, without changing the FEN.
                        turn_hint_key = json.dumps(
                            [
                                current_fen,
                                state.get("playingAsColor"),
                                state.get("playingAs"),
                                state.get("playerNameColor"),
                                state.get("isMyTurn"),
                                configured_turn_color,
                            ],
                            sort_keys=True,
                            default=str,
                        )
                        if current_fen and turn_hint_key != turn_hint_last_key:
                            turn_hint_last_key = turn_hint_key
                            try:
                                turn_board = chess.Board(current_fen)
                                turn_side = "white" if turn_board.turn == chess.WHITE else "black"
                                api_player_color, api_color_conflict = tracker.normalize_api_bot_color(
                                    state.get("playingAsColor"),
                                    state.get("playingAs"),
                                )
                                player_name_color = tracker.normalize_chess_color(
                                    state.get("playerNameColor")
                                )
                                detected_color = api_player_color or player_name_color
                                side_conflict = bool(
                                    api_color_conflict
                                    or (
                                        api_player_color
                                        and player_name_color
                                        and api_player_color != player_name_color
                                    )
                                    or (
                                        configured_turn_color
                                        and detected_color
                                        and configured_turn_color != detected_color
                                    )
                                )
                                api_is_my_turn = state.get("isMyTurn")
                                player_color = configured_turn_color or detected_color
                                if side_conflict:
                                    driver.execute_script(
                                        "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                        "window.__variationLineAnalyzer?.setTurnHintStatus?.(arguments[0]);",
                                        "SIDE MISMATCH · check bot_color setting",
                                    )
                                    print("[TURN HINT] Side mismatch; no arrow shown.", flush=True)
                                elif player_color and api_is_my_turn is not True and turn_side != player_color:
                                    driver.execute_script(
                                        "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                        "window.__variationLineAnalyzer?.setTurnHintStatus?.('ON · waiting for your turn');"
                                    )
                                    turn_hint_last_status = "WAITING"
                                elif api_is_my_turn is False:
                                    driver.execute_script(
                                        "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                        "window.__variationLineAnalyzer?.setTurnHintStatus?.('ON · waiting for your turn');"
                                    )
                                    turn_hint_last_status = "WAITING"
                                elif not player_color and api_is_my_turn is not True:
                                    driver.execute_script(
                                        "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                        "window.__variationLineAnalyzer?.setTurnHintStatus?.("
                                        "'ON · side unknown · check player_name/bot_color');"
                                    )
                                    if turn_hint_last_status != "SIDE UNKNOWN":
                                        print(
                                            "[TURN HINT] Side unresolved. Check player_name in "
                                            "tracker_settings.txt, or set bot_color=white/black.",
                                            flush=True,
                                        )
                                    turn_hint_last_status = "SIDE UNKNOWN"
                                else:
                                    selected_move, _selected_info, selection_meta, _root_label, _root_value, _root_cp, selection_error = select_strategy_move(
                                        engine, book, strategy_module, turn_board,
                                        args.search_time, None,
                                    )
                                    if selected_move is None:
                                        message = "ON · no legal move selected"
                                        driver.execute_script(
                                            "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                            "window.__variationLineAnalyzer?.setTurnHintStatus?.(arguments[0]);",
                                            message,
                                        )
                                        turn_hint_last_status = message
                                        print(
                                            f"[TURN HINT] Could not select a move: {selection_error}",
                                            flush=True,
                                        )
                                    else:
                                        selected_san = turn_board.san(selected_move)
                                        selected_rank = selection_meta.get("rank")
                                        selected_source = str(selection_meta.get("source", "CHESSKIDA"))
                                        shown = driver.execute_script(
                                            "return window.__variationLineAnalyzer?.drawTurnSuggestion?.(arguments[0]) || {ok:false};",
                                            {
                                                "uci": selected_move.uci(),
                                                "san": selected_san,
                                                "rank": selected_rank,
                                                "source": selected_source,
                                            },
                                        )
                                        if mode_control_file is not None:
                                            try:
                                                selected_rank_value = (
                                                    int(selected_rank) if selected_rank is not None else None
                                                )
                                            except (TypeError, ValueError):
                                                selected_rank_value = None
                                            move_reference = {
                                                "event_id": str(time.time_ns()),
                                                "fen": current_fen,
                                                "uci": selected_move.uci(),
                                                "san": selected_san,
                                                "ply": turn_board.ply() + 1,
                                                "side": (
                                                    "White" if turn_board.turn == chess.WHITE else "Black"
                                                ),
                                                "rank": selected_rank_value,
                                                "source": selected_source,
                                            }
                                            publish_autoplay_mode(
                                                mode_control_file, False, move_reference
                                            )
                                        if isinstance(shown, dict) and shown.get("ok"):
                                            message = "ON · your turn · " + selected_san
                                            print(
                                                f"[TURN HINT] {selected_san} | "
                                                f"source={selected_source} | "
                                                f"rank={selected_rank if selected_rank is not None else '—'} | no move submitted.",
                                                flush=True,
                                            )
                                        else:
                                            reason = shown.get("reason", "arrow-display-failed") if isinstance(shown, dict) else "invalid-display-response"
                                            message = "ON · arrow display failed · " + str(reason)
                                            print(f"[TURN HINT] {message}.", flush=True)
                                        driver.execute_script(
                                            "window.__variationLineAnalyzer?.setTurnHintStatus?.(arguments[0]);",
                                            message,
                                        )
                                        turn_hint_last_status = message
                            except Exception as exc:
                                driver.execute_script(
                                    "window.__variationLineAnalyzer?.clearTurnSuggestion?.();"
                                    "window.__variationLineAnalyzer?.setTurnHintStatus?.(arguments[0]);",
                                    f"ON · analysis error · {type(exc).__name__}",
                                )
                                turn_hint_last_status = "ERROR"
                                print(
                                    f"[TURN HINT] Analysis failed ({type(exc).__name__}).",
                                    flush=True,
                                )

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

                        action = str(event.get("action") or "draw").lower()
                        if action == "attack_map":
                            source_square = str(event.get("square") or "").strip().lower()
                            try:
                                map_board = chess.Board(event_fen or current_fen)
                                source = chess.parse_square(source_square)
                            except ValueError:
                                print("[CONTROL MAP] Invalid position or square; no arrows drawn.", flush=True)
                                continue

                            # Toggle off a repeated click on the same piece; selecting
                            # a different piece replaces the prior control map.
                            if last_attack_map_square == source_square and generated:
                                clear_generated(driver, generated)
                                last_attack_map_square = ""
                                print(f"[CONTROL MAP] Cleared map for {source_square}.", flush=True)
                                continue

                            if generated:
                                clear_generated(driver, generated)
                            last_attack_map_square = ""
                            piece = map_board.piece_at(source)
                            if piece is None:
                                print(f"[CONTROL MAP] No piece on {source_square}.", flush=True)
                                continue
                            shown_squares = draw_native_piece_control_map(
                                driver,
                                map_board,
                                source_square,
                                event.get("geometry") if isinstance(event.get("geometry"), dict) else {},
                                generated,
                            )
                            if shown_squares:
                                last_attack_map_square = source_square
                            continue

                        if action in {"clear", "clear_all"}:
                            remaining_arrows = int(event.get("remainingArrows", 0) or 0)
                            if action == "clear" and remaining_arrows > 0:
                                # Preserve both the native arrows still on screen and
                                # the current variation queue until the user has cleared
                                # every tracked arrow themselves.
                                print(
                                    f"[ARROW] One of your arrows was toggled off; "
                                    f"{remaining_arrows} tracked arrow(s) remain. "
                                    "Queue retained; clear all arrows to restart.",
                                    flush=True,
                                )
                                if awaiting_clear:
                                    show_hints(
                                        driver, [],
                                        f"Preview complete · {remaining_arrows} arrow(s) remain · clear them manually to restart"
                                    )
                                continue

                            # Only a clear-all gesture or removal of the final tracked
                            # arrow resets the hypothetical line back to actual board FEN.
                            reset_fen = current_fen or last_fen
                            try:
                                analysis_board = chess.Board(reset_fen)
                                last_fen = reset_fen
                            except ValueError:
                                show_hints(driver, [], "Queue cleared · current board position is invalid")
                                continue
                            accepted_steps = 0
                            pending_hints = []
                            branch_active = False
                            awaiting_clear = False
                            queued_followup_hint = None
                            line_previous_eval_white_cp = None
                            show_hints(driver, [], "Queue cleared · real board position restored · draw a new arrow")
                            # Turn-only suggestion remains independent and refreshes only on the next FEN.
                            print(
                                "[RESET] All tracked user arrows are cleared. "
                                "Returned to the actual board position; waiting for a new arrow.",
                                flush=True,
                            )
                            continue

                        if action != "draw":
                            continue
                        if awaiting_clear:
                            print(
                                "[WAIT] Clear all your existing native arrows first; "
                                "they will not be removed automatically.",
                                flush=True,
                            )
                            continue
                        event_uci = str(event.get("uci") or (
                            str(event.get("from") or "").lower()
                            + str(event.get("to") or "").lower()
                        )).lower()
                        if len(event_uci) < 4:
                            continue

                        chosen_move = None
                        followed_hint = False
                        first_hint_uci = (
                            str(pending_hints[0].get("uci") or "").lower()
                            if branch_active and pending_hints else ""
                        )

                        # The user's first arrow starts the queue. While a queue
                        # exists, tracing its first hint continues it; any other
                        # legal move starts a different branch from this position.
                        if first_hint_uci and event_uci[:4] == first_hint_uci[:4]:
                            try:
                                suggested_move = chess.Move.from_uci(first_hint_uci)
                                if suggested_move in analysis_board.legal_moves:
                                    chosen_move = suggested_move
                                    followed_hint = True
                            except ValueError:
                                chosen_move = None

                        if chosen_move is None:
                            try:
                                from_square = chess.parse_square(event_uci[:2])
                                to_square = chess.parse_square(event_uci[2:4])
                            except ValueError:
                                message = f"INVALID ARROW · {event_uci[:4]} · draw a legal move"
                                show_hints(driver, pending_hints, message)
                                print(f"[MOVE IGNORED] {message}.", flush=True)
                                continue

                            legal_candidates = [
                                move for move in analysis_board.legal_moves
                                if move.from_square == from_square
                                and move.to_square == to_square
                            ]
                            if legal_candidates:
                                # A drawn arrow cannot encode promotion choice;
                                # prefer queen promotion when several choices exist.
                                chosen_move = next(
                                    (move for move in legal_candidates if move.promotion == chess.QUEEN),
                                    legal_candidates[0],
                                )

                        if chosen_move is None:
                            message = f"ILLEGAL MOVE · {event_uci[:4]} · draw a legal move"
                            show_hints(driver, pending_hints, message)
                            print(
                                f"[MOVE IGNORED] {event_uci[:4]} is not legal in the current "
                                "hypothetical position. Your arrow is left on the board.",
                                flush=True,
                            )
                            continue

                        try:
                            accepted_san = analysis_board.san(chosen_move)
                            hinted_after_cp = (
                                pending_hints[0].get("eval_white_cp")
                                if followed_hint and pending_hints else None
                            )
                            hinted_followup = (
                                dict(pending_hints[1])
                                if followed_hint and len(pending_hints) > 1 else None
                            )
                            was_branch_active = branch_active
                            if not was_branch_active or not followed_hint:
                                # A first arrow starts the sequence. A different legal
                                # arrow during a sequence starts a new branch here.
                                accepted_steps = 0

                            analysis_board.push(chosen_move)
                            accepted_steps += 1
                            branch_active = True
                            pending_hints = []
                            queued_followup_hint = hinted_followup if followed_hint else None
                            if followed_hint and isinstance(hinted_after_cp, (int, float)):
                                line_previous_eval_white_cp = int(hinted_after_cp)
                            else:
                                try:
                                    _label, _value, line_previous_eval_white_cp = eval_white(
                                        engine, analysis_board, min(args.search_time, 0.01),
                                        strategy_module,
                                    )
                                except Exception:
                                    line_previous_eval_white_cp = None

                            if followed_hint:
                                print(
                                    f"[CONFIRMED] {accepted_san} · "
                                    f"{accepted_steps}/{progress_limit} · continuing hinted line",
                                    flush=True,
                                )
                            elif not was_branch_active:
                                print(
                                    f"[START] Your arrow selected {accepted_san} · "
                                    f"starting the variation ({accepted_steps}/{progress_limit})",
                                    flush=True,
                                )
                            else:
                                print(
                                    f"[BRANCH] Your arrow selected {accepted_san} · "
                                    f"new variation from this position ({accepted_steps}/{progress_limit})",
                                    flush=True,
                                )
                            show_next_step()
                        except Exception as exc:
                            print(
                                f"[STEP ERROR] Could not advance virtual position "
                                f"({type(exc).__name__}).",
                                flush=True,
                            )
                            show_next_step()

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
                if book is not None:
                    book.close()
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
        print(f"[ERROR] Analyzer failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
