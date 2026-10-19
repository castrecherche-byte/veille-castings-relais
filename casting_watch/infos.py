"""Fiche pratique d'une annonce : ce qui est demande (photos, taille...), lieu, dates, comment postuler.

Tout vient du texte public de l'annonce : rien n'est devine, chaque ligne affichee est une ligne de l'annonce.
"""
import re

from casting_watch.filters import canonical, extract_age_ranges
from casting_watch.models import HEADING_MARK, RawListing

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MONTHS = "janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre"

# (rubrique, motif cherche dans la ligne canonique, motif a exclure, nombre max de lignes)
FIELDS = [
    ("Âge", rf"age requis|doit avoir|avoir \d+ ans|ans au \d|ans en (?:{MONTHS})|\bnee? en\b|^age\b", r"date de naissance", 2),
    ("Photos / vidéo", r"photo|video|self ?tape|book\b", None, 3),
    ("Taille / mensurations", r"taille|mesur|poids|pointure|\b1 ?m ?\d{2}\b|\b1\d{2} ?cm\b", None, 2),
    ("Profil recherché", r"^profil|cheveux|yeux|^type\b|type (?:caucasien|europeen|asiatique|africain|metis|mediterraneen)|origine|debutant|experience|sachant|bilingue|parlant", None, 4),
    ("Lieu", r"^lieu\b|resid|habit|decor|tournage (?:a|en|au|dans|vers|pres)\b|^ville\b", None, 3),
    ("Dates", rf"^dates?\b|dates? de tournage|^tournage\b|tournage (?:prevu|aura lieu|le|les|du|entre|au printemps|en (?:{MONTHS}))|disponib", r"naissance", 3),
    ("Rémunération", r"remuner|cachet|defraiement|tarif|€|euros?\b|\bbrut\b", None, 2),
    ("Date limite", r"jusqu ?au|avant le|date limite|cloture|au plus tard", None, 2),
    ("Pour postuler", r"envoy|joindre|pieces a|objet du mail|formulaire|par mail|e mail|adresse mail|cliquez|postuler|candidater", None, 3),
]
SHORT = 180  # les lignes courtes (souvent "Label : valeur") passent avant les longs paragraphes
TOO_LONG = 420

# Lignes publicitaires ou generiques des sites, jamais utiles dans une fiche.
NOISE = re.compile(
    r"€ ?/ ?mois|sans engagement|stripe|debloqu|alerte|abonnement|reserve aux abonnes|nos conseils|vigilance|"
    r"jamais d argent|aucune commission|partager l annonce|^je postule$|^postuler maintenant$|"
    r"recruteur certifie|annonce verifiee|production reelle|\bcandidature s\b|nos recommandations|"
    r"consulter nos|reussir un casting|comprendre notre methode|ca me ressemble|^comment candidater$|"
    r"non precisee?\.?$|consignes d envoi|notre guide|castings actuellement disponibles|relu avant publication"
)
LABEL_ONLY = re.compile(r"^(lieu|tournage|cachet|candidatures|remuneration|defraiement|dates?|age|ville|duree|objet du mail)\s*:?$")
LIST_HEADER = re.compile(r"(envoy|joindre|fournir|pieces|transmettre|postuler|comprendre|comporter|contenir|dossier|candidature|profils? recherches?)")
HEADER_WITHOUT_COLON = re.compile(r"^(pieces a envoyer|documents a fournir|a envoyer|elements a envoyer)$")
END_OF_LIST = re.compile(r"^du \d|^(remuneration|defraiement|lieu|tournage|cachet|dates?|dates? de tournage) :")
TRAILING_NOISE = re.compile(r"\s*Visible sans abonnement\.?$", re.I)
LABELED = re.compile(r"^[^:]{2,30} : \S")


def _is_heading(line: str) -> bool:
    return line.startswith(HEADING_MARK)


def _plain(line: str) -> str:
    return line.removeprefix(HEADING_MARK)


def _merge_labels(lines: list[str]) -> list[str]:
    """'Lieu' + 'Ile-de-France' -> 'Lieu : Ile-de-France' ; 'Remuneration :' + 'Prevue' idem."""
    merged: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        nxt = _plain(lines[index + 1]) if index + 1 < len(lines) else ""
        if nxt and len(nxt) < 120 and LABEL_ONLY.match(canonical(_plain(line))):
            merged.append(f"{_plain(line).rstrip(' :')} : {nxt}")
            index += 2
            continue
        merged.append(line)
        index += 1
    return merged


