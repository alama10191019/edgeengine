#!/usr/bin/env python3
"""
EdgeEngine community picks: collects publicly posted sports picks and parlays from sources that
allow automated access, extracts the bets, counts what is trending and settles game bets from
final scores. Python standard library only; called by engine.py on every update.

Nothing here is generated: every pick shown comes from a real public post and links to it.
A pick is only kept when it can be tied to a game (and, for player bets, a player) on the
current EdgeEngine slate. Anything unclear is stored as "review" and not shown.

Admin (edit this file on GitHub; there is no login page):
  SOURCES        turn a source on or off
  QUERIES        what to search for
  BLOCK_AUTHORS  account handles to ignore
  BLOCK_POSTS    post links or ids to remove
"""
import gzip, hashlib, json, os, re, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

USER_AGENT = "EdgeEngineCommunityBot/1.0 (+https://edgeengine-picks.netlify.app)"
KEEP_DAYS = 7            # posts older than this are deleted from the store
MAX_PAGES = 2            # pages per search per run
PAGE_SIZE = 50
MIN_CONF = 0.6           # extraction confidence needed to show a pick
MAX_FEED = 160
EXCERPT = 240

# ---- source registry -------------------------------------------------------------------------
# collection_allowed / redistribution_allowed are only True where the platform's own published
# rules were checked. Everything else stays off until its access requirement is met.
SOURCES = [
    {"id": "bluesky", "name": "Bluesky public posts", "platform": "Bluesky", "url": "https://bsky.app",
     "api": "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts (public AT Protocol API, no login)",
     "sports": "all", "parlays": True, "picks": True, "access": "None. Public API.",
     "collection_allowed": True, "redistribution_allowed": True, "enabled": True,
     "basis": "bsky.social/robots.txt: 'Crawling the public API is allowed'. Posts are public by design. "
              "Authors who opt out of logged-out display are skipped; deleted posts are removed on the next update."},
    {"id": "mastodon", "name": "Mastodon public hashtag timelines", "platform": "Mastodon", "url": "https://mastodon.social",
     "api": "https://mastodon.social/api/v1/timelines/tag/<tag> (works without login)",
     "sports": "all", "parlays": True, "picks": True, "access": "None technically.",
     "collection_allowed": False, "redistribution_allowed": False, "enabled": False,
     "basis": "Not approved: the server's terms on automated collection and republishing were not confirmed."},
    {"id": "reddit", "name": "Reddit (r/sportsbook, r/sportsbetting)", "platform": "Reddit", "url": "https://www.reddit.com/r/sportsbook/",
     "api": "Reddit Data API (OAuth)", "sports": "all", "parlays": True, "picks": True,
     "access": "A Reddit account, a registered API app and acceptance of Reddit's Data API terms. Requests without credentials are refused.",
     "collection_allowed": False, "redistribution_allowed": False, "enabled": False,
     "basis": "Not connected: needs credentials, and Reddit's terms restrict republishing user content."},
    {"id": "discord", "name": "Discord betting servers", "platform": "Discord", "url": "https://discord.com",
     "api": "Discord bot API", "sports": "all", "parlays": True, "picks": True,
     "access": "A bot added by each server's owner with message-content permission.",
     "collection_allowed": False, "redistribution_allowed": False, "enabled": False,
     "basis": "Not connected: no server has authorised a bot."},
    {"id": "tout_sites", "name": "Pick and handicapping websites", "platform": "Web", "url": "",
     "api": "None found", "sports": "all", "parlays": True, "picks": True,
     "access": "Written permission or a licensed feed from each site.",
     "collection_allowed": False, "redistribution_allowed": False, "enabled": False,
     "basis": "Not approved: publicly viewable is not permission to copy and republish."},
]
QUERIES = ["parlay", "NBA picks", "NFL picks", "moneyline", "player props", "best bets", "SGP", "CFB picks", "soccer picks", "prop bets",
           "NBA parlay", "NFL parlay", "nbapicks", "nflpicks", "sportsbetting", "POTD", "lock of the day", "NBA props"]
BLOCK_AUTHORS = set()
BLOCK_POSTS = set()

