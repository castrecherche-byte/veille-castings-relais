"""Recherche web automatique de l'annonce d'origine, via l'API officielle de Tavily (cle gratuite).

Tavily offre 1 000 recherches gratuites par mois. On ne cherche qu'une fois par annonce, et seulement
pour les annonces payantes ; les resultats sont gardes en base. Seules les pistes gratuites sont proposees.
"""
import re
from urllib.parse import urlparse

import httpx

from casting_watch.filters import canonical
from casting_watch.instagram import CASTING_WORDS
from casting_watch.origine import (
    DATE,
    FREE_WITH_ACCOUNT_SITES,
    GENERIC_TITLE_WORDS,
    PAID_INSTAGRAM_ACCOUNTS,
    PAID_SITES,
    WORD,
    free_with_account_label,
    paid_site_mentioned,
)

TAVILY_URL = "https://api.tavily.com/search"
# Mots d'une vraie annonce (et pas d'un article qui parle du « casting » d'un film).
AD_WORDS = re.compile(r"recherch|postul|candidat|envoy|inscri|audition|self ?tape|profil|disponib|remuner|cachet|\bage\b")
# Sites de cinema/tele : leurs pages « casting » listent les acteurs d'un film, ce ne sont pas des annonces.
MEDIA_DOMAINS = (
    "allocine.fr", "programme-tv.net", "senscritique.com", "tf1.fr", "canalplus.com", "imdb.com", "wikipedia.org",
    "lemediaplus.com", "telerama.fr", "premiere.fr", "france.tv", "telestar.fr", "toutelatele.com", "book.fr",
)


class WebSearchError(RuntimeError):
    pass


def tavily_search(key: str, query: str, max_results: int = 8, transport: httpx.BaseTransport | None = None) -> list[dict]:
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "search_depth": "basic", "max_results": max_results, "country": "france"},
        )
    if response.status_code == 401:
        raise WebSearchError("clé Tavily invalide : vérifiez-la dans « Profil, Gmail et Instagram »")
    if response.status_code in (432, 433):
        raise WebSearchError("quota mensuel Tavily atteint : la recherche web reprendra le mois prochain")
    if response.status_code == 429:
        raise WebSearchError("trop de recherches web d'un coup, nouvel essai au prochain passage")
    if response.status_code != 200:
        raise WebSearchError(f"recherche web impossible (erreur {response.status_code})")
    return response.json().get("results", [])


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


SOCIAL_DOMAINS = ("instagram.com", "facebook.com", "fb.com")


def describe_site(domain: str) -> tuple[int, str]:
    if is_social(domain):
        return 0, "réseau social, contact souvent dans le post"
    if "castprod" in domain:
        return 1, "gratuit, avec ton compte Castprod"
    if "allcasting" in domain:
        return 1, "gratuit, bouton « Postuler maintenant »"
    for site, name in FREE_WITH_ACCOUNT_SITES.items():
        if site in domain:
            return 1, free_with_account_label(name)
    return 2, "site inconnu : gratuit ou payant, à vérifier"


def is_paid(url: str, text: str) -> bool:
    """Site d'annonces payant, compte Instagram d'un tel site, ou post qui renvoie vers lui pour postuler."""
    domain = domain_of(url)
    if any(site in domain for site in PAID_SITES):
        return True
    first = next((p.lower() for p in urlparse(url).path.split("/") if p), "")
    return is_social(domain) and (first in PAID_INSTAGRAM_ACCOUNTS or bool(paid_site_mentioned(text)))


def is_social(domain: str) -> bool:
    return any(domain == social or domain.endswith("." + social) for social in SOCIAL_DOMAINS)


def points_to_post(url: str) -> bool:
    """Lien vers le post lui-meme (et non vers la page d'un groupe ou d'un compte, ou il faut chercher)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not is_social(domain_of(url)):
        return True
    return any(p in ("p", "reel", "reels", "posts", "permalink", "videos", "photos", "story.php") for p in parts) or (
        "story_fbid" in url or "permalink.php" in url
    )


def keep_relevant(results: list[dict], query: str, listing_url: str, limit: int = 5) -> list[dict]:
    """Garde les resultats qui ressemblent a la meme annonce : ils citent au moins 2 mots distinctifs de la
    recherche (nom du film, date...) et parlent de casting. Ecarte le site d'origine, les sites payants, les
    fiches « casting du film » des sites de cinema/tele et les pages d'accueil des sites d'annonces."""
    own = domain_of(listing_url)
    date = DATE.search(query)
    date_words = set(WORD.findall(canonical(date.group(0)))) if date else set()
    keywords = {w for w in WORD.findall(canonical(query)) if w not in GENERIC_TITLE_WORDS and w not in date_words}
    if not keywords and not date:
        return []  # recherche trop vague : les resultats ne seraient que du bruit
    needed = min(2, len(keywords))
    kept = []
    for result in results:
        url = result.get("url") or ""
        domain = domain_of(url)
        text = result.get("content") or result.get("extrait") or ""
        if not url.startswith("http") or domain == own or is_paid(url, f"{result.get('title', '')} {text}"):
            continue
        if any(domain.endswith(media) for media in MEDIA_DOMAINS):
            continue
        if not is_social(domain) and len([p for p in urlparse(url).path.split("/") if p]) <= 1 and "?" not in url:
            continue  # page d'accueil ou rubrique d'un site d'annonces, pas une annonce
        haystack = canonical(f"{result.get('title', '')} {text}")
        same_date = bool(date and canonical(date.group(0)) in haystack)
        if not same_date and (not keywords or sum(word in haystack for word in keywords) < needed):
            continue
        if not CASTING_WORDS.search(haystack) or not AD_WORDS.search(haystack):
            continue  # pas une annonce : homonyme, article de presse, liste d'acteurs...
        level, label = describe_site(domain)
        kept.append(
            {
                "title": (result.get("title") or domain)[:160],
                "url": url,
                "site": domain,
                "acces": label,
                "niveau": level,
                "meme_date": same_date,
                "direct": points_to_post(url),
                "extrait": " ".join(text.split())[:240],
            }
        )
    # La meme date de tournage d'abord, puis les reseaux sociaux, puis l'ordre de Tavily.
    kept.sort(key=lambda item: (not item["meme_date"], item["niveau"], not item["direct"]))
    return kept[:limit]


def trim(results: list[dict]) -> list[dict]:
    """Ce qu'on garde des resultats bruts (le filtre est applique a l'affichage, donc ajustable sans re-chercher)."""
    return [
        {"title": r.get("title") or "", "url": r.get("url") or "", "content": " ".join((r.get("content") or "").split())[:600]}
        for r in results
    ]
