#!/usr/bin/env python3
"""
bb_context_build.py — Bettors Bureau game context builder.

For every team in every game (NFL, NCAAF, NBA, NCAAB, NHL, MLB) works out the team's
state GOING INTO the game, using only games already played:
  * season record, ATS record, O/U record, home/away splits, last-5 / last-10, streak, rest
  * Elo rating (results-based, carries across seasons with an offseason pull to the mean)
  * market rating learned from closing prices (what the books think, before this game)
  * price relationship: vig-free ML win prob vs the win prob the spread implies

…and writes it to the Supabase table game_context (create it with game_context_table.sql).

Run:  python3 bb_context_build.py              (all sports)
      python3 bb_context_build.py nba nhl      (just those)
      python3 bb_context_build.py --dry        (compute + print summary, no upload)
Uses SUPABASE_URL / SUPABASE_KEY from .env (same as the loaders).
"""
import json
import math
import os
import sys
from collections import defaultdict, deque
from datetime import date

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(HERE, ".env")
if os.path.exists(p):
    for line in open(p):
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))
URL = (os.environ.get("SUPABASE_URL") or "https://qkvkupbekkozhmqqvxqh.supabase.co").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY")
H = {"apikey": KEY or "", "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

ALL = ["nfl", "ncaaf", "nba", "ncaab", "nhl", "mlb", "wnba", "cfl"]
# Elo settings per sport: K, home advantage (elo pts), offseason regression to mean
ELO = {"nfl": (20, 48, 1 / 3), "ncaaf": (25, 55, 1 / 2), "nba": (20, 70, 1 / 4),
       "ncaab": (20, 80, 1 / 2), "nhl": (6, 30, 1 / 3), "mlb": (4, 24, 1 / 3),
       "wnba": (20, 70, 1 / 4), "cfl": (20, 48, 1 / 3)}
# market-rating learning rate per game; ML-based for sports where the spread is a fixed ±1.5
MKT_LR = {"nfl": 0.20, "ncaaf": 0.20, "nba": 0.10, "ncaab": 0.12, "nhl": 0.06, "mlb": 0.04,
          "wnba": 0.10, "cfl": 0.20}
ML_SPORTS = {"nhl", "mlb"}


# ------------------------------------------------------------------ helpers
def season_of(sport, d):
    if sport in ("mlb", "wnba", "cfl"):                       # seasons inside one calendar year
        return d.year
    cutoff = 8 if sport in ("nfl", "ncaaf") else 9          # Aug for football, Sep for hoops/hockey
    return d.year if d.month >= cutoff else d.year - 1


def raw_prob(ml):
    if ml is None:
        return None
    ml = float(ml)
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def fnum(x):
    try:
        return float(x) if x is not None and x != "" else None
    except (TypeError, ValueError):
        return None


def fetch_all(table, select, extra=""):
    out, off = [], 0
    while True:
        r = requests.get(f"{URL}/rest/v1/{table}?select={select}{extra}",
                         headers={**H, "Range": f"{off}-{off + 999}"}, timeout=120)
        r.raise_for_status()
        rows = r.json()
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


# ------------------------------------------------------------------ load games
def load_games(sport):
    """Return list of games: dict with home/away rows merged."""
    if sport == "nfl":
        lines = fetch_all("si_lines", "game_date,date_sort,home_team,visitor_team,team,is_home,"
                                      "close_spread,close_total,close_ml")
        scores = fetch_all("si_games", "game_date,home_team,away_team,home_score,away_score")
        sc = {}
        for s in scores:
            sc[(s["game_date"], s["home_team"], s["away_team"])] = (s["home_score"], s["away_score"])
        rows = []
        for r in lines:
            k = (r["game_date"], r["home_team"], r["visitor_team"])
            hs = sc.get(k)
            if hs is None:  # tolerate a 1-day date difference between sources
                d = date.fromisoformat(r["game_date"])
                for off in (-1, 1):
                    hs = sc.get((date.fromordinal(d.toordinal() + off).isoformat(), k[1], k[2]))
                    if hs:
                        break
            r["home_score"], r["visitor_score"] = (hs or (None, None))
            r["season_type"] = "Regular"
            rows.append(r)
    else:
        rows = fetch_all("bb_games", "game_date,date_sort,home_team,visitor_team,team,is_home,season_type,"
                                     "home_score,visitor_score,close_spread,close_total,close_ml,days_rest",
                         f"&sport=eq.{sport}")
    games = {}
    for r in rows:
        if (r.get("season_type") or "") == "Preseason":
            continue
        g = games.setdefault((r["date_sort"], r["home_team"], r["visitor_team"]),
                             {"date_sort": int(r["date_sort"]), "game_date": r["game_date"],
                              "home": r["home_team"], "away": r["visitor_team"],
                              "hs": r.get("home_score"), "as": r.get("visitor_score"), "rows": {}})
        g["rows"]["home" if r["is_home"] else "away"] = r
    return sorted(games.values(), key=lambda g: (g["date_sort"], g["home"]))


# ------------------------------------------------------------------ spread → win prob fit
def fit_spread_curve(games):
    """One-parameter logistic: P(win) = 1 / (1 + exp(b * spread)). Grid-search b by log-likelihood."""
    pts = []
    for g in games:
        h = g["rows"].get("home")
        if not h or g["hs"] is None or g["as"] is None or g["hs"] == g["as"]:
            continue
        sp = fnum(h.get("close_spread"))
        if sp is None:
            continue
        pts.append((sp, 1 if g["hs"] > g["as"] else 0))
    if len(pts) < 200:
        return None
    best = None
    for i in range(1, 400):
        b = i / 1000
        ll = 0.0
        for sp, w in pts:
            pr = 1 / (1 + math.exp(b * sp))
            pr = min(max(pr, 1e-6), 1 - 1e-6)
            ll += math.log(pr if w else 1 - pr)
        if best is None or ll > best[1]:
            best = (b, ll)
    return best[0]


# ------------------------------------------------------------------ main build
class TeamState:
    __slots__ = ("season", "gp", "w", "l", "t", "aw", "al", "ap", "ov", "un", "hw", "hl", "rw", "rl",
                 "last", "last_ats", "margin_sum", "ats_sum", "ats_n", "streak", "last_date")

    def __init__(self, season):
        self.season = season
        self.gp = self.w = self.l = self.t = self.aw = self.al = self.ap = self.ov = self.un = 0
        self.hw = self.hl = self.rw = self.rl = 0
        self.last, self.last_ats = deque(maxlen=10), deque(maxlen=10)
        self.margin_sum = self.ats_sum = 0.0
        self.ats_n = self.streak = 0
        self.last_date = None


def build(sport):
    games = load_games(sport)
    print(f"{sport}: {len(games)} games loaded")
    b = None if sport in ML_SPORTS else fit_spread_curve(games)
    if b:
        print(f"{sport}: spread→win curve slope {b:.3f}  (e.g. -3 → {1 / (1 + math.exp(b * -3)):.1%})")
    K, HFA, REG = ELO[sport]
    lr = MKT_LR[sport]
    elo, mkt, last_season = defaultdict(lambda: 1500.0), defaultdict(float), {}
    mkt_hfa = 0.25 if sport in ML_SPORTS else 2.5
    states = {}
    out = []

    def state(team, season):
        s = states.get(team)
        if s is None or s.season != season:
            if s is not None:  # offseason: pull ratings back toward average
                elo[team] = 1500 + (elo[team] - 1500) * (1 - REG)
                mkt[team] *= (1 - REG)
            s = TeamState(season)
            states[team] = s
        return s

    for g in games:
        d = date.fromisoformat(g["game_date"])
        season = season_of(sport, d)
        hr, ar = g["rows"].get("home"), g["rows"].get("away")
        if not hr or not ar:
            continue
        sh, sa = state(g["home"], season), state(g["away"], season)

        # ---- pre-game prices
        h_ml, a_ml = fnum(hr.get("close_ml")), fnum(ar.get("close_ml"))
        ph, pa = raw_prob(h_ml), raw_prob(a_ml)
        h_imp = ph / (ph + pa) if ph and pa else None
        h_sp = fnum(hr.get("close_spread"))
        total = fnum(hr.get("close_total")) or fnum(ar.get("close_total"))
        h_sp_prob = (1 / (1 + math.exp(b * h_sp))) if (b and h_sp is not None) else None

        # ---- pre-game ratings
        elo_h, elo_a = elo[g["home"]], elo[g["away"]]
        if sport in ML_SPORTS:
            proj_h = 1 / (1 + math.exp(-(mkt[g["home"]] - mkt[g["away"]] + mkt_hfa)))   # projected home win prob
        else:
            proj_h = -(mkt[g["home"]] - mkt[g["away"]] + mkt_hfa)                        # projected home spread

        hs, as_ = g["hs"], g["as"]
        done = hs is not None and as_ is not None

        for side, me, op, st, os_, ml_imp, sp, spp, my_elo, op_elo, my_sc, op_sc in (
            ("home", g["home"], g["away"], sh, sa, h_imp, h_sp, h_sp_prob, elo_h + HFA, elo_a,
             hs, as_),
            ("away", g["away"], g["home"], sa, sh, (1 - h_imp) if h_imp is not None else None,
             (-h_sp if h_sp is not None else None), (1 - h_sp_prob) if h_sp_prob is not None else None,
             elo_a, elo_h + HFA, as_, hs)):
            row = g["rows"][side]
            if sport in ML_SPORTS:
                proj = proj_h if side == "home" else 1 - proj_h
                vs_mkt = (ml_imp - proj) if ml_imp is not None else None
            else:
                proj = proj_h if side == "home" else -proj_h
                vs_mkt = (sp - proj) if sp is not None else None
            last = list(st.last)
            last_ats = list(st.last_ats)
            margin = (my_sc - op_sc) if done else None
            covered = None
            if done and sp is not None and margin + sp != 0:
                covered = margin + sp > 0
            went_over = None
            if done and total is not None and (my_sc + op_sc) != total:
                went_over = (my_sc + op_sc) > total
            rest = (d - st.last_date).days if st.last_date else None
            out.append({
                "sport": sport, "date_sort": g["date_sort"], "home_team": g["home"], "visitor_team": g["away"],
                "team": me, "game_date": g["game_date"], "season": season, "is_home": side == "home",
                "opponent": op,
                "close_spread": sp, "close_total": total,
                "close_ml": int(fnum(row.get("close_ml"))) if fnum(row.get("close_ml")) is not None else None,
                "ml_implied_prob": round(ml_imp, 4) if ml_imp is not None else None,
                "spread_win_prob": round(spp, 4) if spp is not None else None,
                "ml_vs_spread": round(ml_imp - spp, 4) if (ml_imp is not None and spp is not None) else None,
                "games_played": st.gp, "wins": st.w, "losses": st.l, "ties": st.t,
                "win_pct": round(st.w / st.gp, 3) if st.gp else None,
                "ats_w": st.aw, "ats_l": st.al, "ats_p": st.ap,
                "ats_pct": round(st.aw / (st.aw + st.al), 3) if (st.aw + st.al) else None,
                "overs": st.ov, "unders": st.un,
                "home_w": st.hw, "home_l": st.hl, "away_w": st.rw, "away_l": st.rl,
                "last5_w": sum(last[-5:]), "last5_ats_w": sum(1 for x in last_ats[-5:] if x == 1),
                "last10_w": sum(last), "last10_ats_w": sum(1 for x in last_ats if x == 1),
                "avg_margin": round(st.margin_sum / st.gp, 2) if st.gp else None,
                "avg_ats_margin": round(st.ats_sum / st.ats_n, 2) if st.ats_n else None,
                "streak": st.streak, "days_rest": rest,
                "elo": round(my_elo, 1), "opp_elo": round(op_elo, 1), "elo_diff": round(my_elo - op_elo, 1),
                "mkt_rating": round(mkt[me], 3), "opp_mkt_rating": round(mkt[op], 3),
                "mkt_spread_proj": round(proj, 3), "spread_vs_mkt": round(vs_mkt, 3) if vs_mkt is not None else None,
                "opp_win_pct": round(os_.w / os_.gp, 3) if os_.gp else None,
                "opp_ats_pct": round(os_.aw / (os_.aw + os_.al), 3) if (os_.aw + os_.al) else None,
                "result_margin": margin, "won": (margin > 0) if margin not in (None, 0) else None,
                "covered": covered, "went_over": went_over,
            })

        # ---- post-game updates (only after every pre-game value above is recorded)
        if done:
            for me, st, my_sc, op_sc, sp, home in ((g["home"], sh, hs, as_, h_sp, True),
                                                   (g["away"], sa, as_, hs, (-h_sp if h_sp is not None else None), False)):
                m = my_sc - op_sc
                st.gp += 1
                if m > 0:
                    st.w += 1; st.streak = st.streak + 1 if st.streak > 0 else 1
                    if home: st.hw += 1
                    else: st.rw += 1
                elif m < 0:
                    st.l += 1; st.streak = st.streak - 1 if st.streak < 0 else -1
                    if home: st.hl += 1
                    else: st.rl += 1
                else:
                    st.t += 1; st.streak = 0
                st.last.append(1 if m > 0 else 0)
                st.margin_sum += m
                if sp is not None:
                    c = m + sp
                    st.ats_sum += c; st.ats_n += 1
                    if c > 0: st.aw += 1; st.last_ats.append(1)
                    elif c < 0: st.al += 1; st.last_ats.append(0)
                    else: st.ap += 1; st.last_ats.append(None)
                if total is not None:
                    tp = hs + as_
                    if tp > total: st.ov += 1
                    elif tp < total: st.un += 1
                st.last_date = d
            # Elo update with margin-of-victory multiplier
            m = hs - as_
            exp_h = 1 / (1 + 10 ** (-((elo_h + HFA) - elo_a) / 400))
            res = 1.0 if m > 0 else 0.0 if m < 0 else 0.5
            mult = math.log(abs(m) + 1) * 2.2 / (abs((elo_h + HFA) - elo_a) * 0.001 + 2.2) if m else 1.0
            delta = K * mult * (res - exp_h)
            elo[g["home"]] += delta
            elo[g["away"]] -= delta
        # market rating update from this game's closing price (known before kickoff, so it's
        # applied AFTER the row above was written — the row only sees prior games' prices)
        if sport in ML_SPORTS:
            if h_imp is not None and 0 < h_imp < 1:
                target = math.log(h_imp / (1 - h_imp))
                pred = mkt[g["home"]] - mkt[g["away"]] + mkt_hfa
                err = target - pred
                mkt[g["home"]] += lr * err; mkt[g["away"]] -= lr * err; mkt_hfa += lr * 0.05 * err
        elif h_sp is not None:
            pred = -(mkt[g["home"]] - mkt[g["away"]] + mkt_hfa)
            err = pred - h_sp
            mkt[g["home"]] += lr * err; mkt[g["away"]] -= lr * err; mkt_hfa += lr * 0.05 * err
    return out


def upload(rows):
    ep = f"{URL}/rest/v1/game_context?on_conflict=sport,date_sort,home_team,visitor_team,team"
    hd = {**H, "Prefer": "resolution=merge-duplicates,return=minimal"}
    for i in range(0, len(rows), 1000):
        for _ in range(3):
            r = requests.post(ep, headers=hd, data=json.dumps(rows[i:i + 1000]), timeout=180)
            if r.status_code < 300:
                break
        else:
            sys.exit(f"Supabase error {r.status_code}: {r.text[:400]}")
        if (i // 1000) % 20 == 0:
            print(f"  uploaded {min(i + 1000, len(rows))}/{len(rows)}")


def summary(sport, rows):
    def rate(sel):
        xs = [r for r in rows if sel(r) and r["won"] is not None]
        return (sum(r["won"] for r in xs) / len(xs), len(xs)) if xs else (None, 0)
    print(f"{sport}: {len(rows)} team rows built")
    for lab, sel in (("ML pricier than spread (ml_vs_spread > +3%)", lambda r: (r["ml_vs_spread"] or 0) > 0.03),
                     ("ML cheaper than spread (ml_vs_spread < -3%)", lambda r: (r["ml_vs_spread"] or 0) < -0.03),
                     ("Elo edge > 100 pts", lambda r: (r["elo_diff"] or 0) > 100)):
        wr, n = rate(sel)
        if n:
            print(f"   {lab:<46} win rate {wr:.1%}  (n={n})")


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    sports = [s for s in sys.argv[1:] if s in ALL] or ALL
    if not KEY:
        sys.exit("No SUPABASE_KEY in .env")
    for s in sports:
        rows = build(s)
        summary(s, rows)
        if not dry:
            upload(rows)
            print(f"{s}: uploaded to game_context")