# ---- text patterns ---------------------------------------------------------------------------
STAT_WORDS = [
    (r"pts\s*\+\s*reb\s*\+\s*ast|pra\b|points\s*\+\s*rebounds\s*\+\s*assists", "points + rebounds + assists"),
    (r"rec(?:eiving|\.)?\s*(?:yds|yards)", "receiving yards"), (r"rush(?:ing|\.)?\s*(?:yds|yards)", "rushing yards"),
    (r"pass(?:ing|\.)?\s*(?:yds|yards)", "passing yards"), (r"pass(?:ing)?\s*tds?|passing touchdowns?", "passing touchdowns"),
    (r"receptions?|catches|rec\b", "receptions"), (r"points?|pts\b", "points"), (r"rebounds?|rebs?\b", "rebounds"),
    (r"assists?|asts?\b", "assists"), (r"3pm\b|threes|3-?pointers?(?: made)?|three[- ]pointers?(?: made)?|3s\b", "three-pointers"),
    (r"steals?|stl\b", "steals"), (r"blocks?|blk\b", "blocks"), (r"touchdowns?|tds?\b", "touchdowns"),
]
STAT_RE = "(?:" + "|".join(p for p, _ in STAT_WORDS) + ")"
NUM = r"(\d{1,3}(?:\.\d)?)"
RE_PROP_OU = re.compile(r"\b(over|under|o|u)\s*" + NUM + r"\s*(" + STAT_RE + ")", re.I)
RE_PROP_PLUS = re.compile(NUM + r"\s*\+\s*(" + STAT_RE + ")", re.I)
RE_TOTAL = re.compile(r"\b(over|under|o|u)\s*" + NUM + r"(?![\d+])", re.I)
RE_SPREAD = re.compile(r"^\s*(?:\([A-Z]{2,4}\)\s*)?(?:spread\s*)?([+-]\d{1,2}(?:\.5)?)(?!\d|\.\d)")
RE_ML = re.compile(r"^\s*(?:\([A-Z]{2,4}\)\s*)?(ml\b|moneyline|money line)", re.I)
RE_PK = re.compile(r"^\s*(pk\b|pick'?em)", re.I)
RE_ODDS = re.compile(r"(?<![\w.])([+-]\d{3,4})(?!\d|\.\d)")
RE_PARLAY = re.compile(r"\b(parlay|sgp|same[- ]game|\d+\s*-?\s*leg(?:ger)?s?|teaser|round robin)\b", re.I)
RE_LEGCOUNT = re.compile(r"\b(\d+)\s*-?\s*leg", re.I)
RE_PLACED = re.compile(r"\b(i(?:'m| am)? (?:on|taking|took|placed|bet|hammered|riding)|placed|locked in|my (?:bet|slip|ticket)|bet ?slip|i got|put \$?\d+)\b", re.I)
RE_SKIP = re.compile(r"\b(not picks|line mov|moved (?:from|off)|price check|spread moves|recap|results?:|yesterday'?s|last night'?s|missed|cashed|finished with|went \d+-\d+|"
                     r"final score|promo code|use code|sign[- ]?up bonus|deposit match|bonus bets?|probably aren'?t|fade|stay away|avoid|trap)\b|❌", re.I)
RE_TEAMTOT = re.compile(r"team total|team points|\btt\b", re.I)
RE_URL = re.compile(r"https?://\S+|\b[\w-]+\.(?:com|app|net|io|gg|ai|co)/\S*")
SPLIT = re.compile(r"\n+|[;|•→▶➡✅🔥]|,\s+|\s\+\s|\s/\s|\s&\s|\band\b(?=\s+[A-Z])")
HINTS = {"nba": r"\bnba\b|#nba|basketball", "nfl": r"\bnfl\b|#nfl|\b(?:tnf|snf|mnf)\b", "cfb": r"\bcfb\b|college football|ncaaf", "soccer": r"soccer|\bepl\b|premier league|uefa|la ?liga|serie a|bundesliga|\bmls\b|champions league|ligue 1"}
OTHER_SPORT = re.compile(r"college basketball|\bcbb\b|ncaab|\bnhl\b|hockey|\bmlb\b|baseball|wnba|tennis|\bufc\b|\bmma\b|\bkbo\b|\bnbl\b", re.I)
EMOJI_SPORT = {"\U0001F3C0": {"nba"}, "\U0001F3C8": {"nfl", "cfb"}, "⚽": {"soccer"}}
EMOJI_OTHER = "\U0001F3D2⚾\U0001F3BE\U0001F94A⛳\U0001F3CE\U0001F94E\U0001F3D0"
COLLIDE = {"kings", "jets", "panthers", "cardinals", "giants", "rangers", "wild", "bruins", "wildcats", "tigers", "bulldogs", "eagles", "hawks", "bears", "lions", "cougars", "huskies"}
RE_LEGLIKE = re.compile(r"\bML\b|moneyline|(?<![\w.])[+-]\d{1,2}(?:\.5)?(?!\d|\.\d)|\b(?:o|u|over|under)\s*\d|\d\s*\+\s*[A-Za-z]", re.I)
RE_DAY = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b", re.I)
RE_TONIGHT = re.compile(r"\b(tonight|today)\b", re.I)
SIDE_WORD = {"o": "over", "u": "under", "over": "over", "under": "under"}


def utc_now():
    return datetime.now(timezone.utc)


