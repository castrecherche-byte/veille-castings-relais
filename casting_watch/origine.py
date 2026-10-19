"""Retrouver une annonce ailleurs que sur un site payant.

Deux aides, sans rien telecharger de plus :
- les autres versions de la meme annonce deja en base (un autre site, un post Instagram) ;
- des liens de recherche tout prets (Google, Instagram et Facebook via Google), que l'on ouvre soi-meme.
"""
import math
import re
from collections import Counter
from urllib.parse import quote_plus

from casting_watch.filters import canonical

WORD = re.compile(r"[a-z]{3,}")
# Une meme annonce partage au moins 3 mots tres rares (noms du film, du role, details du profil...) :
# des mots presents dans 3 annonces au plus de toute la base.
MIN_SHARED_RARE_WORDS = 3
RARE_MAX_LISTINGS = 3

# Comment obtenir le contact, du plus accessible au moins accessible. Niveau 3 = payant pour postuler.
PAID = 3
PAID_LABEL = "payant pour postuler"
ACCESS_LEVELS = [
    ("Instagram", 0, "gratuit, contact dans le post"),
    ("Facebook", 0, "gratuit, contact dans le post"),
    ("Ajout manuel", 0, "gratuit, contact dans le message"),
    ("Newsletter", 0, "gratuit, reçu par newsletter"),
    ("Castprod", 1, "gratuit, avec ton compte Castprod"),
    ("Allcasting", 1, "gratuit, bouton « Postuler maintenant »"),
    ("Casting.fr", PAID, PAID_LABEL),
    ("OnTourne", PAID, PAID_LABEL),
    ("Figurants.com", PAID, PAID_LABEL),
]
# Sites d'annonces payants : leurs pages, leurs comptes Instagram et les posts qui renvoient vers eux.
PAID_SITES = {"ontourne": "OnTourne", "casting.fr": "Casting.fr", "figurants.com": "Figurants.com"}
# Comptes Instagram / pages Facebook de ces sites (meme nom dans le lien).
PAID_INSTAGRAM_ACCOUNTS = {"castingsfigurants": "Figurants.com", "castingsfigurations": "Figurants.com"}
PAID_MENTION = re.compile(r"figurants\s*\.\s*com|ontourne|\bcasting\s*\.\s*fr\b", re.IGNORECASE)


def paid_site_mentioned(text: str) -> str:
    """Nom du site payant vers lequel renvoie un post (« postulez sur figurants.com »), sinon ''."""
    found = PAID_MENTION.search(text or "")
    if not found:
        return ""
    key = re.sub(r"\s+", "", found.group(0).lower())
    return next((name for site, name in PAID_SITES.items() if site in key), "")

GENERIC_TITLE_WORDS = set(
    """
    casting castings figuration figurant figurants figurante figurantes enfant enfants ado ados adolescent adolescente
    adolescents adolescentes fille filles garcon garcons petite petit petites petits jeune jeunes bebe bebes ans pour
    avec dans une des les aux sur entre film films serie series long longs metrage metrages court cinema role roles
    paris ile france region parisienne tournage recherche recherches recherchee recherchees recherchons profil profils
    urgent the and comedienne comediennes comedien comediens plusieurs prochain nouveau nouvelle origine tous toutes
    mere pere duo ete est sont une principal principaux principale secondaire secondaires silhouette silhouettes
    rue fait faisant parlant tele emission publicite publicitaire pub clip video photo shooting mannequin mannequins
    son sa ses environ deux trois homme hommes femme femmes tournage tournages tourne tournee projet projets ville
    vous nous votre vos leur leurs tres plus aussi etre avoir doit sera seront peut
    """.split()
)


# Sites gratuits ou il faut un compte (gratuit) pour voir le contact, et leurs comptes Instagram.
FREE_WITH_ACCOUNT_SITES = {
    "francecasting": "France Casting",
    "france-casting": "France Casting",
    "shootingmode": "ShootingMode",
}
FREE_WITH_ACCOUNT_INSTAGRAM = {"france.casting": "France Casting"}


def free_with_account_label(site: str) -> str:
    return f"gratuit, avec un compte {site} (à créer si besoin)"


