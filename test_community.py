#!/usr/bin/env python3
"""Tests for community.py. Run: python3 -m unittest test_community -v
EVERYTHING IN THIS FILE IS SYNTHETIC TEST DATA. None of it is a real post, and none of it is ever
written to the live store or shown on the site (tests use a temporary folder and a fake network)."""
import os, tempfile, unittest
from datetime import datetime, timedelta, timezone
import community as C

NOW = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)   # a Wednesday
T = lambda h: (NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%MZ")


def game(i, sp, home, away, hours, tot=None):
    mk = lambda t, n: {"id": str(n), "n": t[0], "a": t[1], "loc": t[2], "nick": t[3], "short": t[3]}
    return {"id": f"SYN{i}", "sp": sp, "lg": sp, "date": T(hours), "odds": {"tot": tot} if tot else {}, "home": mk(home, i * 10 + 1), "away": mk(away, i * 10 + 2)}


GAMES = [
    game(1, "nba", ("Boston Celtics", "BOS", "Boston", "Celtics"), ("Los Angeles Lakers", "LAL", "Los Angeles", "Lakers"), 6, 221.5),
    game(2, "nba", ("Miami Heat", "MIA", "Miami", "Heat"), ("New York Knicks", "NY", "New York", "Knicks"), 7, 215.5),
    game(3, "nfl", ("Dallas Cowboys", "DAL", "Dallas", "Cowboys"), ("Carolina Panthers", "CAR", "Carolina", "Panthers"), 30, 44.5),
    game(4, "cfb", ("Duke Blue Devils", "DUKE", "Duke", "Blue Devils"), ("Miami Hurricanes", "MIA", "Miami", "Hurricanes"), 72, 51.5),
]
PROPS = [{"player": "Jayson Tatum", "sp": "nba", "eid": "SYN1"}, {"player": "Jalen Brunson", "sp": "nba", "eid": "SYN2"}]
ROSTERS = {"nba:12": {"t": NOW.isoformat(), "p": {"1": "LeBron James", "2": "Austin Reaves"}}}
SL = C.Slate(GAMES, PROPS, ROSTERS)
ex = lambda text, hours_ago=1: C.extract(text, NOW - timedelta(hours=hours_ago), SL)


class Extraction(unittest.TestCase):
    def test_parlay_three_legs_with_combined_odds(self):
        r = ex("3 leg parlay tonight: Celtics ML, Tatum 25+ points, Knicks +4.5. +425")
        self.assertEqual(r["bet_type"], "parlay"); self.assertEqual(r["total_odds"], 425); self.assertFalse(r["partial"])
        self.assertEqual([(l["market"], l["side"], l["line"]) for l in r["legs"]], [("ml", "home", None), ("prop", "over", 24.5), ("spread", "away", 4.5)])
        self.assertTrue(all(l["odds"] is None for l in r["legs"]))   # odds are never invented for a leg

    def test_player_is_the_selection_not_his_team(self):
        r = ex("NBA: Jayson Tatum over 27.5 points -115")
        self.assertEqual(len(r["legs"]), 1); self.assertEqual(r["legs"][0]["player"], "Jayson Tatum"); self.assertEqual(r["legs"][0]["odds"], -115)

    def test_single_spread_and_total(self):
        r = ex("NBA best bet: Lakers +6.5 (-110)")
        self.assertEqual((r["bet_type"], r["legs"][0]["market"], r["legs"][0]["line"], r["legs"][0]["odds"]), ("single", "spread", 6.5, -110))
        r = ex("Knicks at Heat under 214.5 for me. NBA")
        self.assertEqual((r["legs"][0]["market"], r["legs"][0]["side"], r["legs"][0]["line"]), ("total", "under", 214.5))

    def test_missing_line_or_market_gives_nothing(self):
        self.assertIsNone(ex("NBA tonight: I like the Celtics a lot"))
        self.assertIsNone(ex("Tatum is going to have a big night, NBA"))

    def test_invalid_inputs(self):
        for bad in ("", None, "short", "x" * 3000):
            self.assertIsNone(ex(bad))

    def test_odds_are_not_read_as_spreads(self):
        r = ex("NBA: Celtics ML -240")
        self.assertEqual((r["legs"][0]["market"], r["legs"][0]["odds"]), ("ml", -240))

    def test_recaps_promos_and_fades_are_skipped(self):
        for t in ("NBA results: Celtics ML cashed, Lakers +6.5 missed", "Use code WIN for bonus bets! Celtics ML", "Fade the Celtics ML tonight NBA"):
            self.assertEqual(ex(t)["status"], "skipped")

    def test_other_sports_do_not_borrow_team_names(self):
        self.assertIsNone(ex("College basketball picks: Duke -7.5 vs Illinois"))          # Duke football is on the slate, basketball is not
        self.assertIsNone(ex("NHL parlay: \U0001F3D2 Panthers ML \n\U0001F3D2 Bruins ML"))   # hockey Panthers, not Carolina
        r = ex("CFB Saturday: Duke -3.5")
        self.assertEqual(r["legs"][0]["eid"], "SYN4")

    def test_ambiguous_name_needs_a_sport(self):
        self.assertIsNone(ex("Miami ML is the play"))                 # Heat or Hurricanes: unknown, so nothing
        self.assertEqual(ex("NBA play: Miami ML")["legs"][0]["eid"], "SYN2")

    def test_partial_parlay_is_flagged_and_keeps_stated_odds(self):
        r = ex("NBA Parlay, +599 at BetMGM:\n- Jayson Tatum under 8.5 points (-115)\n- Some Unknown under 15.5 points (-115)\n- Jalen Brunson over 7.5 rebounds (+100)")
        self.assertTrue(r["partial"]); self.assertEqual(r["legs_total"], 3); self.assertEqual(len(r["legs"]), 2); self.assertEqual(r["total_odds"], 599)

    def test_last_name_needs_game_context(self):
        self.assertIsNone(ex("Reaves o2.5 3PM"))
        self.assertEqual(ex("Lakers at Celtics \U0001F3C0 Reaves o2.5 3PM")["legs"][0]["player"], "Austin Reaves")

    def test_weekday_must_match_game(self):
        self.assertIsNone(ex("NBA Monday Picks: Celtics ML"))        # the game is on Wednesday
        self.assertIsNotNone(ex("NBA Wednesday Picks: Celtics ML"))

    def test_earlier_game_blocks_match(self):
        sl = C.Slate(GAMES, PROPS, ROSTERS, schedule=[("nba", "11", T(-2))])   # Celtics also played 2 hours ago
        self.assertIsNone(C.extract("NBA: Celtics ML tonight", NOW - timedelta(hours=5), sl))

    def test_placed_versus_recommended(self):
        self.assertEqual(ex("I placed this: NBA Celtics ML")["stance"], "placed")
        self.assertEqual(ex("NBA lean: Celtics ML")["stance"], "recommended")


class Dedup(unittest.TestCase):
    def test_hash_ignores_links_tags_spacing(self):
        self.assertEqual(C.content_hash("Celtics ML  tonight #NBA https://a.co/x"), C.content_hash("celtics ml tonight #bets"))
        self.assertNotEqual(C.content_hash("Celtics ML"), C.content_hash("Lakers ML"))


class Settlement(unittest.TestCase):
    L = lambda s, m, side, line=None, sp="nba": {"market": m, "side": side, "line": line, "sp": sp}

    def test_legs(self):
        self.assertEqual(C.settle_leg(self.L("ml", "home"), 110, 100), "win")
        self.assertEqual(C.settle_leg(self.L("ml", "away"), 110, 100), "loss")
        self.assertEqual(C.settle_leg(self.L("ml", "home", sp="soccer"), 1, 1), "loss")
        self.assertEqual(C.settle_leg(self.L("spread", "away", 10.0), 110, 100), "push")
        self.assertEqual(C.settle_leg(self.L("spread", "away", 10.5), 110, 100), "win")
        self.assertEqual(C.settle_leg(self.L("total", "under", 215.5), 110, 100), "win")
        self.assertEqual(C.settle_leg(self.L("prop", "over", 24.5), 110, 100), "unknown")   # player bets are never guessed

    def test_parlay_and_no_result(self):
        mk = lambda: {"legs": [{"eid": "A", "market": "ml", "side": "home", "line": None, "sp": "nba", "start": T(-40), "st": "pending"},
                                {"eid": "B", "market": "ml", "side": "home", "line": None, "sp": "nba", "start": T(-40), "st": "pending"}], "st": "pending"}
        p = mk(); C.settle_post(p, {"A": [100, 90, ""], "B": [90, 100, ""]}, NOW); self.assertEqual(p["st"], "loss")
        p = mk(); C.settle_post(p, {"A": [100, 90, ""], "B": [100, 90, ""]}, NOW); self.assertEqual(p["st"], "win")
        p = mk(); C.settle_post(p, {"A": [100, 90, ""]}, NOW); self.assertEqual(p["st"], "unknown")       # B never got a final score
        p = mk(); p["partial"] = True; C.settle_post(p, {"A": [100, 90, ""], "B": [100, 90, ""]}, NOW); self.assertEqual(p["st"], "unknown")
        self.assertTrue(p["settled"])   # status changes are recorded


def fake_net(posts, alive=None, fail=False):
    def fetch(url):
        if fail:
            return None, "HTTP 503"
        if "getPosts" in url:
            return {"posts": [{"uri": u} for u in (alive if alive is not None else [p["uri"] for p in posts])]}, None
        return {"posts": posts}, None
    return fetch


def bpost(i, handle, text, hours_ago=1, labels=()):
    return {"uri": f"at://synthetic/{i}", "author": {"handle": handle, "displayName": handle, "labels": [{"val": v} for v in labels]},
            "record": {"text": text, "createdAt": (NOW - timedelta(hours=hours_ago)).isoformat()}}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(); self.store = os.path.join(self.dir.name, "store.json.gz")
        self._sleep = C.time.sleep; C.time.sleep = lambda s: None

    def tearDown(self):
        C.time.sleep = self._sleep; self.dir.cleanup()

    def run_(self, posts, **kw):
        return C.run(GAMES, PROPS, kw.pop("results", {}), self.store, NOW, rosters=ROSTERS, fetch=fake_net(posts, **kw))

    def test_collect_store_trend_and_api_shape(self):
        posts = [bpost(1, "a.test", "NBA: Celtics ML -150"), bpost(2, "b.test", "NBA lock: Celtics ML"), bpost(3, "c.test", "NBA: Celtics ML is my play"),
                 bpost(4, "d.test", "NBA: Lakers ML +130"), bpost(5, "a.test", "NBA again: Celtics ML"),            # same account, same bet
                 bpost(6, "e.test", "NBA: Celtics ML -150"),                                                        # copy of post 1
                 bpost(7, "hidden.test", "NBA: Celtics ML", labels=("!no-unauthenticated",)), bpost(8, "f.test", "nothing about betting here at all")]
        out = self.run_(posts)
        self.assertEqual(out["counts"]["shown"], 5)                       # 1,2,3,4,6; 5 dropped, 7 opted out, 8 no bet
        self.assertEqual(sum(1 for p in out["feed"] if p["dup"]), 1)
        top = out["trending"]["bets"][0]
        self.assertEqual((top["label"], top["n7"], top["of"]), ("Boston Celtics to win", 3, 4))   # 3 accounts of 4; the copy is not counted
        self.assertEqual(top["share"], 0.75)
        for k in ("asof", "feed", "sources", "trending", "perf", "runs", "counts"):
            self.assertIn(k, out)
        self.assertTrue(all(p["url"].startswith("https://bsky.app/profile/") for p in out["feed"]))
        self.assertTrue(os.path.exists(self.store))
        again = self.run_(posts)                                           # second run: nothing is saved twice
        self.assertEqual(again["counts"]["shown"], 5); self.assertEqual(again["runs"][0]["saved"], 0)

    def test_different_lines_are_not_merged(self):
        out = self.run_([bpost(1, "a.test", "NBA: Lakers +6.5"), bpost(2, "b.test", "NBA: Lakers +7.5"), bpost(3, "c.test", "NBA: Celtics -6.5")])
        by = {b["label"]: b for b in out["trending"]["bets"]}
        self.assertEqual(by["Los Angeles Lakers +6.5"]["of"], 2)           # only the two sides of the 6.5 line are compared
        self.assertEqual(by["Los Angeles Lakers +7.5"]["of"], 1)

    def test_deleted_posts_are_removed(self):
        posts = [bpost(1, "a.test", "NBA: Celtics ML"), bpost(2, "b.test", "NBA: Lakers ML")]
        self.run_(posts)
        out = self.run_(posts, alive=["at://synthetic/1"])
        self.assertEqual([p["id"] for p in out["feed"]], ["at://synthetic/1"]); self.assertEqual(out["runs"][0]["removed"], 1)

    def test_source_failure_is_reported_not_hidden(self):
        out = C.run(GAMES, PROPS, {}, self.store, NOW, rosters=ROSTERS, fetch=fake_net([], fail=True))
        b = next(s for s in out["sources"] if s["id"] == "bluesky")
        self.assertEqual(out["feed"], []); self.assertIn("HTTP 503", b["last_error"]); self.assertIsNone(b["last_ok"])

    def test_sources_without_permission_never_collect(self):
        for s in C.SOURCES:
            if not (s["collection_allowed"] and s["redistribution_allowed"]):
                self.assertFalse(s["enabled"], s["id"])
        saved = C.SOURCES[0]["redistribution_allowed"]; C.SOURCES[0]["redistribution_allowed"] = False
        try:
            self.assertEqual(self.run_([bpost(1, "a.test", "NBA: Celtics ML")])["feed"], [])
        finally:
            C.SOURCES[0]["redistribution_allowed"] = saved

    def test_results_and_account_record(self):
        posts = [bpost(1, "a.test", "NBA: Celtics ML"), bpost(2, "a.test", "NBA: Knicks +4.5")]
        self.run_(posts)
        C_now = NOW + timedelta(hours=12)
        out = C.run(GAMES, PROPS, {"SYN1": [120, 100, ""], "SYN2": [110, 100, ""]}, self.store, C_now, rosters=ROSTERS, fetch=fake_net(posts))
        self.assertEqual({p["st"] for p in out["feed"]}, {"win", "loss"})
        self.assertEqual((out["perf"][0]["w"], out["perf"][0]["l"], out["perf"][0]["rate"]), (1, 1, 0.5))


if __name__ == "__main__":
    unittest.main()
