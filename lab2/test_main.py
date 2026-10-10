"""Offline check of the crawler: fake HTTP + in-memory MongoDB (pip install mongomock). Run: python test_main.py"""
import mongomock
import requests
import yaml

from main import Crawler, normalize

CFG = yaml.safe_load("""
db: {host: x, port: 1, database: t, collection: documents}
logic: {user_agent: test, delay_between_requests: 0, max_documents: 0, refresh_interval_days: 7,
        retry_attempts: 2, timeout_seconds: 1}
source:
  name: Wikinews
  domain: ru.wikinews.org
  seeds: [https://ru.wikinews.org/wiki/A]
  skip_namespaces: [Категория]
""")
BASE = "https://ru.wikinews.org/wiki/"


def page(title, text, links=()):
    url = normalize(BASE + title)
    a = "".join(f'<a href="{h}">x</a>' for h in links)
    return f'<html><head><title>{title}</title><link rel="canonical" href="{url}"></head><body>' \
           f'<div id="mw-content-text"><p>{text}</p>{a}</div></body></html>'


class Resp:
    def __init__(self, code, text="", ctype="text/html"):
        self.status_code, self.text, self.content, self.headers = code, text, text.encode(), {"Content-Type": ctype}
        self.url = ""


class Fake:
    """url -> html | int status | Exception; counts requests per url."""
    def __init__(self, pages):
        self.pages, self.hits, self.on_get = pages, {}, None

    def get(self, url, headers=None, timeout=None):
        if url.endswith("robots.txt"):
            return Resp(404)
        self.hits[url] = self.hits.get(url, 0) + 1
        if self.on_get:
            self.on_get()
        p = self.pages[url]
        if isinstance(p, Exception):
            raise p
        return Resp(p) if isinstance(p, int) else Resp(200, p)


def site():
    return {
        normalize(BASE + "A"): page("A", "alpha", ["/wiki/B", "/wiki/C#frag", "/wiki/%D0%9A%D0%B0%D1%82%D0%B5%D0%B3%D0%BE%D1%80%D0%B8%D1%8F:X",
                                                    "https://example.org/wiki/Z", "/w/index.php?title=A&action=edit", "mailto:a@b.c"]),
        normalize(BASE + "B"): page("B", "beta", ["/wiki/A", "/wiki/Foo:_headline_with_colon"]),
        normalize(BASE + "C"): page("C", "gamma"),
        normalize(BASE + "Foo:_headline_with_colon"): page("Foo:_headline_with_colon", "delta"),
    }


def test_crawl_dedup_and_filters():
    db, fake = mongomock.MongoClient().db, Fake(site())
    Crawler(CFG, db, fake).run()
    assert sorted(d["title"] for d in db.documents.find()) == ["A", "B", "C", "Foo:_headline_with_colon"]
    assert set(fake.hits.values()) == {1}  # every URL fetched once, despite A<->B links
    d = db.documents.find_one({"title": "A"})
    assert d["source"] == "Wikinews" and d["crawl_timestamp"] > 0 and "alpha" in d["html_content"]


def test_redirect_to_skipped_namespace():
    pages = {normalize(BASE + "A"): page("A", "alpha", ["/wiki/R"]),
             normalize(BASE + "R"): page("Категория:X", "cat")}  # R serves a category
    db = mongomock.MongoClient().db
    Crawler(CFG, db, Fake(pages)).run()
    assert [d["title"] for d in db.documents.find()] == ["A"]
    assert db.documents_queue.find_one({"_id": normalize(BASE + "R")})["state"] == "failed"


def test_resume_after_stop():
    db, fake = mongomock.MongoClient().db, Fake(site())
    c = Crawler(CFG, db, fake)
    fake.on_get = lambda: c.stop.set() if len(fake.hits) == 2 else None  # "Ctrl+C" during 2nd page
    c.run()
    assert db.documents.count_documents({}) == 2
    Crawler(CFG, db, fake).run()  # restart: continues from the queue
    assert db.documents.count_documents({}) == 4 and set(fake.hits.values()) == {1}


def test_recrawl_only_changed():
    db, fake = mongomock.MongoClient().db, Fake(site())
    Crawler(CFG, db, fake).run()
    cfg = {**CFG, "logic": {**CFG["logic"], "refresh_interval_days": -1}}  # everything is stale
    fake.pages[normalize(BASE + "C")] = page("C", "gamma CHANGED")
    Crawler(cfg, db, fake).run()
    assert db.documents.count_documents({}) == 4
    assert all(v == 2 for v in fake.hits.values())
    c = db.documents.find_one({"title": "C"})
    assert len(c["history"]) == 1 and "CHANGED" in c["html_content"]
    assert "history" not in db.documents.find_one({"title": "B"})


def test_http_errors():
    pages = site()
    pages[normalize(BASE + "B")] = 404                              # permanent
    pages[normalize(BASE + "C")] = 503                              # transient, gives up after retry_attempts
    pages[normalize(BASE + "A")] = page("A", "alpha", ["/wiki/B", "/wiki/C", "/wiki/D"])
    pages[normalize(BASE + "D")] = requests.ConnectionError("boom")
    db, fake = mongomock.MongoClient().db, Fake(pages)
    Crawler(CFG, db, fake).run()
    states = {d["_id"].rsplit("/", 1)[1]: d["state"] for d in db.documents_queue.find()}
    assert states == {"A": "done", "B": "failed", "C": "error", "D": "error"}
    assert fake.hits[normalize(BASE + "B")] == 1 and fake.hits[normalize(BASE + "C")] == 2
    assert db.documents.count_documents({}) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
