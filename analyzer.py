#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Lichess Error Gallery + Trainer
- Загружает последние партии пользователя с Lichess
- Анализирует Stockfish'ем
- Находит ходы с потерей оценки (inaccuracy/mistake/blunder)
- Рендерит статический HTML-отчёт с интерактивными досками:
  пользователь должен найти лучший ход (перетяжкой). Верно -> «Успех!», неверно -> откат.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import berserk
import chess
import chess.engine
import chess.pgn


# ----------------------------- утилиты ----------------------------------------

def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return default if v in (None, "") else v


def to_millis(date_str: str) -> int:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def score_to_cp(score: chess.engine.PovScore) -> int:
    # mate = +/-100000
    if score.is_mate():
        m = score.mate()
        return 100000 if (m and m > 0) else -100000
    return int(score.score(mate_score=100000))


def classify(delta: int, thresholds: Dict[str, int]) -> Optional[str]:
    if delta >= thresholds["blunder"]:
        return "blunder"
    if delta >= thresholds["mistake"]:
        return "mistake"
    if delta >= thresholds["inaccuracy"]:
        return "inaccuracy"
    return None


def lichess_ply_link(game_id: str, ply: int) -> str:
    return f"https://lichess.org/{game_id}#{ply}"


# ----------------------------- анализатор -------------------------------------

