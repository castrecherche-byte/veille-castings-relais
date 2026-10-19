"""Lecture des posts des comptes Instagram suivis, via l'API officielle de Meta (Business Discovery).

Aucune page Instagram n'est lue directement : seules les donnees que l'API officielle renvoie au compte
Instagram professionnel de l'utilisateur sont utilisees. Fonctionne pour les comptes suivis qui sont
eux-memes professionnels (Entreprise ou Createur), ce qui est le cas de la plupart des directeurs de casting.
"""
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from casting_watch.filters import canonical
from casting_watch.models import RawListing, utc_now_iso

GRAPH = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v25.0"
INSTAGRAM_SOURCE_URL = "instagram-api"
INSTAGRAM_APPLY_NOTE = "Suivre les consignes du post Instagram (mail indiqué ou message privé au compte)."
CASTING_WORDS = re.compile(
    r"casting|recherch|cherchons|on cherche|figura|\brole|audition|candidat|silhouette|tournage|self ?tape"
)
MEDIA_FIELDS = "caption,permalink,timestamp,media_type"


class InstagramError(RuntimeError):
    pass


def _graph_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return f"erreur HTTP {response.status_code}"
    code, subcode, message = error.get("code"), error.get("error_subcode"), error.get("message", "")
    if code == 190:
        return "le jeton Instagram a expiré ou n'est plus valable : renouvelez-le dans « Connexion Instagram »"
    if code in (4, 17, 32, 613):
        return "trop de requêtes vers Instagram pour le moment, nouvel essai au prochain passage"
    if code == 110 or subcode == 2207013 or "cannot find user" in message.lower():
        return "compte introuvable ou non professionnel (l'API ne lit que les comptes Entreprise/Créateur)"
    if code == 10 or code == 200:
        return f"permission manquante dans l'application Meta ({message})"
    return message or f"erreur HTTP {response.status_code}"


class GraphClient:
    def __init__(self, token: str, api_version: str = DEFAULT_API_VERSION, transport: httpx.BaseTransport | None = None):
        self.token = token
        self.base = f"{GRAPH}/{api_version}"
        self.client = httpx.Client(timeout=20, transport=transport)

    def get(self, path: str, **params) -> dict:
        response = self.client.get(f"{self.base}/{path.lstrip('/')}", params={**params, "access_token": self.token})
        if response.status_code != 200:
            raise InstagramError(_graph_error(response))
        return response.json()

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def find_instagram_accounts(client: GraphClient) -> list[dict]:
    """Comptes Instagram professionnels relies aux Pages Facebook du jeton : [{"id", "username", "page"}]."""
    data = client.get("me/accounts", fields="name,instagram_business_account{id,username}", limit=50)
    found = []
    for page in data.get("data", []):
        account = page.get("instagram_business_account")
        if account:
            found.append({"id": account["id"], "username": account.get("username", ""), "page": page.get("name", "")})
    return found


def exchange_long_lived_token(short_token: str, app_id: str, app_secret: str,
                              api_version: str = DEFAULT_API_VERSION,
                              transport: httpx.BaseTransport | None = None) -> tuple[str, str | None]:
    """Jeton de courte duree -> jeton de 60 jours. Retourne (jeton, date d'expiration ISO ou None)."""
    with httpx.Client(timeout=20, transport=transport) as client:
        response = client.get(
            f"{GRAPH}/{api_version}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            },
        )
    if response.status_code != 200:
        raise InstagramError(_graph_error(response))
    payload = response.json()
    expires_in = payload.get("expires_in")
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).replace(microsecond=0).isoformat()
        if expires_in else None
    )
    return payload["access_token"], expires_at


def account_posts(client: GraphClient, ig_user_id: str, username: str, limit: int = 12) -> list[dict]:
    fields = f"business_discovery.username({username}){{username,media.limit({limit}){{{MEDIA_FIELDS}}}}}"
    data = client.get(ig_user_id, fields=fields)
    return data.get("business_discovery", {}).get("media", {}).get("data", [])


