"""Deep search: the studio's research plugin -- several searches, several engines, the best pages
read, the passages that answer the question kept, and the gaps searched again.

The model takes the decisions (which searches to make, whether what was found is enough) and
writes the answer; this module does everything else, without a model: it asks several search
engines at once, merges their lists (reciprocal rank fusion, at most two pages of a site), reads
the pages, cuts them into passages and ranks the passages against the question (BM25, in Persian
and English alike). The engine (projects/nimbus-2-apex-live/modal/engine.py, deep_answer) runs
the steps and streams each one to the studio as it goes.

The words the model reads at each step are here too (PLAN, REVIEW, ANSWER): what a deep search
is, and how its answer is written.
"""

from __future__ import annotations

import concurrent.futures
import math
import re
import urllib.parse

try:
    from web import UA, Web, WebError, _text     # in the engine's container: /root/web.py
except ImportError:
    from desktop.web import UA, Web, WebError, _text

# How deep a search goes: rounds of searching, pages read in a round, searches in the plan.
DEPTHS = {
    1: {"rounds": 1, "pages": 5, "queries": 3},     # quick
    2: {"rounds": 2, "pages": 6, "queries": 4},     # standard
    3: {"rounds": 3, "pages": 8, "queries": 6},     # deep
}
PAGE_CHARS = 24000          # of one page, before it is cut into passages
PASSAGE = 900               # characters a passage
PER_PAGE = 3                # passages kept from one page
EVIDENCE_CHARS = 26000      # of passages the answer reads (about 8,000 tokens)
REVIEW_CHARS = 12000        # of passages the check reads

PLAN = """# Deep search -- step 1: the searches
The user turned on deep search: before answering, you research the question on the web. Write the searches that,
together, find everything a complete, correct and current answer needs.
- Write {n} searches at most (at least 2). Each is a short query a search engine understands -- keywords, names,
  numbers, dates -- not a sentence to a person.
- Cover the question's different sides: the main fact; the latest numbers or news; official or primary sources;
  comparisons, prices or criticism when the question asks for them.
- International topics: search in English. Iranian topics, or when Persian sources matter: also in Persian.
- Anything that changes (prices, versions, events, people's roles) gets the current year in the query.
- Never put a version number, price, score or date you remember into a search: what you remember is out of
  date. Search for the newest instead ("latest", "newest", "today", the current year).
Answer only with the JSON object."""

REVIEW = """# Deep search -- step 2: is it enough?
Below are passages from the pages read so far, each with its source number. Decide whether they answer the user's
question completely and reliably.
- "enough": true when every part of the question is answered by a reliable passage, and anything recent is confirmed.
- Otherwise say in "missing" what is still unknown (one sentence), and give 1 to 3 new searches in "queries" that
  would find it -- different from the searches already made: {done}
Answer only with the JSON object."""

ANSWER = """# Deep search -- step 3: the answer
You researched this question on the web. What you found is below: numbered sources, each with passages from its page.
Write the answer from them.
- Answer the question directly first, then the details. Write in the user's language.
- Every fact taken from a source carries its number in brackets right after it -- [2], or [1][3]. Use only the
  numbers below. Never invent a source, a number, a date, a name or a quote.
- When sources disagree, say so, and say which is more reliable or more recent, and why.
- For anything that changes, prefer official and primary sources and the most recent date; give dates when the
  sources do.
- What the sources do not answer, say plainly. Fill a gap from your own knowledge only when you say that you are.
- Use headings, lists and tables where they make the answer clearer.
- End with a section titled "Sources" (in Persian, «منابع») listing each source you cited: [n] title -- address."""