def parse_ts(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def content_hash(text):
    """Same text with different links, tags or spacing counts as one post (reposts and copies)."""
    t = RE_URL.sub(" ", text or "")
    t = re.sub(r"[#@]\w+", " ", t)
    return hashlib.sha256(norm(t).encode()).hexdigest()[:24]


def stat_name(raw):
    for pat, name in STAT_WORDS:
        if re.fullmatch(pat, raw.strip(), re.I):
            return name
    return None


# ---- slate index: what a post can be matched against -------------------------------------------
class Slate:
    def __init__(self, games, props=None, rosters=None, schedule=None):
        self.games = {g["id"]: g for g in games}
        self.sched = {}    # (sp, team_id) -> start times of every game, including ones already under way or finished
        for sp, tid, start in schedule or []:
            st = parse_ts(start)
            if st:
                self.sched.setdefault((sp, str(tid)), []).append(st)
        self.alias = {}    # lowercase name -> set of (sp, team_id)
        self.abbr = {}     # exact-case abbreviation -> set of (sp, team_id)
        self.team_games = {}   # (sp, team_id) -> [(start, eid, side)]
        self.team_name = {}
        for g in games:
            for side in ("home", "away"):
                t = g[side]; key = (g["sp"], str(t["id"]))
                self.team_games.setdefault(key, []).append((g["date"], g["id"], side))
                self.team_name[key] = t["n"]
                for nm in {t.get("n"), t.get("nick"), t.get("short")} | ({t.get("loc")} if g["sp"] in ("cfb", "soccer", "nba", "nfl") else set()):
                    n = norm(nm)
                    if len(n) >= 4:
                        self.alias.setdefault(n, set()).add(key)
                if t.get("a") and len(t["a"]) >= 2:
                    self.abbr.setdefault(t["a"].upper(), set()).add(key)
        for v in self.team_games.values():
            v.sort()
        self.players = {}  # lowercase full name / unique last name -> (name, sp, team_id or None, eid or None)
        full = {}
        for p in props or []:
            full[norm(p["player"])] = (p["player"], p["sp"], None, p["eid"])
        for key, r in (rosters or {}).items():
            sp, tid = key.split(":", 1)
            if (sp, tid) in self.team_games:
                for nm in (r.get("p") or {}).values():
                    if nm and norm(nm) not in full:
                        full[norm(nm)] = (nm, sp, tid, None)
        last = {}
        for n, v in full.items():
            parts = n.split()
            if len(parts) >= 2 and len(parts[-1]) >= 4:
                last.setdefault(parts[-1], []).append(v)
        self.players = dict(full)
        for ln, vs in last.items():
            if len(vs) == 1 and ln not in self.alias and ln not in self.players:
                self.players[ln] = vs[0]
        names = sorted(set(self.alias) | set(self.players), key=len, reverse=True)
        self.name_re = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(n) for n in names) + r")(?![a-z0-9])") if names else None
        self.abbr_re = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(sorted(map(re.escape, self.abbr), key=len, reverse=True)) + r")(?![A-Za-z0-9])") if self.abbr else None

    def game_for(self, key, posted):
        """Next game for this team that starts after the post was written (within 7 days)."""
        for start, eid, side in self.team_games.get(key, []):
            st = parse_ts(start)
            if st and (posted is None or posted < st <= posted + timedelta(days=7)):
                if posted is not None and any(posted - timedelta(hours=4) < x < st - timedelta(hours=1) for x in self.sched.get(key, [])):
                    return None, None   # the team had an earlier game after this post: the post is about that one
                return eid, side
        return None, None

    def resolve(self, keys, hints):
        keys = set(keys)
        if len(keys) > 1 and hints:
            keys = {k for k in keys if k[0] in hints} or keys
        return next(iter(keys)) if len(keys) == 1 else None


# ---- extraction --------------------------------------------------------------------------------
def _entities(seg, slate, hints, other=False, post_games=None):
    """Teams and players named in one piece of text, with the character position where each name ends.
    other: the post also talks about sports that are not tracked. post_games: games named anywhere in the post
    (None while those are still being worked out)."""
    if any(c in seg for c in EMOJI_OTHER):
        return []
    seg_h = set().union(*[v for c, v in EMOJI_SPORT.items() if c in seg]) if any(c in seg for c in EMOJI_SPORT) else None
    if seg_h is None and re.search(r"\bfootball\b", seg, re.I):
        seg_h = {"nfl", "cfb"}
    use = seg_h or hints
    out = []
    if slate.name_re:
        low = re.sub(r"[^a-z0-9]", " ", seg.lower())
        for m in slate.name_re.finditer(low):
            n = m.group(1)
            if n in slate.players and not (n in slate.alias):
                pl = slate.players[n]
                if " " not in n:   # last name only: accept only when the post names that player's game
                    if post_games is None:
                        continue
                    eid = pl[3] or slate.game_for((pl[1], pl[2]), None)[0]
                    if eid not in post_games:
                        continue
                out.append(("player", pl, m.start(), m.end()))
            elif n in slate.alias:
                keys = slate.alias[n]
                if seg_h:
                    keys = {k for k in keys if k[0] in seg_h}
                k = slate.resolve(keys, use) if keys else None
                if not k:
                    continue
                fullname = norm(slate.team_name[k]) == n
                if k[0] == "cfb" and not fullname and "cfb" not in (use or ()) and not (seg_h and "cfb" in seg_h):
                    continue   # college names are shared with basketball: need a football signal
                if other and not seg_h and (n in COLLIDE or k[0] not in (hints or ())):
                    continue   # mixed-sport post and nothing says which sport this name belongs to
                out.append(("team", k, m.start(), m.end()))
    if slate.abbr_re:
        for m in slate.abbr_re.finditer(seg):
            if any(a <= m.start() < b for _, _, a, b in out):
                continue
            keys = slate.abbr[m.group(1)]
            if seg_h:
                keys = {k for k in keys if k[0] in seg_h}
            k = slate.resolve(keys, use) if keys else None
            if k and (k[0] in (use or ()) ) and not (other and not seg_h):
                out.append(("abbr", k, m.start(), m.end()))
    return sorted(out, key=lambda x: x[2])


