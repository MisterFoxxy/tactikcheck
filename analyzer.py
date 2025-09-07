#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lichess Error Gallery: download games, analyze with Stockfish, export a static HTML gallery.
"""
import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Third-party
import berserk  # pip install berserk
import chess.pgn  # pip install python-chess
import chess.engine
import chess.svg
import io

# ---- Helpers ----------------------------------------------------------------

def env(var: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(var)
    if v is None or v == "":
        return default
    return v

def to_millis(date_str: str) -> int:
    """YYYY-MM-DD -> epoch millis (UTC 00:00)."""
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)

def score_to_cp(score: chess.engine.PovScore) -> int:
    """Convert Stockfish score to signed centipawns. Mate -> huge value."""
    s = score
    if s.is_mate():
        m = s.mate()  # positive if mate for side to move in m plies, negative if mated
        return 100000 if m and m > 0 else -100000
    cp = s.score(mate_score=100000)
    return int(cp)

def classify(delta_cp: int, thresholds: Dict[str, int]) -> Optional[str]:
    """Return 'inaccuracy' | 'mistake' | 'blunder' or None based on cp loss (>=)."""
    if delta_cp >= thresholds["blunder"]:
        return "blunder"
    if delta_cp >= thresholds["mistake"]:
        return "mistake"
    if delta_cp >= thresholds["inaccuracy"]:
        return "inaccuracy"
    return None

def lichess_ply_link(game_id: str, ply: int) -> str:
    """Anchor to a specific half-move (ply)."""
    return f"https://lichess.org/{game_id}#{ply}"

# ---- Core analysis -----------------------------------------------------------

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
        who: Tuple[bool, bool] = (True, True),  # (white, black)
        thresholds: Dict[str, int] = None,
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
        self.engine = None  # lazy

    def _make_client(self):
        """Create Lichess client. No timeout arg to support berserk 0.14.x."""
        if self.token:
            session = berserk.TokenSession(self.token)
            return berserk.Client(session=session)
        else:
            return berserk.Client()

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

    # --- Download ------------------------------------------------------------

    def fetch_pgns(self) -> List[str]:
        params = {
            "max": self.max_games,
            "moves": True,
            "opening": True,
            "clocks": False,
            "evals": False,
            "as_pgn": True,
        }
        if self.since:
            params["since"] = to_millis(self.since)
        if self.until:
            params["until"] = to_millis(self.until) + 24 * 3600 * 1000 - 1
        if self.perf:
            params["perfType"] = ",".join(self.perf)

        print(f"Downloading games for {self.user} (max={self.max_games})...", file=sys.stderr)
        pgn_iter = self.client.games.export_by_player(self.user, **params)
        pgns = []
        for i, pgn in enumerate(pgn_iter, start=1):
            if not pgn:
                continue
            pgns.append(pgn if isinstance(pgn, str) else pgn.get("pgn", ""))
        print(f"Downloaded {len(pgns)} PGNs.", file=sys.stderr)
        return pgns

    # --- Analysis ------------------------------------------------------------

    def analyze_pgn(self, pgn_text: str) -> Dict[str, Any]:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        headers = game.headers
        gid = headers.get("LichessURL", "").split("/")[-1] or headers.get("Site", "").split("/")[-1]
        result = {
            "game_id": gid,
            "white": headers.get("White", "?"),
            "black": headers.get("Black", "?"),
            "white_elo": headers.get("WhiteElo"),
            "black_elo": headers.get("BlackElo"),
            "date": headers.get("UTCDate", headers.get("Date", "")),
            "time_control": headers.get("TimeControl", ""),
            "opening": headers.get("Opening", ""),
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
            side_to_move = board.turn  # before making the move
            ply += 1

            mover_is_white = side_to_move
            if (mover_is_white and not self.who[0]) or ((not mover_is_white) and not self.who[1]):
                board.push(move)
                continue

            info_best = engine.analyse(board, limit=limit, multipv=1)
            best_cp = score_to_cp(info_best["score"].pov(side_to_move))

            info_played = engine.analyse(board, limit=limit, root_moves=[move])
            played_cp = score_to_cp(info_played["score"].pov(side_to_move))

            delta = best_cp - played_cp  # how much worse than best

            label = classify(delta, self.thresholds)
            if label and delta >= self.min_cp_show:
                board.push(move)
                fen_after = board.fen()
                san = node.san()
                move_no = (ply + 1) // 2
                who_str = "white" if mover_is_white else "black"
                result["errors"].append({
                    "ply": ply,
                    "move_no": move_no,
                    "who": who_str,
                    "san": san,
                    "cp_loss": delta,
                    "category": label,
                    "fen_after": fen_after,
                })
            else:
                board.push(move)

        return result

    # --- Render --------------------------------------------------------------

    def _svg_from_fen(self, fen: str) -> str:
        board = chess.Board(fen)
        svg = chess.svg.board(board, size=380, coordinates=True)
        return svg

    def render_gallery(self, analyzed: List[Dict[str, Any]]):
        out = self.out_dir
        out.mkdir(exist_ok=True, parents=True)
        cards = []
        total_games = len(analyzed)
        total_errors = 0

        for g in analyzed:
            gid = g["game_id"] or ""
            white = g["white"]
            black = g["black"]
            welo = g["white_elo"] or ""
            belo = g["black_elo"] or ""
            date = g["date"]
            opening = g["opening"]
            tc = g["time_control"]
            for e in g["errors"]:
                total_errors += 1
                svg = self._svg_from_fen(e["fen_after"])
                cards.append({
                    "game_id": gid,
                    "white": white, "black": black, "welo": welo, "belo": belo,
                    "date": date, "opening": opening, "tc": tc,
                    "ply": e["ply"], "move_no": e["move_no"], "san": e["san"],
                    "who": e["who"], "cp_loss": e["cp_loss"], "category": e["category"],
                    "link": lichess_ply_link(gid, e["ply"]),
                    "svg": svg,
                })

        html = self._build_html(cards, total_games, total_errors)
        (out / "index.html").write_text(html, encoding="utf-8")
        print(f"Wrote gallery: {out/'index.html'}  ({total_games} games, {total_errors} flagged moves)")

    def _build_html(self, cards: List[Dict[str, Any]], total_games: int, total_errors: int) -> str:
        def esc(s: str) -> str:
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        items = []
        for c in cards:
            meta = f"{esc(c['white'])} ({esc(c['welo'])}) — {esc(c['black'])} ({esc(c['belo'])})"
            sub = f"{esc(c['date'])} • {esc(c['opening'])} • {esc(c['tc'])}"
            items.append(f"""
