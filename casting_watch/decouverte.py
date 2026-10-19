"""Decouverte de comptes Instagram de casting enfants (chaque jour).

0. Les comptes cites (@nom) dans les posts des comptes deja suivis sont verifies en premier (gratuit).
1. Quelques recherches web (Tavily), differentes chaque jour, font ressortir des comptes Instagram de
   directeurs/directrices de casting.
2. Chaque compte est verifie avec l'API officielle Instagram (Business Discovery) : on lit ses derniers posts
   et on compte ceux qui sont des castings avec des enfants (et en Ile-de-France).
3. Les comptes cites (@nom) dans les posts des bons comptes sont verifies aussi : souvent d'autres directeurs.
Seuls les comptes qui publient vraiment des castings enfants sont proposes.
"""
import re
from collections import Counter

import httpx

from casting_watch.filters import canonical
from casting_watch.instagram import DEFAULT_API_VERSION, GraphClient, InstagramError
from casting_watch.origine import PAID_INSTAGRAM_ACCOUNTS, paid_site_mentioned
from casting_watch.websearch import WebSearchError, tavily_search

# Recherches web, utilisees a tour de role : QUERIES_PER_DAY par jour, pour trouver du nouveau chaque jour.
QUERIES = [
    "directrice de casting enfants Paris",
    "directeur de casting enfants figuration Île-de-France",
    "casting enfants recherche fille 10 ans Paris",
    "casting sauvage enfants directrice de casting",
    "directrice de casting cinéma Paris",
    "directeur de casting série télé France",
    "assistante casting recherche enfants tournage",
    "casting director Paris film kids",
    "casting enfants publicité Paris recherche",
    "casting ado collège lycée long-métrage",
    "chargée de casting figuration Paris",
    "casting sauvage Paris rôle jeune fille",
    "directrice de casting court-métrage enfants",
    "casting enfants série plateforme Netflix France",
    "casting fratrie enfants tournage Île-de-France",
]
QUERIES_PER_DAY = 5
POSTS_READ = 25
MIN_KIDS_CASTINGS = 2  # au moins 2 castings enfants dans les 25 derniers posts
# Un compte sans « casting » dans son nom, son pseudo ou sa bio doit en publier beaucoup pour etre propose.
MIN_KIDS_CASTINGS_WITHOUT_CASTING_PROFILE = 5
CASTING_PROFILE = re.compile(r"casting|cast\b|direct(?:eur|rice|or) de|figuration|figurant")


def queries_for_day(day_number: int) -> list[str]:
    """Les QUERIES_PER_DAY recherches du jour, a tour de role dans QUERIES."""
    start = (day_number * QUERIES_PER_DAY) % len(QUERIES)
    return [QUERIES[(start + offset) % len(QUERIES)] for offset in range(QUERIES_PER_DAY)]

NOT_USERS = {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "developer", "legal", "direct"}
HANDLE_IN_URL = re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{2,30})/?")
MENTION = re.compile(r"@([A-Za-z0-9._]{3,30})")
CASTING = re.compile(r"casting|recherch|figura|\brole|audition|self ?tape")
KIDS = re.compile(r"enfant|fille|garcon|\bados?\b|\b\d{1,2} ?(?:a|-) ?\d{1,2} ans|\b[4-9] ans|\b1[0-4] ans|kids|mineur")
IDF = re.compile(r"paris|ile de france|\bidf\b|region parisienne|\b9[1-5]\b|\b7[578]\b")


def clean_handle(raw: str) -> str:
    return raw.lower().strip(".")


def handles_in_results(results: list[dict]) -> Counter:
    found = Counter()
    for result in results:
        url_match = HANDLE_IN_URL.match(result.get("url") or "")
        if url_match and url_match.group(1).lower() not in NOT_USERS:
            found[clean_handle(url_match.group(1))] += 2
        for handle in MENTION.findall(f"{result.get('title', '')} {result.get('content', '')}"):
            found[clean_handle(handle)] += 1
    return found