class Analyzer:
    def __init__(
        self,
        user: str,
        token: Optional[str],
        out_dir: Path,
        max_games: int = 20,
        since: Optional[str] = None,
        until: Optional[str] = None,
        perf: Optional[List[str]] = None,
        stockfish_path: Optional[str] = None,
        depth: int = 14,
        threads: int = 2,
        hash_mb: int = 256,
        thresholds: Dict[str, int] = None,
        min_cp_show: int = 50,
    ):
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
        self.thresholds = thresholds or {"inaccuracy": 50, "mistake": 150, "blunder": 300}
        self.min_cp_show = min_cp_show
        self.stockfish_path = stockfish_path or env("STOCKFISH_PATH", "stockfish")
        self.client = self._make_client()
        self.engine: Optional[chess.engine.SimpleEngine] = None

    # --- infra

    def _make_client(self):
        if self.token:
            session = berserk.TokenSession(self.token)
            return berserk.Client(session=session)
        return berserk.Client()

    def _eng(self) -> chess.engine.SimpleEngine:
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

    # --- загрузка

    def _assert_user_exists(self):
        # Простая проверка: тянем json пользователя через public endpoint (без токена)
        import urllib.request, json
        url = f"https://lichess.org/api/user/{self.user}"
        req = urllib.request.Request(url, headers={"User-Agent": "tactikcheck"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        assert "username" in data, "User not found"

    def fetch_pgns(self) -> List[str]:
        self._assert_user_exists()
        params = dict(
            max=self.max_games,
            moves=True,
            opening=True,
            clocks=False,
            evals=False,
            as_pgn=True,
        )
        if self.since:
            params["since"] = to_millis(self.since)
        if self.until:
            params["until"] = to_millis(self.until) + 24 * 3600 * 1000 - 1
        if self.perf:
            # В berserk параметр называется perf_type (а не perfType)
            params["perf_type"] = ",".join(self.perf)

        print(f"Downloading games for {self.user} (max={self.max_games})...", file=sys.stderr)
        it = self.client.games.export_by_player(self.user, **params)
        pgns: List[str] = []
        for pgn in it:
            if not pgn:
                continue
            pgns.append(pgn if isinstance(pgn, str) else pgn.get("pgn", ""))
        print(f"Got {len(pgns)} PGNs.", file=sys.stderr)
        return pgns

    # --- анализ

    def analyze_pgn(self, pgn_text: str) -> Dict[str, Any]:
        game = chess.pgn.read_game(io.StringIO(pgn_text))
        headers = game.headers
        gid = headers.get("LichessURL", "").split("/")[-1] or headers.get("Site", "").split("/")[-1]
        meta = {
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
        eng = self._eng()
        limit = chess.engine.Limit(depth=self.depth)

        while not node.is_end():
            node = node.variation(0)
            move = node.move
            side_to_move = board.turn  # чей ход ПЕРЕД ходом из партии
            ply += 1

            # 1) оценка лучшего для текущей стороны
            info_best = eng.analyse(board, limit=limit)
            best_cp = score_to_cp(info_best["score"].pov(side_to_move))
            # сам лучший ход (UCI), надёжно через play()
            best_move_uci = eng.play(board, limit=limit).move.uci()

            # 2) оценка реально сыгранного хода
            info_played = eng.analyse(board, limit=limit, root_moves=[move])
            played_cp = score_to_cp(info_played["score"].pov(side_to_move))

            delta = best_cp - played_cp
            label = classify(delta, self.thresholds)

            # если существенная потеря — сохраним позицию ДО хода + лучший ход
            if label and delta >= self.min_cp_show:
                fen_before = board.fen()
                san = node.san()  # SAN текущего (уже известного) хода
                who = "white" if side_to_move == chess.WHITE else "black"
                meta["errors"].append({
                    "ply": ply,
                    "move_no": (ply + 1) // 2,
                    "who": who,
                    "san": san,
                    "cp_loss": delta,
                    "category": label,
                    "fen_before": fen_before,
                    "best_uci": best_move_uci,
                    "link": lichess_ply_link(gid, ply),
                })

            # Переходим к позиции ПОСЛЕ хода из партии
            board.push(move)

        return meta

    # --- рендер

    def render_gallery(self, analyzed: List[Dict[str, Any]]):
        out = self.out_dir
        out.mkdir(parents=True, exist_ok=True)
        cards: List[Dict[str, Any]] = []
        total_games = len(analyzed)
        total_errors = 0

        for g in analyzed:
            gid = g["game_id"]
            for e in g["errors"]:
                total_errors += 1
                cards.append({
                    "game_id": gid,
                    "white": g["white"], "black": g["black"],
                    "welo": g["white_elo"] or "", "belo": g["black_elo"] or "",
                    "date": g["date"], "opening": g["opening"], "tc": g["time_control"],
                    "ply": e["ply"], "move_no": e["move_no"], "san": e["san"],
                    "who": e["who"], "cp_loss": e["cp_loss"], "category": e["category"],
                    "fen_before": e["fen_before"], "best_uci": e["best_uci"],
                    "link": e["link"],
                })

        html = self._build_html(cards, total_games, total_errors)
        (out / "index.html").write_text(html, encoding="utf-8")
        print(f"Wrote gallery: {out/'index.html'}  ({total_games} games, {total_errors} flagged moves)")

    def _build_html(self, cards: List[Dict[str, Any]], total_games: int, total_errors: int) -> str:
        # карточки
        items = []
        for i, c in enumerate(cards, 1):
            # ориентируем доску по стороне, которая должна ходить
            orient = "white" if c["who"] == "white" else "black"
            badge = c["category"].upper()
            items.append(f"""
<div class="card tactic"
     data-id="{i}"
     data-fen="{c['fen_before']}"
     data-best="{c['best_uci']}"
     data-turn="{ 'w' if c['who']=='white' else 'b' }">
  <div class="head">
    <span class="tag {c['category']}">{badge}</span>
    <span class="title">#{c['move_no']} • {c['san']}</span>
  </div>
  <div class="meta">
    {c['white']} ({c['welo']}) — {c['black']} ({c['belo']})<br/>
    {c['date']} • {c['opening']} • {c['tc']}
  </div>
  <div class="cp">Δ {c['cp_loss']} cp</div>
  <div class="link"><a href="{c['link']}" target="_blank" rel="noopener">{c['game_id']}</a></div>

  <div class="board-wrap">
    <chess-board id="board-{i}" class="board"
                 position="{c['fen_before']}"
                 orientation="{orient}"
                 draggable-pieces
                 animation-duration="200">
    </chess-board>
    <div class="help">Сыграй лучший ход — перетяни фигуру (или клик-клик). Ход другой стороны запрещён.</div>
    <button id="ok-{i}" class="ok" style="display:none">✅ Успех!</button>
  </div>
</div>
""")

        # весь HTML (инлайн CSS + CDN скрипты; без локальных ассетов)
        # В f-строке экранируем фигурные скобки двойными {{ }}
        html = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Lichess Error Gallery + Trainer</title>
  <style>
    :root {{
      --bg: #0b0c10; --card:#15181d; --stroke:#262a31; --text:#e6e6e6; --muted:#9aa4b2;
      --chip:#2a2f37; --inacc:#d7b300; --mist:#ff7a00; --blun:#ff3b30; --accent:#4ea1ff;
    }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif }}
    header {{ padding:16px 20px; border-bottom:1px solid var(--stroke); position:sticky; top:0; background:rgba(11,12,16,.86); backdrop-filter:blur(6px); z-index:10 }}
    h1 {{ margin:0 0 6px 0; font-size:20px }}
    .stats {{ color:var(--muted); font-size:13px }}
    main.grid {{ display:grid; gap:16px; padding:16px; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)) }}
    .card {{ border:1px solid var(--stroke); background:var(--card); border-radius:12px; padding:12px }}
    .head {{ display:flex; gap:10px; align-items:center; margin-bottom:6px; font-weight:600 }}
    .tag {{ font-size:12px; text-transform:uppercase; letter-spacing:.6px; padding:2px 8px; border-radius:999px }}
    .tag.inaccuracy {{ background:var(--inacc); color:#000 }}
    .tag.mistake {{ background:var(--mist); color:#000 }}
    .tag.blunder {{ background:var(--blun); color:#fff }}
    .meta, .cp, .link, .help {{ color:var(--muted); font-size:13px; margin-top:6px }}
    .board-wrap {{ margin-top:10px }}
    chess-board.board {{ width: 360px; max-width: 100%; display:block; border-radius:8px; overflow:hidden; border:1px solid var(--stroke) }}
    .ok {{ margin-top:10px; background:#1f6f3e; color:#fff; border:none; padding:8px 12px; border-radius:8px; cursor:pointer }}
    .ok:hover {{ filter:brightness(1.05) }}
    footer {{ text-align:center; color:var(--muted); font-size:12px; padding:16px }}
  </style>

  <!-- Доска как web-component + логика правил -->
  <script type="module" src="https://unpkg.com/chessboard-element?module"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/chess.js/0.13.4/chess.min.js" integrity="sha512-1v6Y7rphQbqJpS6kpV7mL0k0b8f0kRr8p2r3+0Xk8j7H8mS7pVq4HkQ5xvT0vL0xG0Y4+5vCNY1Q3Q1xv7z+0w==" crossorigin="anonymous"></script>
</head>
<body>
  <header>
    <h1>Ляпы под микроскопом — Error Gallery + Trainer</h1>
    <div class="stats">Просканировано игр: <b>{total_games}</b> • Найдено позиций: <b>{total_errors}</b></div>
    <div class="stats">Кликни/перетащи лучший ход на каждой доске. Верно — появится «Успех!», неверно — фигура откатится.</div>
  </header>

  <main class="grid" id="grid">
    {''.join(items)}
  </main>

  <footer>Отчёт сгенерирован автоматически. Доски интерактивны без перехода на внешние сайты.</footer>

  <script>
  (() => {{
    // Инициализация всех карточек с тактиками
    const tactics = Array.from(document.querySelectorAll('.card.tactic'));
    for (const t of tactics) {{
      const id   = t.dataset.id;
      const fen  = t.dataset.fen;
      const best = (t.dataset.best || '').trim().toLowerCase(); // uci
      const turn = t.dataset.turn; // 'w'/'b'

      const board = document.getElementById('board-' + id);
      const okBtn = document.getElementById('ok-' + id);

      // chess.js – логика правил/легальности
      const game = new window.Chess();
      try {{
        game.load(fen);
      }} catch (e) {{
        console.error('Bad FEN', fen, e);
        continue;
      }}

      let solved = false;

      // Разрешаем двигать только фигуры стороны, чей ход в позиции
      board.addEventListener('drag-start', (e) => {{
        if (solved) {{ e.preventDefault(); return; }}
        const piece = e.detail.piece; // 'wP','bK',...
        if (game.game_over()) {{ e.preventDefault(); return; }}
        if ((game.turn() === 'w' && piece.startsWith('b')) ||
            (game.turn() === 'b' && piece.startsWith('w'))) {{
          e.preventDefault();
        }}
      }});

      // Обработка хода
      board.addEventListener('drop', (e) => {{
        if (solved) {{ e.detail.setAction('snapback'); return; }}
        const source = e.detail.source;
        const target = e.detail.target;

        // Попытка применить ход (promotion по умолчанию в ферзя)
        const move = game.move({{ from: source, to: target, promotion: 'q' }});
        if (move === null) {{
          e.detail.setAction('snapback');
          return;
        }}

        // Сравнение с лучшим ходом
        const playedUci = (source + target + (move.promotion || '')).toLowerCase();
        if (playedUci !== best) {{
          // неверно — откат и возврат состояния
          game.undo();
          e.detail.setAction('snapback');
          return;
        }}

        // Верно — фиксируем и показываем "Успех!"
        solved = true;
        okBtn.style.display = 'inline-block';
      }});

      // После анимации синхронизируем позицию (например, если было взятие)
      board.addEventListener('snap-end', () => {{
        board.setPosition(game.fen());
      }});
    }}
  }})();
  </script>
</body>
</html>
"""
        return html


# ----------------------------- CLI --------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Lichess Error Gallery + Trainer")
    p.add_argument("--user", required=True, help="Lichess username")
    p.add_argument("--token", default=os.environ.get("LICHESS_TOKEN", ""), help="Lichess API token (optional)")
    p.add_argument("--out", default="out", help="Output directory")
    p.add_argument("--max-games", type=int, default=int(env("MAX_GAMES", "10")))
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--perf", help="comma: bullet,blitz,rapid,classical,correspondence")
    p.add_argument("--depth", type=int, default=int(env("DEPTH", "12")))
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--hash-mb", type=int, default=256)
    p.add_argument("--min-cp", type=int, default=int(env("MIN_CP", "50")))
    p.add_argument("--mistake", type=int, default=int(env("MISTAKE", "150")))
    p.add_argument("--blunder", type=int, default=int(env("BLUNDER", "300")))
    args = p.parse_args()

    thresholds = {"inaccuracy": max(0, args.min_cp), "mistake": args.mistake, "blunder": args.blunder}
    perf = args.perf.split(",") if args.perf else []

    analyzer = Analyzer(
        user=args.user,
        token=(args.token or None),
        out_dir=Path(args.out),
        max_games=args.max_games,
        since=args.since,
        until=args.until,
        perf=perf,
        depth=args.depth,
        threads=args.threads,
        hash_mb=args.hash_mb,
        thresholds=thresholds,
        min_cp_show=args.min_cp,
    )

    try:
        pgns = analyzer.fetch_pgns()
        analyzed: List[Dict[str, Any]] = []
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
