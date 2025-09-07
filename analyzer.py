#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import datetime as dt
import io
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import berserk
import chess.pgn
import chess.engine

# ----------------- utils -----------------
def env(var: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(var)
    if v is None or v == "":
        return default
    return v

def to_millis(date_str: str) -> int:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)

def score_to_cp(score: chess.engine.PovScore) -> int:
    if score.is_mate():
        m = score.mate()
        return 100000 if m and m > 0 else -100000
    return int(score.score(mate_score=100000))

def classify(delta_cp: int, thresholds: Dict[str, int]) -> Optional[str]:
    if delta_cp >= thresholds["blunder"]:
        return "blunder"
    if delta_cp >= thresholds["mistake"]:
        return "mistake"
    if delta_cp >= thresholds["inaccuracy"]:
        return "inaccuracy"
    return None

def lichess_ply_link(game_id: str, ply: int) -> str:
    return f"https://lichess.org/{game_id}#{ply}"

def split_pgn_bulk(text: str) -> List[str]:
    text = text.strip()
    if not text:
        return []
    parts = re.split(r'\r?\n\r?\n(?=\[Event )', text)
    return [p.strip() for p in parts if p.strip()]

def _download(url: str, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        return
    req = urllib.request.Request(url, headers={"User-Agent":"tactikcheck/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        f.write(r.read())

def ensure_assets(out_dir: Path):
    """
    Кладём все зависимости локально, чтобы ничего не дёргать с CDN.
    chess.js + chessboard.js + css + 12 спрайтов фигур.
    """
    assets = out_dir / "assets"
    js_dir = assets
    img_dir = assets / "img"

    # Надёжные источники (Cloudflare/CDNJS + GitHub raw)
    chess_js = "https://cdnjs.cloudflare.com/ajax/libs/chess.js/0.13.4/chess.min.js"
    board_js = "https://cdnjs.cloudflare.com/ajax/libs/chessboard-js/1.0.0/chessboard-1.0.0.min.js"
    board_css = "https://cdnjs.cloudflare.com/ajax/libs/chessboard-js/1.0.0/chessboard-1.0.0.min.css"
    # спрайты берём из исходного репозитория
    base_img = "https://raw.githubusercontent.com/oakmac/chessboardjs/v1.0.0/img/chesspieces/wikipedia/"

    _download(chess_js, js_dir / "chess.min.js")
    _download(board_js, js_dir / "chessboard-1.0.0.min.js")
    _download(board_css, js_dir / "chessboard-1.0.0.min.css")

    pieces = ["wP","wR","wN","wB","wQ","wK","bP","bR","bN","bB","bQ","bK"]
    for p in pieces:
        _download(base_img + f"{p}.png", img_dir / f"{p}.png")

# ----------------- analyzer -----------------
class Analyzer:
    def __init__(
        self,
        user: str,
        token: Optional[str],
        out_dir: Path,
        max_games: int = 200,
        since: Optional[str] = None,
        until: Optional[str] = None,
        perf: Optional[List[str]] = None,
        stockfish_path: Optional[str] = None,
        depth: int = 14,
        threads: int = 2,
        hash_mb: int = 256,
        who: Tuple[bool, bool] = (True, True),
        thresholds: Optional[Dict[str, int]] = None,
        min_cp_show: int = 50,
    ) -> None:
        self.user = user
        self.token = token
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.max_games = max_games
        self.since = since
        self.until = until
        self.perf = perf or []
        self.depth = depth
        self.threads = threads
        self.hash_mb = hash_mb
        self.min_cp_show = min_cp_show
        self.thresholds = thresholds or {"inaccuracy": 50, "mistake": 150, "blunder": 300}
        self.stockfish_path = stockfish_path or env("STOCKFISH_PATH", "stockfish")
        self.client = self._make_client()
        self.engine = None
        self.who = who

    def _username(self) -> str:
        u = self.user
        while isinstance(u, (list, tuple)) and len(u) == 1:
            u = u[0]
        if isinstance(u, (list, tuple)):
            u = u[0] if u else ""
        return str(u)

    def _make_client(self):
        if self.token:
            session = berserk.TokenSession(self.token)
            return berserk.Client(session=session)
        return berserk.Client()

    def _assert_user_exists(self):
        uname = self._username()
        print(f"Verifying username: '{uname}'", file=sys.stderr)
        try:
            info = self.client.users.get_public_data(uname)
            if not info or ("id" not in info and "username" not in info):
                raise RuntimeError(f"user '{uname}' not found or profile is private")
        except Exception as e:
            raise RuntimeError(f"Failed to verify user '{uname}': {e}")

    def _engine(self) -> chess.engine.SimpleEngine:
        if self.engine is None:
            self.engine = chess.engine.SimpleEngine.popen_uci(self.stockfish_path)
            self.engine.configure({"Threads": self.threads, "Hash": self.hash_mb})
        return self.engine

    def close(self):
        try:
            if self.engine:
                self.engine.quit()
        except Exception:
            pass

    def _debug_params(self, params: Dict[str, Any]):
        safe = dict(params)
        if "since" in safe:
            safe["since"] = f"{safe['since']} (ms)"
        if "until" in safe:
            safe["until"] = f"{safe['until']} (ms)"
        print(f"Request params: {safe}", file=sys.stderr)

    def _download_via_berserk(self, uname: str, params: Dict[str, Any]) -> List[str]:
        self._debug_params(params)
        pgn_iter = self.client.games.export_by_player(uname, **params)
        pgns: List[str] = []
        for item in pgn_iter:
            if isinstance(item, str):
                pgn = item.strip()
            elif isinstance(item, (bytes, bytearray)):
                pgn = bytes(item).decode("utf-8", errors="ignore").strip()
            else:
                pgn = (item or {}).get("pgn", "").strip()
            if not pgn:
                continue
            if "\n\n[Event " in pgn:
                pgns.extend(split_pgn_bulk(pgn))
            else:
                pgns.append(pgn)
        return pgns

    def _download_via_http(self, uname: str, params: Dict[str, Any]) -> List[str]:
        query = {
            "max": params.get("max", self.max_games),
            "moves": "true",
            "opening": "true",
            "clocks": "false",
            "evals": "false",
        }
        if "since" in params:
            query["since"] = str(params["since"])
        if "until" in params:
            query["until"] = str(params["until"])
        if params.get("perf_type"):
            query["perfType"] = params["perf_type"]
        url = f"https://lichess.org/api/games/user/{urllib.parse.quote(uname)}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={"Accept": "application/x-chess-pgn","User-Agent":"tactikcheck/1.0"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        print(f"HTTP fallback GET {url}", file=sys.stderr)
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        pgns = split_pgn_bulk(raw)
        print(f"HTTP fallback got {len(pgns)} PGNs.", file=sys.stderr)
        return pgns

    def fetch_pgns(self) -> List[str]:
        self._assert_user_exists()
        uname = self._username()
        base_params = {
            "max": self.max_games,
            "moves": True,
            "opening": True,
            "clocks": False,
            "evals": False,
            "as_pgn": True,
        }
        if self.since:
            base_params["since"] = to_millis(self.since)
        if self.until:
            base_params["until"] = to_millis(self.until) + 24 * 3600 * 1000 - 1

        attempts: List[Dict[str, Any]] = []
        if self.perf:
            p = dict(base_params); p["perf_type"] = ",".join(self.perf)
            attempts.append(p)
        attempts.append(dict(base_params))

        all_pgns: List[str] = []
        for idx, params in enumerate(attempts, 1):
            print(f"Downloading games for {uname} via berserk (attempt {idx}/{len(attempts)})...", file=sys.stderr)
            try:
                pgns = self._download_via_berserk(uname, params)
                print(f"Got {len(pgns)} PGNs on attempt {idx} (berserk).", file=sys.stderr)
                all_pgns = pgns
                if pgns:
                    break
            except Exception as e:
                print(f"berserk attempt {idx} failed: {e}", file=sys.stderr)

        if not all_pgns:
            try:
                print("Switching to HTTP fallback...", file=sys.stderr)
                for idx, params in enumerate(attempts, 1):
                    pgns = self._download_via_http(uname, params)
                    if pgns:
                        all_pgns = pgns
                        break
            except Exception as e:
                print(f"HTTP fallback failed: {e}", file=sys.stderr)

        if not all_pgns:
            raise RuntimeError("No games fetched. Reasons: wrong username, filters, or no public games.")

        preview = (all_pgns[0] or "")[:200].replace("\n", " ")
        print(f"PGN[0] preview: {preview} ...", file=sys.stderr)
        return all_pgns

    def analyze_pgn(self, pgn_text: str) -> Dict[str, Any]:
        if not pgn_text.strip():
            return {"game_id":"", "white":"?","black":"?","white_elo":"","black_elo":"","date":"","time_control":"","opening":"","errors":[]}
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        if game is None:
            return {"game_id":"", "white":"?","black":"?","white_elo":"","black_elo":"","date":"","time_control":"","opening":"","errors":[]}

        headers = game.headers
        gid = headers.get("LichessURL","").split("/")[-1] or headers.get("Site","").split("/")[-1]
        result = {
            "game_id": gid,
            "white": headers.get("White","?"),
            "black": headers.get("Black","?"),
            "white_elo": headers.get("WhiteElo"),
            "black_elo": headers.get("BlackElo"),
            "date": headers.get("UTCDate", headers.get("Date","")),
            "time_control": headers.get("TimeControl",""),
            "opening": headers.get("Opening",""),
            "errors": [],
        }

        board = game.board()
        node = game
        ply = 0
        engine = self._engine()
        limit = chess.engine.Limit(depth=self.depth)

        while not node.is_end():
            node = node.variation(0)
            move = node.move
            side_to_move = board.turn
            ply += 1

            mover_is_white = side_to_move
            if (mover_is_white and not self.who[0]) or ((not mover_is_white) and not self.who[1]):
                board.push(move); continue

            fen_before = board.fen()

            info_best_raw = engine.analyse(board, limit=limit, multipv=1)
            info_best = info_best_raw[0] if isinstance(info_best_raw, list) else info_best_raw
            best_cp = score_to_cp(info_best["score"].pov(side_to_move))
            best_move_obj = engine.play(board, limit).move
            best_uci = best_move_obj.uci()
            best_san = board.san(best_move_obj)

            info_played_raw = engine.analyse(board, limit=limit, root_moves=[move])
            info_played = info_played_raw[0] if isinstance(info_played_raw, list) else info_played_raw
            played_cp = score_to_cp(info_played["score"].pov(side_to_move))

            played_uci = move.uci()
            played_san = board.san(move)

            delta = best_cp - played_cp
            label = classify(delta, self.thresholds)
            board.push(move)

            if label and delta >= self.min_cp_show:
                san = node.san()
                move_no = (ply + 1)//2
                who_str = "white" if mover_is_white else "black"
                result["errors"].append({
                    "ply": ply, "move_no": move_no, "who": who_str, "san": san,
                    "cp_loss": delta, "category": label,
                    "fen_before": fen_before, "fen_after": board.fen(),
                    "best_uci": best_uci, "best_san": best_san,
                    "played_uci": played_uci, "played_san": played_san,
                })
        return result

    def render_gallery(self, analyzed: List[Dict[str, Any]]):
        out = self.out_dir
        out.mkdir(exist_ok=True, parents=True)

        # 1) Готовим ассеты локально
        ensure_assets(out)

        # 2) Карточки
        cards = []
        total_games = len(analyzed)
        total_errors = 0
        idx = 0
        for g in analyzed:
            gid = g.get("game_id","") or ""
            white = g.get("white","?"); black = g.get("black","?")
            welo = g.get("white_elo","") or ""; belo = g.get("black_elo","") or ""
            date = g.get("date",""); opening = g.get("opening",""); tc = g.get("time_control","")
            for e in g.get("errors", []):
                idx += 1; total_errors += 1
                cards.append({
                    "id": idx, "game_id": gid,
                    "white": white, "black": black, "welo": welo, "belo": belo,
                    "date": date, "opening": opening, "tc": tc,
                    "ply": e["ply"], "move_no": e["move_no"], "san": e["san"],
                    "who": e["who"], "cp_loss": e["cp_loss"], "category": e["category"],
                    "fen_before": e["fen_before"], "best_uci": e["best_uci"], "best_san": e["best_san"],
                    "played_uci": e["played_uci"], "played_san": e["played_san"],
                    "link": lichess_ply_link(gid, e["ply"]),
                })

        html = self._build_html(cards, total_games, total_errors)
        (out / "index.html").write_text(html, encoding="utf-8")
        print(f"Wrote gallery: {out/'index.html'}  ({total_games} games, {total_errors} flagged moves)")

    def _build_html(self, cards: List[Dict[str, Any]], total_games: int, total_errors: int) -> str:
        def esc(s: str) -> str:
            return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

        items = []
        for c in cards:
            meta = f"{esc(c['white'])} ({esc(c['welo'])}) — {esc(c['black'])} ({esc(c['belo'])})"
            sub  = f"{esc(c['date'])} • {esc(c['opening'])} • {esc(c['tc'])}"
            orient = 'white' if c['who']=='white' else 'black'
            items.append(f"""
<div class="card" data-cat="{c['category']}" data-who="{c['who']}" data-cp="{c['cp_loss']}"
     data-fen="{esc(c['fen_before'])}" data-best="{esc(c['best_uci'])}"
     data-link="{esc(c['link'])}" data-orient="{orient}">
  <div class="info">
    <div class="title">
      <span class="tag {c['category']}">{c['category']}</span>
      <a href="{esc(c['link'])}" target="_blank" rel="noopener">#{c['ply']} • {esc(c['san'])}</a>
    </div>
    <div class="meta">{meta}</div>
    <div class="sub">{sub}</div>
    <div class="cp">Δ {c['cp_loss']} cp</div>
    <div class="game"><a href="https://lichess.org/{esc(c['game_id'])}" target="_blank" rel="noopener">{esc(c['game_id'])}</a></div>

    <div class="trainer">
      <div id="board-{c['id']}" class="board"></div>
      <div class="train-ui">
        <span class="msg">Сыграй лучший ход — перетяни фигуру или кликами: клетка-источник → клетка-цель.</span>
        <a class="ok-btn" href="{esc(c['link'])}" target="_blank" rel="noopener" style="display:none;">✅ Успех — открыть на Lichess</a>
      </div>
    </div>
  </div>
</div>
""")
        items_html = "".join(items)

        # локальные ассеты
        tpl = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lichess Error Gallery</title>

  <link rel="stylesheet" href="assets/chessboard-1.0.0.min.css">

  <style>
    :root {
      --bg:#0b0c10; --card:#15181d; --text:#e6e6e6; --muted:#9aa4b2;
      --inacc:#d7b300; --mist:#ff7a00; --blun:#ff3b30; --chip:#2a2f37;
      --accent:#4ea1ff; --ok:#16c47f; --bad:#ff4757;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:var(--bg);color:var(--text)}
    header{padding:16px 20px;position:sticky;top:0;background:rgba(11,12,16,.9);backdrop-filter:blur(6px);border-bottom:1px solid #222;z-index:10}
    h1{margin:0 0 8px 0;font-size:20px}
    .stats{color:var(--muted);font-size:13px}
    .filters{display:flex;gap:12px;flex-wrap:wrap;margin-top:10px}
    .chip{background:var(--chip);padding:6px 10px;border-radius:999px;display:flex;gap:8px;align-items:center}
    .chip input{transform:translateY(1px)}
    .chip label{font-size:13px}
    .range{display:flex;align-items:center;gap:8px}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px;padding:16px}
    .card{background:var(--card);border-radius:12px;overflow:hidden;border:1px solid #262a31}
    .info{padding:12px}
    .title{display:flex;align-items:center;gap:10px;font-weight:600}
    .title a{color:var(--text);text-decoration:none}.title a:hover{color:var(--accent)}
    .meta,.sub{color:var(--muted);font-size:13px;margin-top:6px}
    .cp{margin-top:8px;font-variant-numeric:tabular-nums}
    .tag{font-size:12px;text-transform:uppercase;letter-spacing:.6px;padding:2px 8px;border-radius:999px}
    .tag.inaccuracy{background:var(--inacc);color:#000}
    .tag.mistake{background:var(--mist);color:#000}
    .tag.blunder{background:var(--blun);color:#fff}
    .trainer{display:flex;gap:12px;align-items:flex-start;margin-top:12px}
    .board{width:360px;max-width:100%%;min-height:360px;background:#0e1116;border:1px solid #262a31}
    .board .square-55d63.selected{box-shadow:0 0 0 3px var(--accent) inset}
    .train-ui{display:flex;flex-direction:column;gap:8px;font-size:13px;color:var(--muted)}
    .ok-btn{background:var(--ok);color:#000;text-decoration:none;padding:8px 10px;border-radius:8px;display:inline-block;font-weight:600}
    .bad{color:var(--bad)}
    footer{text-align:center;color:var(--muted);font-size:12px;padding:16px}
  </style>
</head>
<body>
<header>
  <h1>Ляпы под микроскопом — Error Gallery</h1>
  <div class="stats">Просканировано игр: <b>%(total_games)d</b> • Найдено позиций: <b>%(total_errors)d</b></div>
  <div class="filters">
    <span class="chip"><input type="checkbox" id="f-inacc" checked><label for="f-inacc">Inaccuracy</label></span>
    <span class="chip"><input type="checkbox" id="f-mist" checked><label for="f-mist">Mistake</label></span>
    <span class="chip"><input type="checkbox" id="f-blun" checked><label for="f-blun">Blunder</label></span>
    <span class="chip"><input type="checkbox" id="f-white" checked><label for="f-white">Белые</label></span>
    <span class="chip"><input type="checkbox" id="f-black" checked><label for="f-black">Чёрные</label></span>
    <span class="chip range">
      <label for="f-cp">Мин. Δcp</label>
      <input type="range" id="f-cp" min="0" max="800" step="10" value="0">
      <span id="f-cpv">0</span>
    </span>
  </div>
</header>
<main class="grid" id="grid">
  %(items)s
</main>
<footer>Статический отчёт + тренажёр. Ссылки ведут на соответствующие позиции в партиях на Lichess.</footer>

<script src="assets/chess.min.js"></script>
<script src="assets/chessboard-1.0.0.min.js"></script>
<script>
const qs = s => document.querySelector(s);
const qsa = s => Array.from(document.querySelectorAll(s));
const f = { inacc: qs('#f-inacc'), mist: qs('#f-mist'), blun: qs('#f-blun'),
            white: qs('#f-white'), black: qs('#f-black'), cp: qs('#f-cp'), cpv: qs('#f-cpv') };

function applyFilters(){
  const show={inaccuracy:f.inacc.checked,mistake:f.mist.checked,blunder:f.blun.checked};
  const who={white:f.white.checked,black:f.black.checked};
  const mincp=parseInt(f.cp.value,10)||0; f.cpv.textContent=mincp;
  qsa('.card').forEach(card=>{
    const ok = !!show[card.dataset.cat] && !!who[card.dataset.who] && (+card.dataset.cp>=mincp);
    card.style.display = ok ? '' : 'none';
  });
}
['change','input'].forEach(ev=>[f.inacc,f.mist,f.blun,f.white,f.black,f.cp].forEach(el=>el.addEventListener(ev,applyFilters)));
applyFilters();

const PIECES_URL='assets/img/{piece}.png';

function initTrainer(){
  if(typeof Chess==='undefined'||typeof Chessboard==='undefined'){
    console.error('Chess libs not loaded');
    qsa('.card .train-ui .msg').forEach(el=>el.textContent='Не удалось загрузить доску (локальные библиотеки не подгрузились).');
    return;
  }
  qsa('.card').forEach(card=>{
    if(card.dataset.init==='1') return;
    card.dataset.init='1';
    const fen=card.dataset.fen;
    const best=(card.dataset.best||'').trim().toLowerCase();
    const orient=(card.dataset.orient==='white')?'white':'black';
    const boardEl=card.querySelector('.board');
    const okBtn=card.querySelector('.ok-btn');
    const msgEl=card.querySelector('.msg');
    if(!fen||!boardEl||!best) return;

    const game=new Chess(fen);
    const cfg={
      position:fen,draggable:true,orientation:orient,
      pieceTheme:PIECES_URL,
      onDrop:(source,target)=>{
        const move=game.move({from:source,to:target,promotion:'q'});
        if(move===null) return 'snapback';
        const uci=(source+target+(move.promotion?move.promotion:'')).toLowerCase();
        if(uci===best){
          msgEl.textContent=`Верно! Лучший ход: ${move.san}`; msgEl.classList.remove('bad'); okBtn.style.display='inline-block';
        }else{
          msgEl.textContent='Неверно. Попробуй ещё раз.'; msgEl.classList.add('bad');
          setTimeout(()=>{game.undo(); board.position(game.fen());},200);
        }
      }
    };
    const board=Chessboard(boardEl,cfg);
    board.resize(); setTimeout(()=>board.resize(),0);

    // клик-ту-мув
    let selected=null;
    boardEl.addEventListener('click',(e)=>{
      const sqEl=e.target.closest('.square-55d63'); if(!sqEl) return;
      const sq=sqEl.getAttribute('data-square');
      const clearSel=()=>boardEl.querySelectorAll('.square-55d63.selected').forEach(el=>el.classList.remove('selected'));
      if(!selected){ selected=sq; clearSel(); sqEl.classList.add('selected'); return; }
      const move=game.move({from:selected,to:sq,promotion:'q'}); clearSel(); selected=null;
      if(move===null) return;
      const uci=(move.from+move.to+(move.promotion?move.promotion:'')).toLowerCase();
      if(uci===best){ msgEl.textContent=`Верно! Лучший ход: ${move.san}`; msgEl.classList.remove('bad'); okBtn.style.display='inline-block'; }
      else { msgEl.textContent='Неверно. Попробуй ещё раз.'; msgEl.classList.add('bad'); setTimeout(()=>{game.undo(); board.position(game.fen());},200); }
      board.position(game.fen());
    });
    window.addEventListener('resize',()=>board.resize());
  });
}
if(document.readyState!=='loading') initTrainer();
document.addEventListener('DOMContentLoaded',initTrainer);
window.addEventListener('load',initTrainer);
</script>
</body>
</html>
"""
        return tpl % {"total_games": total_games, "total_errors": total_errors, "items": items_html}

# ----------------- CLI -----------------
def main():
    p = argparse.ArgumentParser(description="Lichess Error Gallery")
    p.add_argument("--user", required=True)
    p.add_argument("--token", default=os.environ.get("LICHESS_TOKEN",""))
    p.add_argument("--out", default="out")
    p.add_argument("--max-games", type=int, default=200)
    p.add_argument("--since"); p.add_argument("--until")
    p.add_argument("--perf", help="bullet,blitz,rapid,classical")
    p.add_argument("--depth", type=int, default=14)
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--hash-mb", type=int, default=256)
    p.add_argument("--who", default="white,black")
    p.add_argument("--min-cp", type=int, default=50)
    p.add_argument("--mistake", type=int, default=150)
    p.add_argument("--blunder", type=int, default=300)
    args = p.parse_args()

    out_dir = Path(args.out)
    thresholds = {"inaccuracy": max(0,args.min_cp), "mistake": args.mistake, "blunder": args.blunder}
    who = (("white" in args.who), ("black" in args.who))
    perf = args.perf.split(",") if args.perf else []

    analyzer = Analyzer(
        user=args.user, token=(args.token or None), out_dir=out_dir,
        max_games=args.max_games, since=args.since, until=args.until, perf=perf,
        stockfish_path=env("STOCKFISH_PATH","stockfish"),
        depth=args.depth, threads=args.threads, hash_mb=args.hash_mb,
        who=who, thresholds=thresholds, min_cp_show=args.min_cp,
    )
    try:
        pgns = analyzer.fetch_pgns()
        analyzed: List[Dict[str, Any]] = []
        for idx, pgn in enumerate(pgns, 1):
            print(f"[{idx}/{len(pgns)}] Analyzing...", file=sys.stderr)
            try: analyzed.append(analyzer.analyze_pgn(pgn))
            except Exception as e: print(f"  Skipped game due to error: {e}", file=sys.stderr)
        analyzer.render_gallery(analyzed)
    finally:
        analyzer.close()

if __name__ == "__main__":
    main()
