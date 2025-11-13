# noticias_harvester.py
# -*- coding: utf-8 -*-
import os, json, time, re, sys, unicodedata, smtplib, ssl, random
from datetime import datetime
from urllib.parse import urljoin
import requests
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError
from bs4 import BeautifulSoup
import trafilatura
import extruct
import yaml
from email.message import EmailMessage
from w3lib.html import get_base_url
from dateutil import tz, parser as dateparser

CONFIG_FILE = "config.yaml"

# ========= CONFIG =========
def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}

CFG = load_config()

# ========= FUENTES =========
SOURCES_RAW = CFG.get("sources", [])
SOURCES = []
for s in SOURCES_RAW:
    url = s.get("url")
    if not url:
        continue
    SOURCES.append({
        "name": s.get("name", "SIN_NOMBRE"),
        "listing": s.get("listing", url),           # permite RSS/feed si se define
        "homepage": url,
        "domain_prefix": s.get("domain_prefix", url),
        "max_to_fetch": s.get("max_to_fetch", 400),
    })

# ========= RED =========
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
    "Connection": "keep-alive",
}
TIMEOUT = 20
SLEEP_BETWEEN = 0.8

SESSION = requests.Session()
RETRIES = Retry(
    total=4,
    backoff_factor=0.6,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
    raise_on_status=False,
)
SESSION.mount("https://", HTTPAdapter(max_retries=RETRIES))
SESSION.mount("http://", HTTPAdapter(max_retries=RETRIES))

def log(m):
    print(m, flush=True)

def http_get(url: str, timeout: int = TIMEOUT) -> requests.Response:
    time.sleep(0.25 + random.random() * 0.5)  # jitter
    r = SESSION.get(url, headers=DEFAULT_HEADERS, timeout=timeout, allow_redirects=True)
    if r.status_code == 403:
        raise HTTPError(f"403 Forbidden for {url}", response=r)
    r.raise_for_status()
    return r

# ========= EMAIL (Gmail SSL 465) =========
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = "anartz.azumendi@brainandcode.tech"
SMTP_PASS = os.getenv("SMTP_PASS")
TO_EMAILS = CFG.get("to_emails", ["anartz2001@gmail.com"])

