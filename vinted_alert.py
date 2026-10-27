"""Alerte Vinted (Discord et/ou e-mail).

Variables d'environnement :
  SEARCH_URL       lien de ta recherche Vinted (copié depuis le navigateur)
  DISCORD_WEBHOOK  adresse du webhook Discord
  DISCORD_USER_ID  (optionnel) ton identifiant Discord : chaque alerte te mentionne
  LOOP_SECONDS     (optionnel) durée pendant laquelle le script boucle, en secondes
  LOOP_PAUSE       (optionnel) pause entre deux vérifications, 60 par défaut
Optionnel pour l'e-mail : GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
"""
import html as htmllib
import json
import os
import random
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from urllib.parse import parse_qsl, urlparse

import requests

# Écrit chaque ligne tout de suite dans le journal (sinon elle reste en mémoire)
sys.stdout.reconfigure(line_buffering=True)

SEEN_FILE = "seen.json"
MAX_SEEN = 1000
MAX_NEW_AT_ONCE = 20  # au-delà, on suppose que la page a été réorganisée
MAX_ENRICH = 10  # nombre max d'articles dont on ouvre la page pour lire le prix

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS") or 0)
LOOP_PAUSE = int(os.environ.get("LOOP_PAUSE") or 60)


def fail(msg):
    # Le préfixe ::error:: fait apparaître le message dans les annotations de GitHub
    print("::error::" + " ".join(str(msg).split())[:400])
    sys.exit(1)


SEARCH_URL = os.environ.get("SEARCH_URL", "").strip()
if not SEARCH_URL.startswith("http"):
    fail("Le secret SEARCH_URL est absent ou ne commence pas par https://")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "").strip()
DISCORD_USER_ID = re.sub(r"\D", "", os.environ.get("DISCORD_USER_ID", ""))
GMAIL_USER = os.environ.get("GMAIL_USER", "").strip()
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
MAIL_TO = os.environ.get("MAIL_TO", "").strip() or GMAIL_USER

_SESSION = None


def get_session():
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    return _SESSION


def fetch_items():
    """Lit la page de résultats (HTML) et repère les liens d'articles."""
    parsed = urlparse(SEARCH_URL)
    host = parsed.netloc
    params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in ("order", "page", "time")]
    params.append(("order", "newest_first"))

    r = get_session().get(f"https://{host}{parsed.path}", params=params, timeout=25)
    if r.status_code != 200:
        fail(f"Vinted a repondu HTTP {r.status_code} : {r.text[:120]}")

    page = r.text
    # Liens sous la forme /items/123456-titre (parfois avec antislashs dans le JSON de la page)
    found = re.findall(r"items\\?/(\d{6,})-([a-z0-9][a-z0-9\-]*)", page)
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
        m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        title = m.group(1).strip() if m else "sans titre"
        fail(f"Aucun article trouve dans la page (titre : {title}, {len(page)} caracteres)")
    return host, items[:100]


def enrich(item):
    """Ouvre la page de l'article pour lire son vrai titre et son prix."""
    try:
        r = get_session().get(item["url"], timeout=25)
        if r.status_code != 200:
            return
        page = r.text
        name = price = currency = None
        for block in re.findall(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S | re.I
        ):
            try:
                data = json.loads(block)
            except ValueError:
                continue
            for d in data if isinstance(data, list) else [data]:
                if not isinstance(d, dict) or not d.get("name"):
                    continue
                name = name or d.get("name")
                offers = d.get("offers")
                if isinstance(offers, list) and offers:
                    offers = offers[0]
                if isinstance(offers, dict) and offers.get("price") is not None:
                    price = offers.get("price")
                    currency = offers.get("priceCurrency")
        if price is None:  # plan B : balises meta
            m = re.search(r'product:price:amount"\s+content="([^"]+)"', page) or re.search(
                r'content="([^"]+)"\s+property="product:price:amount"', page)
            if m:
                price = m.group(1)
            m = re.search(r'product:price:currency"\s+content="([^"]+)"', page) or re.search(
                r'content="([^"]+)"\s+property="product:price:currency"', page)
            if m:
                currency = m.group(1)
        if name:
            item["title"] = htmllib.unescape(str(name)).strip()
        if price is not None:
            item["price"] = {"amount": price, "currency_code": currency or "EUR"}
    except Exception as e:  # on garde le titre issu du lien si la page ne répond pas
        print(f"Détails indisponibles pour {item['id']} : {e}")