def _total_ok(g, line):
    tot = (g.get("odds") or {}).get("tot")
    if tot:
        return abs(line - tot) <= max(1.0, 0.2 * tot)
    lo, hi = {"nba": (170, 280), "nfl": (28, 70), "cfb": (28, 90), "soccer": (0.5, 6.5)}.get(g["sp"], (0, 999))
    return lo <= line <= hi


def extract(text, posted, slate):
    """Turns one post into a structured bet. Returns None when nothing reliable is found.
    Never fills in odds, lines or events that the post does not state."""
    if not text or len(text) < 12 or len(text) > 2500:
        return None
    if RE_SKIP.search(text):
        return {"status": "skipped", "reason": "recap, line-move or promotion wording", "legs": []}
    hints = {sp for sp, pat in HINTS.items() if re.search(pat, text, re.I)}
    clean = RE_URL.sub(" ", text)
    is_parlay = bool(RE_PARLAY.search(clean))
    other = bool(OTHER_SPORT.search(clean))
    legs, used_odds, post_games = [], [], set()
    segs = [s.strip() for s in SPLIT.split(clean) if s and s.strip()]
    leglike = sum(1 for s_ in segs if RE_LEGLIKE.search(RE_ODDS.sub(" ", s_)))
    for seg in segs:
        ents = _entities(seg, slate, hints, other)
        for kind, k, a, b in ents:
            if kind != "player":
                eid, _ = slate.game_for(k, posted)
                if eid:
                    post_games.add(eid)
    for seg in segs:
        ents = _entities(seg, slate, hints, other, post_games)
        seg_legs = []
        for i, (kind, k, a, b) in enumerate(ents):
            nxt = ents[i + 1][2] if i + 1 < len(ents) else len(seg)
            tail = seg[b:nxt]
            if kind == "player":
                name, sp, tid, eid = k
                if not eid and tid:
                    eid, _ = slate.game_for((sp, tid), posted)
                if not eid:
                    continue
                m = RE_PROP_OU.search(tail); m2 = RE_PROP_PLUS.search(tail)
                if m and (not m2 or m.start() <= m2.start()):
                    st = stat_name(m.group(3))
                    if st:
                        seg_legs.append({"eid": eid, "market": "prop", "player": name, "stat": st, "side": SIDE_WORD[m.group(1).lower()], "line": float(m.group(2)),
                                         "label": f"{name} {SIDE_WORD[m.group(1).lower()]} {float(m.group(2)):g} {st}", "conf": 0.9})
                elif m2:
                    st = stat_name(m2.group(2))
                    if st:
                        v = float(m2.group(1))
                        seg_legs.append({"eid": eid, "market": "prop", "player": name, "stat": st, "side": "over", "line": v - 0.5,
                                         "label": f"{name} {v:g}+ {st}", "conf": 0.9})
                continue
            eid, side = slate.game_for(k, posted)
            if not eid:
                continue
            g = slate.games[eid]; tn = slate.team_name[k]
            if RE_TEAMTOT.search(tail):
                continue  # team totals are not tracked
            m = RE_SPREAD.match(tail)
            if m:
                ln = float(m.group(1))
                if abs(ln) <= (4.5 if g["sp"] == "soccer" else 45):
                    seg_legs.append({"eid": eid, "market": "spread", "side": side, "line": ln, "label": f"{tn} {ln:+g}", "conf": 0.9})
                    continue
            if RE_PK.match(tail):
                seg_legs.append({"eid": eid, "market": "spread", "side": side, "line": 0.0, "label": f"{tn} pick'em", "conf": 0.85}); continue
            if RE_ML.match(tail):
                seg_legs.append({"eid": eid, "market": "ml", "side": side, "line": None, "label": f"{tn} to win", "conf": 0.9})
                continue
            bare = re.sub(r"[^A-Za-z0-9+\- ]", " ", seg[:a] + seg[b:]); bare = RE_ODDS.sub(" ", bare).strip()
            if is_parlay and len(ents) == 1 and len(bare.split()) <= 1 and kind != "abbr":
                seg_legs.append({"eid": eid, "market": "ml", "side": side, "line": None, "label": f"{tn} to win", "conf": 0.6, "implied": True})
        if not any(l["market"] != "prop" for l in seg_legs) and not RE_TEAMTOT.search(seg):
            # game total: "over 215.5" in a piece of text that names a team, or in a post about one game
            m = RE_TOTAL.search(seg)
            if m and not RE_PROP_OU.search(seg):
                teams = {slate.game_for(k, posted)[0] for kind, k, _, _ in ents if kind != "player"} - {None}
                cand = teams if len(teams) == 1 else (post_games if len(post_games) == 1 and not ents else set())
                if len(cand) == 1:
                    eid = next(iter(cand)); g = slate.games[eid]; ln = float(m.group(2))
                    if _total_ok(g, ln):
                        sd = SIDE_WORD[m.group(1).lower()]
                        seg_legs.append({"eid": eid, "market": "total", "side": sd, "line": ln, "label": f"{g['away']['a']} at {g['home']['a']} {sd} {ln:g}", "conf": 0.85})
        od = RE_ODDS.findall(seg)
        if len(seg_legs) == 1 and len(od) == 1:
            seg_legs[0]["odds"] = int(od[0]); used_odds.append(od[0])
        legs += seg_legs
    # timing words must agree with the matched game: "Monday picks" cannot be about a Wednesday game
    days = {d.lower() for d in RE_DAY.findall(clean)}
    tonight = bool(RE_TONIGHT.search(clean))
    def timing_ok(l):
        st = parse_ts(slate.games[l["eid"]]["date"])
        if days and st.astimezone(ET).strftime("%A").lower() not in days:
            return False
        return not (tonight and not days and st > posted + timedelta(hours=20))
    legs = [l for l in legs if timing_ok(l)]
    seen, uniq = set(), []
    for l in legs:
        key = (l["eid"], l["market"], l.get("player"), l.get("stat"), l["side"], l["line"])
        if key not in seen:
            seen.add(key); uniq.append(l)
    legs = uniq
    if not legs:
        return None
    all_odds = RE_ODDS.findall(clean)
    total_odds = None
    said = RE_LEGCOUNT.search(clean)
    is_parlay = is_parlay and (len(legs) >= 2 or leglike >= 2)
    legs_total = max(len(legs), leglike, int(said.group(1)) if said and int(said.group(1)) <= 25 else 0) if is_parlay else len(legs)
    partial = is_parlay and legs_total > len(legs)
    if is_parlay:
        m = re.search(r"(?:parlay|sgp|combined|total odds|pays)[^\n+\-]{0,30}([+-]\d{3,5})(?!\d|\.\d)", clean, re.I)
        if m:                              # "Parlay, +599 at BetMGM"
            total_odds = int(m.group(1))
        elif len(all_odds) == 1:           # one price on a parlay post is the combined price
            total_odds = int(all_odds[0])
            for l in legs:
                l.pop("odds", None)
        elif not partial:
            left = list(all_odds)
            for l in legs:
                if l.get("odds") is not None and f"{l['odds']:+d}" in left:
                    left.remove(f"{l['odds']:+d}")
            if len(left) == 1:
                total_odds = int(left[0])
    for l in legs:
        g = slate.games[l["eid"]]
        l["sp"] = g["sp"]; l["game"] = f"{g['away']['n']} at {g['home']['n']}"; l["start"] = g["date"]; l["st"] = "pending"
        l.setdefault("odds", None)
    conf = min(l["conf"] for l in legs)
    return {"status": "ok" if conf >= MIN_CONF else "review", "bet_type": "parlay" if is_parlay else "single", "legs": legs,
            "total_odds": total_odds, "stance": "placed" if RE_PLACED.search(clean) else "recommended", "conf": round(conf, 2),
            "legs_total": legs_total, "partial": partial, "sgp": is_parlay and not partial and len({l["eid"] for l in legs}) == 1}