# ========= UTILIDADES =========
def norm(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()

def extract_urls_regex(html, base, domain_prefix):
    urls = set()
    for href in re.findall(r'href="([^"]+?\.html)"', html):
        url = urljoin(base, href)
        if url.startswith(domain_prefix) and not any(x in url for x in ["/album/", "/video/", "/fotogaleria/"]):
            urls.add(url)
    return list(urls)

def parse_listing_document(url, domain_prefix, max_to_fetch, debug_name):
    """
    1) RSS/Atom si hay <rss> o <feed>.
    2) HTML con selectores comunes.
    3) Fallback por regex.
    """
    try:
        res = http_get(url)
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            log(f"[SKIP] {debug_name}: 403 en {url}. Se ignora la fuente.")
            return []
        raise
    html = res.text
    items = []
    soup = BeautifulSoup(html, "lxml")

    # 1) RSS/Atom
    if soup.find("rss") or soup.find("feed"):
        for it in soup.select("item"):
            link = it.find("link")
            title = it.find("title")
            pub = it.find("pubdate") or it.find("dc:date") or it.find("published")
            u = (link.text or link.get_text(strip=True)) if link else ""
            if not u:
                continue
            if not u.startswith("http"):
                u = urljoin(url, u)
            items.append({
                "url": u,
                "title": title.get_text(strip=True) if title else "",
                "time_hint": pub.get_text(strip=True) if pub else "",
            })
            if len(items) >= max_to_fetch:
                break
        if not items:
            for e in soup.select("entry"):
                link = e.find("link")
                href = link.get("href") if link else ""
                title = e.find("title")
                updated = e.find("updated") or e.find("published")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = urljoin(url, href)
                items.append({
                    "url": href,
                    "title": title.get_text(strip=True) if title else "",
                    "time_hint": updated.get_text(strip=True) if updated else "",
                })
                if len(items) >= max_to_fetch:
                    break

    # 2) HTML
    if not items:
        candidates = (
            soup.select("article a[href$='.html']") or
            soup.select("h2 a[href$='.html'], h3 a[href$='.html']")
        )
        for a in candidates:
            href = a.get("href")
            if not href:
                continue
            url_abs = urljoin(url, href)
            if not url_abs.startswith(domain_prefix):
                continue
            title = a.get_text(strip=True)
            parent = a.find_parent(["article", "li", "div"])
            time_el = parent.select_one("time, .ue-c-article__published-date, .mod-date") if parent else None
            time_hint = time_el.get_text(strip=True) if time_el else ""
            items.append({"url": url_abs, "title": title, "time_hint": time_hint})
            if len(items) >= max_to_fetch:
                break

    # 3) Fallback regex
    if len(items) < 5:
        for u in extract_urls_regex(res.text, url, domain_prefix):
            items.append({"url": u, "title": "", "time_hint": ""})
            if len(items) >= max_to_fetch:
                break

    # dedup
    seen, out = set(), []
    for it in items:
        u = it["url"]
        if u in seen:
            continue
        seen.add(u)
        out.append(it)
        if len(out) >= max_to_fetch:
            break
    return out

def parse_all_listings():
    all_items = []
    for src in SOURCES:
        name = src["name"]
        log(f"— Fuente: {name}")
        try:
            items = parse_listing_document(src["listing"], src["domain_prefix"], src["max_to_fetch"], f"{name.lower()}_listing")
            if len(items) == 0 and src["homepage"] != src["listing"]:
                log(f"Aviso: 0 enlaces en {name} listing. Probando portada…")
                items = parse_listing_document(src["homepage"], src["domain_prefix"], src["max_to_fetch"], f"{name.lower()}_home")
        except Exception as e:
            log(f"[ERROR] {name}: {e}")
            items = []
        log(f"{name}: enlaces encontrados = {len(items)}")
        for it in items:
            it["source"] = name
        all_items.extend(items)
    # dedup global
    dedup, out = set(), []
    for it in all_items:
        if it["url"] in dedup:
            continue
        dedup.add(it["url"])
        out.append(it)
    log(f"Total combinado (sin duplicados): {len(out)}")
    return out

def extract_jsonld(html_text, url):
    try:
        data = extruct.extract(html_text, base_url=get_base_url(html_text, url), syntaxes=['json-ld'])
        jsonld = data.get('json-ld', []) if data else []
        for block in jsonld:
            t = block.get("@type")
            if t == "NewsArticle" or (isinstance(t, list) and "NewsArticle" in t):
                return block
    except Exception:
        return None
    return None

def normalize_datetime(dt_str, tzname="Europe/Madrid"):
    if not dt_str:
        return None
    try:
        dt = dateparser.parse(dt_str)
        if not dt:
            return None
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=tz.UTC)
        target = tz.gettz(tzname)
        return dt.astimezone(target)
    except Exception:
        return None

def extract_published_from_html(soup, tzname="Europe/Madrid"):
    # Metas comunes y <time>
    meta_selectors = [
        ('meta', {'property': 'article:published_time'}),
        ('meta', {'name': 'date'}),
        ('meta', {'itemprop': 'datePublished'}),
        ('meta', {'name': 'pubdate'}),
        ('meta', {'property': 'og:updated_time'}),
        ('time', {}),
    ]
    for sel in meta_selectors:
        tag = soup.find(*sel)
        if not tag:
            continue
        content = tag.get("content") or tag.get("datetime") or tag.get_text(strip=True)
        dt = normalize_datetime(content, tzname)
        if dt:
            return dt
    return None

