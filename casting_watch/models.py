from dataclasses import dataclass
from datetime import datetime, timezone

# Debut de ligne marquant un intertitre dans detail_text.
HEADING_MARK = "## "


@dataclass(frozen=True)
class RawListing:
    source_name: str
    source_url: str
    title: str
    url: str
    text: str  # texte utilise pour le filtrage (carte + page de l'annonce)
    fetched_at: str
    detail_text: str = ""  # texte de la page de l'annonce, une ligne par paragraphe
    links: tuple[str, ...] = ()  # liens utiles trouves dans l'annonce (mailto, formulaires...)


@dataclass(frozen=True)
class MatchDecision:
    accepted: bool
    score: int
    reasons: list[str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
