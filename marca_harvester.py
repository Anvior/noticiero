# noticias_harvester.py
# -*- coding: utf-8 -*-
import os, json, time, re, sys, unicodedata, smtplib, ssl
from datetime import datetime
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import trafilatura
import extruct
from email.message import EmailMessage
from w3lib.html import get_base_url
from dateutil import tz, parser as dateparser

# ========= FUENTES =========
SOURCES = [
    {
        "name": "MARCA",
        "listing": "https://www.marca.com/ultimas-noticias.html",
        "homepage": "https://www.marca.com/",
        "domain_prefix": "https://www.marca.com/",
        "max_to_fetch": 60,
    },
    {
        "name": "EXPANSION",
        # sección que sí existe y se actualiza varias veces al día
        "listing": "https://www.expansion.com/economia.html?intcmp=MENUHOM24101&s_kw=economia",
        "homepage": "https://www.expansion.com/",
        "domain_prefix": "https://www.expansion.com/",
        "max_to_fetch": 60,
    },
]


# ========= RED =========
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/"
}
TIMEOUT = 15
RETRIES = 2
SLEEP_BETWEEN = 0.8
STATE_FILE = None   # estado combinado

# ========= EMAIL (Gmail SSL 465) =========
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = "anartz2001@gmail.com"
SMTP_PASS = os.getenv("SMTP_PASS")   # App Password (16 chars, sin espacios)
TO_EMAILS = ["anartz2001@gmail.com"]  # añade más si quieres

# ========= UTILIDADES =========
def log(m): print(m, flush=True)

def norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()

def http_get(url):
    for i in range(RETRIES):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception:
            if i == RETRIES - 1:
                raise
            time.sleep(1.2 * (i + 1))

def extract_urls_regex(html, base, domain_prefix):
    urls = set()
    for href in re.findall(r'href="([^"]+?\.html)"', html):
        url = urljoin(base, href)
        if url.startswith(domain_prefix) and not any(x in url for x in ["/album/", "/video/", "/fotogaleria/"]):
            urls.add(url)
    return list(urls)

def parse_listing_from(url, domain_prefix, max_to_fetch, debug_name):
    res = http_get(url)
    html = res.text


    soup = BeautifulSoup(html, "lxml")
    items = []

    # 1) selectores habituales
    candidates = (
        soup.select("article a[href$='.html']") or
        soup.select("h2 a[href$='.html'], h3 a[href$='.html']")
    )
    for a in candidates:
        href = a.get("href")
        if not href: continue
        url_abs = urljoin(url, href)
        if not url_abs.startswith(domain_prefix): continue
        title = a.get_text(strip=True)
        parent = a.find_parent(["article", "li", "div"])
        time_el = parent.select_one("time, .ue-c-article__published-date, .mod-date") if parent else None
        time_hint = time_el.get_text(strip=True) if time_el else ""
        items.append({"url": url_abs, "title": title, "time_hint": time_hint})

    # 2) fallback: regex
    if len(items) < 5:
        for u in extract_urls_regex(html, url, domain_prefix):
            items.append({"url": u, "title": "", "time_hint": ""})

    # dedup + recorte
    seen, out = set(), []
    for it in items:
        u = it["url"]
        if u in seen: continue
        seen.add(u)
        out.append(it)
        if len(out) >= max_to_fetch: break
    return out

def parse_all_listings():
    all_items = []
    for src in SOURCES:
        name = src["name"]
        log(f"— Fuente: {name}")
        items = parse_listing_from(src["listing"], src["domain_prefix"], src["max_to_fetch"], f"{name.lower()}_listing")
        if len(items) == 0:
            log(f"Aviso: 0 enlaces en {name} listing. Probando portada…")
            items = parse_listing_from(src["homepage"], src["domain_prefix"], src["max_to_fetch"], f"{name.lower()}_home")
        log(f"{name}: enlaces encontrados = {len(items)}")
        for it in items:
            it["source"] = name
        all_items.extend(items)
    # dedup cross-site
    dedup, out = set(), []
    for it in all_items:
        if it["url"] in dedup: continue
        dedup.add(it["url"])
        out.append(it)
    log(f"Total combinado (sin duplicados): {len(out)}")
    return out

def extract_jsonld(html_text, url):
    data = extruct.extract(html_text, base_url=get_base_url(html_text, url), syntaxes=['json-ld'])
    jsonld = data.get('json-ld', []) if data else []
    for block in jsonld:
        t = block.get("@type")
        if t == "NewsArticle" or (isinstance(t, list) and "NewsArticle" in t):
            return block
    return None

