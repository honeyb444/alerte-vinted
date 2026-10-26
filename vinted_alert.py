"""Alerte Vinted par e-mail.

Variables d'environnement attendues :
  SEARCH_URL         lien de ta recherche Vinted (copié depuis le navigateur)
  DISCORD_WEBHOOK    adresse du webhook Discord (pour recevoir sur Discord)
Optionnel, en plus ou à la place de Discord :
  GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO   (pour recevoir aussi par e-mail)
"""
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlparse

import requests

SEEN_FILE = "seen.json"
MAX_SEEN = 500

def fail(msg):
    # Le préfixe ::error:: fait apparaître le message dans les annotations de GitHub
    print("::error::" + " ".join(str(msg).split())[:400])
    sys.exit(1)


SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
if not SEARCH_URL.startswith("http"):
    fail("Le secret SEARCH_URL est absent ou ne commence pas par https://")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip() or GMAIL_USER


def fetch_items():
    """Lit la page de résultats (HTML) et repère les liens d'articles.

    L'ancienne API JSON de Vinted n'est plus accessible : on lit donc la page
    que Vinted génère côté serveur pour la recherche.
    """
    parsed = urlparse(SEARCH_URL)
    host = parsed.netloc
    params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in ("order", "page", "time")]
    params.append(("order", "newest_first"))

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    r = session.get(f"https://{host}{parsed.path}", params=params, timeout=25)
    if r.status_code != 200:
        fail(f"Vinted a repondu HTTP {r.status_code} : {r.text[:120]}")

    html = r.text
    # Les liens apparaissent sous la forme /items/123456-titre-de-l-article
    # (parfois avec des antislashs dans le JSON intégré à la page)
    found = re.findall(r"items\\?/(\d{6,})-([a-z0-9][a-z0-9\-]*)", html)
    items, seen_ids = [], set()
    for item_id, slug in found:
        item_id = int(item_id)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append({
            "id": item_id,
            "title": slug.replace("-", " "),
            "url": f"https://{host}/items/{item_id}-{slug}",
        })
    if not items:
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        title = m.group(1).strip() if m else "sans titre"
        fail(f"Aucun article trouve dans la page (titre : {title}, {len(html)} caracteres)")
    return host, items[:40]


def price_text(item):
    p = item.get("price")
    if p is None:
        return ""
    if isinstance(p, dict):
        return f"{p.get('amount')} {p.get('currency_code', '')}".strip()
    return f"{p} {item.get('currency', '')}".strip()


def item_link(host, item):
    url = item.get("url") or f"/items/{item['id']}"
    return url if url.startswith("http") else f"https://{host}{url}"


def send_discord(text):
    # Discord limite les messages à 2000 caractères : on découpe par blocs
    blocks, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > 1900:
            blocks.append(cur)
            cur = ""
        cur += part + "\n\n"
    if cur.strip():
        blocks.append(cur)
    for block in blocks:
        for _ in range(3):
            r = requests.post(DISCORD_WEBHOOK, json={"content": block.strip()}, timeout=20)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)))
                continue
            break
        if r.status_code >= 300:
            fail(f"Discord a repondu HTTP {r.status_code} : {r.text[:150]}")


def send_mail(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = MAIL_TO
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_PASSWORD)
        smtp.send_message(msg)


def notify(subject, body):
    if not DISCORD_WEBHOOK and not (GMAIL_USER and GMAIL_PASSWORD):
        fail("Le secret DISCORD_WEBHOOK est absent")
    if DISCORD_WEBHOOK:
        send_discord(f"**{subject}**\n\n{body}")
    if GMAIL_USER and GMAIL_PASSWORD:
        send_mail(subject, body)


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return None
    with open(SEEN_FILE) as f:
        return json.load(f)


def save_seen(ids):
    with open(SEEN_FILE, "w") as f:
        json.dump(ids[-MAX_SEEN:], f)


def main():
    host, items = fetch_items()
    current_ids = [it["id"] for it in items]
    seen = load_seen()

    if seen is None:
        # Premier lancement : on mémorise l'existant sans spammer
        save_seen(current_ids)
        notify(
            "Alerte Vinted activée",
            f"L'alerte fonctionne. {len(items)} articles actuels mémorisés.\n"
            "Tu recevras un e-mail à chaque nouvel article.",
        )
        print("Initialisation terminée.")
        return

    seen_set = set(seen)
    new_items = [it for it in items if it["id"] not in seen_set]
    if new_items:
        lines = []
        for it in new_items:
            parts = [it.get("title", "Sans titre"), price_text(it)]
            if it.get("brand_title"):
                parts.append(it["brand_title"])
            if it.get("size_title"):
                parts.append(f"taille {it['size_title']}")
            lines.append(" | ".join(p for p in parts if p) + "\n" + item_link(host, it))
        subject = f"Vinted : {len(new_items)} nouvel(s) article(s)"
        notify(subject, "\n\n".join(lines))
        print(f"{len(new_items)} nouveaux articles envoyés.")
    else:
        print("Rien de nouveau.")

    save_seen(seen + [i for i in current_ids if i not in seen_set])


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