# ---- collection --------------------------------------------------------------------------------
def http_json(url, tries=3):
    """GET with retries and back-off. Returns (data, error)."""
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (400, 401, 403, 404):
                break
            time.sleep(4 * (i + 1) if e.code == 429 else 1.5 * (i + 1))
        except Exception as e:
            err = type(e).__name__; time.sleep(1.5 * (i + 1))
    return None, err


BSKY_HOSTS = ["https://api.bsky.app", "https://public.api.bsky.app"]


def bluesky_collect(src_state, now, fetch=http_json):
    """Searches public Bluesky posts. Incremental: only posts newer than the last successful run."""
    since = src_state.get("since") or (now - timedelta(hours=36)).isoformat(timespec="seconds").replace("+00:00", "Z")
    posts, errors, requests_made = {}, [], 0
    host = src_state.get("host") or BSKY_HOSTS[0]
    for q in QUERIES:
        cursor = None
        for _ in range(MAX_PAGES):
            qs = {"q": q, "limit": PAGE_SIZE, "sort": "latest", "lang": "en", "since": since}
            if cursor:
                qs["cursor"] = cursor
            d, err = fetch(f"{host}/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(qs)); requests_made += 1
            if d is None and not posts and not cursor and q == QUERIES[0]:   # first request failed: try the other public host
                for alt in BSKY_HOSTS:
                    if alt != host:
                        d, err2 = fetch(f"{alt}/xrpc/app.bsky.feed.searchPosts?" + urllib.parse.urlencode(qs)); requests_made += 1
                        if d is not None:
                            host = alt; err = None
            if d is None:
                errors.append(f"{q}: {err}"); break
            for p in d.get("posts") or []:
                a = p.get("author") or {}; rec = p.get("record") or {}
                if any((l.get("val") or "") == "!no-unauthenticated" for l in a.get("labels") or []):
                    continue   # author asked not to be shown to logged-out viewers
                if rec.get("reply"):
                    continue
                uri = p.get("uri") or ""; rkey = uri.rsplit("/", 1)[-1]
                posts[uri] = {"id": uri, "src": "bluesky", "author": a.get("handle") or "", "name": a.get("displayName") or a.get("handle") or "",
                              "bot": any((l.get("val") or "") == "bot" for l in a.get("labels") or []),
                              "url": f"https://bsky.app/profile/{a.get('handle')}/post/{rkey}", "posted": rec.get("createdAt") or p.get("indexedAt"),
                              "text": rec.get("text") or "", "has_image": "images" in str((p.get("embed") or {}).get("$type", ""))}
            cursor = d.get("cursor")
            if not cursor or len(d.get("posts") or []) < PAGE_SIZE:
                break
            time.sleep(0.4)
        time.sleep(0.4)
    ok = len(errors) < len(QUERIES)
    if ok:
        src_state["since"] = (now - timedelta(minutes=20)).isoformat(timespec="seconds").replace("+00:00", "Z")   # small overlap; duplicates are dropped by id
        src_state["host"] = host
    return list(posts.values()), errors, requests_made, ok


def bluesky_still_there(uris, fetch=http_json):
    """Returns the set of post ids that still exist, or None if the check could not be made."""
    alive = set()
    for i in range(0, len(uris), 25):
        qs = "&".join("uris=" + urllib.parse.quote(u, safe="") for u in uris[i:i + 25])
        d, err = fetch(f"{BSKY_HOSTS[0]}/xrpc/app.bsky.feed.getPosts?{qs}")
        if d is None:
            return None
        alive |= {p.get("uri") for p in d.get("posts") or []}
        time.sleep(0.3)
    return alive


ADAPTERS = {"bluesky": bluesky_collect}


# ---- settlement --------------------------------------------------------------------------------
def settle_leg(leg, hs, as_):
    """Game bets only. Player bets stay 'unknown': their box-score numbers are not checked here."""
    m, side, line = leg["market"], leg["side"], leg.get("line")
    if m == "ml":
        if hs == as_:
            return "loss" if leg.get("sp") == "soccer" else "push"
        return "win" if (hs > as_) == (side == "home") else "loss"
    if m == "spread":
        d = (hs - as_ if side == "home" else as_ - hs) + line
        return "push" if d == 0 else ("win" if d > 0 else "loss")
    if m == "total":
        t = hs + as_
        return "push" if t == line else ("win" if (t > line) == (side == "over") else "loss")
    return "unknown"


def settle_post(post, results, now):
    changed = False
    for l in post["legs"]:
        if l["st"] != "pending":
            continue
        r = results.get(l["eid"]); start = parse_ts(l["start"])
        if r and l["market"] != "prop":
            l["st"] = settle_leg(l, r[0], r[1]); changed = True
        elif start and now > start + timedelta(hours=30 if l["market"] != "prop" else 6):
            l["st"] = "unknown"; changed = True   # no final score reached us (postponed, cancelled) or a player bet
    sts = [l["st"] for l in post["legs"]]
    old = post.get("st", "pending")
    if "loss" in sts:
        post["st"] = "loss"
    elif "pending" in sts:
        post["st"] = "pending"
    elif "unknown" in sts or post.get("partial"):
        post["st"] = "unknown"
    elif all(s == "push" for s in sts):
        post["st"] = "push"
    else:
        post["st"] = "win"
    if post["st"] != old:
        post.setdefault("settled", []).append([now.isoformat(timespec="seconds"), old, post["st"]])
    return changed


# ---- trending and consensus ----------------------------------------------------------------------
def leg_key(l):
    return (l["eid"], l["market"], l.get("player") or "", l.get("stat") or "", l["side"], l["line"])


def analytics(posts, now):
    """Counts are distinct accounts, never posts, so one account repeating itself counts once.
    Consensus is only computed among bets on the same game, market and line."""
    live = [p for p in posts if p["status"] == "ok" and not p.get("dup")]
    d1, d2, d7 = now - timedelta(hours=24), now - timedelta(hours=48), now - timedelta(days=7)
    bets, teams, players, sports, authors = {}, {}, {}, {}, {}
    for p in live:
        t = parse_ts(p["posted"])
        if not t or t < d7:
            continue
        a = authors.setdefault(p["author"], {"author": p["author"], "name": p["name"], "posts": 0, "legs": 0, "src": p["src"]})
        a["posts"] += 1; a["legs"] += len(p["legs"])
        for l in p["legs"]:
            b = bets.setdefault(leg_key(l), {"label": l["label"], "game": l["game"], "sp": l["sp"], "market": l["market"], "eid": l["eid"], "side": l["side"], "line": l["line"],
                                             "start": l["start"], "a24": set(), "aprev": set(), "a7": set(), "pending": False})
            b["a7"].add(p["author"]); b["pending"] |= l["st"] == "pending"
            if t >= d1:
                b["a24"].add(p["author"])
            elif t >= d2:
                b["aprev"].add(p["author"])
            if l["st"] == "pending":
                sports.setdefault(l["sp"], set()).add(p["id"])
                if l["market"] == "prop":
                    players.setdefault(l["player"], set()).add(p["author"])
                elif l["market"] in ("ml", "spread"):
                    teams.setdefault(l["label"].rsplit(" to win", 1)[0] if l["market"] == "ml" else re.sub(r" [+-][\d.]+$| pick'em$", "", l["label"]), set()).add(p["author"])
    groups = {}
    for k, b in bets.items():
        if b["pending"]:
            groups.setdefault((k[0], k[1], k[2], k[3], abs(k[5]) if k[1] == "spread" and k[5] is not None else k[5]), []).append(b)
    trending = []
    for k, b in bets.items():
        if not b["pending"]:
            continue
        g = groups[(k[0], k[1], k[2], k[3], abs(k[5]) if k[1] == "spread" and k[5] is not None else k[5])]
        allsrc = set().union(*[x["a7"] for x in g])
        trending.append({"label": b["label"], "game": b["game"], "sp": b["sp"], "market": b["market"], "eid": b["eid"], "start": b["start"],
                         "n24": len(b["a24"]), "n7": len(b["a7"]), "change": len(b["a24"]) - len(b["aprev"]),
                         "of": len(allsrc), "share": round(len(b["a7"]) / len(allsrc), 3) if len(allsrc) >= 3 else None})
    trending.sort(key=lambda x: (-x["n7"], -x["n24"], x["start"]))
    top = lambda d, n=12: [{"name": k, "n": len(v)} for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))[:n]]
    return {"bets": trending[:60], "teams": top(teams), "players": top(players), "sports": top(sports, 8),
            "authors": sorted(authors.values(), key=lambda a: -a["legs"])[:20]}