def price_text(item):
    p = item.get("price")
    if p is None:
        return ""
    if isinstance(p, dict):
        amount, cur = p.get("amount"), p.get("currency_code", "")
    else:
        amount, cur = p, item.get("currency", "")
    try:
        amount = f"{float(amount):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        amount = str(amount)
    cur = {"EUR": "€", "GBP": "£", "USD": "$"}.get(cur, cur)
    return f"{amount} {cur}".strip()


def send_discord(text):
    # Discord limite les messages à 2000 caractères : on découpe par blocs
    blocks, cur = [], ""
    for part in text.split("\n\n"):
        if len(cur) + len(part) + 2 > 1800:
            blocks.append(cur)
            cur = ""
        cur += part + "\n\n"
    if cur.strip():
        blocks.append(cur)
    for i, block in enumerate(blocks):
        payload = {"content": block.strip()}
        if DISCORD_USER_ID and i == 0:
            # Une mention déclenche une notification même si le salon est en « @mentions seulement »
            payload["content"] = f"<@{DISCORD_USER_ID}> " + payload["content"]
            payload["allowed_mentions"] = {"users": [DISCORD_USER_ID]}
        else:
            payload["allowed_mentions"] = {"parse": []}
        for _ in range(3):
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=20)
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
    # On garde l'ordre d'arrivée, sans doublon, et on conserve les plus récents
    unique = list(dict.fromkeys(ids))
    with open(SEEN_FILE, "w") as f:
        json.dump(unique[-MAX_SEEN:], f)


def main():
    host, items = fetch_items()
    current_ids = [it["id"] for it in items]
    seen = load_seen()

    if seen is None:
        # Premier lancement : on mémorise l'existant sans spammer
        save_seen(current_ids)
        print(f"[{time.strftime('%H:%M:%S')}] Initialisation : {len(items)} articles mémorisés.")
        notify(
            "Alerte Vinted activée",
            f"L'alerte fonctionne. {len(items)} articles actuels mémorisés.\n"
            "Tu recevras un message à chaque nouvel article.",
        )
        return

    seen_set = set(seen)
    # Les numéros d'articles ne suivent pas l'ordre de publication :
    # on se fie uniquement à la liste des articles déjà vus.
    new_items = [it for it in items if it["id"] not in seen_set]
    print(
        f"[{time.strftime('%H:%M:%S')}] {len(items)} articles lus ; "
        f"{len(new_items)} nouveaux ; {len(items) - len(new_items)} déjà connus"
    )

    if len(new_items) > MAX_NEW_AT_ONCE:
        # Beaucoup d'articles « nouveaux » d'un coup : la page a sans doute été réorganisée
        notify(
            "Alerte Vinted : page réorganisée",
            f"{len(new_items)} articles inconnus d'un coup. Je les ignore pour ne pas "
            "t'envoyer de vieilles annonces. Les prochains seront envoyés normalement.",
        )
    elif new_items:
        for it in new_items[:MAX_ENRICH]:
            enrich(it)
            time.sleep(1)
        lines = []
        for it in new_items:
            price = price_text(it)
            head = f"{it['title']} — {price}" if price else it["title"]
            lines.append(f"{head}\n{it['url']}")
        subject = f"Vinted : {len(new_items)} nouvel(s) article(s)"
        notify(subject, "\n\n".join(lines))
        print(f"{len(new_items)} nouveaux articles envoyés.")
    else:
        print("Rien de nouveau.")

    # Les articles de la page passent en fin de liste : ils restent « connus » tant qu'ils y sont
    cur = set(current_ids)
    save_seen([i for i in seen if i not in cur] + current_ids)


def run():
    if LOOP_SECONDS <= 0:
        main()
        return
    end = time.time() + LOOP_SECONDS
    ok = errors = 0
    while True:
        try:
            main()
            ok += 1
        except SystemExit:
            errors += 1
        except Exception as e:
            errors += 1
            print(f"::error::{type(e).__name__}: {e}")
        if time.time() + LOOP_PAUSE >= end:
            break
        time.sleep(max(5, LOOP_PAUSE + random.randint(-5, 5)))
    print(f"Terminé : {ok} vérifications réussies, {errors} échecs.")
    if ok == 0:
        fail("Toutes les verifications ont echoue")


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        fail(f"{type(e).__name__}: {e}")
