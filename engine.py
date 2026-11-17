#!/usr/bin/env python3
"""Self-learning sports picks engine (stdlib only).

Commands:
  run --state S [--data D] [--cache DIR] [--template T --out O]
      1. retrain the model on ~15 months of results (walk-forward replay)
      2. grade earlier picks and parlays, update what was learned from them
      3. make picks, parlays and same-game parlays, build the dashboard
  The state file holds only the ledger (picks, parlays, track record, live-learned settings);
  team ratings are rebuilt from results on every run, so a lost state never corrupts the model.
"""
import argparse, base64, gzip, lzma, time, itertools, json, math, os, subprocess, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = "https://site.api.espn.com/apis/site/v2/sports/"

# key: (section, label, path, extra query)
LEAGUES = {
    "nfl": ("nfl", "NFL", "football/nfl", ""),
    "nba": ("nba", "NBA", "basketball/nba", ""),
    "cfb": ("cfb", "College Football", "football/college-football", "&groups=80&limit=300"),
    # every soccer competition in one feed and one shared rating pool (clubs meet in cups, national teams in internationals)
    "soccer": ("soccer", "Soccer", "soccer/all", "&limit=1000"),
}
# starting parameters; everything marked "learned" is adjusted from results
DEFAULTS = {
    "nfl": dict(k=20, hfa=45, slope=25.0, sig=13.0, tsig=13.5, avg=45.0, alpha=0.12),
    "nba": dict(k=18, hfa=65, slope=28.0, sig=12.5, tsig=18.0, avg=228.0, alpha=0.08),
    "cfb": dict(k=28, hfa=55, slope=20.0, sig=16.0, tsig=17.0, avg=54.0, alpha=0.15),
    "soccer": dict(k=20, hfa=55, slope=260.0, sig=1.6, tsig=1.6, avg=2.8, alpha=0.08),
}
TIERS = [(0.70, "Very likely"), (0.60, "High"), (0.53, "Average"), (0.0, "Below average")]
DEFAULT_BLEND = {"ml": 0.35, "spread": 0.25, "total": 0.25}
SOCCER_DAYS = 3   # how many days ahead soccer picks are shown
KEEP_GRADED_DAYS = 6


# ---------------------------------------------------------------- utilities
def load_state(path):
    raw = open(path, "rb").read()
    if path.endswith(".b64"):  # compressed text form, used for storage between runs
        raw = base64.b64decode(raw)
        raw = lzma.decompress(raw) if raw[:6] == b"\xfd7zXZ\x00" else gzip.decompress(raw)
    return json.loads(raw)


def save_state(S, path):
    raw = json.dumps(S, separators=(",", ":")).encode()
    if path.endswith(".b64"):
        raw = base64.b64encode(lzma.compress(raw, preset=9))
    open(path, "wb").write(raw)


