import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

API = "https://ru.wikipedia.org/w/api.php"
session = requests.Session()
session.headers["User-Agent"] = "info-search-lab1/1.0 (student corpus; zmskvxd@gmail.com)"
JUNK = "style, sup.reference, table, .mw-editsection, .navbox, .hatnote, .thumb, .reflist, .noprint, ol.references"
MIN_CHARS = 1000  # skip stubs


def api(**params):
    """GET to MediaWiki API with retries; raises after 5 failures."""
    for attempt in range(5):
        try:
            r = session.get(API, params={"format": "json", "formatversion": 2, **params}, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:  # rate limited: obey Retry-After
                time.sleep(int(r.headers.get("Retry-After", 10)))
                continue
        except (requests.RequestException, ValueError):
            pass
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"API failed: {params}")


def random_pages(n=500):
    """Random non-redirect articles from the main namespace: [(pageid, title)]."""
    data = api(action="query", list="random", rnnamespace=0, rnfilterredir="nonredirects", rnlimit=n)
    return [(p["id"], p["title"]) for p in data["query"]["random"]]


def extract_text(html):
    """Article HTML -> plain text of paragraphs and headings (no tables, refs, navboxes)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select(JUNK):
        tag.decompose()
    return "\n".join(t.get_text(" ", strip=True) for t in soup.select("p, h2, h3, li") if t.get_text(strip=True))


def download(page, folder):
    """Fetch one article, save .html and .json; returns True if saved."""
    pageid, title = page
    path = folder / str(pageid)
    if path.with_suffix(".json").exists():
        return False
    try:
        parse = api(action="parse", pageid=pageid, prop="text", disableeditsection=1)["parse"]
    except (RuntimeError, KeyError):  # failed or deleted page: skip, don't kill the crawl
        return False
    html, text = parse["text"], extract_text(parse["text"])
    if len(text) < MIN_CHARS:
        return False
    path.with_suffix(".html").write_text(html, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({
        "url": f"https://ru.wikipedia.org/?curid={pageid}", "source": "wikipedia", "pageid": pageid,
        "title": title, "text": text, "n_chars": len(text), "n_words": len(text.split()),
        "fetched_at": int(time.time()),
    }, ensure_ascii=False), encoding="utf-8")
    return True


def crawl(out, limit, workers):
    folder = out / "wikipedia"
    folder.mkdir(parents=True, exist_ok=True)
    count = len(list(folder.glob("*.json")))
    seen = {int(f.stem) for f in folder.glob("*.json")}
    with ThreadPoolExecutor(workers) as pool:
        while count < limit:
            batch = [p for p in random_pages() if p[0] not in seen][: limit - count + 50]
            seen.update(p[0] for p in batch)
            count += sum(pool.map(lambda p: download(p, folder), batch))
            print(f"total={count}", flush=True)


def stats(out):
    files = list((out / "wikipedia").glob("*.json"))
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    n = max(len(docs), 1)
    raw = sum(f.with_suffix(".html").stat().st_size for f in files)
    text = sum(len(d["text"].encode()) for d in docs)
    words = sum(d["n_words"] for d in docs)
    chars = "".join(d["text"] for d in docs)
    cyr = sum("а" <= c.lower() <= "я" or c in "ёЁ" for c in chars) / max(1, sum(c.isalpha() for c in chars))
    print(f"docs={len(docs)}  raw={raw/1e6:.1f} MB  text={text/1e6:.1f} MB")
    print(f"avg doc={raw/n/1e3:.1f} KB  avg text={text/n/1e3:.1f} KB  avg words={words/n:.0f}  cyrillic={cyr:.1%}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "corpus")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    stats(a.out) if a.stats else crawl(a.out, a.limit, a.workers)