def extract_article(url, tzname="Europe/Madrid"):
    try:
        res = http_get(url)
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            raise RuntimeError(f"403 al abrir artículo: {url}")
        raise
    html = res.text
    meta = extract_jsonld(html, url) or {}

    published = normalize_datetime(meta.get("datePublished") or meta.get("dateModified"), tzname)
    headline = meta.get("headline")
    article_body = meta.get("articleBody")

    # Autor
    author = ""
    auth = meta.get("author")
    def pick_name(x):
        if isinstance(x, dict):
            return x.get("name") or x.get("@id") or ""
        if isinstance(x, str):
            return x
        return ""
    if isinstance(auth, list):
        names = [pick_name(a) for a in auth if pick_name(a)]
        author = ", ".join(names)
    else:
        author = pick_name(auth)

    soup = BeautifulSoup(html, "lxml")

    if not author:
        for sel in [
            ('meta', {"name":"author"}),
            ('meta', {"property":"article:author"}),
            ('meta', {"name":"byl"}),
            ('meta', {"name":"dc.creator"}),
            ('meta', {"name":"parsely-author"}),
        ]:
            tag = soup.find(*sel)
            if tag and tag.get("content"):
                author = tag["content"].strip()
                break
        if not author:
            cand = soup.select_one('[itemprop="author"] [itemprop="name"], [rel="author"], .author, .byline, .by-author')
            if cand:
                author = cand.get_text(strip=True)

    if not article_body:
        article_body = trafilatura.extract(html, url=url, include_comments=False, include_tables=False) or ""
        article_body = article_body.strip()

    if not headline:
        h = soup.select_one("h1") or soup.select_one("header h1")
        headline = h.get_text(strip=True) if h else ""

    # fecha desde HTML si falta
    if not published:
        published_dt = extract_published_from_html(soup, tzname)
        if published_dt:
            published = published_dt

    return {
        "url": url,
        "title": headline or "",
        "author": author or "",
        "published": published.isoformat() if isinstance(published, datetime) else (published if published else None),
        "content": article_body or ""
    }

def is_recent(dt_iso, tzname="Europe/Madrid", hours=None):
    hours = hours or CFG.get("hours_recent", 24)
    if not dt_iso:
        return False
    try:
        target = tz.gettz(tzname)
        now = datetime.now(target)
        dt = dateparser.parse(dt_iso).astimezone(target)
        return (now - dt).total_seconds() <= hours * 3600
    except Exception:
        return False

def build_html_multi(arts, tzname="Europe/Madrid"):
    target = tz.gettz(tzname)
    now = datetime.now(target).strftime("%Y-%m-%d %H:%M")
    blocks = []
    for a in arts:
        p = a.get("published")
        p_h = dateparser.parse(p).strftime("%Y-%m-%d %H:%M") if p else "Sin fecha"
        author_html = f'<div style="font-size:12px;color:#555;">{a.get("author")}</div>' if a.get("author") else ""
        content_html = a.get("content","")
        blocks.append(f"""
        <article style="margin-bottom:24px;">
          <div style="font-size:12px;color:#999">{a.get('source','')}</div>
          <h3 style="margin:2px 0 2px 0;">{a['title']}</h3>
          {author_html}
          <div style="font-size:12px;color:#666;">{p_h} — <a href="{a['url']}">{a['url']}</a></div>
          <p style="white-space:pre-wrap; line-height:1.45; margin-top:10px;">
            {content_html}
          </p>
        </article>""")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Noticias ( {now} )</title></head>
