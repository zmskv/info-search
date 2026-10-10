import argparse
import hashlib
import logging
import signal
import sys
import threading
import time
from collections import Counter
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml
from bs4 import BeautifulSoup
from pymongo import ASCENDING, MongoClient, UpdateOne
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("wikinews")
SAFE = "/:;@!*(),~"  


def normalize(url, base=None):
    """Absolute https URL, no query/fragment, MediaWiki-style path ('_' for spaces, upper-case %XX)."""
    p = urlparse(urljoin(base, url) if base else url)
    path = unquote(p.path).replace(" ", "_")
    return "https://" + p.netloc.lower() + quote(path, safe=SAFE)


def make_session(logic):
    s = requests.Session()
    s.headers["User-Agent"] = logic["user_agent"]
    retry = Retry(total=logic["retry_attempts"], backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], raise_on_status=False)  
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class Crawler:
    def __init__(self, cfg, db, session=None):
        logic, src = cfg["logic"], cfg["source"]
        self.name, self.domain, self.seeds = src["name"], src["domain"], src["seeds"]
        self.skip = {n.replace(" ", "_") for n in src["skip_namespaces"]}  
        self.ua, self.delay, self.timeout = logic["user_agent"], logic["delay_between_requests"], logic["timeout_seconds"]
        self.limit, self.max_tries, self.refresh_days = logic["max_documents"], logic["retry_attempts"], logic["refresh_interval_days"]
        self.session = session or make_session(logic)
        self.docs = db[cfg["db"]["collection"]]
        self.queue = db[cfg["db"]["collection"] + "_queue"]
        self.docs.create_index("url", unique=True)
        self.queue.create_index([("state", ASCENDING), ("tries", ASCENDING), ("added", ASCENDING)])
        self.stop = threading.Event()
        self.stats = Counter()
        self.robots = self.load_robots()

    def load_robots(self):
        rp = RobotFileParser()
        try:
            r = self.session.get(f"https://{self.domain}/robots.txt", timeout=self.timeout)
            ok = r.status_code == 200
        except requests.RequestException:
            ok = False
        if not ok:  
            log.warning("robots.txt unavailable, crawling without it")
            rp.allow_all = True
            return rp
        rp.parse(r.content.decode("utf-8-sig").splitlines())
        return rp

    def wanted(self, url):
        """Same domain, /wiki/ article path, not in a service namespace, allowed by robots.txt."""
        p = urlparse(url)
        if p.netloc != self.domain or not p.path.startswith("/wiki/"):
            return False
        ns, colon, _ = unquote(p.path[len("/wiki/"):]).partition(":")
        return not (colon and ns in self.skip) and self.robots.can_fetch(self.ua, url)

    def enqueue(self, urls):
        now = time.time()
        ops = [UpdateOne({"_id": u}, {"$setOnInsert": {"state": "new", "tries": 0, "refresh": False, "added": now}},
                         upsert=True) for u in set(urls) if self.wanted(u)]
        if ops:
            self.queue.bulk_write(ops, ordered=False)

    def requeue(self):
        """On start: re-check documents older than refresh_interval_days, retry URLs that errored last time."""
        cutoff = int(time.time() - self.refresh_days * 86400)
        self.queue.update_many({"state": "done", "done_at": {"$lt": cutoff}},
                               {"$set": {"state": "new", "tries": 0, "refresh": True}})
        self.queue.update_many({"state": "error"}, {"$set": {"state": "new", "tries": 0}})

    def next_item(self):
        flt = {"state": "new"}
        if self.limit and self.docs.estimated_document_count() >= self.limit:
            flt["refresh"] = True  # corpus is full: only refresh what we already have
        return self.queue.find_one(flt, sort=[("tries", ASCENDING), ("added", ASCENDING)])

    def crawl_one(self, url):
        """Fetch and store one page; returns the queue outcome: done / failed (permanent) / error (retry)."""
        old = self.docs.find_one({"url": url}, {"html_content": 0})
        headers = {"If-Modified-Since": old["last_modified"]} if old and old.get("last_modified") else {}
        try:
            r = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("error %s: %s", url, e)
            return "error"
        if r.status_code == 304:
            self.docs.update_one({"url": url}, {"$set": {"crawl_timestamp": int(time.time())}})
            self.stats["unchanged"] += 1
            return "done"
        if r.status_code == 429 or r.status_code >= 500:  
            log.warning("HTTP %s %s", r.status_code, url)
            return "error"
        if r.status_code != 200 or "text/html" not in r.headers.get("Content-Type", ""):
            log.warning("skip HTTP %s %s", r.status_code, url)
            return "failed"

        soup = BeautifulSoup(r.text, "html.parser")
        body = soup.select_one("#mw-content-text") or soup
        text = body.get_text(" ", strip=True) 
        canon = soup.select_one("link[rel=canonical]")
        doc_url = normalize(canon["href"]) if canon else normalize(r.url)
        if doc_url != url:  
            if not self.wanted(doc_url):  
                return "failed"
            old = self.docs.find_one({"url": doc_url}, {"html_content": 0})
            self.queue.update_one({"_id": doc_url}, {"$setOnInsert": {"added": time.time(), "tries": 0, "refresh": False},
                                                     "$set": {"state": "done", "done_at": int(time.time())}}, upsert=True)
        h = hashlib.md5(text.encode()).hexdigest()
        now = int(time.time())
        if old and old["content_hash"] == h:
            self.docs.update_one({"url": doc_url}, {"$set": {"crawl_timestamp": now, "last_modified": r.headers.get("Last-Modified", "")}})
            self.stats["unchanged"] += 1
        else:
            update = {"$set": {
                "url": doc_url, "html_content": r.text, "source": self.name, "crawl_timestamp": now,
                "content_hash": h, "title": soup.title.get_text(strip=True) if soup.title else "",
                "content_length": len(r.content), "text_length": len(text), "status_code": 200,
                "last_modified": r.headers.get("Last-Modified", ""),
            }}
            if old: 
                update["$push"] = {"history": {"content_hash": old["content_hash"], "crawl_timestamp": old["crawl_timestamp"]}}
            self.docs.update_one({"url": doc_url}, update, upsert=True)
            self.stats["updated" if old else "new"] += 1
            log.info("%s %s", "updated" if old else "new", doc_url)
        self.enqueue(normalize(a["href"], url) for a in body.select("a[href]"))
        return "done"

    def finish(self, item, outcome):
        if outcome == "error":
            tries = item["tries"] + 1
            self.queue.update_one({"_id": item["_id"]}, {"$set": {"tries": tries, "state": "error" if tries >= self.max_tries else "new"}})
        else:
            self.queue.update_one({"_id": item["_id"]}, {"$set": {"state": outcome, "refresh": False, "done_at": int(time.time())}})
        self.stats[outcome] += 1

    def run(self):
        self.requeue()
        self.enqueue(normalize(s) for s in self.seeds)
        n = 0
        while not self.stop.is_set():
            item = self.next_item()
            if not item:
                break
            self.finish(item, self.crawl_one(item["_id"]))
            n += 1
            if n % 50 == 0:
                log.info("progress: %s, queue=%d", dict(self.stats), self.queue.count_documents({"state": "new"}))
            self.stop.wait(self.delay)  # returns at once if Ctrl+C was pressed
        log.info("%s: %s", "stopped" if self.stop.is_set() else "queue is empty", dict(self.stats))