def access_level(source_name: str, text: str = "") -> tuple[int, str]:
    handle = source_name.split("@", 1)[1].strip().lower() if "@" in source_name else ""
    if handle in PAID_INSTAGRAM_ACCOUNTS:
        return PAID, f"{PAID_LABEL} (compte de {PAID_INSTAGRAM_ACCOUNTS[handle]})"
    if handle in FREE_WITH_ACCOUNT_INSTAGRAM:
        return 1, free_with_account_label(FREE_WITH_ACCOUNT_INSTAGRAM[handle])
    for prefix, level, label in ACCESS_LEVELS:
        if source_name.startswith(prefix):
            if level == 0 and paid_site_mentioned(text):
                return PAID, f"{PAID_LABEL} (le post renvoie vers {paid_site_mentioned(text)})"
            return level, label
    return 2, "accès à vérifier"


def site_of(source_name: str) -> str:
    return re.split(r" \(| @| #", source_name)[0]


def listing_words(title: str, text: str) -> set[str]:
    return set(WORD.findall(canonical(f"{title} {text}")))


class OriginFinder:
    """Index de toutes les annonces en base, pour retrouver les autres versions d'une annonce."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.words = [listing_words(row["title"], row["text"]) for row in rows]
        self.df = Counter(word for words in self.words for word in words)
        total = max(1, len(rows))
        self.idf = {word: math.log(total / count) for word, count in self.df.items()}

    def twins(self, index: int) -> list[dict]:
        """Autres versions gratuites de l'annonce n° index (autres sites, posts), les plus accessibles d'abord."""
        row, words = self.rows[index], self.words[index]
        found = []
        for other_index, other in enumerate(self.rows):
            if other_index == index or site_of(other["source_name"]) == site_of(row["source_name"]):
                continue
            shared = [w for w in words & self.words[other_index] if self.df[w] <= RARE_MAX_LISTINGS]
            if len(shared) < MIN_SHARED_RARE_WORDS:
                continue
            level, label = access_level(other["source_name"], f"{other['title']} {other['text']}")
            if level >= PAID:
                continue  # une autre version payante n'aide pas
            found.append(
                {
                    "title": other["title"],
                    "url": other["url"],
                    "source": other["source_name"],
                    "acces": label,
                    "niveau": level,
                    "score": sum(self.idf[w] for w in shared),
                }
            )
        found.sort(key=lambda twin: (twin["niveau"], -twin["score"]))
        return found

    def search_query(self, index: int) -> str:
        """'casting' + les mots les plus distinctifs du titre (nom du film, de la serie, d'un acteur...)."""
        candidates = []
        for raw in re.findall(r"[^\s«»\"“”:;,!?()+.]+", self.rows[index]["title"]):
            useful = [w for w in WORD.findall(canonical(raw)) if w not in GENERIC_TITLE_WORDS]
            if useful and raw not in (c[1] for c in candidates):
                candidates.append((max(self.idf.get(w, 0) for w in useful), raw))
        best = sorted(candidates, key=lambda c: -c[0])[:4]
        words = [raw for _, raw in candidates if any(raw == b for _, b in best)]
        if len(words) >= 2:
            return " ".join(["casting"] + words)
        # Titre sans nom propre : le titre lui-meme (sans les petits mots) et la date de tournage.
        title_words = [
            raw for raw in re.findall(r"[^\s«»\"“”:;,!?()+]+", self.rows[index]["title"])
            if canonical(raw) not in SMALL_WORDS
        ]
        date = DATE.search(self.rows[index]["text"])
        return " ".join(["casting"] + title_words + ([date.group(0)] if date else []))


SMALL_WORDS = set("casting pour le la les de du des un une a en et avec dans entre au aux sur d l".split())
MONTHS = "janvier|fevrier|février|mars|avril|mai|juin|juillet|aout|août|septembre|octobre|novembre|decembre|décembre"
DATE = re.compile(rf"\b\d{{1,2}}(?:er)? (?:{MONTHS})(?: \d{{4}})?", re.IGNORECASE)


def search_links(query: str) -> list[dict]:
    google = "https://www.google.com/search?q="
    return [
        {"label": "Google", "url": google + quote_plus(query)},
        {"label": "Instagram", "url": google + quote_plus(f"{query} site:instagram.com")},
        {"label": "Facebook", "url": google + quote_plus(f"{query} site:facebook.com")},
    ]
