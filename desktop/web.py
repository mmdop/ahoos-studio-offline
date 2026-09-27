"""The internet, for a model that otherwise has none.

Three ways to reach it, chosen in Settings:

  direct     this computer's own connection -- its VPN, its proxy -- with no key
             and no account. Search is DuckDuckGo's plain-HTML page, read the
             way a browser without scripts reads it, with Bing as the fallback.
  provider   a search API with a key: Tavily, Brave, Exa, Serper or Jina, each
             of which answers in clean JSON and was built for this.
  custom     any other search API, described by its address, its key header
             and where the results sit in its answer.

Reading a page (`fetch`) always uses this computer's connection: a search
provider finds pages, it does not fetch them for you.

WHAT IS REFUSED

An address that resolves to this machine or the local network, unless Settings
allow it. The model reads text from the web, and a page can tell it to fetch
something; without this rule, "fetch http://127.0.0.1:…" would be the app's own
API reading the person's folder back to whoever wrote that page.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
PAGE_BYTES = 3_000_000
PAGE_CHARS = 2500                # what the model reads of one page
TIMEOUT = 20

PROVIDERS = {
    "tavily": {"name": "Tavily", "site": "https://app.tavily.com", "hint": "tvly-…"},
    "brave": {"name": "Brave Search", "site": "https://api-dashboard.search.brave.com", "hint": "BSA…"},
    "exa": {"name": "Exa", "site": "https://dashboard.exa.ai", "hint": ""},
    "serper": {"name": "Serper (Google)", "site": "https://serper.dev", "hint": ""},
    "jina": {"name": "Jina", "site": "https://jina.ai", "hint": "jina_…"},
}

DEFAULT = {
    "mode": "off",               # off | direct | provider | custom
    "provider": "tavily",
    "keys": {},                  # provider id -> key
    "custom": {"name": "", "url": "", "method": "GET", "body": "", "key": "", "key_header": "Authorization",
               "key_prefix": "Bearer ", "results": "results", "title": "title", "link": "url",
               "snippet": "content"},
    "proxy": "",                 # http://host:port, or empty for the system's own
    "allow_local": False,
}


class WebError(RuntimeError):
    """The search or the page did not come back."""


class Web:
    def __init__(self, settings: dict) -> None:
        self.settings = {**DEFAULT, **(settings or {})}
        self.settings["custom"] = {**DEFAULT["custom"], **(self.settings.get("custom") or {})}

    # -- plumbing ---------------------------------------------------------------

    def label(self) -> str:
        mode = self.settings["mode"]
        if mode == "provider":
            return PROVIDERS.get(self.settings["provider"], {}).get("name", "provider")
        if mode == "custom":
            return self.settings["custom"].get("name") or "custom"
        return "direct"

    def _opener(self) -> urllib.request.OpenerDirector:
        proxy = str(self.settings.get("proxy") or "").strip()
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
        return urllib.request.build_opener(*handlers)

    def _request(self, url: str, *, data: bytes | None = None, headers: dict | None = None,
                 limit: int = PAGE_BYTES) -> tuple[bytes, str, str]:
        request = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
        try:
            with self._opener().open(request, timeout=TIMEOUT) as response:
                body = response.read(limit + 1)[:limit]
                return body, response.headers.get("Content-Type", ""), response.geturl()
        except urllib.error.HTTPError as error:
            detail = error.read(300).decode("utf-8", "replace")
            if error.code in (401, 403):
                raise WebError(f"{urllib.parse.urlsplit(url).netloc} refused the request ({error.code}): "
                               "check the key in Settings. " + detail[:120]) from error
            raise WebError(f"{urllib.parse.urlsplit(url).netloc} answered {error.code}: {detail[:160]}") from error
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise WebError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {reason}") from error

    def _json(self, url: str, *, body: dict | None = None, headers: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        sent = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            sent["Content-Type"] = "application/json"
        raw, _, _ = self._request(url, data=data, headers=sent)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as error:
            raise WebError("the search service did not answer in JSON") from error

    def _key(self, provider: str) -> str:
        key = str((self.settings.get("keys") or {}).get(provider) or "").strip()
        if not key:
            raise WebError(f"no API key for {PROVIDERS[provider]['name']} in Settings")
        return key

    # -- search -------------------------------------------------------------------

    def search(self, query: str, count: int = 5) -> list[dict]:
        mode = self.settings["mode"]
        if mode == "off":
            raise WebError("internet access is off in Settings")
        if mode == "provider":
            provider = self.settings["provider"]
            method = getattr(self, f"_search_{provider}", None)
            if method is None:
                raise WebError(f"unknown search provider {provider!r}")
            results = method(query, count)
        elif mode == "custom":
            results = self._search_custom(query, count)
        else:
            results = self._search_direct(query, count)
        clean = []
        for item in results:
            url = str(item.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            clean.append({"title": _text(item.get("title") or url)[:200], "url": url,
                          "snippet": _text(item.get("snippet") or "")[:400]})
        return clean[:count]

    def _search_tavily(self, query: str, count: int) -> list[dict]:
        answer = self._json("https://api.tavily.com/search",
                            body={"query": query, "max_results": count},
                            headers={"Authorization": "Bearer " + self._key("tavily")})
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content")}
                for r in answer.get("results") or []]

    def _search_brave(self, query: str, count: int) -> list[dict]:
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": query, "count": count})
        answer = self._json(url, headers={"X-Subscription-Token": self._key("brave")})
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description")}
                for r in (answer.get("web") or {}).get("results") or []]

    def _search_exa(self, query: str, count: int) -> list[dict]:
        answer = self._json("https://api.exa.ai/search",
                            body={"query": query, "numResults": count,
                                  "contents": {"text": {"maxCharacters": 400}}},
                            headers={"x-api-key": self._key("exa")})
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("text") or r.get("summary")}
                for r in answer.get("results") or []]

    def _search_serper(self, query: str, count: int) -> list[dict]:
        answer = self._json("https://google.serper.dev/search", body={"q": query, "num": count},
                            headers={"X-API-KEY": self._key("serper")})
        return [{"title": r.get("title"), "url": r.get("link"), "snippet": r.get("snippet")}
                for r in answer.get("organic") or []]

    def _search_jina(self, query: str, count: int) -> list[dict]:
        url = "https://s.jina.ai/?" + urllib.parse.urlencode({"q": query})
        answer = self._json(url, headers={"Authorization": "Bearer " + self._key("jina"),
                                          "X-Respond-With": "no-content"})
        return [{"title": r.get("title"), "url": r.get("url"), "snippet": r.get("description") or r.get("content")}
                for r in answer.get("data") or []]

    def _search_custom(self, query: str, count: int) -> list[dict]:
        spec = self.settings["custom"]
        address = str(spec.get("url") or "").strip()
        if not address.startswith(("http://", "https://")):
            raise WebError("the custom search has no address in Settings")
        quoted = urllib.parse.quote(query)
        url = address.replace("{query}", quoted).replace("{count}", str(count))
        headers = {}
        if spec.get("key"):
            headers[str(spec.get("key_header") or "Authorization")] = f"{spec.get('key_prefix') or ''}{spec['key']}"
        body = None
        if str(spec.get("method") or "GET").upper() == "POST":
            template = str(spec.get("body") or '{"query": "{query}"}')
            body = json.loads(template.replace("{query}", json.dumps(query)[1:-1]).replace("{count}", str(count)))
        answer = self._json(url, body=body, headers=headers)
        items = _dig(answer, str(spec.get("results") or ""))
        if not isinstance(items, list):
            raise WebError(f"no list at '{spec.get('results')}' in the custom service's answer")
        return [{"title": _dig(i, spec.get("title") or "title"), "url": _dig(i, spec.get("link") or "url"),
                 "snippet": _dig(i, spec.get("snippet") or "snippet")} for i in items if isinstance(i, dict)]

    def _search_direct(self, query: str, count: int) -> list[dict]:
        errors = []
        for engine in (self._duckduckgo, self._bing):
            try:
                found = engine(query)
                if found:
                    return found[:count]
            except WebError as error:
                errors.append(str(error))
        if errors:
            raise WebError("; ".join(errors))
        return []

    def _duckduckgo(self, query: str) -> list[dict]:
        raw, _, _ = self._request("https://html.duckduckgo.com/html/",
                                  data=urllib.parse.urlencode({"q": query}).encode(),
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
        page = raw.decode("utf-8", "replace")
        links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|div|td)>', page, re.S)
        results = []
        for index, (href, title) in enumerate(links):
            href = html.unescape(href)
            if "duckduckgo.com/y.js" in href:          # an advert
                continue
            if "uddg=" in href:
                href = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [href])[0]
            if href.startswith("//"):
                href = "https:" + href
            results.append({"title": title, "url": href, "snippet": snippets[index] if index < len(snippets) else ""})
        return results

    def _bing(self, query: str) -> list[dict]:
        raw, _, _ = self._request("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query}))
        page = raw.decode("utf-8", "replace")
        results = []
        for block in re.findall(r'<li class="b_algo"(.*?)</li>', page, re.S):
            link = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not link:
                continue
            snippet = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            results.append({"title": link.group(2), "url": html.unescape(link.group(1)),
                            "snippet": snippet.group(1) if snippet else ""})
        return results

    # -- pages ----------------------------------------------------------------------

    def fetch(self, url: str, chars: int = PAGE_CHARS) -> dict:
        if self.settings["mode"] == "off":
            raise WebError("internet access is off in Settings")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise WebError("only http and https addresses can be read")
        if not self.settings.get("allow_local"):
            refuse_local(parts.hostname)
        raw, kind, final = self._request(url, headers={"Accept": "text/html,text/plain;q=0.9,*/*;q=0.5"})
        if not self.settings.get("allow_local"):
            refuse_local(urllib.parse.urlsplit(final).hostname or "")
        charset = re.search(r"charset=([\w-]+)", kind or "")
        text = raw.decode(charset.group(1) if charset else "utf-8", "replace")
        if "html" in (kind or "") or text.lstrip().lower().startswith(("<!doctype", "<html")):
            title, body = html_to_text(text)
        elif "json" in (kind or ""):
            title, body = final, text
        elif (kind or "").startswith("text/") or not kind:
            title, body = final, text
        else:
            raise WebError(f"{parts.hostname} sent {kind.split(';')[0]}, which is not text")
        cut = len(body) > chars
        return {"url": final, "host": urllib.parse.urlsplit(final).hostname or "", "title": title.strip()[:200],
                "text": body[:chars] + ("\n… (the rest of the page was cut)" if cut else "")}

    def test(self) -> dict:
        started = time.monotonic()
        found = self.search("AhoosAI Studio", 3)
        return {"ok": True, "results": len(found), "seconds": round(time.monotonic() - started, 1),
                "via": self.label(), "first": found[0] if found else None}


def refuse_local(host: str) -> None:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError as error:
        raise WebError(f"could not find {host}: {error}") from error
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise WebError(f"{host} is on this computer or its local network; reading it is turned off "
                           "in Settings")


def _dig(value, path: str):
    for part in [p for p in str(path or "").split(".") if p]:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _text(fragment) -> str:
    text = re.sub(r"<[^>]+>", "", str(fragment or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


_DROP = re.compile(r"<(script|style|noscript|svg|template|iframe|head|nav|footer|form)\b.*?</\1\s*>", re.S | re.I)
_BLOCK = re.compile(r"</?(p|div|section|article|br|li|ul|ol|h[1-6]|tr|table|pre|blockquote|header|main)\b[^>]*>",
                    re.I)


def html_to_text(page: str) -> tuple[str, str]:
    """A page's title and its readable text: what a person would read, in order."""
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    title_text = _text(title.group(1)) if title else ""
    main = re.search(r"<(main|article)\b[^>]*>(.*)</\1>", page, re.S | re.I)
    body = main.group(2) if main else page
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = _DROP.sub(" ", body)
    body = re.sub(r"<li\b[^>]*>", "\n- ", body, flags=re.I)
    body = _BLOCK.sub("\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    body = html.unescape(body)
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    return title_text, re.sub(r"\n{3,}", "\n\n", text)