<div class="card" data-cat="{c['category']}" data-who="{c['who']}" data-cp="{c['cp_loss']}">
  <div class="thumb">{c['svg']}</div>
  <div class="info">
    <div class="title">
      <span class="tag {c['category']}">{c['category']}</span>
      <a href="{esc(c['link'])}" target="_blank" rel="noopener">#{c['ply']} • {esc(c['san'])}</a>
    </div>
    <div class="meta">{meta}</div>
    <div class="sub">{sub}</div>
    <div class="cp">Δ {c['cp_loss']} cp</div>
    <div class="game"><a href="https://lichess.org/{esc(c['game_id'])}" target="_blank" rel="noopener">{esc(c['game_id'])}</a></div>
  </div>
</div>
""")

        html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lichess Error Gallery</title>
  <style>
    :root {{
      --bg: #0b0c10;
      --card: #15181d;
      --text: #e6e6e6;
      --muted: #9aa4b2;
      --inacc: #d7b300;
      --mist:  #ff7a00;
      --blun:  #ff3b30;
      --chip:  #2a2f37;
      --accent:#4ea1ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; background: var(--bg); color: var(--text);}}
    header {{ padding: 16px 20px; position: sticky; top:0; background: rgba(11,12,16,0.9); backdrop-filter: blur(6px); border-bottom: 1px solid #222; z-index: 10; }}
    h1 {{ margin: 0 0 8px 0; font-size: 20px; }}
    .stats {{ color: var(--muted); font-size: 13px; }}
    .filters {{ display:flex; gap:12px; flex-wrap:wrap; margin-top:10px; }}
    .chip {{ background: var(--chip); padding: 6px 10px; border-radius: 999px; display:flex; gap:8px; align-items:center; }}
    .chip input {{ transform: translateY(1px); }}
    .chip label {{ font-size: 13px; }}
    .range {{ display:flex; align-items:center; gap:8px; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; padding: 16px; }}
    .card {{ background: var(--card); border-radius: 12px; overflow: hidden; border:1px solid #262a31; }}
    .thumb svg {{ display:block; width:100%; height:auto; background:#0e1116; }}
    .info {{ padding: 12px; }}
    .title {{ display:flex; align-items:center; gap:10px; font-weight:600; }}
    .title a {{ color: var(--text); text-decoration: none; }}
    .title a:hover {{ color: var(--accent); }}
    .meta, .sub {{ color: var(--muted); font-size: 13px; margin-top: 6px; }}
    .cp {{ margin-top: 8px; font-variant-numeric: tabular-nums; }}
    .tag {{ font-size:12px; text-transform:uppercase; letter-spacing:.6px; padding:2px 8px; border-radius: 999px; }}
    .tag.inaccuracy {{ background: var(--inacc); color:#000; }}
    .tag.mistake {{ background: var(--mist); color:#000; }}
    .tag.blunder {{ background: var(--blun); color:#fff; }}
    footer {{ text-align:center; color: var(--muted); font-size:12px; padding: 16px; }}
  </style>
</head>
<body>
<header>
  <h1>Ляпы под микроскопом — Error Gallery</h1>
  <div class="stats">Просканировано игр: <b>{total_games}</b> • Найдено позиций: <b>{total_errors}</b></div>
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
  {''.join(items)}
</main>
<footer>Статический отчёт, создан локально. Ссылки ведут на соответствующие позиции в партиях на Lichess.</footer>
<script>
const qs = s => document.querySelector(s);
const qsa = s => Array.from(document.querySelectorAll(s));
const f = { inacc: qs('#f-inacc'), mist: qs('#f-mist'), blun: qs('#f-blun'),
            white: qs('#f-white'), black: qs('#f-black'), cp: qs('#f-cp'), cpv: qs('#f-cpv') };

function applyFilters() {{
  const show = {{
    inaccuracy: f.inacc.checked,
    mistake: f.mist.checked,
    blunder: f.blun.checked
  }};
  const who = {{ white: f.white.checked, black: f.black.checked }};
  const mincp = parseInt(f.cp.value, 10) || 0;
  f.cpv.textContent = mincp;
  qsa('.card').forEach(card => {{
    const cat = card.dataset.cat;
    const side = card.dataset.who;
    const cp = parseInt(card.dataset.cp, 10);
    const ok = !!show[cat] && !!who[side] && cp >= mincp;
    card.style.display = ok ? '' : 'none';
  }});
}}
['change','input'].forEach(ev => {{
  [f.inacc, f.mist, f.blun, f.white, f.black, f.cp].forEach(el => el.addEventListener(ev, applyFilters));
}});
applyFilters();
</script>
</body>
</html>
"""
        return html

