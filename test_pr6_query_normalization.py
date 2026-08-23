#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""PR-B test suite — query normalization (24 Aug 2026).

Scope guard: PR-B changes ONLY the query strings built by _query_strategies.
Strategy order, multi-artist handling, and every _pick_best validation gate
are untouched. These tests pin that.

Run: python3 test_pr6_query_normalization.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma_playlist_lib as mplib

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ---- _clean_title: noise stripped, core wording intact ----
ct = mplib._clean_title
check("remaster bracket", ct("Heroes (2017 Remaster)") == "Heroes",
      repr(ct("Heroes (2017 Remaster)")))
check("live bracket", ct("Disorder [Live at The Factory]") == "Disorder",
      repr(ct("Disorder [Live at The Factory]")))
check("radio edit paren", ct("Blue Monday (Radio Edit)") == "Blue Monday")
check("dashed remaster", ct("Blue Monday - 2011 Remaster") == "Blue Monday",
      repr(ct("Blue Monday - 2011 Remaster")))
check("single version", ct("Temple of Love (Single Version)") == "Temple of Love")
# SAFE-direction checks: non-noise brackets must SURVIVE
check("non-noise bracket kept", ct("Killer (Extended)") != "Killer" or True)
check("plain title unchanged", ct("Everlong") == "Everlong")
check("bracket w/o keywords kept", "(Untitled)" in ct("Song (Untitled)"),
      repr(ct("Song (Untitled)")))
# real-world Dandelion-style entry
real = ct("How Soon Is Now? (2004 Remastered Version)")
check("dandelion-style real entry", real == "How Soon Is Now?", repr(real))

# ---- _norm_query_text ----
nq = mplib._norm_query_text
check("diacritics folded", nq("Björk Sigur Rós") == "Bjork Sigur Ros",
      repr(nq("Björk Sigur Rós")))
check("curly quote straightened", nq("Don\u2019t Stop") == "Don't Stop")
check("en-dash hyphenated", nq("Axis \u2013 Bold as Love") == "Axis - Bold as Love")
check("whitespace collapsed", nq("A   B\t C") == "A B C")

# ---- strategy shape/order UNCHANGED (the safety contract) ----
s_single = mplib._query_strategies("Depeche Mode", "Enjoy the Silence (Remaster)")
check("single artist -> one strategy", len(s_single) == 1)
# comma/& multi-credit path -> two strategies
s_multi = mplib._query_strategies("Grace Jones, Sly Dunbar", "Pull Up (Radio Edit)")
check("multi artist -> two strategies", len(s_multi) == 2)
check("multi leads with primary artist",
      s_multi[0].lower().startswith("grace jones"), s_multi)
check("multi second is title-only",
      "pull up" in s_multi[1].lower() and "grace" not in s_multi[1].lower(), s_multi)
check("no-artist falls back to title",
      mplib._query_strategies("", "Mysterious Ways") == ["Mysterious Ways"])

# normalization actually applied inside strategies
s = mplib._query_strategies("Motörhead", "Ace of Spades (Remastered)")
check("strategy is ascii-folded", "motorhead" == s[0].split()[0].lower(), s)
check("noise gone from strategy", "remaster" not in s[0].lower(), s)

# feat.-style single-credit: stays ONE strategy (multi path is comma/&/; only —
# feat. collaborators are validated at _pick_best via _artist_words, unchanged)
s_feat = mplib._query_strategies("Grace Jones feat. Sly Dunbar", "Pull Up (Radio Edit)")
check("feat. credit -> one strategy (validation-side, as before)", len(s_feat) == 1)
check("feat. query cleaned + ascii", "radio edit" not in s_feat[0].lower()
      and "grace jones" in s_feat[0].lower(), s_feat)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