def performance(posts):
    """Record per account from settled game bets only. Nothing is shown until results exist."""
    out = {}
    for p in posts:
        if p["status"] != "ok" or p.get("dup"):
            continue
        for l in p["legs"]:
            if l["st"] in ("win", "loss", "push"):
                r = out.setdefault(p["author"], {"author": p["author"], "name": p["name"], "w": 0, "l": 0, "p": 0})
                r[{"win": "w", "loss": "l", "push": "p"}[l["st"]]] += 1
    rows = list(out.values())
    for r in rows:
        r["n"] = r["w"] + r["l"] + r["p"]; r["rate"] = round(r["w"] / (r["w"] + r["l"]), 3) if r["w"] + r["l"] else None
    return sorted(rows, key=lambda r: -r["n"])[:30]


# ---- store ("database": one compressed JSON file kept with the pick ledger) -------------------------
def load_store(path):
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return json.loads(gzip.decompress(f.read()))
        except Exception:
            pass
    return {"version": 1, "posts": {}, "sources": {}, "runs": [], "seen": {}}


def save_store(store, path):
    if path:
        with open(path, "wb") as f:
            f.write(gzip.compress(json.dumps(store, separators=(",", ":")).encode(), 9))


# ---- one update ------------------------------------------------------------------------------------
def update_rosters(store, games, now, roster_fn, limit=40):
    """Player names for NBA and NFL teams playing in the next 4 days, refreshed every 3 days.
    roster_fn(sport, team_id) -> {athlete_id: name}; engine.py supplies it."""
    ros = store.setdefault("rosters", {}); n = 0
    for g in sorted(games, key=lambda g: g["date"]):
        st = parse_ts(g["date"])
        if g["sp"] not in ("nba", "nfl") or not st or st > now + timedelta(days=4):
            continue
        for side in ("home", "away"):
            key = f"{g['sp']}:{g[side]['id']}"; c = ros.get(key)
            if (c and (now - (parse_ts(c["t"]) or now)).total_seconds() < 3 * 86400) or n >= limit:
                continue
            n += 1
            try:
                names = roster_fn(g["sp"], g[side]["id"]) or {}
            except Exception:
                names = {}
            if names:
                ros[key] = {"t": now.isoformat(timespec="seconds"), "p": names}
    cut = now - timedelta(days=10)
    for k in [k for k, v in ros.items() if (parse_ts(v["t"]) or now) < cut]:
        ros.pop(k)
    return ros


