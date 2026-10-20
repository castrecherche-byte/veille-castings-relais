"""Relais : un seul endroit lit la boite mail dediee aux newsletters et publie les annonces.

- `publier()` tourne sur le relais (par exemple GitHub Actions, toutes les heures). Le mot de passe de la boite
  vient des variables d'environnement (secrets), jamais du code ni de l'executable partage.
- `lire()` tourne dans chaque copie de l'outil : elle telecharge le fichier publie (lecture seule, sans mot de
  passe) et en refait des annonces.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from casting_watch.models import RawListing
from casting_watch.newsletters import fetch_newsletter_listings

FORMAT_VERSION = 1
FIELDS = ("source_name", "source_url", "title", "url", "text", "fetched_at", "detail_text")


class RelayError(RuntimeError):
    pass


HIDDEN = "[adresse masquée]"
# Liens personnels des newsletters (desinscription, preferences, suivi) : jamais publies.
PERSONAL_LINK = re.compile(
    r"https?://\S*(?:unsubscri|desinscri|désinscri|desabonn|désabonn|optout|opt-out|preference|manage|"
    r"[?&](?:e|email|mail|u|uid|token|t|hash|sub|subscriber)=)\S*",
    re.IGNORECASE,
)


def anonymize(text: str, address: str) -> str:
    """Retire l'adresse de la boite (et son identifiant) et les liens personnels avant publication."""
    if not text:
        return text
    text = PERSONAL_LINK.sub("", text)
    if address:
        local = address.split("@")[0]
        text = re.sub(re.escape(address), HIDDEN, text, flags=re.IGNORECASE)
        text = re.sub(rf"\b{re.escape(local)}\b", HIDDEN, text, flags=re.IGNORECASE)
    return text


def anonymized(listing: RawListing, address: str) -> RawListing:
    url = listing.url
    # Lien personnel, ou lien vers l'e-mail dans la boite du relais (inutile pour les autres) :
    # remplace par un identifiant stable, sans rien de personnel.
    if PERSONAL_LINK.fullmatch(url) or "mail.google.com" in url or (address and address.lower() in url.lower()):
        url = "newsletter:" + hashlib.sha1(f"{listing.title}\n{listing.text}".encode("utf-8")).hexdigest()[:16]
    return RawListing(
        source_name=listing.source_name,
        source_url=listing.source_url,
        title=anonymize(listing.title, address),
        url=url,
        text=anonymize(listing.text, address),
        fetched_at=listing.fetched_at,
        detail_text=anonymize(listing.detail_text, address),
    )


def to_json(listings: list[RawListing], counts: dict[str, int]) -> dict:
    return {
        "format": FORMAT_VERSION,
        "publie_le": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "newsletters": counts,
        "annonces": [{field: getattr(item, field) for field in FIELDS} for item in listings],
    }


def from_json(data: dict) -> list[RawListing]:
    if data.get("format") != FORMAT_VERSION:
        raise RelayError("fichier du relais dans un format inconnu (mettez l'outil à jour)")
    return [RawListing(**{field: item.get(field, "") for field in FIELDS}) for item in data.get("annonces", [])]


def publier(out_path: str, imap_factory=None) -> dict:
    """Lit la boite dediee (GMAIL_ADDRESS / GMAIL_APP_PASSWORD) et ecrit le fichier d'annonces."""
    address, password = os.environ.get("GMAIL_ADDRESS", ""), os.environ.get("GMAIL_APP_PASSWORD", "")
    if not address or not password:
        raise RelayError("variables GMAIL_ADDRESS et GMAIL_APP_PASSWORD manquantes (secrets du relais)")
    options = {"imap_factory": imap_factory} if imap_factory else {}
    listings, counts = fetch_newsletter_listings(address, password, **options)
    data = to_json([anonymized(listing, address) for listing in listings], counts)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def lire(url: str, timeout: float = 20, transport: httpx.BaseTransport | None = None) -> tuple[list[RawListing], str]:
    """Annonces publiees par le relais, et la date de publication."""
    try:
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=True) as client:
            response = client.get(url)
    except httpx.HTTPError as error:
        raise RelayError(f"relais injoignable ({error})") from error
    if response.status_code != 200:
        raise RelayError(f"relais injoignable (erreur {response.status_code})")
    try:
        data = response.json()
    except ValueError as error:
        raise RelayError("le relais n'a pas renvoyé un fichier d'annonces valide") from error
    return from_json(data), data.get("publie_le", "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relais newsletters : lit la boîte dédiée et publie les annonces.")
    parser.add_argument("--out", default="site/annonces.json", help="Fichier à écrire")
    args = parser.parse_args(argv)
    try:
        data = publier(args.out)
    except Exception as error:  # le journal du relais doit dire clairement ce qui ne va pas
        print(f"Relais en erreur : {error}", file=sys.stderr)
        return 1
    print(f"{len(data['annonces'])} annonce(s) publiée(s) ; e-mails lus : {data['newsletters']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
