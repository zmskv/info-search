import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path

# Abbreviations written with a trailing dot that are kept as one token ("г." != "г" + ".").
ABBR = ("г гг вв им ул просп пер обл руб коп тыс млн млрд трлн см мм км кг мин сек англ лат греч нем фр рус "
        "ср напр др пр проф акад доц св ст чел экз рис илл табл ред изд пос гор дер ок").split()

PATTERN = re.compile(r"""
    (?P<url>(?:https?://|www\.)[^\s<>"'\])]*[^\s<>"'\]).,;:!?]) |
    (?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+) |
    (?P<initials>[А-ЯЁA-HJ-UW-Z]\.\s?(?:[А-ЯЁA-HJ-UW-Z]\.\s?)?(?=[А-ЯЁA-Z][а-яёa-z])) |
    (?P<abbr>
        (?i:(?:т\.\s?[дпеню]|н\.\s?э|и\.\s?о)\.) |                # т. д., т. е., н. э.
        (?:[^\W\d_]\.){2,} |                                       # dotted: т.д., e.g., и.о.
        (?:(?<=[\dIVXХ][ \xa0])|(?<=[\dIVXХ]))[вг]\. |             # "XX в.", "1990 г." (not the preposition "в")
        (?i:(?:%s)\.)                                             # list above
    ) |
    (?P<number>
        (?<!\d)\d{1,3}(?:[ \xa0\u202f\u2009]\d{3}(?!\d))+(?![\w-]) |   # 10 000
        \d+(?:[.,:/]\d+)*(?!\w|-\w)                                    # 3,14  12.03.2020  10:30
    ) |
    (?P<compound>[^\W_]+(?:[-\u2010\u2011][^\W_]+)+) |                 # северо-запад, Ростов-на-Дону, F-16, 1990-е
    (?P<word>[^\W_]+(?:['\u2019][^\W_]+)*) |                           # words, O'Neil, 3D
    (?P<punct>[^\w\s])
""" % "|".join(ABBR), re.VERBOSE)

DROP = str.maketrans("", "", "\u00ad\u0301") 


def tokenize(text, lower=True, yo=True, punct=False, split_compounds=False):
    """text -> [(kind, token)]; kind is word/compound/number/abbr/initials/url/email (+ punct if punct=True)."""
    out = []
    for m in PATTERN.finditer(text.translate(DROP)):
        kind, tok = m.lastgroup, m.group()
        if kind == "punct" and not punct:
            continue
        if kind in ("abbr", "initials"):
            tok = re.sub(r"\s", "", tok)
        if kind != "url" and kind != "email":
            tok = tok.lower() if lower else tok
            tok = tok.replace("ё", "е").replace("Ё", "Е") if yo else tok
        if kind == "compound" and (split_compounds or re.fullmatch(r"[\d\-\u2010\u2011]+", tok)):
            out += [("number" if p.isdigit() else "word", p) for p in re.split("[-\u2010\u2011]", tok)]
        else:
            out.append((kind, tok))
    return out


def baseline(text):
    """What lab-less tokenization would give: split on anything that is not a letter or digit."""
    return re.findall(r"\w+", text.lower())


def run(corpus, limit):
    kinds, freq = Counter(), Counter()
    n_docs = n_bytes = n_base = 0
    elapsed = 0.0
    for f in sorted(corpus.glob("*.json"))[:limit]:
        text = json.loads(f.read_text(encoding="utf-8"))["text"]
        t0 = time.perf_counter()
        toks = tokenize(text)
        elapsed += time.perf_counter() - t0
        n_base += len(baseline(text))
        kinds.update(k for k, _ in toks)
        freq.update(toks)
        n_docs, n_bytes = n_docs + 1, n_bytes + len(text.encode())
    n = sum(kinds.values())
    print(f"docs={n_docs}  text={n_bytes/1e6:.1f} MB  tokens={n}  unique={len(freq)}  baseline(\\w+)={n_base}")
    print(f"avg token length={sum(len(t)*c for (_, t), c in freq.items())/n:.2f}  time={elapsed:.2f} s  "
          f"speed={n_bytes/1024/elapsed:.0f} KB/s  tokens/s={n/elapsed:.0f}")
    print("kinds:", {k: f"{c} ({c/n:.1%})" for k, c in kinds.most_common()})
    for kind in ("compound", "abbr", "initials", "number", "url", "email"):
        print(f"top {kind}:", [t for (k, t), _ in freq.most_common() if k == kind][:12])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, default=Path(__file__).parent.parent / "lab1/corpus/wikipedia")
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--text", help="tokenize this string and print tokens with kinds instead of running on the corpus")
    a = ap.parse_args()
    if a.text:
        for k, t in tokenize(a.text):
            print(f"{k:9} {t}")
    else:
        run(a.corpus, a.limit)