def run(games, props, results, store_path, now=None, rosters=None, fetch=http_json, check_deleted=True, roster_fn=None, schedule=None):
    now = now or utc_now()
    store = load_store(store_path)
    posts, seen = store["posts"], store["seen"]
    if rosters is None:
        rosters = update_rosters(store, games, now, roster_fn) if roster_fn else (store.get("rosters") or {})
    slate = Slate(games, props, rosters, schedule)
    run_log = {"t": now.isoformat(timespec="seconds"), "found": 0, "new": 0, "saved": 0, "review": 0, "dupes": 0, "removed": 0, "errors": []}
    for src in SOURCES:
        ss = store["sources"].setdefault(src["id"], {})
        if not (src["enabled"] and src["collection_allowed"] and src["redistribution_allowed"] and src["id"] in ADAPTERS):
            continue
        try:
            got, errors, nreq, ok = ADAPTERS[src["id"]](ss, now, fetch)
        except Exception as e:
            got, errors, nreq, ok = [], [type(e).__name__], 0, False
        ss["last_checked"] = now.isoformat(timespec="seconds"); ss["requests"] = nreq
        ss["last_error"] = "; ".join(errors[:3]) if errors else None
        if ok:
            ss["last_ok"] = now.isoformat(timespec="seconds")
        run_log["found"] += len(got); run_log["errors"] += [f"{src['id']}: {e}" for e in errors[:3]]
        for p in got:
            if p["id"] in posts or p["id"] in seen or p["author"] in BLOCK_AUTHORS or p["id"] in BLOCK_POSTS or p["url"] in BLOCK_POSTS:
                continue
            posted = parse_ts(p["posted"])
            if not posted or posted > now + timedelta(minutes=10) or posted < now - timedelta(days=KEEP_DAYS):
                continue
            run_log["new"] += 1
            ex = extract(p["text"], posted, slate)
            seen[p["id"]] = now.isoformat(timespec="seconds")[:10]
            if not ex or not ex["legs"]:
                continue
            h = content_hash(p["text"])
            dup = next((o["id"] for o in posts.values() if o.get("hash") == h and o["author"] != p["author"]), None)
            same = next((o for o in posts.values() if o["author"] == p["author"] and {leg_key(l) for l in o["legs"]} == {leg_key(l) for l in ex["legs"]}), None)
            if same:
                run_log["dupes"] += 1; continue      # the same account posting the same bet again
            t = " ".join(p["text"].split())
            rec = {"id": p["id"], "src": p["src"], "author": p["author"], "name": p["name"], "bot": p["bot"], "url": p["url"], "posted": p["posted"],
                   "collected": now.isoformat(timespec="seconds"), "hash": h, "excerpt": t[:EXCERPT] + ("…" if len(t) > EXCERPT else ""),
                   "status": ex["status"], "bet_type": ex["bet_type"], "sgp": ex["sgp"], "legs": ex["legs"], "legs_total": ex["legs_total"], "partial": ex["partial"], "total_odds": ex["total_odds"], "stance": ex["stance"],
                   "conf": ex["conf"], "st": "pending", "verification": "unverified"}
            if dup:
                rec["dup"] = dup; run_log["dupes"] += 1
            posts[p["id"]] = rec
            run_log["saved" if ex["status"] == "ok" else "review"] += 1
    # deletions: anything shown must still exist at the source
    if check_deleted:
        shown = [p["id"] for p in posts.values() if p["src"] == "bluesky" and p["status"] == "ok" and p["st"] == "pending"][:150]
        if shown:
            alive = bluesky_still_there(shown, fetch)
            if alive is not None:
                for pid in shown:
                    if pid not in alive:
                        posts.pop(pid, None); run_log["removed"] += 1
    for pid in [k for k, p in posts.items() if p["author"] in BLOCK_AUTHORS or k in BLOCK_POSTS or p["url"] in BLOCK_POSTS]:
        posts.pop(pid, None); run_log["removed"] += 1
    for p in posts.values():
        if p["status"] == "ok":
            settle_post(p, results or {}, now)
    lim = now - timedelta(days=KEEP_DAYS)
    for pid in [k for k, p in posts.items() if (parse_ts(p["posted"]) or lim) < lim]:
        posts.pop(pid, None)
    cut = (now - timedelta(days=KEEP_DAYS + 1)).isoformat()[:10]
    store["seen"] = {k: v for k, v in seen.items() if v >= cut}
    store["runs"] = (store["runs"] + [run_log])[-60:]
    save_store(store, store_path)
    return public_view(store, now)