def check_account(client: GraphClient, ig_user_id: str, handle: str) -> dict | None:
    """Chiffres d'un compte professionnel ; None s'il n'existe pas ou n'est pas professionnel."""
    fields = (
        f"business_discovery.username({handle})"
        f"{{username,name,followers_count,biography,media.limit({POSTS_READ}){{caption,timestamp}}}}"
    )
    try:
        data = client.get(ig_user_id, fields=fields).get("business_discovery", {})
    except InstagramError:
        return None
    posts = data.get("media", {}).get("data", [])
    # Les posts qui renvoient vers un site payant (« postulez sur figurants.com ») ne comptent pas.
    captions = [canonical(post.get("caption") or "") for post in posts if not paid_site_mentioned(post.get("caption"))]
    castings = [caption for caption in captions if CASTING.search(caption)]
    kids = [caption for caption in castings if KIDS.search(caption)]
    mentions = {clean_handle(h) for post in posts for h in MENTION.findall(post.get("caption") or "")}
    return {
        "username": data.get("username", handle).lower(),
        "name": data.get("name") or "",
        "followers": data.get("followers_count") or 0,
        "bio": " ".join((data.get("biography") or "").split())[:200],
        "castings": len(castings),
        "enfants": len(kids),
        "idf": sum(1 for caption in kids if IDF.search(caption)),
        "dernier_post": (posts[0].get("timestamp") or "")[:10] if posts else "",
        "mentions": mentions,
    }


def worth_proposing(account: dict) -> bool:
    """Un vrai compte de casting qui publie des castings enfants, sans renvoyer vers un site payant."""
    if not account or paid_site_mentioned(account["bio"]):
        return False
    bio = MENTION.sub(" ", account["bio"])  # « co-fondatrice de @castingfr » ne fait pas d'elle une directrice de casting
    casting_profile = CASTING_PROFILE.search(canonical(f"{account['username']} {account['name']} {bio}"))
    needed = MIN_KIDS_CASTINGS if casting_profile else MIN_KIDS_CASTINGS_WITHOUT_CASTING_PROFILE
    return account["enfants"] >= needed


def mentions_in_texts(texts: list[str]) -> Counter:
    """Comptes cites (@nom) dans des textes deja lus, par exemple les posts des comptes suivis."""
    return Counter(clean_handle(handle) for text in texts for handle in MENTION.findall(text or ""))


def discover(
    web_key: str,
    ig_settings: dict,
    known: set[str],
    max_checks: int = 30,
    web_transport: httpx.BaseTransport | None = None,
    ig_transport: httpx.BaseTransport | None = None,
    queries: list[str] | None = None,
    cited: Counter | None = None,
) -> dict:
    """Retourne {"comptes": [...comptes a proposer], "recherches_web": n, "verifies": n, "erreur": ""}.
    `cited` : comptes deja cites dans les posts des comptes suivis (verifies en premier, sans recherche web)."""
    report = {"comptes": [], "recherches_web": 0, "verifies": 0, "erreur": ""}
    candidates = Counter()
    for query in QUERIES if queries is None else queries:
        try:
            results = tavily_search(web_key, f"{query} site:instagram.com", max_results=20, transport=web_transport)
        except (WebSearchError, httpx.HTTPError) as error:
            report["erreur"] = str(error)
            break
        report["recherches_web"] += 1
        candidates.update(handles_in_results(results))

    checked = set(known)
    queue = [handle for handle, _ in (cited or Counter()).most_common() if handle not in checked]
    queue += [handle for handle, _ in candidates.most_common() if handle not in checked and handle not in queue]
    api_version = ig_settings.get("api_version") or DEFAULT_API_VERSION
    with GraphClient(ig_settings["access_token"], api_version, ig_transport) as client:
        while queue and report["verifies"] < max_checks:
            handle = queue.pop(0)
            if handle in checked or handle in PAID_INSTAGRAM_ACCOUNTS:
                continue
            checked.add(handle)
            account = check_account(client, ig_settings["ig_user_id"], handle)
            report["verifies"] += 1
            if not worth_proposing(account):
                continue
            report["comptes"].append({key: value for key, value in account.items() if key != "mentions"})
            queue += sorted(account["mentions"] - checked)  # les comptes qu'il cite : souvent des confreres
    return report