def fetch(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except Exception:
            try:
                out = subprocess.run(["curl", "-sS", "--compressed", "-m", "40", url], capture_output=True, timeout=60).stdout
                d = json.loads(out)
                if "events" in d:
                    return d
            except Exception:
                pass
    return {"events": [], "_failed": True}


def amer_to_dec(a):
    if a is None:
        return None
    return 1 + (a / 100 if a > 0 else 100 / -a)


def dec_to_amer(d):
    if d is None or d <= 1.0001:
        return None
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def to_int(x):
    try:
        s = str(x).replace("+", "").strip()
        if s.upper() in ("EVEN", "EV"):
            return 100
        return int(float(s))
    except Exception:
        return None


def to_line(x):
    try:
        return float(str(x).lstrip("ou+").replace("+", ""))
    except Exception:
        return None


def phi(z):
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def tier_of(p):
    for cut, name in TIERS:
        if p >= cut:
            return name


def novig(odds_list):
    imp = [1 / amer_to_dec(o) if o is not None else None for o in odds_list]
    if any(i is None for i in imp):
        return [None] * len(odds_list)
    s = sum(imp)
    return [i / s for i in imp]


# ---------------------------------------------------------------- parsing
def parse_event(e, lg):
    c = e["competitions"][0]
    st = c["status"]["type"]
    out = {"id": e["id"], "lg": lg, "date": e["date"], "state": st.get("state"),
           "final": bool(st.get("completed")) and st.get("name") in ("STATUS_FINAL", "STATUS_FULL_TIME", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"),
           "neutral": bool(c.get("neutralSite")), "season": (e.get("season") or {}).get("year"),
           "league": (c.get("altGameNote") or "").split(",")[0].strip() if lg == "soccer" else LEAGUES[lg][1]}
    if lg == "soccer" and (not out["league"] or out["league"].startswith("NCAA")):
        return None
    for x in c["competitors"]:
        t = x["team"]
        out[x["homeAway"]] = {"id": t["id"], "name": t.get("displayName") or t.get("name"), "abbr": t.get("abbreviation") or (t.get("name") or "")[:3].upper(),
                              "score": to_int(x.get("score")), "rec": next((r.get("summary") for r in x.get("records", [])), None),
                              "loc": t.get("location") or "", "nick": t.get("name") or "", "short": t.get("shortDisplayName") or ""}
    if "home" not in out or "away" not in out:
        return None
    o = (c.get("odds") or [None])[0]
    if o:
        g = lambda *ks: _dig(o, ks)
        od = {"book": g("provider", "name"),
              "ml_h": to_int(g("moneyline", "home", "close", "odds")), "ml_a": to_int(g("moneyline", "away", "close", "odds")),
              "ml_d": to_int(g("moneyline", "draw", "close", "odds")) or to_int(g("drawOdds", "moneyLine")),
              "sp": to_line(g("pointSpread", "home", "close", "line")), "sp_open": to_line(g("pointSpread", "home", "open", "line")),
              "sp_h": to_int(g("pointSpread", "home", "close", "odds")), "sp_a": to_int(g("pointSpread", "away", "close", "odds")),
              "tot": to_line(g("total", "over", "close", "line")), "tot_open": to_line(g("total", "over", "open", "line")),
              "tot_o": to_int(g("total", "over", "close", "odds")), "tot_u": to_int(g("total", "under", "close", "odds"))}
        if od["sp"] is None and o.get("spread") is not None and LEAGUES[lg][0] != "soccer":
            od["sp"] = to_line(o.get("spread"))
        if od["tot"] is None and o.get("overUnder") is not None:
            od["tot"] = to_line(o.get("overUnder"))
        out["odds"] = od
    return out


def _dig(d, ks):
    for k in ks:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


BAD_DATES = set()  # leagues whose feed returned implausible game dates in this run


def fetch_days(lg, days):
    _, _, path, extra = LEAGUES[lg]
    urls = [f"{BASE}{path}/scoreboard?dates={d}{extra}" for d in days]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(fetch, urls))
    # the feed sometimes answers with an empty day when busy: ask once more before believing it
    empty = [i for i, r in enumerate(res) if not r.get("events")]
    if empty and len(empty) <= 40:
        time.sleep(1.5)
        with ThreadPoolExecutor(4) as ex:
            again = list(ex.map(fetch, [urls[i] for i in empty]))
        for i, r in zip(empty, again):
            if r.get("events"):
                res[i] = r
    evs, failed = {}, 0
    for tok, r in zip(days, res):
        failed += 1 if r.get("_failed") else 0
        got = []
        for e in r.get("events", []):
            try:
                p = parse_event(e, lg)
            except Exception:
                p = None
            if p:
                got.append(p)
        # sanity check: the feed has been seen returning games stamped with a wrong year
        try:
            if len(tok) == 8 and tok.isdigit():
                want = datetime.strptime(tok, "%Y%m%d")
                bad = [p for p in got if abs((datetime.fromisoformat(p["date"][:10]) - want).days) > 3]
            else:
                yr = int(tok[:4]); bad = [p for p in got if int(p["date"][:4]) not in (yr, yr + 1)]
        except Exception:
            bad = []
        if bad:
            failed += 1; BAD_DATES.add(lg)
            continue
        for p in got:
            evs[p["id"]] = p
    return list(evs.values()), failed


# ---------------------------------------------------------------- model
def new_state():
    return {"version": 1, "leagues": {}, "calib": {}, "blend": {}, "picks": {}, "parlays": {}, "perf": {}, "daily": {},
            "backtest": {}, "log": [], "parlay_perf": {}}


def lstate(S, lg):
    sec = LEAGUES[lg][0]
    if lg not in S["leagues"]:
        d = DEFAULTS[sec]
        S["leagues"][lg] = {"teams": {}, "hfa": d["hfa"], "slope": d["slope"], "sig": d["sig"], "tsig": d["tsig"], "avg": d["avg"],
                            "sxy": 0.0, "sxx": 0.0, "n": 0, "home_w": 0, "done": {}, "season": None}
    return S["leagues"][lg]


def team(L, t):
    T = L["teams"].get(t["id"])
    if T is None:
        T = L["teams"][t["id"]] = {"n": t["name"], "a": t["abbr"], "elo": 1500.0, "off": 0.0, "def": 0.0, "g": 0, "last": []}
    T["n"], T["a"] = t["name"], t["abbr"]
    return T


def predict(S, ev):
    """Raw model outputs for a game: win probs, expected margin (home), expected total."""
    lg = ev["lg"]; sec = LEAGUES[lg][0]; L = lstate(S, lg)
    H, A = team(L, ev["home"]), team(L, ev["away"])
    hfa = 0 if ev["neutral"] else L["hfa"]
    d = H["elo"] - A["elo"] + hfa
    exp_total = L["avg"] + H["off"] + A["def"] + A["off"] + H["def"]
    if sec == "soccer":
        sup = 0.5 * (H["off"] + A["def"] - A["off"] - H["def"]) + 0.5 * d / L["slope"] + (0 if ev["neutral"] else 0.15)
        tot = max(1.6, exp_total)
        lh, la = max(0.25, (tot + sup) / 2), max(0.2, (tot - sup) / 2)
        ph = [math.exp(-lh) * lh ** i / math.factorial(i) for i in range(10)]
        pa = [math.exp(-la) * la ** i / math.factorial(i) for i in range(10)]
        grid = {(i, j): ph[i] * pa[j] for i in range(10) for j in range(10)}
        p_h = sum(v for (i, j), v in grid.items() if i > j); p_d = sum(v for (i, j), v in grid.items() if i == j)
        return {"p_h": p_h, "p_d": p_d, "p_a": 1 - p_h - p_d, "mu": lh - la, "tot": lh + la, "grid": grid, "d": d}
    p_h = 1 / (1 + 10 ** (-d / 400))
    return {"p_h": p_h, "p_d": 0.0, "p_a": 1 - p_h, "mu": d / L["slope"], "tot": exp_total, "d": d}


def cover_prob(pr, L, line_home):
    """P(home covers line), P(push)."""
    if "grid" in pr:
        w = sum(v for (i, j), v in pr["grid"].items() if i - j + line_home > 1e-9)
        pu = sum(v for (i, j), v in pr["grid"].items() if abs(i - j + line_home) < 1e-9)
        return w, pu
    return phi((pr["mu"] + line_home) / L["sig"]), 0.0


def over_prob(pr, L, line):
    if "grid" in pr:
        w = sum(v for (i, j), v in pr["grid"].items() if i + j - line > 1e-9)
        pu = sum(v for (i, j), v in pr["grid"].items() if abs(i + j - line) < 1e-9)
        return w, pu
    return phi((pr["tot"] - line) / L["tsig"]), 0.0


def calibrate(S, key, p):
    """Confidence scaling learned from results: stretches or shrinks probabilities toward what actually happened."""
    a = S["calib"].get(key, {}).get("a", 1.0)
    p = min(0.985, max(0.015, p))
    return 1 / (1 + math.exp(-a * math.log(p / (1 - p))))


def calib_update(S, key, p, won):
    C = S["calib"].setdefault(key, {"a": 1.0, "n": 0, "bins": {}})
    p = min(0.985, max(0.015, p)); x = math.log(p / (1 - p))
    q = 1 / (1 + math.exp(-C["a"] * x))
    C["n"] += 1
    C["a"] = round(min(1.6, max(0.4, C["a"] + max(0.004, 0.05 / math.sqrt(C["n"])) * ((1 if won else 0) - q) * x)), 4)
    b = C["bins"].setdefault(str(int(q * 20)), [0, 0, 0.0])  # reliability table of calibrated confidence vs outcome
    b[0] += 1; b[1] += 1 if won else 0; b[2] = round(b[2] + q, 3)


def learn_from_result(S, ev):
    """Update ratings and learned parameters from one final game. Returns the pre-game prediction."""
    lg = ev["lg"]; sec = LEAGUES[lg][0]; D = DEFAULTS[sec]; L = lstate(S, lg)
    day = ev["date"][:10]
    for t in (ev["home"], ev["away"]):  # back after a long break (new season): pull ratings part-way back to average
        T = team(L, t)
        if T.get("ld") and (datetime.fromisoformat(day) - datetime.fromisoformat(T["ld"])).days > 75:
            T["elo"] = 1500 + 0.75 * (T["elo"] - 1500); T["off"] *= 0.7; T["def"] *= 0.7
        T["ld"] = day
    pr = predict(S, ev)
    H, A = team(L, ev["home"]), team(L, ev["away"])
    hs, as_ = ev["home"]["score"], ev["away"]["score"]
    m = hs - as_
    res = 1.0 if m > 0 else 0.0 if m < 0 else 0.5
    exp = pr["p_h"] + 0.5 * pr["p_d"]
    if sec == "soccer":
        mult = 1 if abs(m) <= 1 else 1.5 if abs(m) == 2 else (11 + abs(m)) / 8
    else:
        dw = pr["d"] if m > 0 else -pr["d"]
        mult = math.log(abs(m) + 1) * 2.2 / (max(-800, dw) * 0.001 + 2.2)
    young = 1.5 if min(H["g"], A["g"]) < 4 else 1.0
    delta = D["k"] * mult * young * (res - exp)
    H["elo"] = round(H["elo"] + delta, 2); A["elo"] = round(A["elo"] - delta, 2)
    if not ev["neutral"]:
        L["hfa"] = round(min(130, max(0, L["hfa"] + 0.5 * (res - exp))), 3)
    # margin slope + spread of outcomes (learned)
    x = pr["d"]
    L["sxy"] = round(L["sxy"] * 0.999 + x * m, 2); L["sxx"] = round(L["sxx"] * 0.999 + x * x, 2); L["n"] += 1
    if L["n"] > 60 and L["sxy"] > 0:
        fit = L["sxx"] / L["sxy"]
        L["slope"] = round(0.98 * L["slope"] + 0.02 * min(D["slope"] * 2, max(D["slope"] * 0.5, fit)), 3)
    if L["n"] > 30:
        L["sig"] = round(math.sqrt(0.99 * L["sig"] ** 2 + 0.01 * (m - pr["mu"]) ** 2), 4)
        L["tsig"] = round(math.sqrt(0.99 * L["tsig"] ** 2 + 0.01 * (hs + as_ - pr["tot"]) ** 2), 4)
    # scoring profiles
    a = D["alpha"]; half = L["avg"] / 2
    ho, hd, ao, ad = H["off"], H["def"], A["off"], A["def"]
    H["off"] = round(ho + a * ((hs - half - ad) - ho), 3); A["def"] = round(ad + a * ((hs - half - ho) - ad), 3)
    A["off"] = round(ao + a * ((as_ - half - hd) - ao), 3); H["def"] = round(hd + a * ((as_ - half - ao) - hd), 3)
    L["avg"] = round(L["avg"] + 0.01 * (hs + as_ - L["avg"]), 3)
    L["home_w"] += 1 if m > 0 else 0
    for T, pf, pa in ((H, hs, as_), (A, as_, hs)):
        T["g"] += 1
        T["last"] = (T["last"] + [["W" if pf > pa else "L" if pf < pa else "D", pf, pa]])[-5:]
    return pr


# ---------------------------------------------------------------- picks
def blend_w(S, key, mkt):
    return S["blend"].get(key, {}).get("w", DEFAULT_BLEND[mkt])


def make_picks(S, ev):
    lg = ev["lg"]; sec = LEAGUES[lg][0]; L = lstate(S, lg)
    pr = predict(S, ev); od = ev.get("odds") or {}
    h, a = ev["home"]["abbr"], ev["away"]["abbr"]
    picks = []

    def fin(mkt, pm, pk):
        key = f"{sec}:{mkt}"
        pc = calibrate(S, key, pm)
        w = blend_w(S, key, mkt)
        return pc if pk is None else w * pc + (1 - w) * pk

    def add(mkt, sel, label, line, odds, pm, pk):
        p = fin(mkt, pm, pk)
        dec = amer_to_dec(odds)
        picks.append({"id": f"{ev['id']}:{mkt}", "eid": ev["id"], "lg": lg, "sp": sec, "d": ev["date"], "mkt": mkt, "sel": sel, "label": label,
                      "line": line, "odds": odds, "pm": round(pm, 4), "pk": None if pk is None else round(pk, 4), "p": round(p, 4),
                      "tier": tier_of(p), "ev": None if dec is None else round(p * dec - 1, 4), "edge": None if pk is None else round(p - pk, 4),
                      "st": "pending", "game": f"{ev['away']['name']} at {ev['home']['name']}"})

    # moneyline
    if sec == "soccer":
        opts = [("home", pr["p_h"], od.get("ml_h"), f"{h} to win"), ("draw", pr["p_d"], od.get("ml_d"), "Draw"), ("away", pr["p_a"], od.get("ml_a"), f"{a} to win")]
        nv = novig([o[2] for o in opts])
    else:
        opts = [("home", pr["p_h"], od.get("ml_h"), f"{h} to win"), ("away", pr["p_a"], od.get("ml_a"), f"{a} to win")]
        nv = novig([o[2] for o in opts])
    scored = []
    for (sel, pm, odds, label), pk in zip(opts, nv):
        w = blend_w(S, f"{sec}:ml", "ml")
        scored.append((pm if pk is None else w * pm + (1 - w) * pk, sel, pm, odds, label, pk))
    _, sel, pm, odds, label, pk = max(scored)
    add("ml", sel, label, None, odds, pm, pk)
    # spread
    if od.get("sp") is not None:
        ln = od["sp"]; w_, pu = cover_prob(pr, L, ln)
        ph = w_ / max(1e-9, 1 - pu)
        nvs = novig([od.get("sp_h"), od.get("sp_a")])
        if ph >= 0.5:
            add("spread", "home", f"{h} {ln:+g}", ln, od.get("sp_h"), ph, nvs[0])
        else:
            add("spread", "away", f"{a} {-ln:+g}", ln, od.get("sp_a"), 1 - ph, nvs[1])
    # total
    if od.get("tot") is not None:
        ln = od["tot"]; w_, pu = over_prob(pr, L, ln)
        po = w_ / max(1e-9, 1 - pu)
        nvt = novig([od.get("tot_o"), od.get("tot_u")])
        if po >= 0.5:
            add("total", "over", f"Over {ln:g}", ln, od.get("tot_o"), po, nvt[0])
        else:
            add("total", "under", f"Under {ln:g}", ln, od.get("tot_u"), 1 - po, nvt[1])
    # same-game parlays: one side leg + one total leg, with the link between the two outcomes taken from the score model
    sides, tots = [], []
    for (sel, pm, odds, label), pk in zip(opts, nv):
        if odds is not None:
            sides.append((label, odds, pm, fin("ml", pm, pk), ("ml", sel, None)))
    if od.get("sp") is not None and od.get("sp_h") is not None and od.get("sp_a") is not None:
        ln = od["sp"]; w_, pu = cover_prob(pr, L, ln); ph = w_ / max(1e-9, 1 - pu); nvs = novig([od["sp_h"], od["sp_a"]])
        sides.append((f"{h} {ln:+g}", od["sp_h"], ph, fin("spread", ph, nvs[0]), ("spread", "home", ln)))
        sides.append((f"{a} {-ln:+g}", od["sp_a"], 1 - ph, fin("spread", 1 - ph, nvs[1]), ("spread", "away", ln)))
    if od.get("tot") is not None and od.get("tot_o") is not None and od.get("tot_u") is not None:
        ln = od["tot"]; w_, pu = over_prob(pr, L, ln); po = w_ / max(1e-9, 1 - pu); nvt = novig([od["tot_o"], od["tot_u"]])
        tots.append((f"Over {ln:g}", od["tot_o"], po, fin("total", po, nvt[0]), ("total", "over", ln)))
        tots.append((f"Under {ln:g}", od["tot_u"], 1 - po, fin("total", 1 - po, nvt[1]), ("total", "under", ln)))
    combos = []
    for sl, so, spm, sp_, sk in sides:
        for tl, to, tpm, tp_, tk in tots:
            corr = 1.0
            if "grid" in pr:  # soccer: exact joint chance from the score grid
                def hit(i, j, k):
                    m, sel_, ln_ = k
                    if m == "ml":
                        return (i > j) if sel_ == "home" else (i < j) if sel_ == "away" else (i == j)
                    if m == "spread":
                        v = i - j + ln_
                        return v > 1e-9 if sel_ == "home" else v < -1e-9
                    v = i + j - ln_
                    return v > 1e-9 if sel_ == "over" else v < -1e-9
                joint = sum(v for (i, j), v in pr["grid"].items() if hit(i, j, sk) and hit(i, j, tk))
                corr = min(1.6, max(0.4, joint / max(1e-6, spm * tpm)))
            p = min(sp_, tp_, sp_ * tp_ * corr)
            dec = amer_to_dec(so) * amer_to_dec(to)
            combos.append({"legs": [sl, tl], "odds": [so, to], "lp": [round(sp_, 3), round(tp_, 3)], "p": round(p, 4), "corr": round(corr, 2),
                           "dec": round(dec, 3), "amer": dec_to_amer(dec), "fair": dec_to_amer(1 / p) if 0.001 < p < 0.999 else None, "ev": round(p * dec - 1, 4)})
    sgp = []
    if combos:
        def take(name, c):
            if c and all(c["legs"] != x["legs"] for x in sgp):
                sgp.append({"name": name, **c})
        take("Safest", max(combos, key=lambda c: c["p"]))
        take("Best value", max(combos, key=lambda c: c["ev"]))
        ls = [c for c in combos if c["p"] >= 0.12]
        take("Long shot", max(ls, key=lambda c: c["dec"]) if ls else None)
    pr["sgp"] = sgp
    return picks, pr


def grade(pick, hs, as_):
    m = hs - as_
    if pick["mkt"] == "ml":
        r = "home" if m > 0 else "away" if m < 0 else "draw"
        if r == "draw" and pick["sp"] != "soccer":
            return "push"
        return "win" if r == pick["sel"] else "loss"
    if pick["mkt"] == "spread":
        v = m + pick["line"]
        if v == 0:
            return "push"
        return "win" if (v > 0) == (pick["sel"] == "home") else "loss"
    v = hs + as_ - pick["line"]
    if v == 0:
        return "push"
    return "win" if (v > 0) == (pick["sel"] == "over") else "loss"


def record_result(S, pick, res, day):
    dec = amer_to_dec(pick["odds"]) or 1.909
    units = dec - 1 if res == "win" else -1 if res == "loss" else 0
    for scope in (pick["sp"], "all"):
        for mk in (pick["mkt"], "all"):
            for tr in (pick["tier"], "all"):
                r = S["perf"].setdefault(scope, {}).setdefault(mk, {}).setdefault(tr, [0, 0, 0, 0.0])
                r[0 if res == "win" else 1 if res == "loss" else 2] += 1; r[3] = round(r[3] + units, 3)
    dd = S["daily"].setdefault(day, [0, 0, 0.0])
    if res != "push":
        dd[0] += 1; dd[1] += 1 if res == "win" else 0
    dd[2] = round(dd[2] + units, 3)
    if res == "push":
        return
    key = f"{pick['sp']}:{pick['mkt']}"
    if pick["mkt"] != "ml":  # moneyline confidence is re-learned from full history on every run
        calib_update(S, key, pick["pm"], res == "win")
    if pick["pk"] is not None:
        B = S["blend"].setdefault(key, {"w": DEFAULT_BLEND[pick["mkt"]], "s": []})
        B["s"] = (B["s"] + [[pick["pm"], pick["pk"], 1 if res == "win" else 0]])[-600:]
        if len(B["s"]) >= 40:  # choose the model/market mix that would have scored best so far
            def ll(w):
                t = 0
                for pm, pk, y in B["s"]:
                    q = min(0.98, max(0.02, w * pm + (1 - w) * pk)); t -= math.log(q if y else 1 - q)
                return t
            best = min([i / 20 for i in range(0, 17)], key=ll)
            B["w"] = round(0.7 * B["w"] + 0.3 * best, 3)


# ---------------------------------------------------------------- parlays
def parlay(name, legs, note, stake=10):
    p = math.prod(l["p"] for l in legs); dec = math.prod(amer_to_dec(l["odds"]) for l in legs)
    n = len(legs)
    miss1 = sum(math.prod((1 - l["p"]) if i == j else l["p"] for i, l in enumerate(legs)) for j in range(n))
    return {"name": name, "note": note, "n": n, "p": round(p, 4), "dec": round(dec, 3), "amer": dec_to_amer(dec), "payout": round(stake * dec, 2),
            "ev": round(p * dec - 1, 4), "fair": dec_to_amer(1 / p) if p < 0.999 else None, "p_miss1": round(miss1, 4), "tier": tier_of(p),
            "legs": [{k: l[k] for k in ("id", "eid", "lg", "sp", "d", "mkt", "sel", "label", "line", "odds", "p", "game")} for l in legs], "st": "pending"}


def round_robin(legs, size, stake=5):
    combos = list(itertools.combinations(range(len(legs)), size))
    n = len(legs); dist = [0.0] * (n + 1); exp_ret = 0.0; p_profit = 0.0
    for mask in itertools.product([0, 1], repeat=n):
        pr = math.prod(l["p"] if m else 1 - l["p"] for l, m in zip(legs, mask))
        ret = sum(stake * math.prod(amer_to_dec(legs[i]["odds"]) for i in c) for c in combos if all(mask[i] for i in c))
        dist[sum(mask)] += pr; exp_ret += pr * ret
        if ret > stake * len(combos):
            p_profit += pr
    return {"size": size, "combos": len(combos), "cost": stake * len(combos), "stake": stake, "exp_return": round(exp_ret, 2),
            "p_profit": round(p_profit, 4), "dist": [round(x, 4) for x in dist],
            "max_return": round(sum(stake * math.prod(amer_to_dec(legs[i]["odds"]) for i in c) for c in combos), 2),
            "legs": [{k: l[k] for k in ("label", "game", "odds", "p", "sp")} for l in legs]}


def build_parlays(pool, tag):
    """pool: one best pick per game (with odds). Returns list of parlays + round robins."""
    out = []; seen = set()
    by_p = sorted(pool, key=lambda x: -x["p"])

    def put(name, legs, note):
        sig = tuple(sorted(l["id"] for l in legs))
        if len(legs) >= 2 and sig not in seen:
            seen.add(sig); out.append(parlay(name, legs, note))

    safe = [x for x in by_p if x["p"] >= 0.62]
    put("Safe Double", safe[:2], "The two most likely picks on the slate.")
    put("Safe Treble", safe[:3] if len(safe) >= 3 else [], "Top three picks by hit probability.")
    plus = [x for x in by_p if amer_to_dec(x["odds"]) >= 1.3]
    put("Balanced 4-Leg", plus[:4], "Four solid picks, skipping the heaviest favorites so the payout is worth it.")
    val = sorted([x for x in pool if x["p"] >= 0.5 and (x["ev"] or -1) > 0], key=lambda x: -x["ev"])
    put("Value Treble" if len(val) >= 3 else "Value Double", val[:3], "The picks where the model sees the biggest edge over the sportsbook price.")
    price = [x for x in by_p if amer_to_dec(x["odds"]) >= 1.6]
    put("Long Shot 5-Leg", price[:5], "Five picks at meaningful prices. Low hit rate, large payout.")
    put("Lottery 7-Leg", price[:7], "Seven legs. For fun only.")
    secs = {}
    for x in by_p:
        secs.setdefault(x["sp"], []).append(x)
    names = {"nfl": "NFL", "nba": "NBA", "cfb": "College Football", "soccer": "Soccer"}
    if len(secs) >= 2:
        put("Cross-Sport Combo", [v[0] for v in secs.values()], "The best pick from each sport.")
    for s, v in secs.items():
        put(f"{names[s]} Treble", v[:3], f"Top three {names[s]} picks.")
    rr = []
    if len(by_p) >= 4:
        rr = [round_robin(by_p[:4], 2), round_robin(by_p[:4], 3)]
    return {"tag": tag, "parlays": out, "rr": rr}


# ---------------------------------------------------------------- commands
# ---------------------------------------------------------------- prediction markets
# Public, documented feeds only: Polymarket (leaderboard, positions, markets) and Kalshi (markets).
# Everything here is optional: if a feed fails, picks are unaffected and the section shows as unavailable.
POLY_DATA = "https://data-api.polymarket.com/v2/"
POLY_GAMMA = "https://gamma-api.polymarket.com/"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/"
KALSHI_SERIES = {"nfl": ["KXNFLGAME"], "nba": ["KXNBAGAME"], "cfb": ["KXNCAAFGAME"],
                 "soccer": ["KXEPLGAME", "KXLALIGAGAME", "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXUCLGAME", "KXUELGAME", "KXUEFANLGAME", "KXMLSGAME", "KXBRASILEIROGAME", "KXLIGAMXGAME"]}
ESPORTS = ("cs2-", "dota2-", "lol-", "val-", "codmw-", "r6-", "rl-", "sc2-", "ow-", "mlbb-", "hok-", "kog-")
TOP_TRADERS = 12


def fetch_json(url):
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip", "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except Exception:
            try:
                out = subprocess.run(["curl", "-sS", "--compressed", "-m", "30", "-A", "Mozilla/5.0", url], capture_output=True, timeout=45).stdout
                d = json.loads(out)
                if not (isinstance(d, dict) and d.get("error")):
                    return d
            except Exception:
                pass
    return None


def nrm(x):
    x = (x or "").lower().replace("st.", "state").replace("&", "and")
    return " ".join("".join(c if c.isalnum() or c == " " else " " for c in x).split())


def team_keys(t):
    ks = {nrm(t.get("n")), nrm(t.get("loc")), nrm(t.get("nick")), nrm(t.get("short"))}
    if t.get("loc") and t.get("nick"):
        ks.add(nrm(t["loc"] + " " + t["nick"][0]))  # Kalshi style: "Los Angeles C"
    ks.discard("")
    return ks


def fnum(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def poly_markets():
    """Most-traded sports markets on Polymarket in the last 24 hours (moneyline, spread, total)."""
    out = []
    for kind, pages in (("moneyline", 3), ("spreads", 1), ("totals", 1)):
        for pg in range(pages):
            ms = fetch_json(f"{POLY_GAMMA}markets?closed=false&sports_market_types={kind}&limit=100&offset={pg * 100}&order=volume24hr&ascending=false")
            if not isinstance(ms, list):
                break
            for m in ms:
                try:
                    slug = ((m.get("events") or [{}])[0].get("slug")) or m.get("slug") or ""
                    out.append({"q": m.get("question") or "", "kind": m.get("sportsMarketType") or kind, "slug": slug, "start": (m.get("gameStartTime") or "").replace(" ", "T").replace("+00", "Z"),
                                "outs": json.loads(m.get("outcomes") or "[]"), "px": [fnum(x) for x in json.loads(m.get("outcomePrices") or "[]")], "v24": fnum(m.get("volume24hr")),
                                "bid": fnum(m.get("bestBid")), "ask": fnum(m.get("bestAsk"))})
                except Exception:
                    continue
            if len(ms) < 100:
                break
    return out


def kalshi_events(series):
    evs, cursor = [], ""
    for _ in range(4):
        d = fetch_json(f"{KALSHI}events?limit=100&status=open&with_nested_markets=true&series_ticker={series}" + (f"&cursor={cursor}" if cursor else ""))
        if not isinstance(d, dict):
            break
        evs += d.get("events") or []
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    out = []
    months = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
    for e in evs:
        try:
            tok = e["event_ticker"].split("-")[1]
            day = f"20{tok[:2]}-{months[tok[2:5]]:02d}-{tok[5:7]}"
            mk = []
            for m in e.get("markets") or []:
                bid, ask, last = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars")), fnum(m.get("last_price_dollars"))
                px = (bid + ask) / 2 if bid > 0 and ask > 0 and ask - bid <= 0.1 else last
                mk.append({"name": m.get("yes_sub_title") or "", "sfx": m["ticker"].rsplit("-", 1)[-1], "px": px, "ask": ask, "v24": fnum(m.get("volume_24h_fp"))})
            out.append({"ticker": e["event_ticker"], "series": series, "title": e.get("title") or "", "day": day, "markets": mk, "v24": sum(x["v24"] for x in mk)})
        except Exception:
            continue
    return out


def fetch_markets(games, now):
    M = {"errors": [], "lb": {}, "traders": [], "shared": [], "poly_trending": [], "kalshi_trending": [], "matched": {"polymarket": 0, "kalshi": 0}}
    # ---- Polymarket leaderboard (sports category)
    for period in ("DAY", "WEEK", "MONTH", "ALL"):
        d = fetch_json(f"{POLY_DATA}leaderboard?category=SPORTS&timePeriod={period}&orderBy=PNL&limit=25")
        rows = (d or {}).get("data") if isinstance(d, dict) else None
        if rows:
            M["lb"][period] = [{"rank": r.get("rank"), "id": r.get("user_id"), "name": (r.get("user_name") or "")[:28] or (r.get("user_id") or "")[:10], "pnl": round(fnum(r.get("pnl"))), "vol": round(fnum(r.get("volume")))} for r in rows]
    if not M["lb"]:
        M["errors"].append("Polymarket leaderboard unavailable")
    # ---- Polymarket markets: trending + game prices
    pm = poly_markets()
    if not pm:
        M["errors"].append("Polymarket markets unavailable")
    seen = set()
    for m in sorted(pm, key=lambda m: -m["v24"]):
        if m["slug"].startswith(ESPORTS) or (m["q"], m["slug"]) in seen or len(m["outs"]) != 2 or len(m["px"]) != 2:
            continue
        seen.add((m["q"], m["slug"]))
        i = 0 if m["px"][0] >= m["px"][1] else 1
        M["poly_trending"].append({"q": m["q"], "kind": m["kind"], "slug": m["slug"], "start": m["start"], "fav": m["outs"][i], "p": round(m["px"][i], 3), "v24": round(m["v24"])})
        if len(M["poly_trending"]) >= 30:
            break
    by_slug = {}
    for m in pm:
        if m["kind"] == "moneyline" and m["start"]:
            by_slug.setdefault(m["slug"], []).append(m)

    def start_close(a, b, hours=3):
        try:
            return abs((datetime.fromisoformat(a.replace("Z", "+00:00")) - datetime.fromisoformat(b.replace("Z", "+00:00"))).total_seconds()) <= hours * 3600
        except Exception:
            return False
    for g in games:
        hk, ak = team_keys(g["home"]), team_keys(g["away"])
        for slug, ms in by_slug.items():
            if not start_close(ms[0]["start"], g["date"]):
                continue
            res = {}; ask = {}
            for m in ms:
                if len(m["outs"]) != 2 or len(m["px"]) != 2:
                    continue
                if m["outs"] == ["Yes", "No"]:  # soccer: one yes/no market per outcome
                    q = m["q"]
                    if q.startswith("Will ") and " win on " in q:
                        nm = nrm(q[5:q.index(" win on ")])
                        if nm in hk: res["home"] = m["px"][0]; ask["home"] = m["ask"]
                        elif nm in ak: res["away"] = m["px"][0]; ask["away"] = m["ask"]
                    elif q.startswith("Will ") and " end in a draw" in q and " vs. " in q:
                        a_, b_ = q[5:q.index(" end in a draw")].split(" vs. ", 1)
                        if {nrm(a_) in hk or nrm(a_) in ak, nrm(b_) in hk or nrm(b_) in ak} == {True}:
                            res["draw"] = m["px"][0]; ask["draw"] = m["ask"]
                else:
                    o0, o1 = nrm(m["outs"][0]), nrm(m["outs"][1])
                    a0, a1 = m["ask"], (1 - m["bid"] if m["bid"] > 0 else 0)   # cost to buy each side
                    if o0 in hk and o1 in ak: res["home"], res["away"] = m["px"][0], m["px"][1]; ask["home"], ask["away"] = a0, a1
                    elif o0 in ak and o1 in hk: res["away"], res["home"] = m["px"][0], m["px"][1]; ask["away"], ask["home"] = a0, a1
            if "home" in res and "away" in res and (g["sp"] != "soccer" or "draw" in res):
                tot = sum(res.values())
                if 0.9 <= tot <= 1.1:
                    g.setdefault("mk", {})["poly"] = {**{k: round(v / tot, 3) for k, v in res.items()}, "slug": slug, "v24": round(sum(m["v24"] for m in ms)),
                                                        "ask": {k: round(v, 3) for k, v in ask.items() if 0.01 < v < 0.99}}
                    M["matched"]["polymarket"] += 1
                    break
    # ---- Kalshi: game prices + trending
    k_all = []
    for sp, series_list in KALSHI_SERIES.items():
        if not any(g["sp"] == sp for g in games):
            continue
        for series in series_list:
            evs = kalshi_events(series)
            k_all += [dict(e, sp=sp) for e in evs]
    if not k_all:
        M["errors"].append("Kalshi markets unavailable")
    for g in games:
        gd = datetime.fromisoformat(g["date"].replace("Z", "+00:00")).astimezone(ET).date()
        hk, ak = team_keys(g["home"]), team_keys(g["away"])
        ha, aa = g["home"]["a"].upper(), g["away"]["a"].upper()
        for e in k_all:
            if e["sp"] != g["sp"] or abs((datetime.fromisoformat(e["day"]).date() - gd).days) > 1:
                continue
            res = {}; ask = {}
            for m in e["markets"]:
                nm = nrm(m["name"])
                if nm in ("tie", "draw"): res["draw"] = m["px"]; ask["draw"] = m["ask"]
                elif nm in hk or (m["sfx"] == ha and nm not in ak): res["home"] = m["px"]; ask["home"] = m["ask"]
                elif nm in ak or (m["sfx"] == aa and nm not in hk): res["away"] = m["px"]; ask["away"] = m["ask"]
            if "home" in res and "away" in res and len(e["markets"]) == len(res):
                tot = sum(res.values())
                if 0.85 <= tot <= 1.15:
                    g.setdefault("mk", {})["kalshi"] = {**{k: round(v / tot, 3) for k, v in res.items()}, "ticker": e["ticker"], "series": e["series"], "v24": round(e["v24"]),
                                                          "ask": {k: round(v, 3) for k, v in ask.items() if 0.01 < v < 0.99}}
                    M["matched"]["kalshi"] += 1
                    break
    for e in sorted(k_all, key=lambda e: -e["v24"])[:20]:
        if e["v24"] <= 0 or not e["markets"]:
            continue
        fav = max(e["markets"], key=lambda m: m["px"])
        M["kalshi_trending"].append({"title": e["title"], "sp": e["sp"], "day": e["day"], "fav": fav["name"], "p": round(fav["px"], 3), "v24": round(e["v24"]), "ticker": e["ticker"], "series": e["series"]})
    # ---- top traders' open positions (this week's sports leaderboard)
    today = now.date().isoformat()
    game_slugs = {g["mk"]["poly"]["slug"]: g for g in games if g.get("mk", {}).get("poly")}
    agg = {}
    for r in (M["lb"].get("WEEK") or [])[:TOP_TRADERS]:
        d = fetch_json(f"{POLY_DATA}positions?user={r['id']}&limit=100&sort_by=current_value")
        rows = (d or {}).get("data") if isinstance(d, dict) else None
        if rows is None:
            continue
        pos = []
        for p in rows:
            if p.get("status") != "OPEN" or p.get("redeemable") or fnum(p.get("current_value")) < 50 or (p.get("end_date") or "9999") < today:
                continue
            slug = p.get("event_slug") or ""
            parts = slug.split("-")
            is_game = any(len(x) == 4 and x.isdigit() for x in parts[-4:-1]) and not slug.startswith(ESPORTS) and len(parts) >= 5 and parts[1].isalpha() and len(parts[1]) <= 6
            item = {"title": (p.get("title") or "")[:90], "out": p.get("outcome") or "", "slug": slug, "avg": round(fnum(p.get("avg_price")), 3), "cur": round(fnum(p.get("current_price")), 3),
                    "val": round(fnum(p.get("current_value"))), "pnl": round(fnum(p.get("unrealized_pnl"))), "game": is_game}
            pos.append(item)
            if is_game:
                a = agg.setdefault((slug, item["title"], item["out"]), {"n": 0, "val": 0, "who": []})
                a["n"] += 1; a["val"] += item["val"]; a["who"].append(r["name"])
        pos.sort(key=lambda x: (not x["game"], -x["val"]))
        M["traders"].append({"rank": r["rank"], "id": r["id"], "name": r["name"], "pnl": r["pnl"], "vol": r["vol"], "open": len(pos), "positions": pos[:10]})
    if M["lb"] and not M["traders"]:
        M["errors"].append("Polymarket positions unavailable")
    for (slug, title, out), a in sorted(agg.items(), key=lambda kv: (-kv[1]["n"], -kv[1]["val"]))[:25]:
        M["shared"].append({"slug": slug, "title": title, "out": out, "n": a["n"], "val": a["val"], "who": a["who"][:6]})
        g = game_slugs.get(slug)
        if g is not None:
            g["mk"].setdefault("smart", []).append({"title": title, "out": out, "n": a["n"], "val": a["val"]})
    return M


# ---------------------------------------------------------------- player props (NBA and NFL)
# Lines come from the same feed's prop listings; projections come from each player's game log.
CORE = "https://sports.core.api.espn.com/v2/sports/"
GAMELOG = "https://site.web.api.espn.com/apis/common/v3/sports/"
PROP_SPORTS = {"nba": ("basketball/nba", "basketball/leagues/nba", 36), "nfl": ("football/nfl", "football/leagues/nfl", 150)}   # hours ahead to look for lines
PROP_STATS = {
    "nba": {"Total Points": (["points"], "points"), "Total Rebounds": (["totalRebounds"], "rebounds"), "Total Assists": (["assists"], "assists"),
            "Total 3-Point Field Goals": (["threes"], "three-pointers"), "Total Points and Rebounds": (["points", "totalRebounds"], "points + rebounds"),
            "Total Points and Assists": (["points", "assists"], "points + assists"), "Total Assists and Rebounds": (["assists", "totalRebounds"], "rebounds + assists"),
            "Total Points, Rebounds, and Assists": (["points", "totalRebounds", "assists"], "points + rebounds + assists"),
            "Total Steals": (["steals"], "steals"), "Total Blocks": (["blocks"], "blocks")},
    "nfl": {"Total Passing Yards": (["passingYards"], "passing yards"), "Total Pass Completions": (["completions"], "completions"), "Total Passing Attempts": (["passingAttempts"], "pass attempts"),
            "Total Passing Touchdowns": (["passingTouchdowns"], "passing touchdowns"), "Total Passing Interceptions": (["interceptions"], "interceptions"),
            "Total Rushing Yards": (["rushingYards"], "rushing yards"), "Total Carries": (["rushingAttempts"], "carries"),
            "Total Receiving Yards": (["receivingYards"], "receiving yards"), "Total Receptions": (["receptions"], "receptions"),
            "Total Passing Plus Rushing Yards": (["passingYards", "rushingYards"], "passing + rushing yards"),
            "Total Rushing Plus Receiving Yards": (["rushingYards", "receivingYards"], "rushing + receiving yards")},
}
PROP_PRICE_EST = -115      # used for payout estimates when the feed has no price for a line
PROPS_PER_GAME = 12


def _roster(sp, team_id):
    d = fetch_json(f"https://site.api.espn.com/apis/site/v2/sports/{PROP_SPORTS[sp][0]}/teams/{team_id}/roster")
    out = {}
    for grp in (d or {}).get("athletes") or []:
        for a in (grp.get("items") if isinstance(grp, dict) and "items" in grp else [grp]):
            if isinstance(a, dict) and a.get("id"):
                out[str(a["id"])] = a.get("displayName") or a.get("fullName") or ""
    return out


def _gamelog(sp, aid):
    """Per-game numbers for one player this regular season, newest first: [[eventId, date, {stat: value}], ...]"""
    d = fetch_json(f"{GAMELOG}{PROP_SPORTS[sp][0]}/athletes/{aid}/gamelog")
    if not isinstance(d, dict) or not d.get("names"):
        return None
    names, evs, rows = d["names"], d.get("events") or {}, []
    for st in d.get("seasonTypes") or []:
        if "regular" not in (st.get("displayName") or "").lower():
            continue
        for cat in st.get("categories") or []:
            for e in cat.get("events") or []:
                vals = {}
                for n, v in zip(names, e.get("stats") or []):
                    if n.startswith("threePointFieldGoalsMade"):
                        n, v = "threes", str(v).split("-")[0]
                    try:
                        vals[n] = float(str(v).replace(",", ""))
                    except Exception:
                        pass
                eid = str(e.get("eventId"))
                rows.append([eid, ((evs.get(eid) or {}).get("gameDate") or "")[:10], vals])
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows


def _prop_prob(vals, line):
    """Chance of going over the line from recent games: recency-weighted average plus how often it actually happened."""
    n = len(vals)
    w = [0.9 ** i for i in range(n)]
    mu = sum(v * x for v, x in zip(vals, w)) / sum(w)
    var = sum(x * (v - mu) ** 2 for v, x in zip(vals, w)) / sum(w)
    sd = max(math.sqrt(var), 0.35 * math.sqrt(max(mu, 0.5)) + 0.08 * mu, 0.6)
    p_norm = 1 - phi((line - mu) / sd)
    emp = (sum(1 for v in vals if v > line) + 1) / (n + 2)
    raw = 0.6 * p_norm + 0.4 * emp
    return 0.5 + 0.45 * (raw - 0.5), mu     # pulled toward 50%: the lines are set by professionals


def fetch_props(S, games, cache, now):
    meta = {"errors": [], "lines": 0, "players": 0, "graded": 0}
    store = {}
    cf = os.path.join(cache, "players.json.gz") if cache else None
    if cf and os.path.exists(cf):
        try:
            store = json.loads(gzip.decompress(open(cf, "rb").read()))
        except Exception:
            store = {}
    logs, rosters = store.setdefault("logs", {}), store.setdefault("rosters", {})

    def log_for(sp, aid, newer_than=None):
        key = f"{sp}:{aid}"; c = logs.get(key)
        age_ok = c and (now - datetime.fromisoformat(c["t"])).total_seconds() < 20 * 3600
        if c and newer_than and datetime.fromisoformat(c["t"]) < newer_than:
            age_ok = False
        if not age_ok:
            rows = _gamelog(sp, aid)
            if rows is None:
                return (c or {}).get("g")
            c = logs[key] = {"t": now.isoformat(timespec="seconds"), "g": rows[:25]}
        return c["g"]

    # ---- grade earlier player picks from the box scores
    pend = [p for p in S["props"].values() if p["st"] == "pending" and datetime.fromisoformat(p["d"].replace("Z", "+00:00")) + timedelta(hours=5) < now]
    need = {}
    for p in pend:
        need.setdefault((p["sp"], p["aid"]), datetime.fromisoformat(p["d"].replace("Z", "+00:00")) + timedelta(hours=5))
    with ThreadPoolExecutor(6) as ex:
        got = dict(zip(need, ex.map(lambda kv: log_for(kv[0][0], kv[0][1], kv[1]), need.items())))
    for p in pend:
        rows = got.get((p["sp"], p["aid"])) or []
        row = next((r for r in rows if r[0] == p["eid"]), None)
        if row is None:
            if (now - datetime.fromisoformat(p["d"].replace("Z", "+00:00"))).days >= 3:
                p["st"] = "void"      # did not play, or the game is missing from the log
            continue
        val = sum(row[2].get(k, 0) for k in p["keys"])
        p["val"] = val
        p["st"] = "push" if val == p["line"] else "win" if (val > p["line"]) == (p["sel"] == "over") else "loss"
        r = S["prop_perf"].setdefault(p["sp"], {}).setdefault(p["tier"], [0, 0, 0])
        r[0 if p["st"] == "win" else 1 if p["st"] == "loss" else 2] += 1
        meta["graded"] += 1
    # ---- new lines
    out = []
    for g in games:
        sp = g["sp"]
        if sp not in PROP_SPORTS or not g["home"].get("id"):
            continue
        start = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        if not (now < start <= now + timedelta(hours=PROP_SPORTS[sp][2])):
            continue
        items, page, pages = [], 1, 1
        while page <= pages and page <= 3:
            d = fetch_json(f"{CORE}{PROP_SPORTS[sp][1]}/events/{g['id']}/competitions/{g['id']}/odds/100/propBets?limit=1000&page={page}")
            if not isinstance(d, dict) or "items" not in d:
                break
            items += d["items"]; pages = d.get("pageCount") or 1; page += 1
        lines = {}
        for it in items:
            try:
                tname = it["type"]["name"].replace(" (incl. overtime)", "")
                if tname not in PROP_STATS[sp] or "athlete" not in it:
                    continue
                aid = it["athlete"]["$ref"].split("/athletes/")[1].split("?")[0]
                line = fnum(((it.get("current") or {}).get("target") or {}).get("value"), None)
                if line is None:
                    continue
                L = lines.setdefault((aid, tname), {"line": line, "px": []})
                am = to_int(((it.get("odds") or {}).get("american") or {}).get("value"))
                if am is not None:
                    L["px"].append(amer_to_dec(am))
            except Exception:
                continue
        if not lines:
            continue
        for side in ("home", "away"):
            tid = f"{sp}:{g[side]['id']}"; c = rosters.get(tid)
            if not c or (now - datetime.fromisoformat(c["t"])).total_seconds() > 24 * 3600:
                r = _roster(sp, g[side]["id"])
                if r:
                    rosters[tid] = {"t": now.isoformat(timespec="seconds"), "p": r}
        who = {}
        for side in ("home", "away"):
            for aid, nm in ((rosters.get(f"{sp}:{g[side]['id']}") or {}).get("p") or {}).items():
                who[aid] = (nm, g[side]["a"])
        aids = sorted({aid for aid, _ in lines if aid in who})
        with ThreadPoolExecutor(6) as ex:
            glogs = dict(zip(aids, ex.map(lambda a: log_for(sp, a), aids)))
        cand = []
        for (aid, tname), L in lines.items():
            rows = glogs.get(aid)
            if not rows:
                continue
            keys, label = PROP_STATS[sp][tname]
            vals = [sum(r[2].get(k, 0) for k in keys) for r in rows if r[0] != g["id"] and all(k in r[2] for k in keys)][:12]
            if len(vals) < 5:
                continue
            meta["lines"] += 1
            p_over, mu = _prop_prob(vals, L["line"])
            if not L["px"] and L["line"] < 3:
                continue    # tiny lines are priced far from even money; without a posted price the payout would mislead
            if L["px"] and (len(L["px"]) != 2 or max(L["px"]) / min(L["px"]) > 1.12):
                continue    # the feed does not say which price is the over and which the under, so only near-even lines can be priced honestly
            if abs(L["line"] - mu) > max(1.0, 0.25 * max(L["line"], mu)):
                continue    # line far from the player's recent numbers: the book knows something the game log does not (role change, injury)
            sel, p = ("over", p_over) if p_over >= 0.5 else ("under", 1 - p_over)
            p = min(p, 0.66)
            nm, tm_ = who[aid]
            price = dec_to_amer(sum(L["px"]) / len(L["px"])) if L["px"] else None
            cand.append({"id": f"{g['id']}:{aid}:{'+'.join(keys)}", "eid": g["id"], "aid": aid, "lg": g["lg"], "sp": sp, "d": g["date"], "mkt": "prop", "sel": sel, "keys": keys,
                         "player": nm, "team": tm_, "stat": label, "line": L["line"], "label": f"{nm} {sel} {L['line']:g} {label}", "odds": price if price is not None else PROP_PRICE_EST,
                         "est": price is None, "p": round(p, 4), "tier": tier_of(p), "avg": round(sum(vals) / len(vals), 1), "proj": round(mu, 1), "last": [round(v, 1) for v in vals[:5]], "n": len(vals),
                         "game": f"{g['away']['n']} at {g['home']['n']}", "st": "pending"})
        cand.sort(key=lambda x: -x["p"])
        seen_player, keep = {}, []
        for c in cand:                      # at most three lines per player, best first
            if c["p"] < 0.54 or seen_player.get(c["aid"], 0) >= 3:
                continue
            seen_player[c["aid"]] = seen_player.get(c["aid"], 0) + 1; keep.append(c)
            if len(keep) >= PROPS_PER_GAME:
                break
        out += keep
        meta["players"] += len({c["aid"] for c in keep})
    for c in out:
        old = S["props"].get(c["id"])
        if not old or old["st"] == "pending":
            S["props"][c["id"]] = {k: c[k] for k in ("id", "eid", "aid", "sp", "d", "sel", "keys", "line", "label", "p", "tier", "odds", "st", "game")}
    # keep the player cache small
    cut = (now - timedelta(days=5)).isoformat()
    store["logs"] = {k: v for k, v in logs.items() if v["t"] >= cut}
    if cf:
        os.makedirs(cache, exist_ok=True)
        open(cf, "wb").write(gzip.compress(json.dumps(store, separators=(",", ":")).encode(), 6))
    return out, meta


def sport_parlays(games, props):
    """Parlays built separately for each sport: game picks, player picks, and mixes of the two."""
    out = {}
    names = {"nfl": "NFL", "nba": "NBA", "cfb": "College Football", "soccer": "Soccer"}
    for sp in names:
        best = []
        for g in games:
            if g["sp"] != sp:
                continue
            c = [p for p in g["picks"] if p["odds"] is not None and amer_to_dec(p["odds"]) >= 1.05]
            if c:
                best.append(max(c, key=lambda x: x["p"]))
        grp = build_parlays(best, sp) if len(best) >= 2 else {"tag": sp, "parlays": [], "rr": []}
        grp["parlays"] = [p for p in grp["parlays"] if not p["name"].endswith(" Treble") or p["name"] in ("Safe Treble", "Value Treble")]
        pp = sorted([p for p in props if p["sp"] == sp], key=lambda x: -x["p"])
        one = []                              # one line per player, spread across games where possible
        for p in pp:
            if all(p["aid"] != q["aid"] for q in one):
                one.append(p)
        spread, used = [], {}
        for p in one:
            if used.get(p["eid"], 0) < 2:
                spread.append(p); used[p["eid"]] = used.get(p["eid"], 0) + 1
        pl, combos = [], []
        if len(spread) >= 2:
            pl.append(parlay("Player Double", spread[:2], "The two most likely player picks."))
        if len(spread) >= 3:
            pl.append(parlay("Player Treble", spread[:3], "Top three player picks, each a different player."))
        if len(spread) >= 5:
            pl.append(parlay("Player 5-Leg", spread[:5], "Five player picks. Lower chance, bigger payout."))
        overs = [p for p in spread if p["sel"] == "over"]
        if len(overs) >= 3:
            pl.append(parlay("Overs Treble", overs[:3], "Three players projected to go over their line."))
        strong = [b for b in sorted(best, key=lambda x: -x["p"]) if b["p"] >= 0.6]
        if strong and spread:
            a = strong[0]; others = [p for p in spread if p["eid"] != a["eid"]]
            if len(others) >= 1:
                combos.append(parlay("Game + Player", [a, others[0]], "The best game pick with the best player pick from a different game."))
            if len(others) >= 2:
                combos.append(parlay("Game + 2 Players", [a] + others[:2], "One game pick and two player picks from other games."))
            if len(strong) >= 2 and len(others) >= 2:
                combos.append(parlay("2 Games + 2 Players", strong[:2] + [p for p in spread if p["eid"] not in (strong[0]["eid"], strong[1]["eid"])][:2], "Two game picks and two player picks."))
        for q in pl + combos:
            q["est"] = any(l.get("mkt") == "prop" for l in q["legs"])
        sg = sorted([dict(c, game=f"{g['away']['a']} at {g['home']['a']}", d=g["date"]) for g in games if g["sp"] == sp for c in g.get("sgp", []) if c["name"] == "Safest"], key=lambda c: -c["p"])[:8]
        out[sp] = {"game": grp["parlays"], "rr": grp["rr"], "players": pl, "combos": [c for c in combos if c["n"] >= 2], "sgp": sg}
    return out


ODDS_API = "https://api.the-odds-api.com/v4/sports/"


def fetch_odds_api(games, cache, now):
    """Prices from many sportsbooks via The Odds API (licensed). Needs the ODDS_API_KEY environment variable.
    Results are kept for ODDS_API_HOURS (default 20) so a free plan's monthly allowance is not used up."""
    key = os.environ.get("ODDS_API_KEY", "").strip()
    meta = {"enabled": bool(key), "asof": None, "remaining": None, "error": None, "matched": 0}
    if not key:
        return meta
    hours = fnum(os.environ.get("ODDS_API_HOURS"), 20.0)
    sports = [x.strip() for x in os.environ.get("ODDS_API_SPORTS", "americanfootball_nfl,basketball_nba,americanfootball_ncaaf,soccer_epl").split(",") if x.strip()]
    cf = os.path.join(cache, "oddsapi.json.gz") if cache else None
    store = None
    if cf and os.path.exists(cf):
        try:
            store = json.loads(gzip.decompress(open(cf, "rb").read()))
        except Exception:
            store = None
    fresh = store and (now - datetime.fromisoformat(store["t"])).total_seconds() < hours * 3600
    if not fresh:
        events = []
        for sp in sports:
            try:
                req = urllib.request.Request(f"{ODDS_API}{sp}/odds/?regions=us&markets=h2h,spreads,totals&oddsFormat=american", headers={"x-api-key": key, "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    meta["remaining"] = r.headers.get("x-requests-remaining")
                    events += json.loads(r.read())
            except Exception as ex:
                meta["error"] = f"{sp}: {type(ex).__name__}"
        if events:
            store = {"t": now.isoformat(timespec="seconds"), "remaining": meta["remaining"], "events": events}
            if cf:
                os.makedirs(cache, exist_ok=True)
                open(cf, "wb").write(gzip.compress(json.dumps(store, separators=(",", ":")).encode(), 6))
    if not store:
        return meta
    meta["asof"] = store["t"]; meta["remaining"] = meta["remaining"] or store.get("remaining")
    for g in games:
        hk, ak = team_keys(g["home"]), team_keys(g["away"])
        gt = datetime.fromisoformat(g["date"].replace("Z", "+00:00"))
        for e in store["events"]:
            try:
                if abs((datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")) - gt).total_seconds()) > 3 * 3600:
                    continue
                if nrm(e["home_team"]) not in hk or nrm(e["away_team"]) not in ak:
                    continue
                ob = {"ml": {}, "spread": {}, "total": {}}
                sp_line, tot_line = (g.get("odds") or {}).get("sp"), (g.get("odds") or {}).get("tot")
                for b in e.get("bookmakers") or []:
                    for m in b.get("markets") or []:
                        for o in m.get("outcomes") or []:
                            nm, price, pt = nrm(o.get("name")), o.get("price"), o.get("point")
                            side = "home" if nm in hk else "away" if nm in ak else "draw" if nm == "draw" else None
                            if m["key"] == "h2h" and side:
                                ob["ml"].setdefault(side, {})[b["title"]] = price
                            elif m["key"] == "spreads" and side in ("home", "away") and sp_line is not None and pt is not None and abs(pt - (sp_line if side == "home" else -sp_line)) < 0.01:
                                ob["spread"].setdefault(side, {})[b["title"]] = price
                            elif m["key"] == "totals" and nm in ("over", "under") and tot_line is not None and pt is not None and abs(pt - tot_line) < 0.01:
                                ob["total"].setdefault(nm, {})[b["title"]] = price
                if ob["ml"] or ob["spread"] or ob["total"]:
                    g["ob"] = {k: v for k, v in ob.items() if v}; meta["matched"] += 1
                break
            except Exception:
                continue
    return meta


LEDGER_KEYS = ("picks", "parlays", "perf", "daily", "parlay_perf", "log", "results", "trends", "blend", "props", "prop_perf")


def slim(e):
    """Only what training needs, so the history cache stays small."""
    t = lambda x: {"id": x["id"], "name": x["name"], "abbr": x["abbr"], "score": x["score"]}
    return {"id": e["id"], "lg": e["lg"], "date": e["date"], "final": True, "neutral": e["neutral"], "home": t(e["home"]), "away": t(e["away"])}


def build_model(today, cache=None, fresh=None):
    """Replay past results in date order, predicting each game before learning from it.

    cache: folder holding <league>.json.gz with past final results. When present, only the
    recent days in `fresh` (already downloaded by the caller) are merged in, so a run needs
    no history download. Without it, about 15 months of results are downloaded.
    """
    S = new_state()
    start = today - timedelta(days=470)
    season = today.year if today.month >= 8 else today.year - 1
    cut = f"{season}-08-01"
    all_days = [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range((today - start).days + 1)]
    is_final = lambda e: e["final"] and e["home"]["score"] is not None and e["away"]["score"] is not None
    for lg in LEAGUES:
        cf = os.path.join(cache, lg + ".json.gz") if cache else None
        hist = None
        if cf and os.path.exists(cf):
            try:
                hist = json.loads(gzip.decompress(open(cf, "rb").read()))
            except Exception:
                hist = None
        if hist is None:
            if lg in ("nfl", "cfb"):
                q = []
                for yr in (season - 1, season):
                    q += [f"{yr}&seasontype=2&week={w}" for w in range(1, (19 if lg == "nfl" else 17))]
                    q += [f"{yr}&seasontype=3&week={w}" for w in range(1, 6 if lg == "nfl" else 2)]
            else:
                q = all_days
            evs, failed = fetch_days(lg, q)
            if failed > max(3, len(q) * 0.03):
                sys.exit(f"ERROR: {failed} of {len(q)} history requests failed for {lg}; aborting without saving.")
            hist = {e["id"]: slim(e) for e in evs if is_final(e)}
            print(lg, "downloaded full history:", len(hist), "results", flush=True)
        for e in (fresh or {}).get(lg, []):
            if is_final(e):
                hist[e["id"]] = slim(e)
        hist = {k: e for k, e in hist.items() if e["date"][:10] >= start.isoformat()}
        if cf:
            os.makedirs(cache, exist_ok=True)
            open(cf, "wb").write(gzip.compress(json.dumps(hist, separators=(",", ":")).encode(), 6))
        evs = list(hist.values())
        evs = sorted([e for e in evs if e["final"] and e["home"]["score"] is not None and e["away"]["score"] is not None], key=lambda e: e["date"])
        if len(evs) < 50:
            sys.exit(f"ERROR: only {len(evs)} past results found for {lg}; aborting without saving.")
        sec = LEAGUES[lg][0]
        for e in evs:
            pr = learn_from_result(S, e)
            if sec == "soccer":
                sel, pm = max((("home", pr["p_h"]), ("draw", pr["p_d"]), ("away", pr["p_a"])), key=lambda t: t[1])
            else:
                sel, pm = ("home", pr["p_h"]) if pr["p_h"] >= 0.5 else ("away", pr["p_a"])
            m = e["home"]["score"] - e["away"]["score"]
            actual = "home" if m > 0 else "away" if m < 0 else "draw"
            if actual == "draw" and sec != "soccer":
                continue
            won = actual == sel
            if e["date"][:10] >= cut:  # report only on the current season, after a full season of warm-up
                pc = calibrate(S, f"{sec}:ml", pm)
                for tr in (tier_of(pc), "all"):
                    r = S["backtest"].setdefault(sec, {}).setdefault(tr, [0, 0, 0.0]); r[0] += 1; r[1] += 1 if won else 0; r[2] = round(r[2] + pc, 3)
            calib_update(S, f"{sec}:ml", pm, won)
        print(lg, "trained on", len(evs), "games", flush=True)
    return S


def run(args):
    now = datetime.now(timezone.utc); today = now.astimezone(ET).date()
    # recent and upcoming days are downloaded once and reused for training, grading and pricing
    window = {}; failed_total = 0; stale = []
    prev = None
    if args.data and os.path.exists(args.data):
        try:
            prev = json.load(open(args.data))
        except Exception:
            prev = None
    for lg in LEAGUES:
        ahead = SOCCER_DAYS if LEAGUES[lg][0] == "soccer" else 8
        evs, failed = fetch_days(lg, [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(-6, ahead)])
        if failed > 2 or lg in BAD_DATES:   # this league's feed is unhealthy: leave its history and picks untouched
            stale.append(lg); window[lg] = []
        else:
            window[lg] = evs; failed_total += failed
    if len(stale) == len(LEAGUES):
        sys.exit("ERROR: every league feed failed or returned invalid data; aborting without saving.")
    S = build_model(today, args.cache, window)
    led = load_state(args.state) if os.path.exists(args.state) else {}
    for k in LEDGER_KEYS:
        S[k] = led.get(k, [] if k == "log" else {})
    for k, v in led.get("calib", {}).items():
        if not k.endswith(":ml"):
            S["calib"][k] = v
    games = []; graded_now = 0
    etday = lambda iso: datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).date().isoformat()
    for lg in LEAGUES:
        evs = window[lg]
        L = lstate(S, lg); sec = LEAGUES[lg][0]
        if lg in stale:  # show the last good picks for this league instead of dropping its games
            for g in (prev or {}).get("games", []):
                if g.get("lg") == lg and datetime.fromisoformat(g["date"].replace("Z", "+00:00")) > now:
                    g.pop("mk", None); games.append(g)
            continue
        for e in sorted(evs, key=lambda e: e["date"]):
            if not (e["final"] and e["home"]["score"] is not None and e["away"]["score"] is not None):
                continue
            hs, as_ = e["home"]["score"], e["away"]["score"]
            for mk in ("ml", "spread", "total"):
                pk = S["picks"].get(f"{e['id']}:{mk}")
                if not pk or pk["st"] != "pending":
                    continue
                pk["st"] = grade(pk, hs, as_)
                pk["score"] = f"{e['away']['abbr']} {as_} - {e['home']['abbr']} {hs}"
                record_result(S, pk, pk["st"], etday(e["date"])); graded_now += 1
                S["results"][e["id"]] = [hs, as_, e["date"][:10]]
                if mk != "ml" and pk["line"] is not None:  # team trends against the line we saw before kickoff
                    for side in ("home", "away"):
                        t = S["trends"].setdefault(sec, {}).setdefault(e[side]["id"], [0, 0, 0, 0])
                        if mk == "spread" and hs - as_ + pk["line"] != 0:
                            t[0 if (hs - as_ + pk["line"] > 0) == (side == "home") else 1] += 1
                        if mk == "total" and hs + as_ != pk["line"]:
                            t[2 if hs + as_ > pk["line"] else 3] += 1
        for e in sorted(evs, key=lambda e: e["date"]):
            if e["state"] != "pre" or e["final"]:
                continue
            start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if start <= now:
                continue
            if sec == "soccer" and (not e.get("odds") or start.astimezone(ET).date() > today + timedelta(days=SOCCER_DAYS - 1)):
                continue  # soccer: only games with posted odds, nearest days (hundreds of matches a day worldwide)
            picks, pr = make_picks(S, e)
            for pk in picks:
                S["picks"][pk["id"]] = pk
            H, A = team(L, e["home"]), team(L, e["away"]); od = e.get("odds") or {}
            tr = []
            for T, side in ((A, "away"), (H, "home")):
                if T["last"]:
                    s = "".join(x[0] for x in T["last"])
                    tr.append(f"{T['a']} last {len(T['last'])}: {s}, scoring {sum(x[1] for x in T['last'])/len(T['last']):.1f}, allowing {sum(x[2] for x in T['last'])/len(T['last']):.1f}")
                t = S["trends"].get(sec, {}).get(e[side]["id"])
                if t and t[0] + t[1] >= 3:
                    tr.append(f"{T['a']} is {t[0]}-{t[1]} against the spread since tracking began")
                if t and t[2] + t[3] >= 3:
                    tr.append(f"{T['a']} games: {t[2]} overs, {t[3]} unders since tracking began")
            if od.get("sp") is not None and od.get("sp_open") is not None and od["sp"] != od["sp_open"]:
                tr.append(f"Spread moved from {od['sp_open']:+g} to {od['sp']:+g} (home team line)")
            if od.get("tot") is not None and od.get("tot_open") is not None and od["tot"] != od["tot_open"]:
                tr.append(f"Total moved from {od['tot_open']:g} to {od['tot']:g}")
            games.append({"id": e["id"], "lg": lg, "sp": sec, "league": e.get("league") or LEAGUES[lg][1], "sgp": pr.get("sgp", []), "date": e["date"], "book": od.get("book"),
                          "odds": {k: od.get(k) for k in ("ml_h", "ml_a", "ml_d", "sp", "sp_h", "sp_a", "tot", "tot_o", "tot_u") if od.get(k) is not None},
                          "home": {"id": e["home"]["id"], "n": e["home"]["name"], "a": e["home"]["abbr"], "rec": e["home"]["rec"], "elo": round(H["elo"]), "g": H["g"], "loc": e["home"].get("loc", ""), "nick": e["home"].get("nick", ""), "short": e["home"].get("short", "")},
                          "away": {"id": e["away"]["id"], "n": e["away"]["name"], "a": e["away"]["abbr"], "rec": e["away"]["rec"], "elo": round(A["elo"]), "g": A["g"], "loc": e["away"].get("loc", ""), "nick": e["away"].get("nick", ""), "short": e["away"].get("short", "")},
                          "proj": {"margin": round(pr["mu"], 1), "total": round(pr["tot"], 1), "p_h": round(pr["p_h"], 3), "p_d": round(pr["p_d"], 3), "p_a": round(pr["p_a"], 3)},
                          "picks": sorted(picks, key=lambda x: -x["p"]), "trends": tr})
    if not games and (failed_total or stale):
        sys.exit(f"ERROR: no upcoming games and feed requests failed; aborting without saving.")
    notices = []
    if stale:
        names = ", ".join(LEAGUES[lg][1] for lg in stale)
        notices.append(f"{names}: the data source returned invalid data on this update, so these picks are the last good ones and are not being refreshed or graded until it recovers.")
    # grade stored parlays
    R = S["results"]
    for key, grp in S["parlays"].items():
        for pl in grp["parlays"]:
            if pl["st"] != "pending":
                continue
            sts = []
            for lg_ in pl["legs"]:
                r = R.get(lg_["eid"])
                sts.append(grade(lg_, r[0], r[1]) if r else "pending")
                lg_["st"] = sts[-1]
            if "loss" in sts:
                pl["st"] = "loss"
            elif "pending" not in sts:
                pl["st"] = "win"
            if pl["st"] != "pending":
                r = S["parlay_perf"].setdefault(pl["name"], [0, 0, 0.0])
                r[0] += 1; r[1] += 1 if pl["st"] == "win" else 0
                r[2] = round(r[2] + (pl["dec"] - 1 if pl["st"] == "win" else -1), 3)
    # build parlays: each game day and the 7-day window
    best = []
    for g in games:
        c = [p for p in g["picks"] if p["odds"] is not None and amer_to_dec(p["odds"]) >= 1.05]
        if c:
            best.append(max(c, key=lambda x: x["p"]))
    slates = {}
    for b in best:
        slates.setdefault(etday(b["d"]), []).append(b)
    live_parlays = []
    for k, old in S["parlays"].items():  # slates in progress whose remaining games no longer form a pool
        if k.startswith("day:") and k[4:] >= today.isoformat() and k[4:] not in slates and any(p["st"] == "pending" for p in old["parlays"]):
            live_parlays.append(old)
    for day in sorted(slates)[:7]:
        if len(slates[day]) >= 2 or "day:" + day in S["parlays"]:
            grp = build_parlays(slates[day], day)
            old = S["parlays"].get("day:" + day)
            if old and any(l["d"] <= now.strftime("%Y-%m-%dT%H:%MZ") for p in old["parlays"] for l in p["legs"]):
                grp = old  # slate already under way: keep the parlays as issued
            S["parlays"]["day:" + day] = grp
            if grp["parlays"]:
                live_parlays.append(grp)
    if len(best) >= 2:
        wk = build_parlays(best, "week"); live_parlays.append(wk)
        if "week:" + today.isoformat() not in S["parlays"]:
            S["parlays"]["week:" + today.isoformat()] = wk
    # housekeeping
    lim = (today - timedelta(days=KEEP_GRADED_DAYS)).isoformat()
    S["picks"] = {k: v for k, v in S["picks"].items() if v["d"][:10] >= lim}
    S["parlays"] = {k: v for k, v in S["parlays"].items() if k.split(":", 1)[1] >= lim}
    S["results"] = {k: v for k, v in S["results"].items() if v[2] >= lim}
    S["daily"] = {k: v for k, v in S["daily"].items() if k >= (today - timedelta(days=180)).isoformat()}
    msg = f"Graded {graded_now} picks; {len(games)} upcoming games priced." + (f" {failed_total} feed requests failed." if failed_total else "") + (f" Source problem for: {', '.join(stale)}." if stale else "")
    S["log"] = (S["log"] + [{"t": now.isoformat(timespec="seconds"), "msg": msg + " Model retrained on " + str(sum(L["n"] for L in S["leagues"].values())) + " past games."}])[-40:]
    try:
        markets = fetch_markets(games, now)
    except Exception as ex:  # never let the optional market data break the picks
        markets = {"errors": [f"Prediction market data failed: {type(ex).__name__}"], "lb": {}, "traders": [], "shared": [], "poly_trending": [], "kalshi_trending": [], "matched": {}}
    try:
        props, props_meta = fetch_props(S, games, args.cache, now)
    except Exception as ex:
        props, props_meta = [], {"errors": [f"Player picks failed: {type(ex).__name__}"], "lines": 0, "players": 0, "graded": 0}
    S["props"] = {k: v for k, v in S["props"].items() if v["d"][:10] >= lim}
    try:
        sp_parlays = sport_parlays(games, props)
    except Exception:
        sp_parlays = {}
    try:
        books_meta = fetch_odds_api(games, args.cache, now)
    except Exception as ex:
        books_meta = {"enabled": True, "error": type(ex).__name__, "asof": None, "remaining": None, "matched": 0}
    led = {k: S[k] for k in LEDGER_KEYS}
    led["calib"] = {k: v for k, v in S["calib"].items() if not k.endswith(":ml")}; led["version"] = 2
    save_state(led, args.state)
    recent = sorted([p for p in S["picks"].values() if p["st"] != "pending"], key=lambda p: p["d"], reverse=True)[:400]
    past_parlays = [{"key": k, "parlays": [p for p in v["parlays"] if p["st"] != "pending"]} for k, v in sorted(S["parlays"].items(), reverse=True)]
    model = {lg: {"league": LEAGUES[lg][1], "hfa": round(L["hfa"], 1), "sig": round(L["sig"], 2), "tsig": round(L["tsig"], 2), "avg": round(L["avg"], 2), "games": L["n"], "teams": len(L["teams"]),
                  "top": [[T["n"], round(T["elo"])] for T in sorted([T for T in L["teams"].values() if T["g"] >= 10], key=lambda T: -T["elo"])[:8]]} for lg, L in S["leagues"].items()}
    data = {"generated": now.isoformat(timespec="seconds"), "games": sorted(games, key=lambda g: g["date"]), "parlays": live_parlays, "perf": S["perf"],
            "daily": S["daily"], "backtest": S["backtest"], "recent": recent, "past_parlays": [p for p in past_parlays if p["parlays"]],
            "parlay_perf": S["parlay_perf"], "model": model, "log": S["log"][::-1], "calib": {k: {"a": v["a"], "n": v["n"]} for k, v in S["calib"].items()},
            "blend": {k: v.get("w") for k, v in S["blend"].items()}, "default_blend": DEFAULT_BLEND, "soccer_days": SOCCER_DAYS, "markets": markets, "notices": notices, "books_meta": books_meta, "props": props, "props_meta": props_meta, "prop_perf": S["prop_perf"],
            "sport_parlays": sp_parlays, "props_recent": sorted([p for p in S["props"].values() if p["st"] in ("win", "loss", "push")], key=lambda p: p["d"], reverse=True)[:120]}
    if args.data:
        json.dump(data, open(args.data, "w"), separators=(",", ":"))
    if args.template and args.out:  # single-file dashboard with the data baked in
        html = open(args.template).read().replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))
        open(args.out, "w").write(html)
    print(msg); print("games:", len(games), "| parlay groups:", len(live_parlays), "| state bytes:", os.path.getsize(args.state),
          "| player picks:", len(props), props_meta, "| market matches:", markets.get("matched"), "| traders:", len(markets.get("traders", [])), "| market errors:", markets.get("errors"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--state", required=True); r.add_argument("--template"); r.add_argument("--out")
    r.add_argument("--data"); r.add_argument("--cache", help="folder for the results history; makes later runs fast")
    run(ap.parse_args())
