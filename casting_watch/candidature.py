"""Preparation des candidatures : profil de l'enfant, photos, brouillon Gmail.

Rien n'est envoye automatiquement : l'outil cree un BROUILLON dans Gmail (via IMAP et un mot de passe
d'application Google). Le parent le relit et clique lui-meme sur Envoyer.
Le profil et les photos restent sur ce PC (base SQLite et dossier data/profil/photos).
"""
import imaplib
import io
import re
import time
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from PIL import Image, ImageOps

PROFILE_FIELDS = [
    ("prenom", "Prénom"),
    ("nom", "Nom"),
    ("date_naissance", "Date de naissance (JJ/MM/AAAA)"),
    ("taille_cm", "Taille (cm)"),
    ("ville", "Ville de résidence"),
    ("parent_nom", "Nom du parent (si mineur)"),
    ("parent_telephone", "Téléphone de contact"),
    ("parent_email", "Mail de contact"),
    ("video_lien", "Lien vers une vidéo de présentation (facultatif)"),
    ("presentation", "Quelques mots sur la personne (facultatif)"),
]
# "mineur" = "oui" (candidature envoyee par un parent, par defaut) ou "non" (la personne postule elle-meme).
MINOR_KEY = "mineur"


def is_minor(profile: dict) -> bool:
    return profile.get(MINOR_KEY, "oui") != "non"
PHOTO_TYPES = ("jpg", "jpeg", "png", "webp")
GMAIL_IMAP = "imap.gmail.com"
GMAIL_DRAFTS_URL = "https://mail.google.com/mail/u/0/#drafts"
MAX_PHOTO_SIDE = 1600
# "Objet du mail : Role de Gisele" ou 'Objet du mail "CASTING PUB"' (deux-points ou guillemets obligatoires).
SUBJECT_LINE = re.compile(r"objet (?:du (?:mail|message)|de votre mail)?\s*(?::|(?=[\"«“]))\s*(.+)", re.I)


class GmailError(RuntimeError):
    pass


def age_on(birth: str, today: date | None = None) -> int | None:
    try:
        born = datetime.strptime(birth.strip(), "%d/%m/%Y").date()
    except (AttributeError, ValueError):
        return None
    today = today or date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def missing_fields(profile: dict) -> list[str]:
    required = ("prenom", "date_naissance", "taille_cm", "ville", "parent_telephone", "parent_email")
    if is_minor(profile):
        required += ("parent_nom",)
    labels = dict(PROFILE_FIELDS)
    return [labels[key] for key in required if not str(profile.get(key, "")).strip()]


def requested_subject(fiche: dict) -> str:
    """Objet impose par l'annonce ("Objet du mail : Role de Gisele"), sinon chaine vide."""
    lines = fiche.get("a_envoyer", []) + fiche.get("rubriques", {}).get("Pour postuler", [])
    for line in lines:
        if "objet" not in line.lower():
            continue
        match = SUBJECT_LINE.search(line)
        if match:
            subject = match.group(1).strip().strip("\"'«»“” .")
            if subject and len(subject) < 120 and "rôle pour lequel" not in subject.lower():
                return subject
    return ""


def build_subject(fiche: dict, profile: dict, title: str) -> str:
    subject = requested_subject(fiche)
    child = " ".join(part for part in (profile.get("prenom", ""), profile.get("nom", "")) if part).strip()
    if subject:
        return f"{subject} - {child}" if child else subject
    return f"Candidature {child} - {title[:90]}".strip()


def build_body(profile: dict, title: str, url: str, photo_count: int, today: date | None = None) -> str:
    child = " ".join(part for part in (profile.get("prenom", ""), profile.get("nom", "")) if part).strip()
    age = age_on(profile.get("date_naissance", ""), today)
    minor = is_minor(profile)
    intro = f"la candidature de mon enfant {child}" if minor else "ma candidature"
    lines = [
        "Bonjour,",
        "",
        f"Suite à votre annonce « {title} », je vous propose {intro}.",
        "",
        f"- Prénom et nom : {child}",
        f"- Date de naissance : {profile.get('date_naissance', '')}" + (f" ({age} ans)" if age is not None else ""),
        f"- Taille : {profile.get('taille_cm', '')} cm",
        f"- Ville de résidence : {profile.get('ville', '')}",
    ]
    if profile.get("video_lien"):
        lines.append(f"- Vidéo de présentation : {profile['video_lien']}")
    if profile.get("presentation"):
        lines += ["", profile["presentation"].strip()]
    if photo_count:
        lines += ["", f"Vous trouverez ci-joint {photo_count} photo(s) récente(s)."]
    lines += [
        "",
        "Je reste à votre disposition pour tout complément d'information.",
        "",
        "Bien cordialement,",
        profile.get("parent_nom", "") if minor else child,
        profile.get("parent_telephone", ""),
        profile.get("parent_email", ""),
    ]
    if url.startswith("http"):
        lines += ["", f"Annonce : {url}"]
    return "\n".join(lines).rstrip() + "\n"


def photo_bytes(path: Path) -> bytes:
    """Photo redressee, reduite (1600 px max) et en JPEG, pour rester loin de la limite Gmail de 25 Mo."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def build_draft(sender: str, recipient: str, subject: str, body: str, photos: list[Path]) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    if recipient.strip():
        message["To"] = recipient.strip()
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(body)
    for photo in photos:
        message.add_attachment(photo_bytes(photo), maintype="image", subtype="jpeg", filename=f"{photo.stem}.jpg")
    return message


def drafts_folder(imap) -> str:
    """Nom du dossier Brouillons (« [Gmail]/Brouillons » en francais), trouve par son attribut \\Drafts."""
    status, folders = imap.list()
    if status != "OK":
        raise GmailError("impossible de lister les dossiers Gmail")
    for raw in folders or []:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        if "\\Drafts" in line:
            name = line.rsplit(' "/" ', 1)[-1].strip()
            return name if name.startswith('"') else f'"{name}"'
    return '"[Gmail]/Drafts"'


def gmail_login(address: str, app_password: str, imap_factory=imaplib.IMAP4_SSL):
    imap = imap_factory(GMAIL_IMAP)
    try:
        imap.login(address.strip(), app_password.replace(" ", ""))
    except imaplib.IMAP4.error as error:
        raise GmailError(
            "connexion refusée par Gmail : vérifiez l'adresse et le mot de passe d'application "
            "(16 lettres, créé sur myaccount.google.com/apppasswords)"
        ) from error
    return imap


def test_gmail(address: str, app_password: str, imap_factory=imaplib.IMAP4_SSL) -> str:
    imap = gmail_login(address, app_password, imap_factory)
    try:
        return drafts_folder(imap)
    finally:
        imap.logout()


def save_gmail_draft(address: str, app_password: str, message: EmailMessage, imap_factory=imaplib.IMAP4_SSL) -> None:
    imap = gmail_login(address, app_password, imap_factory)
    try:
        status, _ = imap.append(drafts_folder(imap), "(\\Draft)", imaplib.Time2Internaldate(time.time()), message.as_bytes())
        if status != "OK":
            raise GmailError("Gmail a refusé le brouillon")
    finally:
        imap.logout()