def hashtag_posts(client: GraphClient, ig_user_id: str, hashtag: str, limit: int = 50) -> list[dict]:
    """Posts publics des dernieres 24 h pour un hashtag (Meta limite a 30 hashtags differents par semaine)."""
    found = client.get("ig_hashtag_search", user_id=ig_user_id, q=hashtag).get("data", [])
    if not found:
        return []
    data = client.get(f"{found[0]['id']}/recent_media", user_id=ig_user_id, fields=MEDIA_FIELDS, limit=limit)
    return data.get("data", [])


def check_connection(settings: dict, transport: httpx.BaseTransport | None = None) -> str:
    """'' si l'acces Instagram repond, sinon le message d'erreur."""
    token, ig_user_id = settings.get("access_token"), settings.get("ig_user_id")
    if not token or not ig_user_id:
        return "non connecté"
    try:
        with GraphClient(token, settings.get("api_version") or DEFAULT_API_VERSION, transport) as client:
            client.get(ig_user_id, fields="username")
    except InstagramError as error:
        return str(error)
    except httpx.HTTPError:
        return "Instagram injoignable pour le moment (connexion internet ?)"
    return ""


def post_to_listing(source: str, post: dict, fetched_at: str) -> RawListing | None:
    caption = (post.get("caption") or "").strip()
    url = post.get("permalink")
    if not caption or not url:
        return None
    lines = [" ".join(line.split()) for line in caption.splitlines() if line.strip()]
    return RawListing(
        source_name=f"Instagram {source}",
        source_url=INSTAGRAM_SOURCE_URL,
        title=lines[0][:160],
        url=url,
        text=" ".join(lines)[:8000],
        fetched_at=fetched_at,
        detail_text="\n".join(lines)[:8000],
    )


def collect_instagram(
    settings: dict,
    usernames: list[str],
    max_post_age_days: int = 45,
    posts_per_account: int = 12,
    transport: httpx.BaseTransport | None = None,
    pause_seconds: float = 0.5,
    hashtags: list[str] | tuple[str, ...] = (),
) -> tuple[list[RawListing], list[str]]:
    """Posts recents des comptes suivis et des hashtags surveilles qui parlent de casting.
    Retourne (annonces, erreurs)."""
    token, ig_user_id = settings.get("access_token"), settings.get("ig_user_id")
    if not (usernames or hashtags) or not token or not ig_user_id:
        return [], []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_post_age_days)
    fetched_at = utc_now_iso()
    listings: list[RawListing] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    targets = [f"@{name}" for name in usernames] + [f"#{tag}" for tag in hashtags]
    with GraphClient(token, settings.get("api_version") or DEFAULT_API_VERSION, transport) as client:
        for index, target in enumerate(targets):
            if index and pause_seconds:
                time.sleep(pause_seconds)
            try:
                if target.startswith("@"):
                    posts = account_posts(client, ig_user_id, target[1:], posts_per_account)
                else:
                    posts = hashtag_posts(client, ig_user_id, target[1:])
            except InstagramError as error:
                errors.append(f"Instagram {target}: {error}")
                if "jeton" in str(error):
                    break  # inutile d'essayer les autres comptes
                continue
            except httpx.HTTPError as error:
                errors.append(f"Instagram {target}: {error}")
                continue
            for post in posts:
                if post.get("permalink") in seen_urls:
                    continue  # meme post trouve via un compte et un hashtag
                try:
                    published = datetime.fromisoformat(post.get("timestamp", "").replace("+0000", "+00:00"))
                except ValueError:
                    published = None
                if published and published < cutoff:
                    continue
                if not CASTING_WORDS.search(canonical(post.get("caption") or "")):
                    continue
                listing = post_to_listing(target, post, fetched_at)
                if listing:
                    seen_urls.add(listing.url)
                    listings.append(listing)
    return listings, errors