# ---- CLI --------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Lichess Error Gallery")
    p.add_argument("--user", required=True, help="Lichess username")
    p.add_argument("--token", default=os.environ.get("LICHESS_TOKEN", ""), help="Lichess API token (optional)")
    p.add_argument("--out", default="out", help="Output directory")
    p.add_argument("--max-games", type=int, default=200)
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--perf", help="Filter by perfs, comma-separated: bullet,blitz,rapid,classical,correspondence")
    p.add_argument("--depth", type=int, default=14, help="Stockfish analysis depth")
    p.add_argument("--threads", type=int, default=2, help="Stockfish Threads")
    p.add_argument("--hash-mb", type=int, default=256, help="Stockfish Hash (MB)")
    p.add_argument("--who", default="white,black", help="Which side to flag: white,black")
    p.add_argument("--min-cp", type=int, default=50, help="Minimum CP loss to show (inaccuracy threshold)")
    p.add_argument("--mistake", type=int, default=150, help="CP loss threshold for 'mistake'")
    p.add_argument("--blunder", type=int, default=300, help="CP loss threshold for 'blunder'")
    args = p.parse_args()

    out_dir = Path(args.out)
    thresholds = {"inaccuracy": max(0, args.min_cp), "mistake": args.mistake, "blunder": args.blunder}
    who = (("white" in args.who), ("black" in args.who))
    perf = args.perf.split(",") if args.perf else []

    analyzer = Analyzer(
        user=args.user,
        token=(args.token or None),
        out_dir=out_dir,
        max_games=args.max_games,
        since=args.since,
        until=args.until,
        perf=perf,
        stockfish_path=env("STOCKFISH_PATH", "stockfish"),
        depth=args.depth,
        threads=args.threads,
        hash_mb=args.hash_mb,
        who=who,
        thresholds=thresholds,
        min_cp_show=args.min_cp,
    )

    try:
        pgns = analyzer.fetch_pgns()
        analyzed = []
        for idx, pgn in enumerate(pgns, 1):
            print(f"[{idx}/{len(pgns)}] Analyzing...", file=sys.stderr)
            try:
                analyzed.append(analyzer.analyze_pgn(pgn))
            except Exception as e:
                print(f"  Skipped game due to error: {e}", file=sys.stderr)
        analyzer.render_gallery(analyzed)
    finally:
        analyzer.close()

if __name__ == "__main__":
    main()
