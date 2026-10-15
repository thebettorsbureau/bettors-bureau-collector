#!/usr/bin/env python3
"""
bb_collector.py — Bettors Bureau live line / split / steam collector.

Every run (GitHub Actions, every 15 min):
  1. Pulls the Action Network pro report for NFL, NCAAF, NBA, NCAAB, NHL, MLB
     (today + tomorrow).
  2. Upserts game info / status / scores into live_games.
  3. Writes a row to live_odds for every line, price or split % that changed
     since the last run (per book, market, side).
  4. Writes a row to live_signals for every sharp/steam/RLM signal that changed.

Env (GitHub repo secrets, or .env locally):
  SUPABASE_URL, SUPABASE_KEY      required
  AN_TOKEN                        optional — Action Network Pro token. Unlocks the
                                  pro report (steam / sharp signals) on every game.
  AN_AUTH_HEADER                  optional — header name for the token
                                  (default "Authorization", sent as "Bearer <token>").
                                  Set AN_AUTH_SCHEME="" to send the raw token.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SPORTS = ["nfl", "ncaaf", "nba", "ncaab", "nhl", "mlb", "wnba", "ncaaw"]
# College basketball returns nothing unless division=D1 is sent; tournament=0 picks up some
# neutral-site/tournament dates the plain D1 call misses, so both variants are merged.
SPORT_VARIANTS = {
    "ncaab": [{"division": "D1"}, {"division": "D1", "tournament": "0"}],
    "ncaaw": [{"division": "D1"}, {"division": "D1", "tournament": "0"}],
}
BOOKS = "15,30,12,1071,1073,1074,3,21"
MARKETS = {"spread", "total", "moneyline"}
AN = "https://api.actionnetwork.com/web/v2/scoreboard/proreport/{sport}"


def load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


load_env()
SB_URL = (os.environ.get("SUPABASE_URL") or "https://qkvkupbekkozhmqqvxqh.supabase.co").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_KEY")
AN_TOKEN = os.environ.get("AN_TOKEN", "")
AN_HEADER = os.environ.get("AN_AUTH_HEADER") or "Authorization"
AN_SCHEME = os.environ.get("AN_AUTH_SCHEME", "Bearer")
KEEP_DAYS = 4

SB = {"apikey": SB_KEY or "", "Authorization": f"Bearer {SB_KEY}", "Content-Type": "application/json"}
AN_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
AN_PARAMS = {}


def detect_an_auth():
    """Try common ways of sending the Pro token; keep the one that unlocks date-based history."""
    if not AN_TOKEN:
        print("AN auth: no AN_TOKEN set (public feed only)")
        return
    base = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    tries = [
        ("Authorization: Bearer", {**base, "Authorization": f"Bearer {AN_TOKEN}"}, {}),
        ("Authorization: raw", {**base, "Authorization": AN_TOKEN}, {}),
        ("Cookie AN_SESSION_TOKEN_V1", {**base, "Cookie": f"AN_SESSION_TOKEN_V1={AN_TOKEN}"}, {}),
        ("X-Auth-Token", {**base, "X-Auth-Token": AN_TOKEN}, {}),
        ("x-api-key", {**base, "x-api-key": AN_TOKEN}, {}),
        ("query token", base, {"token": AN_TOKEN}),
        ("query access_token", base, {"access_token": AN_TOKEN}),
    ]
    if AN_HEADER and AN_HEADER != "Authorization":
        tries.insert(0, (f"{AN_HEADER} (custom)", {**base, AN_HEADER: f"{AN_SCHEME} {AN_TOKEN}".strip()}, {}))
    test = {"bookIds": BOOKS, "periods": "event", "date": "20260317", "division": "D1", "tournament": "0"}

    def pro_count(hdr, qp):
        try:
            r = requests.get(AN.format(sport="ncaab"), headers=hdr, timeout=30, params={**test, **qp})
            g = r.json().get("games", []) if r.status_code == 200 else []
            return sum(1 for x in g if x.get("pro_report"))
        except Exception:
            return -1

    anon = pro_count(base, {})
    print(f"AN auth: public feed shows pro report on {anon} test games")
    for name, hdr, qp in tries:
        n = pro_count(hdr, qp)
        if n > anon:
            AN_HEADERS.clear(); AN_HEADERS.update(hdr); AN_PARAMS.update(qp)
            print(f"AN auth: token accepted via {name} (pro report on {n} test games)")
            return
    print("AN auth: token NOT accepted by any method (public feed only)")


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------- fetch
def fetch_sport(sport):
    games = {}
    for day in (NOW, NOW + timedelta(days=1)):
        for variant in SPORT_VARIANTS.get(sport, [{}]):
            params = {"bookIds": BOOKS, "periods": "event", "date": day.strftime("%Y%m%d"), **variant, **AN_PARAMS}
            for attempt in range(3):
                try:
                    r = requests.get(AN.format(sport=sport), params=params, headers=AN_HEADERS, timeout=30)
                    if r.status_code == 200:
                        for g in r.json().get("games", []):
                            games[g["id"]] = g
                        break
                except requests.RequestException:
                    pass
                time.sleep(2 * (attempt + 1))
    return list(games.values())


# ---------------------------------------------------------------- parse
def mins_to_start(g):
    try:
        st = datetime.fromisoformat(g["start_time"].replace("Z", "+00:00"))
        return int((st - NOW).total_seconds() // 60)
    except Exception:
        return None


def parse_game(sport, g):
    teams = {t["id"]: t.get("full_name") for t in g.get("teams", [])}
    box = g.get("boxscore") or {}
    game = {
        "sport": sport, "game_id": g["id"], "start_time": g.get("start_time"),
        "season": g.get("season"), "week": g.get("week"),
        "home_team_id": g.get("home_team_id"), "away_team_id": g.get("away_team_id"),
        "home_team": teams.get(g.get("home_team_id")), "away_team": teams.get(g.get("away_team_id")),
        "status": g.get("status"),
        "home_score": box.get("total_home_points"), "away_score": box.get("total_away_points"),
        "updated_at": NOW.isoformat(),
    }
    mts = mins_to_start(g)
    home_id = g.get("home_team_id")

    odds = []
    for book_id, mk in (g.get("markets") or {}).items():
        for market, outcomes in (mk.get("event") or {}).items():
            if market not in MARKETS:
                continue
            for o in outcomes or []:
                if o.get("is_alt_market"):
                    continue
                side = o.get("side")
                if market in ("spread", "moneyline") and side not in ("home", "away"):
                    side = "home" if o.get("team_id") == home_id else "away"
                bi = o.get("bet_info") or {}
                odds.append({
                    "sport": sport, "game_id": g["id"], "book_id": int(book_id), "market": market,
                    "side": side, "value": o.get("value"), "odds": o.get("odds"),
                    "tickets_pct": (bi.get("tickets") or {}).get("percent"),
                    "money_pct": (bi.get("money") or {}).get("percent"),
                    "is_live": bool(o.get("is_live")), "mins_to_start": mts,
                })

    signals = []
    for market, sides in (g.get("pro_report") or {}).items():
        if not isinstance(sides, dict):
            continue
        for side, sigs in sides.items():
            for s in sigs or []:
                meta = s.get("meta") or {}
                signals.append({
                    "sport": sport, "game_id": g["id"], "market": market, "side": side,
                    "signal_type": s.get("signal_type"), "strength": s.get("strength"),
                    "steam_moves": meta.get("steam_moves"),
                    "reverse_line_moves": meta.get("reverse_line_moves"),
                    "explanation": s.get("explanation"), "mins_to_start": mts,
                })
    return game, odds, signals


def odds_key(o):
    return f"o|{o['sport']}|{o['game_id']}|{o['book_id']}|{o['market']}|{o['side']}|{int(o['is_live'])}"


def odds_hash(o):
    return f"{o['value']}|{o['odds']}|{o['tickets_pct']}|{o['money_pct']}"


def sig_key(s):
    return f"s|{s['sport']}|{s['game_id']}|{s['market']}|{s['side']}|{s['signal_type']}"


def sig_hash(s):
    return f"{s['strength']}|{s['steam_moves']}|{s['reverse_line_moves']}"


def changed_only(rows, keyf, hashf, latest):
    out, new_latest = [], {}
    for r in rows:
        k, h = keyf(r), hashf(r)
        if latest.get(k) != h:
            out.append(r)
            new_latest[k] = h
    return out, new_latest


# ---------------------------------------------------------------- supabase
def sb_get_latest(prefixes):
    latest = {}
    for pref in prefixes:
        offset = 0
        while True:
            r = requests.get(f"{SB_URL}/rest/v1/live_latest",
                             params={"select": "k,h", "k": f"like.{pref}*"},
                             headers={**SB, "Range": f"{offset}-{offset + 999}"}, timeout=60)
            r.raise_for_status()
            rows = r.json()
            latest.update({x["k"]: x["h"] for x in rows})
            if len(rows) < 1000:
                break
            offset += 1000
    return latest


def sb_post(table, rows, upsert_on=None):
    if not rows:
        return
    url = f"{SB_URL}/rest/v1/{table}"
    hdr = dict(SB)
    if upsert_on:
        url += f"?on_conflict={upsert_on}"
        hdr["Prefer"] = "resolution=merge-duplicates,return=minimal"
    else:
        hdr["Prefer"] = "return=minimal"
    for i in range(0, len(rows), 500):
        r = requests.post(url, headers=hdr, data=json.dumps(rows[i:i + 500]), timeout=120)
        if r.status_code >= 300:
            raise RuntimeError(f"{table}: {r.status_code} {r.text[:300]}")


# ---------------------------------------------------------------- main
def run(dry=False):
    detect_an_auth()
    tot = {"games": 0, "odds_seen": 0, "odds_written": 0, "sig_seen": 0, "sig_written": 0}
    for sport in SPORTS:
        games_raw = fetch_sport(sport)
        games, odds, sigs = [], [], []
        for g in games_raw:
            gm, o, s = parse_game(sport, g)
            games.append(gm); odds += o; sigs += s
        if not games:
            print(f"{sport}: no games")
            continue
        latest = {} if dry else sb_get_latest([f"o|{sport}|", f"s|{sport}|"])
        o_new, o_lat = changed_only(odds, odds_key, odds_hash, latest)
        s_new, s_lat = changed_only(sigs, sig_key, sig_hash, latest)
        cap = NOW.isoformat()
        for r in o_new + s_new:
            r["captured_at"] = cap
        if not dry:
            sb_post("live_games", games, upsert_on="sport,game_id")
            sb_post("live_odds", o_new)
            sb_post("live_signals", s_new)
            sb_post("live_latest", [{"k": k, "h": h, "updated_at": cap} for k, h in {**o_lat, **s_lat}.items()],
                    upsert_on="k")
        print(f"{sport}: {len(games)} games | odds {len(odds)} seen, {len(o_new)} changed | "
              f"signals {len(sigs)} seen, {len(s_new)} changed")
        tot["games"] += len(games); tot["odds_seen"] += len(odds); tot["odds_written"] += len(o_new)
        tot["sig_seen"] += len(sigs); tot["sig_written"] += len(s_new)
    if not dry:
        cutoff = (NOW - timedelta(days=KEEP_DAYS)).isoformat()
        requests.delete(f"{SB_URL}/rest/v1/live_latest", params={"updated_at": f"lt.{cutoff}"}, headers=SB, timeout=60)
    print("TOTAL", tot)
    return tot


if __name__ == "__main__":
    if not SB_KEY and "--dry" not in sys.argv:
        sys.exit("SUPABASE_KEY not set")
    run(dry="--dry" in sys.argv)