def public_view(store, now):
    posts = list(store["posts"].values())
    ok = sorted([p for p in posts if p["status"] == "ok"], key=lambda p: p["posted"], reverse=True)
    keep = ("id", "src", "author", "name", "bot", "url", "posted", "excerpt", "bet_type", "sgp", "legs", "legs_total", "partial", "total_odds", "stance", "st", "dup", "verification")
    feed = [{k: p.get(k) for k in keep} for p in ok[:MAX_FEED]]
    for p in feed:
        p["legs"] = [{k: l.get(k) for k in ("eid", "sp", "market", "label", "game", "start", "line", "side", "odds", "st", "player", "stat", "implied")} for l in p["legs"]]
    srcs = []
    for s in SOURCES:
        ss = store["sources"].get(s["id"], {})
        active = s["enabled"] and s["collection_allowed"] and s["redistribution_allowed"] and s["id"] in ADAPTERS
        srcs.append({**{k: s[k] for k in ("id", "name", "platform", "url", "api", "access", "basis", "collection_allowed", "redistribution_allowed", "enabled")},
                     "active": active, "last_ok": ss.get("last_ok"), "last_checked": ss.get("last_checked"), "last_error": ss.get("last_error"),
                     "posts": sum(1 for p in ok if p["src"] == s["id"])})
    return {"asof": now.isoformat(timespec="seconds"), "feed": feed, "sources": srcs, "trending": analytics(posts, now), "perf": performance(posts),
            "runs": store["runs"][-12:][::-1], "counts": {"shown": len(ok), "review": sum(1 for p in posts if p["status"] == "review"),
                                                          "parlays": sum(1 for p in ok if p["bet_type"] == "parlay"), "accounts": len({p["author"] for p in ok})},
            "keep_days": KEEP_DAYS}


if __name__ == "__main__":   # manual check: python3 community.py data/data.json
    import sys
    d = json.load(open(sys.argv[1]))
    out = run(d["games"], d.get("props"), {}, sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps({"counts": out["counts"], "run": out["runs"][0], "sources": [(s["id"], s["active"], s["last_error"]) for s in out["sources"]]}, indent=1))
    for p in out["feed"][:40]:
        print("-", p["author"], p["bet_type"], p["total_odds"], "|", "; ".join(l["label"] for l in p["legs"]), "\n    ", p["excerpt"][:150])
