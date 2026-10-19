"""Annonces recues par newsletter dans Gmail (La Newsletter de l'Acteur, Cineaste.org...).

Lecture seule de la boite Gmail de l'utilisateur, avec sa connexion Gmail (mot de passe d'application) :
on cherche les e-mails de ces expediteurs des derniers jours, sans les marquer comme lus, et on decoupe
chaque e-mail en annonces (une newsletter en contient souvent plusieurs).
"""
import email
import imaplib
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import Message
from urllib.parse import quote

from bs4 import BeautifulSoup

from casting_watch.candidature import GmailError, gmail_login
from casting_watch.models import RawListing, utc_now_iso

NEWSLETTER_SOURCE_URL = "newsletter"
# Expediteur (ou domaine) -> nom affiche.
DEFAULT_NEWSLETTERS = {
    "lanewsletterdelacteur.com": "La Newsletter de l'Acteur",
    "cineaste.org": "Cinéaste.org",
}
DAYS_BACK = 45

CASTING_WORDS = re.compile(r"casting|recherch|figura|\br[oô]le|audition|silhouette|tournage", re.IGNORECASE)
SEPARATOR = re.compile(r"^\s*(?:[-_=*•~]{3,}|#{2,}.*)\s*$")
# Debut d'une nouvelle annonce : une ligne courte qui commence par « CASTING », « Offre », « Annonce »...
HEADING = re.compile(r"^\s*(?:\d{1,2}[.)]\s+)?(?:casting|offre|annonce)\b.{0,140}$", re.IGNORECASE)
SKIP_LINKS = re.compile(r"unsubscribe|desinscri|désinscri|desabonn|désabonn|mailto:|preferences|view.?in.?browser|"
                        r"facebook\.com/sharer|twitter\.com/intent", re.IGNORECASE)


def decoded(value: str | None) -> str:
    return str(make_header(decode_header(value or ""))).strip()


def message_text_and_links(message: Message) -> tuple[str, list[str]]:
    """Texte lisible de l'e-mail (texte brut, sinon HTML converti) et liens qu'il contient."""
    plain, html = "", ""
    for part in message.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not html:
            html = text
    links: list[str] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        links = [a["href"] for a in soup.find_all("a", href=True)]
        for tag in soup(["style", "script"]):
            tag.decompose()
        # Le lien reste a cote de son texte, pour savoir a quelle annonce il appartient.
        for anchor in soup.find_all("a", href=True):
            if anchor["href"].startswith("http") and not SKIP_LINKS.search(anchor["href"]):
                anchor.insert_after(f" {anchor['href']} ")
        for hr in soup.find_all("hr"):
            hr.replace_with("\n-----\n")
        for br in soup.find_all(["br", "p", "div", "tr", "h1", "h2", "h3", "h4", "li"]):
            br.insert_before("\n")
        text = soup.get_text()
    else:
        text = plain
        links = re.findall(r"https?://[^\s<>\"')\]]+", plain)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, [link for link in links if link.startswith("http") and not SKIP_LINKS.search(link)]


def split_ads(text: str) -> list[str]:
    """Decoupe une newsletter en annonces : separateurs (-----), titres « CASTING ... », ou paragraphes."""
    blocks, current = [], []
    for line in text.splitlines():
        if SEPARATOR.match(line) or (HEADING.match(line) and current):
            if current:
                blocks.append("\n".join(current).strip())
            current = [] if SEPARATOR.match(line) else [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    ads = [block for block in blocks if len(block) >= 80 and CASTING_WORDS.search(block)]
    return ads or ([text] if CASTING_WORDS.search(text) else [])


def first_link_in(block: str) -> str:
    links = [link for link in re.findall(r"https?://[^\s<>\"')\]]+", block) if not SKIP_LINKS.search(link)]
    return links[0] if links else ""


def gmail_link(message_id: str, index: int) -> str:
    return f"https://mail.google.com/mail/u/0/?annonce={index}#search/rfc822msgid%3A{quote(message_id, safe='')}"


def listings_from_message(message: Message, newsletter: str) -> list[RawListing]:
    text, links = message_text_and_links(message)
    subject = decoded(message.get("Subject"))
    message_id = (message.get("Message-ID") or subject).strip("<> ")
    ads = split_ads(text)
    fetched_at = utc_now_iso()
    listings, used = [], set()
    for index, block in enumerate(ads):
        lines = [line for line in block.splitlines() if line.strip()]
        title = lines[0][:160] if len(ads) > 1 else (subject or lines[0])[:160]
        url = first_link_in(block) or (links[0] if len(ads) == 1 and links else "")
        # Identifiant stable : le lien de l'annonce, sinon un lien qui ouvre l'e-mail dans Gmail
        # (le numero de l'annonce dans l'e-mail le rend unique).
        key = url if url and url not in used else gmail_link(message_id, index)
        used.add(key)
        listings.append(
            RawListing(
                source_name=f"Newsletter {newsletter}",
                source_url=NEWSLETTER_SOURCE_URL,
                title=title,
                url=key,
                text=" ".join(lines)[:8000],
                fetched_at=fetched_at,
                detail_text="\n".join(lines)[:8000],
            )
        )
    return listings


def all_mail_folder(imap) -> str:
    """Dossier « Tous les messages » de Gmail (nom different selon la langue), sinon la boite de reception."""
    status, folders = imap.list()
    for raw in folders or []:
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1].strip()
    return "INBOX"


def fetch_newsletter_listings(
    address: str,
    app_password: str,
    senders: dict[str, str] | None = None,
    days_back: int = DAYS_BACK,
    imap_factory=imaplib.IMAP4_SSL,
) -> tuple[list[RawListing], dict[str, int]]:
    """Annonces des newsletters recues ces derniers jours. Retourne (annonces, nombre d'e-mails par newsletter)."""
    senders = senders or DEFAULT_NEWSLETTERS
    since = (date.today() - timedelta(days=days_back)).strftime("%d-%b-%Y")
    imap = gmail_login(address, app_password, imap_factory)
    listings: list[RawListing] = []
    counts: dict[str, int] = {}
    try:
        status, _ = imap.select(all_mail_folder(imap), readonly=True)  # lecture seule : rien n'est marque comme lu
        if status != "OK":
            raise GmailError("impossible d'ouvrir la boîte Gmail")
        for sender, name in senders.items():
            status, data = imap.search(None, "FROM", f'"{sender}"', "SINCE", since)
            ids = (data[0] or b"").split() if status == "OK" and data else []
            counts[name] = len(ids)
            for message_id in ids:
                status, parts = imap.fetch(message_id, "(BODY.PEEK[])")
                raw = next((part[1] for part in parts or [] if isinstance(part, tuple)), None)
                if status == "OK" and raw:
                    listings.extend(listings_from_message(email.message_from_bytes(raw), name))
    finally:
        imap.logout()
    return listings, counts