def stats(db, cfg):
    docs = db[cfg["db"]["collection"]]
    queue = db[cfg["db"]["collection"] + "_queue"]
    t = next(docs.aggregate([{"$group": {"_id": None, "n": {"$sum": 1}, "raw": {"$sum": "$content_length"},
                                         "text": {"$sum": "$text_length"}}}]), {"n": 0, "raw": 0, "text": 0})
    n = max(t["n"], 1)
    print(f"docs={t['n']}  raw={t['raw']/1e6:.1f} MB  text={t['text']/1e6:.1f} MB")
    print(f"avg doc={t['raw']/n/1e3:.1f} KB  avg text={t['text']/n/1e3:.1f} KB")
    print("queue:", {d["_id"]: d["n"] for d in queue.aggregate([{"$group": {"_id": "$state", "n": {"$sum": 1}}}])})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--stats", action="store_true", help="print corpus statistics and exit")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    lg = cfg.get("logging", {})
    handlers = [logging.StreamHandler()] + ([logging.FileHandler(lg["file"], encoding="utf-8")] if lg.get("file") else [])
    logging.basicConfig(level=lg.get("level", "INFO"), format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    d = cfg["db"]
    db = MongoClient(d["host"], d["port"], serverSelectionTimeoutMS=5000)[d["database"]]
    if a.stats:
        stats(db, cfg)
        sys.exit()
    crawler = Crawler(cfg, db)

    def on_signal(sig, frame):  
        log.info("stopping after the current page (signal again to kill)")
        crawler.stop.set()
        signal.signal(sig, signal.SIG_DFL)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, on_signal)
    crawler.run()