PLAN_SCHEMA = {"type": "object", "properties": {
    "queries": {"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 120}, "minItems": 1, "maxItems": 6}},
    "required": ["queries"]}
REVIEW_SCHEMA = {"type": "object", "properties": {
    "enough": {"type": "boolean"},
    "missing": {"type": "string", "maxLength": 300},
    "queries": {"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 120}, "maxItems": 3}},
    "required": ["enough", "missing", "queries"]}


# -- search: several engines at once ------------------------------------------------------------

def _wikipedia(web: Web, query: str, lang: str) -> list[dict]:
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": query, "srlimit": 3, "format": "json", "utf8": 1})
    answer = web._json(url, headers={"User-Agent": UA + " AhoosAI-deep-search"})
    return [{"title": r.get("title"), "url": f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(str(r.get("title", "")).replace(" ", "_")),
             "snippet": r.get("snippet", "")} for r in (answer.get("query") or {}).get("search") or []]


def html_unescape(text: str) -> str:
    import html
    return html.unescape(text)


PERSIAN = re.compile(r"[؀-ۿ]")


def engines_for(query: str) -> list[tuple[str, callable]]:
    """The engines a query goes to: the two big indexes and Wikipedia in the query's language.
    (Mojeek, tried as a third index, answers a script with a captcha.)"""
    lang = "fa" if PERSIAN.search(query) else "en"
    return [("duckduckgo", lambda w, q: w._duckduckgo(q)), ("bing", lambda w, q: w._bing(q)),
            ("wikipedia", lambda w, q: _wikipedia(w, q, lang))]


def search_one(query: str, web: Web | None = None) -> dict:
    """One query on every engine at once: {"query", "lists": {engine: results}, "errors": {...}}."""
    web = web or Web({"mode": "direct"})
    lists, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fn, web, query): name for name, fn in engines_for(query)}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                found = []
                for item in future.result() or []:
                    url = str(item.get("url") or "").strip()
                    if url.startswith(("http://", "https://")):
                        found.append({"title": _text(item.get("title") or url)[:200], "url": url,
                                      "snippet": _text(item.get("snippet") or "")[:400]})
                lists[name] = found[:10]
            except Exception as error:  # noqa: BLE001 - one engine down is not the search down
                errors[name] = str(error)[:160]
    return {"query": query, "lists": lists, "errors": errors}


def normal(url: str) -> str:
    """A page's address without what does not change the page: scheme, www, fragment, trackers."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    query = "&".join(p for p in parts.query.split("&") if p and not re.match(r"(utm_|fbclid|gclid|ref=|src=)", p))
    return f"{host}{parts.path.rstrip('/')}" + (f"?{query}" if query else "")


def fuse(searches: list[dict], skip: set[str] = frozenset(), per_site: int = 2,
         question: list[str] | None = None) -> list[dict]:
    """Every engine's list of every query, as one ranking (reciprocal rank fusion: a page high in
    many lists comes first), one entry a page, at most `per_site` pages of a site; pages in `skip`
    (normal addresses) are left out. With the `question`'s words, a page whose title and snippet
    share none of them falls back: an engine's top result for a loose query is often off the subject."""
    score, first = {}, {}
    for search in searches:
        for engine, results in search.get("lists", {}).items():
            weight = 0.6 if engine == "wikipedia" else 1.0
            for rank, result in enumerate(results):
                key = normal(result["url"])
                if not key or key in skip:
                    continue
                score[key] = score.get(key, 0.0) + weight / (60 + rank)
                first.setdefault(key, {**result, "engines": set()})
                first[key]["engines"].add(engine)
    terms = set(question or [])
    if terms:
        for key in score:
            said = set(words(first[key]["title"] + " " + first[key]["snippet"] + " " + key))
            score[key] *= 0.3 + len(terms & said) / len(terms)
    ranked, sites = [], {}
    for key in sorted(score, key=score.get, reverse=True):
        site = key.split("/", 1)[0]
        if sites.get(site, 0) >= per_site:
            continue
        sites[site] = sites.get(site, 0) + 1
        item = first[key]
        ranked.append({"title": item["title"], "url": item["url"], "snippet": item["snippet"], "key": key,
                       "engines": sorted(item["engines"]), "score": round(score[key] * 1000, 2)})
    return ranked


# -- reading: pages into passages, ranked against the question -------------------------------------

STOP = set("""a an the and or of to in on for with by is are was were be been it its this that these those as at from
what which who whom how why when where do does did can could should would will about into than then there their
your you i we they he she not no yes if but so such more most very also just only any all some each other
و در به از که این آن با را برای تا یا هم اما اگر چه چی چرا کجا کی چگونه است هست بود شد شده می نمی ها های
یک دو بر پس پیش هر همه نه بله باید شود کند کرد کنید کنم دارد دارند خود ما من تو او شما ایشان""".split())


def words(text: str) -> list[str]:
    """Search words of a text, Persian or English: lower case, Arabic letters as Persian ones,
    short and common words left out."""
    text = text.lower().replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    return [w for w in re.findall(r"[\w؀-ۿ]+", text) if len(w) > 1 and w not in STOP and not w.isdigit() or
            (w.isdigit() and len(w) >= 2)]


def passages(text: str, size: int = PASSAGE) -> list[str]:
    """A page's text in passages of about `size` characters, cut between paragraphs where it can."""
    out, current = [], ""
    for block in re.split(r"\n{1,}", text):
        block = block.strip()
        if not block:
            continue
        while len(block) > size:
            cut = block.rfind(". ", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            if current:
                out.append(current)
                current = ""
            out.append(block[:cut].strip())
            block = block[cut:].strip()
        if len(current) + len(block) + 1 > size and current:
            out.append(current)
            current = block
        else:
            current = (current + "\n" + block).strip()
    if current:
        out.append(current)
    return [p for p in out if len(p) > 80]


def bm25(items: list[str], query: list[str], k1: float = 1.4, b: float = 0.75) -> list[float]:
    """Okapi BM25 of each passage against the query words."""
    docs = [words(p) for p in items]
    if not docs:
        return []
    avg = sum(len(d) for d in docs) / len(docs) or 1.0
    terms = set(query)
    df = {t: sum(1 for d in docs if t in d) for t in terms}
    n = len(docs)
    scores = []
    for d in docs:
        tf = {}
        for w in d:
            if w in terms:
                tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for t, f in tf.items():
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * len(d) / avg))
        scores.append(s)
    return scores


