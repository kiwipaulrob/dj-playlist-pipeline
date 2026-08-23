#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""PR-C test suite — consecutive-spin dedupe + low-count warning (24 Aug 2026).

The dedupe lives inline in ddlib.kexp_play_walk's page loop; these tests
exercise it through the module with _kexp_get monkeypatched (no network).

Run: python3 test_pr7_ingestion_hardening.py
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "ddlib", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dandelion_dash_lib.py"))
ddlib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ddlib)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def play(artist, song, ts, show=15):
    return {"artist": artist, "song": song, "airdate": ts,
            "play_type": "trackplay", "show": show}


# NOTE: kexp_play_walk stops at any page with len(res) < 1000 (short-page =
# end of feed). Real KEXP pages are exactly 1000 entries, so the mock pads
# non-final pages with filler plays to full size.
FILLER = [play(f"Filler{i}", f"Song{i}", f"2026-07-04T00:{i // 60:02d}:{i % 60:02d}Z",
               show=99) for i in range(999)]

PAGES = [
    FILLER + [play("Idles", "Colossus", "2026-07-05T10:00:00Z")],
    [play("Idles", "Colossus", "2026-07-05T10:02:00Z"),   # +2min repeat -> dropped
     play("Idles", "Colossus", "2026-07-05T10:09:00Z")] + FILLER[:998],
         # within extended window -> dropped
    [play("Idles", "Colossus", "2026-07-05T10:20:00Z"),   # 11min after last log -> KEPT
     play("Björk", "Jóga", "2026-07-05T10:25:00Z"),       # different song -> kept
     play("IDLES", "COLOSSUS", "2026-07-05T10:27:00Z"),   # same pair diff case -> dropped
     play("Idles", "Mother", "2026-07-05T10:30:00Z"),     # kept
     ],
]
captured = []


def fake_get(path, timeout=30):
    captured.append(path)
    n = len(captured)
    if n <= len(PAGES):
        return {"results": PAGES[n - 1], "count": 3000}
    return {"results": [], "count": 3000}


ddlib._kexp_get = fake_get
out = ddlib.kexp_play_walk("2026-07")
interesting = [t for t in out if t[0].lower().startswith("filler") is False]
check("returns list", isinstance(out, list) and out is not None)
check("consecutive repeats collapsed across pages",
      interesting == [("Idles", "Colossus"), ("Idles", "Colossus"),
                      ("Björk", "Jóga"), ("IDLES", "COLOSSUS"),
                      ("Idles", "Mother")], f"{interesting}")
check("non-consecutive repeat kept (global dedupe still downstream)",
      interesting.count(("Idles", "Colossus")) == 2, f"{interesting}")
# IDLES/COLOSSUS at 10:27 is NOT consecutive with the 10:20 spin (Björk played
# between) -> correctly KEPT by the consecutive-only dedupe (the downstream
# global artist|title set collapses it later)
check("interleaved repeat kept by consecutive-dedupe (by design)",
      ("IDLES", "COLOSSUS") in interesting, f"{interesting}")

# boundary: exactly 600s apart is a drop; 601s+ is a new spin
SEQ = [
    FILLER[:999] +
    [play("A", "X", "2026-07-06T12:00:00Z"),
     play("A", "X", "2026-07-06T12:10:00Z")],   # exactly 600s -> dropped
    [play("A", "X", "2026-07-06T12:20:01Z"),    # 601s after last log -> kept
     play("A", "Y", "2026-07-06T12:25:00Z")],
]


def fake_get2(path, timeout=30):
    fake_get2.n = getattr(fake_get2, "n", 0) + 1
    if fake_get2.n <= len(SEQ):
        return {"results": SEQ[fake_get2.n - 1], "count": 2000}
    return {"results": [], "count": 0}


ddlib._kexp_get = fake_get2
out2 = ddlib.kexp_play_walk("2026-07")
interesting2 = [t for t in out2 if t[1] in ("X", "Y")]
check("600s drop + 601s keep",
      interesting2 == [("A", "X"), ("A", "Y")], f"{interesting2}")

# malformed airdate must not crash the dedupe (plays with unparseable airdates
# are already excluded upstream by the month-prefix filter — assert that holds)
BAD = [
    FILLER[:999] +
    [play("B", "Y", "2026-07-08T08:00:00Z"),
     play("B", "Y", "not-a-date"),            # filtered out before dedupe (no month prefix)
     play("B", "Y", "2026-07-08T08:05:00Z"),  # 5min after first -> dropped by dedupe
     play("C", "Z", "2026-07-08T09:00:00Z")],
]


def fake_get3(path, timeout=30):
    fake_get3.n = getattr(fake_get3, "n", 0) + 1
    if fake_get3.n <= len(BAD):
        return {"results": BAD[fake_get3.n - 1], "count": 1500}
    return {"results": [], "count": 0}


