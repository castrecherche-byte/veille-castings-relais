import re
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import url2pathname
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Tag

from casting_watch.models import HEADING_MARK, RawListing, utc_now_iso


class SourceBlocked(RuntimeError):
    pass


def resolve_file_url(url: str, config_dir: str) -> Path:
    """Accepte file://chemin/relatif (relatif au dossier de config) et file:///C:/chemin/absolu."""
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        # Forme relative historique : file://tests/fixtures/x.html
        relative = unquote(parsed.netloc + parsed.path)
        return (Path(config_dir) / relative).resolve()
    path = Path(url2pathname(parsed.path))
    if not path.is_absolute():
        path = Path(config_dir) / path
    return path.resolve()


class Fetcher:
    """Client HTTP poli : robots.txt verifie pour chaque URL, pause entre deux requetes."""

    def __init__(
        self,
        timeout: int,
        user_agent: str,
        delay_seconds: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.user_agent = user_agent
        self.delay_seconds = delay_seconds
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            transport=transport,
        )
        self._robots: dict[str, RobotFileParser | bool] = {}
        self._last_request = 0.0

    def allowed(self, url: str) -> bool:
        """Respecte robots.txt. Erreur serveur/reseau ou acces refuse : on s'abstient."""
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._robots:
            response = None
            try:
                response = self.client.get(f"{host}/robots.txt")
            except httpx.HTTPError:
                pass
            if response is None or response.status_code in (401, 403) or response.status_code >= 500:
                self._robots[host] = False
            elif response.status_code >= 400:
                self._robots[host] = True
            else:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                self._robots[host] = parser
        rules = self._robots[host]
        return rules.can_fetch(self.user_agent, url) if isinstance(rules, RobotFileParser) else rules

    def get(self, url: str) -> str:
        if not self.allowed(url):
            raise SourceBlocked(f"robots.txt n'autorise pas {url}")
        wait = self.delay_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.client.get(url)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def fetch_source(
    source: dict,
    config_dir: str,
    timeout: int,
    user_agent: str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    url = source["url"]
    if urlparse(url).scheme == "file":
        if not source.get("allow_file_urls", False):
            raise ValueError(f"File URLs are disabled for source {source['name']}")
        return resolve_file_url(url, config_dir).read_text(encoding="utf-8")
    with Fetcher(timeout, user_agent, delay_seconds=0, transport=transport) as fetcher:
        return fetcher.get(url)


def _clean(node: Tag) -> str:
    return " ".join(node.get_text(" ", strip=True).split())


def extract_listings(source: dict, html: str) -> list[RawListing]:
    """Extraction generique (sans link_pattern) : blocs de texte avec un lien."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select("article, .casting, .annonce, .job, li, div")
    if not candidates:
        candidates = soup.select("body")

    listings: list[RawListing] = []
    seen_urls: set[str] = set()
    fetched_at = utc_now_iso()

    for node in candidates:
        text = _clean(node)
        if len(text) < 40:
            continue

        link = node.find("a", href=True)
        url = urljoin(source["url"], link["href"]) if link else source["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = ""
        heading = node.find(["h1", "h2", "h3", "h4"])
        if heading:
            title = heading.get_text(" ", strip=True)
        elif link:
            title = link.get_text(" ", strip=True)
        if not title:
            title = text[:90]

        listings.append(
            RawListing(
                source_name=source["name"],
                source_url=source["url"],
                title=title[:240],
                url=url,
                text=text[:5000],
                fetched_at=fetched_at,
            )
        )

    return listings


def extract_cards(source: dict, html: str) -> list[RawListing]:
    """Une annonce = le plus grand bloc qui contient un seul lien vers une annonce (link_pattern)."""
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(source["link_pattern"])
    skip_selector = source.get("skip_card_selector")
    skip_url = re.compile(source["skip_url_pattern"]) if source.get("skip_url_pattern") else None
    fetched_at = utc_now_iso()

    def listing_url(anchor: Tag) -> str | None:
        url = urljoin(source["url"], anchor["href"]).split("#")[0]
        return url if pattern.search(url) else None

    listings: list[RawListing] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = listing_url(anchor)
        if not url or url in seen:
            continue
        seen.add(url)

        card = anchor
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]"):
            urls_in_parent = {
                u for a in card.parent.find_all("a", href=True) if (u := listing_url(a))
            }
            if len(urls_in_parent) > 1:
                break
            card = card.parent

        if skip_selector and (card.css.match(skip_selector) or card.select_one(skip_selector)):
            continue
        if skip_url and skip_url.search(url):
            continue

        heading = card.find(["h1", "h2", "h3", "h4"])
        if heading:
            title = _clean(heading)
        else:
            texts = [_clean(a) for a in card.find_all("a", href=True) if listing_url(a) == url]
            title = max(texts, key=len, default="")
        text = _clean(card)
        listings.append(
            RawListing(
                source_name=source["name"],
                source_url=source["url"],
                title=(title or text[:90])[:240],
                url=url,
                text=text[:5000],
                fetched_at=fetched_at,
            )
        )
    return listings


BLOCK_TAGS = [
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "div", "tr", "dt", "dd",
    "section", "article", "ul", "ol", "table", "figure", "blockquote",
]
SHARE_LINK = re.compile(r"facebook\.com/sharer|twitter\.com/intent|x\.com/intent|linkedin\.com/share|whatsapp|pinterest", re.I)
APPLY_LINK = re.compile(r"forms\.gle|docs\.google\.com/forms|typeform|jotform|tally\.so|airtable|framaforms|cognitoforms", re.I)


def extract_detail(html: str, selector: str, page_url: str, stop_pattern: str | None = None) -> tuple[str, tuple[str, ...]]:
    """Texte de l'annonce (une ligne par paragraphe) et liens utiles (mail, formulaire de candidature)."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(selector)
    if node is None:
        return "", ()
    for tag in node(["script", "style", "noscript", "svg", "form"]):
        tag.decompose()

    links: list[str] = []
    for anchor in node.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:") or (APPLY_LINK.search(href) and not SHARE_LINK.search(href)):
            url = href if href.lower().startswith("mailto:") else urljoin(page_url, href)
            if url not in links:
                links.append(url)

    for br in node.find_all("br"):
        br.replace_with("\n")
    for heading in node.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        heading.insert(0, f"\n{HEADING_MARK}")  # les intertitres terminent les listes dans la fiche
    for tag in node.find_all(BLOCK_TAGS):
        tag.append("\n")
    stop = re.compile(stop_pattern, re.I) if stop_pattern else None
    lines: list[str] = []
    for raw in node.get_text(" ").split("\n"):
        line = " ".join(raw.split())
        if not line or line == HEADING_MARK.strip():
            continue
        if stop and stop.search(line.removeprefix(HEADING_MARK)):
            break
        if not lines or lines[-1] != line:
            lines.append(line)
    return "\n".join(lines), tuple(links)


def collect_sources(
    config: dict,
    known: dict[str, RawListing] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[list[RawListing], list[str]]:
    """Retourne (annonces, erreurs). Une source en echec n'interrompt pas les autres.

    known : annonces dont la page de detail est deja en base, pour ne pas la retelecharger.
    """
    known = known or {}
    listings: list[RawListing] = []
    errors: list[str] = []
    timeout = int(config.get("request_timeout_seconds", 20))
    user_agent = config.get("user_agent", "CastingWatchMVP/0.1")
    delay = float(config.get("request_delay_seconds", 1.0))

    with Fetcher(timeout, user_agent, delay, transport) as fetcher:
        for source in config.get("sources", []):
            if not source.get("enabled", True):
                continue
            name = source.get("name", source.get("url"))
            try:
                if urlparse(source["url"]).scheme == "file":
                    html = fetch_source(source, config["_config_dir"], timeout, user_agent)
                else:
                    html = fetcher.get(source["url"])
            except (OSError, ValueError, httpx.HTTPError, SourceBlocked) as error:
                errors.append(f"{name}: {error}")
                continue

            if not source.get("link_pattern"):
                listings.extend(extract_listings(source, html))
                continue

            cards = extract_cards(source, html)
            if not cards:
                errors.append(f"{name}: aucune annonce trouvee (la structure du site a peut-etre change)")
            detail_selector = source.get("detail_selector")
            budget = int(source.get("max_details_per_run", 40))
            for card in cards:
                if detail_selector:
                    previous = known.get(card.url)
                    if previous is not None:
                        card = _with_detail(card, previous.detail_text, previous.links)
                    elif budget > 0:
                        budget -= 1
                        try:
                            html = fetcher.get(card.url)
                            detail, links = extract_detail(html, detail_selector, card.url, source.get("detail_stop"))
                            card = _with_detail(card, detail, links)
                        except (httpx.HTTPError, SourceBlocked) as error:
                            errors.append(f"{name}: detail {card.url}: {error}")
                listings.append(card)
    return listings, errors


def _with_detail(listing: RawListing, detail_text: str, links: tuple[str, ...]) -> RawListing:
    flat = " ".join(detail_text.replace(HEADING_MARK, "").split())
    return RawListing(
        source_name=listing.source_name,
        source_url=listing.source_url,
        title=listing.title,
        url=listing.url,
        text=f"{listing.text} {flat}"[:8000],
        fetched_at=listing.fetched_at,
        detail_text=detail_text[:8000],
        links=links,
    )