<body style="font-family:system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; max-width:800px; margin:24px auto; padding:0 16px;">
<h1 style="margin-bottom:8px;">Noticias de hoy</h1>
<div style="color:#666; font-size:12px; margin-bottom:16px;">Generado {now} ({tzname})</div>
{''.join(blocks) if blocks else '<p>No hay artículos en el rango actual.</p>'}
</body></html>"""

# ========= STATE =========
def load_state():
    return set()

def save_state(seen):
    return

# ========= EMAIL =========
def enviar_correo(html_content, subject):
    if not SMTP_PASS:
        raise RuntimeError("SMTP_PASS no está definido (variable de entorno).")
    msg = EmailMessage()
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(TO_EMAILS)
    msg["Subject"] = subject
    msg.set_content("Resumen diario en HTML.")
    msg.add_alternative(html_content, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context()) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    log(f"Correo enviado a {', '.join(TO_EMAILS)} ✅")

# ========= MAIN =========
def main(keyword=None, tzname="Europe/Madrid"):
    seen = load_state()
    listing = parse_all_listings()

    print("Primeros 15 títulos del listing combinado:")
    for it in listing[:15]:
        print(" -", f"[{it.get('source','?')}] {(it.get('title') or '').strip()}")

    # Normaliza keyword(s) -> lista
    kw_list = None
    if keyword:
        if isinstance(keyword, (list, tuple, set)):
            kw_list = [norm(k) for k in keyword if k]
        else:
            kw_list = [norm(keyword)]

    # Prefiltro ADAPTATIVO por título/URL (solo si reduce significativamente)
    if kw_list:
        before = len(listing)
        pre = [
            it for it in listing
            if any(k in norm(it.get("title","")) or k in norm(it.get("url","")) for k in kw_list)
        ]
        THRESH_ABS = 50
        THRESH_REL = 0.2  # 20%
        use_prefilter = len(pre) >= max(THRESH_ABS, int(before * THRESH_REL))
        if use_prefilter:
            listing = pre
            print(f"Prefiltro por {kw_list} aplicado: {len(listing)} (antes {before})")
        else:
            print(f"Prefiltro por {kw_list} NO aplicado ({len(pre)} candidatos). Buscaré en el cuerpo de {before} URLs.")

    collected = []
    for i, item in enumerate(listing, 1):
        url = item["url"]

        # respeta 'seen' solo si no hay filtro
        if not kw_list and url in seen:
            continue

        time.sleep(SLEEP_BETWEEN)
        try:
            art = extract_article(url, tzname=tzname)
        except Exception as e:
            log(f"Error extrayendo {url}: {e}")
            continue

        # si hay keywords, deben aparecer en título o cuerpo
        if kw_list:
            fulltxt = norm((art.get("title") or "") + " " + (art.get("content") or ""))
            if not any(k in fulltxt for k in kw_list):
                continue

        # exigir fecha y limitar por ventana reciente
        if not art.get("published") or not is_recent(art.get("published"), tzname=tzname):
            continue

        art["source"] = item.get("source","?")
        collected.append(art)
        seen.add(url)
        log(f"[{i}/{len(listing)}] OK [{art['source']}]: {art.get('title','')[:80]}")

    save_state(seen)

    html = build_html_multi(collected, tzname=tzname)

    if collected:
        filtro = ""
        if kw_list:
            filtro_vals = keyword if isinstance(keyword, (list, tuple, set)) else [keyword]
            filtro = f" — filtro: {', '.join(str(k) for k in filtro_vals if k)}"
        asunto = f"Noticias de hoy ({datetime.now().strftime('%Y-%m-%d')}){filtro}"
        enviar_correo(html, subject=asunto)
    else:
        log("No hay artículos para enviar en el rango actual.")

    log(f"Artículos enviados: {len(collected)}")

if __name__ == "__main__":
    kw_env = os.getenv("KEYWORD")
    tz_env = os.getenv("TZNAME")
    kws = CFG.get("keywords") or [CFG.get("keyword")]
    tzname = sys.argv[2] if len(sys.argv) > 2 else (tz_env or CFG.get("tzname","Europe/Madrid"))
    if kw_env and not kws:
        kws = [k.strip() for k in kw_env.split("|") if k.strip()]
    main(keyword=kws, tzname=tzname)
