def normalize_datetime(dt_str, tzname="Europe/Madrid"):
    if not dt_str: return None
    try:
        dt = dateparser.parse(dt_str)
        if not dt: return None
        if not dt.tzinfo: dt = dt.replace(tzinfo=tz.UTC)
        target = tz.gettz(tzname)
        return dt.astimezone(target)
    except Exception:
        return None

def extract_article(url, tzname="Europe/Madrid"):
    res = http_get(url)
    html = res.text
    meta = extract_jsonld(html, url) or {}
    published = normalize_datetime(meta.get("datePublished") or meta.get("dateModified"), tzname)
    headline = meta.get("headline")
    article_body = meta.get("articleBody")

    if not article_body:
        article_body = trafilatura.extract(html, url=url, include_comments=False, include_tables=False) or ""
        article_body = article_body.strip()

    if not headline:
        soup = BeautifulSoup(html, "lxml")
        h = soup.select_one("h1") or soup.select_one("header h1")
        headline = h.get_text(strip=True) if h else ""

    return {
        "url": url, "title": headline or "",
        "published": published.isoformat() if published else None,
        "content": article_body or ""
    }

def is_recent(dt_iso, tzname="Europe/Madrid", hours=24):
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
        blocks.append(f"""
        <article style="margin-bottom:24px;">
          <div style="font-size:12px;color:#999">{a.get('source','')}</div>
          <h3 style="margin:2px 0 6px 0;">{a['title']}</h3>
          <div style="font-size:12px;color:#666;">{p_h} — <a href="{a['url']}">{a['url']}</a></div>
          <p style="white-space:pre-wrap; line-height:1.45; margin-top:10px;">
            {a['content'][:1500]}{'…' if len(a['content'])>1500 else ''}
          </p>
        </article>""")
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8"><title>Noticias ( {now} )</title></head>
<body style="font-family:system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial; max-width:800px; margin:24px auto; padding:0 16px;">
<h1 style="margin-bottom:8px;">Resumen — MARCA + EXPANSIÓN</h1>
<div style="color:#666; font-size:12px; margin-bottom:16px;">Generado {now} ({tzname})</div>
{''.join(blocks) if blocks else '<p>No hay artículos en el rango actual.</p>'}
</body></html>"""

def load_state():

    return set()

def save_state(seen):
    return


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
# ========= MAIN =========
def main(keyword=None, tzname="Europe/Madrid", out_html="noticias_hoy.html", out_json="noticias_hoy.json"):
    seen = load_state()
    listing = parse_all_listings()

    print("Primeros 15 títulos del listing combinado:")
    for it in listing[:15]:
        print(" -", f"[{it.get('source','?')}] {(it.get('title') or '').strip()}")

    kw_norm = norm(keyword) if keyword else None
    if kw_norm:
        before = len(listing)
        listing = [it for it in listing if kw_norm in norm(it.get("title","")) or kw_norm in norm(it.get("url",""))]
        print(f"Enlaces tras prefiltro por '{keyword}': {len(listing)} (antes {before})")
        if len(listing) == 0:
            print("Aviso: 0 coincidencias en títulos/URLs. Continuaré con el listado completo para buscar en el cuerpo.")
            listing = parse_all_listings()

    collected = []
    for i, item in enumerate(listing, 1):
        url = item["url"]

        if not kw_norm and url in seen:
            continue

        time.sleep(SLEEP_BETWEEN)
        try:
            art = extract_article(url, tzname=tzname)
        except Exception as e:
            log(f"Error extrayendo {url}: {e}")
            continue

        # si hay keyword, debe aparecer en título o cuerpo
        if kw_norm and kw_norm not in norm((art.get("title") or "") + " " + (art.get("content") or "")):
            continue

        # exigir fecha y limitar a 24 h
        if not art.get("published") or not is_recent(art.get("published"), tzname=tzname, hours=24):
            continue

        art["source"] = item.get("source","?")
        collected.append(art)
        seen.add(url)
        log(f"[{i}/{len(listing)}] OK [{art['source']}]: {art.get('title','')[:80]}")

    save_state(seen)

    html = build_html_multi(collected, tzname=tzname)

    if collected:
        asunto = f"MARCA + EXPANSIÓN ({datetime.now().strftime('%Y-%m-%d')})" + (f" — filtro: {keyword}" if keyword else "")
        enviar_correo(html, subject=asunto)
    else:
        log("No hay artículos para enviar en el rango actual.")

    log(f"Artículos guardados: {len(collected)} → {out_html} / {out_json}")


if __name__ == "__main__":
    kw     = sys.argv[1] if len(sys.argv) > 1 else None
    tzname = sys.argv[2] if len(sys.argv) > 2 else "Europe/Madrid"
    main(keyword=kw, tzname=tzname)