ddlib._kexp_get = fake_get3
out3 = ddlib.kexp_play_walk("2026-07")
interesting3 = [t for t in out3 if t[0] in ("B", "C")]
check("malformed airdate filtered upstream; dedupe still correct",
      interesting3 == [("B", "Y"), ("C", "Z")], f"{interesting3}")

# ---- PR-E: absent-DJ detection (scrape_month with mocked Fetcher) ----
import importlib.util as _ilu

_dspec = _ilu.spec_from_file_location(
    "_d2ma", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dandelion-to-ma.py"))
d2ma = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(d2ma)


class FakePage:
    def __init__(self, html):
        self._html = html

    def css(self, sel):
        # minimal support for the selectors scrape_month uses
        if sel == 'tr':
            import re as _re
            rows = []
            for tr_html in _re.findall(r"<tr>(.*?)</tr>", self._html, _re.S):
                rows.append(FakeRow(tr_html))
            return rows
        return []


class FakeNode:
    def __init__(self, html):
        self.html_content = html

    def css(self, sel):
        return [self] if sel == '::text' and False else []


class FakeTd(FakeNode):
    pass


class FakeRow:
    def __init__(self, html):
        self._html = html
        import re as _re
        self.tds = [_FakeTd(h) for h in _re.findall(r"<td[^>]*>(.*?)</td>", html, _re.S)]
        b = _re.findall(r"<b>(.*?)</b>", html, _re.S)
        self.b = [type("B", (), {"html_content": b[0]})()] if b else []

    def css(self, sel):
        if sel == 'td.tdblue b':
            return self.b
        if sel == 'td.tdblue':
            return [type("TD", (), {"css": lambda s, _: self.b})()] if self.b else []
        if sel == 'td':
            return self.tds
        if sel == 'td.tdheadings':
            return []
        return []


class _FakeTd:
    """Text-bearing td: first ::text node is its stripped content."""

    def __init__(self, html):
        import re as _re
        self.texts = [t for t in _re.sub(r"<[^>]+>", "", html).split("\n") if t.strip()]

    def css(self, sel):
        class L(list):
            def get(self):
                return self[0] if self else None
        return L(self.texts) if sel == '::text' else L([])


HTML_OK = """
<table>
<tr><td class="tdblue"><a><b>DJ Mark Whitby - August 2026</b></a></td></tr>
<tr><td><a href="#">The Fall</a></td><td>Totally Wired</td><td></td><td></td><td></td></tr>
<tr><td><a href="#">Bauhaus</a></td><td>She's in Parties</td><td></td><td></td><td></td></tr>
<tr><td><a href="#">Chameleons</a></td><td>Swamp Thing</td><td></td><td></td><td></td></tr>
<tr><td class="tdblue"><a><b>DJ Ann Unknown - August 2026</b></a></td></tr>
<tr><td><a href="#">R.E.M.</a></td><td>Driver 8</td><td></td><td></td><td></td></tr>
<tr><td><a href="#">10,000 Maniacs</a></td><td>What's the Weather?</td><td></td><td></td><td></td></tr>
</table>"""

# DJ whose header parses but track rows are all malformed -> absent
HTML_ABSENT = HTML_OK.replace(
    '<tr><td><a href="#">R.E.M.</a></td><td>Driver 8</td><td></td><td></td><td></td></tr>\n'
    '<tr><td><a href="#">10,000 Maniacs</a></td><td>What\'s the Weather?</td><td></td><td></td><td></td></tr>',
    '')

orig_get = d2ma.Fetcher.get


class FakeFetcher:
    html = HTML_OK

    @staticmethod
    def get(url):
        return FakePage(FakeFetcher.html)


d2ma.Fetcher = FakeFetcher

# happy path: no absent DJs
FakeFetcher.html = HTML_OK
secs = d2ma.scrape_month("2026-08")
check("happy path: both DJs have tracks", sorted(secs) == ["DJ Ann Unknown", "DJ Mark Whitby"],
      f"{sorted(secs)}")
check("happy path: absent list empty", d2ma.absent_djs() == [], f"{d2ma.absent_djs()}")

# layout-drift path: one DJ's rows vanish
FakeFetcher.html = HTML_ABSENT
secs2 = d2ma.scrape_month("2026-08")
check("drift: healthy DJ still parsed", list(secs2) == ["DJ Mark Whitby"], f"{list(secs2)}")
check("drift: empty section dropped", "DJ Ann Unknown" not in secs2)
check("drift: absent DJ reported", d2ma.absent_djs() == ["DJ Ann Unknown"],
      f"{d2ma.absent_djs()}")

# fetch failure resets state cleanly
class DeadFetcher:
    @staticmethod
    def get(url):
        raise RuntimeError("connection refused")


d2ma.Fetcher = DeadFetcher
check("fetch failure returns {}", d2ma.scrape_month("2026-08") == {})
check("fetch failure clears stale absent list", d2ma.absent_djs() == [],
      f"{d2ma.absent_djs()}")

d2ma.Fetcher = orig_get

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