def _shorten(line: str, limit: int = 260) -> str:
    return line if len(line) <= limit else line[: limit - 1].rsplit(" ", 1)[0] + "…"


def _lists(lines: list[str]) -> dict[str, list[str]]:
    """Listes annoncees par un intitule ("Pour postuler, envoyez :", "Profil recherche :", "Pieces a envoyer")."""
    found: dict[str, list[str]] = {}
    for index, line in enumerate(lines):
        key = canonical(_plain(line))
        with_colon = line.rstrip().endswith(":") and LIST_HEADER.search(key)
        without_colon = HEADER_WITHOUT_COLON.match(key)
        if not (with_colon or without_colon):
            continue
        kind = "Profil recherché" if "profil" in key else "À envoyer"
        if kind in found:
            continue
        items: list[str] = []
        for follower in lines[index + 1 : index + 13]:
            follower_key = canonical(follower)
            if (
                _is_heading(follower)
                or NOISE.search(follower_key)
                or END_OF_LIST.search(follower_key)
                or LABEL_ONLY.match(follower_key)
                or len(follower) > 260
                or (" / " in follower and len(follower) < 40)
                or (follower.rstrip().endswith(":") and LIST_HEADER.search(follower_key))
            ):
                break
            items.append(_shorten(TRAILING_NOISE.sub("", follower).rstrip(" ;")))
            if without_colon:
                break  # "Pieces a envoyer" est suivi d'une seule phrase
        if items:
            found[kind] = items
    return found


def _format_ranges(ranges: list[tuple[int, int]]) -> str:
    return ", ".join(
        f"{a} ans et +" if b == 99 else f"{a}-{b} ans" if a != b else f"{a} ans" for a, b in sorted(set(ranges))
    )


def build_info(listing: RawListing, apply_note: str = "") -> dict:
    """Retourne {"rubriques": {titre: [lignes]}, "a_envoyer": [...], "ages": "...", "contact": {...}}."""
    raw_lines = [TRAILING_NOISE.sub("", line) for line in listing.detail_text.split("\n") if line.strip()]
    if not raw_lines:
        raw_lines = [listing.text]
    all_lines = _merge_labels(raw_lines)
    lists = _lists(all_lines)
    title_key = canonical(listing.title)
    lines = [
        line for line in all_lines
        if not _is_heading(line)
        and not NOISE.search(canonical(line))
        and canonical(line) != title_key
        and not line.rstrip().endswith(("?", ":"))  # questions-titres et intitules de liste
        and not HEADER_WITHOUT_COLON.match(canonical(line))
        and len(line) <= TOO_LONG
    ]

    def priority(line: str) -> int:
        """'Cachet : 98 € brut' d'abord, puis les lignes courtes, puis les longs paragraphes."""
        if LABELED.match(line):
            return 0
        return 1 if len(line) <= SHORT else 2

    sections: dict[str, list[str]] = {}
    for name, pattern, exclude, limit in FIELDS:
        candidates = [
            line for line in lines
            if re.search(pattern, canonical(line)) and not (exclude and re.search(exclude, canonical(line)))
        ]
        candidates.sort(key=priority)  # tri stable : l'ordre du texte est conserve a priorite egale
        picked: list[str] = []
        for line in candidates:
            short = _shorten(line.rstrip(" ;"))
            if short not in picked:
                picked.append(short)
            if len(picked) == limit:
                break
        if picked:
            sections[name] = picked
    if "Profil recherché" in lists:
        sections["Profil recherché"] = lists["Profil recherché"][:6]

    title_ranges = extract_age_ranges(listing.title)
    ages = _format_ranges(title_ranges or extract_age_ranges(listing.text)[:4])

    everything = f"{listing.text}\n{listing.detail_text}"
    emails = sorted({email.rstrip(".") for email in EMAIL.findall(everything)})
    for link in listing.links:
        if link.lower().startswith("mailto:"):
            address = link[7:].split("?")[0]
            if address and address not in emails:
                emails.append(address)
    forms = [link for link in listing.links if not link.lower().startswith("mailto:")]

    return {
        "rubriques": sections,
        "a_envoyer": lists.get("À envoyer", []),
        "ages": ages,
        "contact": {
            "emails": emails,
            "formulaires": forms,
            "reserve_abonnes": "reserve aux abonnes" in canonical(everything),
            "comment": apply_note,
            "annonce": listing.url,
        },
    }