def read_pages(pages: list[dict], web: Web | None = None, deadline: float = 25.0) -> list[dict]:
    """The pages read at once: each {"url", "title", "text"} or {"url", "error"}. A page still
    coming after `deadline` seconds is left (a slow site held a whole round for two minutes)."""
    web = web or Web({"mode": "direct"})

    def one(page):
        try:
            got = web.fetch(page["url"], chars=PAGE_CHARS)
            return {"url": got["url"], "title": got["title"] or page.get("title") or got["url"], "text": got["text"]}
        except (WebError, ValueError, OSError) as error:
            return {"url": page["url"], "title": page.get("title") or page["url"], "error": str(error)[:200]}

    if not pages:
        return []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(pages)))
    futures = [pool.submit(one, page) for page in pages]
    concurrent.futures.wait(futures, timeout=deadline)
    pool.shutdown(wait=False, cancel_futures=True)
    return [f.result() if f.done() else {"url": page["url"], "title": page.get("title") or page["url"],
                                         "error": f"no answer in {deadline:.0f} s"}
            for f, page in zip(futures, pages)]


def best_passages(page: dict, question: list[str], snippet: str = "", keep: int = PER_PAGE) -> list[tuple[float, str]]:
    """The page's passages that answer the question best, in the page's order, with their scores.
    A page that could not be read gives its search snippet."""
    if page.get("error") or not page.get("text"):
        return [(0.1, snippet)] if snippet else []
    items = passages(page["text"])
    if not items:
        return []
    scores = bm25(items, question)
    top = sorted(range(len(items)), key=lambda i: scores[i], reverse=True)[:keep]
    return [(scores[i], items[i]) for i in sorted(top) if scores[i] > 0] or [(0.05, items[0])]


class Evidence:
    """The sources found so far, numbered in the order they were first read."""

    def __init__(self) -> None:
        self.sources: list[dict] = []          # {"n", "title", "url", "passages": [(score, text)]}
        self.read: set[str] = set()            # normal addresses already read (or tried)

    def add(self, page: dict, kept: list[tuple[float, str]]) -> dict | None:
        self.read.add(normal(page["url"]))
        if not kept:
            return None
        source = {"n": len(self.sources) + 1, "title": page.get("title") or page["url"], "url": page["url"],
                  "passages": kept, "best": max(s for s, _ in kept)}
        self.sources.append(source)
        return source

    def text(self, limit: int) -> str:
        """The sources and their passages, best first within the limit, in number order."""
        chosen, used = [], 0
        ranked = sorted(((s, p, src) for src in self.sources for s, p in src["passages"]), key=lambda x: x[0], reverse=True)
        for score, passage, source in ranked:
            if used + len(passage) > limit:
                continue
            chosen.append((source["n"], passage))
            used += len(passage)
        out = []
        for source in self.sources:
            mine = [p for n, p in chosen if n == source["n"]]
            if mine:
                out.append(f"[{source['n']}] {source['title']}\n{source['url']}\n" + "\n…\n".join(mine))
        return "\n\n".join(out)

    def listing(self) -> list[dict]:
        return [{"n": s["n"], "title": s["title"], "url": s["url"]} for s in self.sources]
