import argparse
import hashlib
import imaplib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from casting_watch.config import load_config, resolve_data_path
from casting_watch.candidature import (
    GmailError,
    MINOR_KEY,
    PHOTO_TYPES,
    PROFILE_FIELDS,
    build_body,
    build_draft,
    build_subject,
    missing_fields,
    save_gmail_draft,
    test_gmail,
)
from casting_watch.db import (
    MANUAL_SOURCE_URL,
    applications_by_listing,
    connect,
    delete_profile_row,
    insert_profile,
    list_profiles,
    update_profile,
    count_all,
    delete_followed_account,
    followed_accounts,
    get_meta,
    known_details,
    list_all,
    record_application,
    row_to_listing,
    save_followed_account,
    save_suggestion,
    save_web_origin,
    set_suggestion_status,
    suggestions,
    web_origins,
    set_meta,
    upsert_listing,
)
from casting_watch.instagram import (
    DEFAULT_API_VERSION,
    INSTAGRAM_APPLY_NOTE,
    INSTAGRAM_SOURCE_URL,
    GraphClient,
    check_connection,
    collect_instagram,
    exchange_long_lived_token,
    find_instagram_accounts,
)
from casting_watch.filters import (
    SearchCriteria,
    clean_age,
    clean_gender,
    clean_location,
    evaluate_listing,
    listing_types,
)
from casting_watch.decouverte import discover, mentions_in_texts
from casting_watch.decouverte import queries_for_day as discovery_queries_for_day
from casting_watch.infos import build_info
from casting_watch.newsletters import NEWSLETTER_SOURCE_URL, fetch_newsletter_listings
from casting_watch.origine import OriginFinder, access_level, search_links

NEWSLETTER_APPLY_NOTE = "Suivre les consignes de la newsletter (adresse ou lien indiqué dans l'annonce, voir aussi l'e-mail)."
from casting_watch.websearch import WebSearchError, keep_relevant, tavily_search, trim
from casting_watch.models import HEADING_MARK, RawListing, utc_now_iso
from casting_watch.scraper import collect_sources
from casting_watch.social import collect_social_sources, instagram_enabled


def exclude_terms(config: dict) -> list | None:
    return (config.get("search") or {}).get("exclude_terms")


def run_search(config_path: str) -> dict:
    config = load_config(config_path)
    db_path = resolve_data_path(config, "database_path")
    # Leve SocialConnectorDisabled si Instagram/Facebook sont actives sans acces autorise.
    collect_social_sources(config)
    connection = connect(db_path)
    listings, errors = collect_sources(config, known_details(connection))

    instagram = {"connecte": False, "comptes_lus": 0, "hashtags_lus": 0, "posts_casting": 0}
    usernames = instagram_usernames(connection)
    settings = get_meta(connection, INSTAGRAM_META)
    hashtags = split_hashtags(settings.get("hashtags", DEFAULT_HASHTAGS))
    if instagram_enabled(config) and settings.get("access_token") and (usernames or hashtags):
        options = config["social_extensions"]["instagram"]
        settings["api_version"] = options.get("api_version")
        posts, instagram_errors = collect_instagram(
            settings,
            usernames,
            max_post_age_days=int(options.get("max_post_age_days", 45)),
            posts_per_account=int(options.get("posts_per_account", 12)),
            hashtags=hashtags,
        )
        listings.extend(posts)
        errors.extend(instagram_errors)
        instagram = {
            "connecte": True,
            "comptes_lus": len(usernames),
            "hashtags_lus": len(hashtags),
            "posts_casting": len(posts),
        }

    newsletters = {"connecte": False, "emails": {}}
    gmail = get_meta(connection, GMAIL_META)
    if gmail.get("app_password"):
        try:
            letters, counts = fetch_newsletter_listings(gmail["address"], gmail["app_password"])
        except (GmailError, OSError, imaplib.IMAP4.error) as error:
            errors.append(f"Newsletters (Gmail) : {error}")
        else:
            listings.extend(letters)
            newsletters = {"connecte": True, "emails": counts, "annonces": len(letters)}

    criteria = SearchCriteria.from_config(config)
    accepted = 0
    for listing in listings:
        decision = evaluate_listing(listing, criteria, exclude_terms(config))
        if decision.accepted:
            accepted += 1
        upsert_listing(connection, listing, decision)

    set_meta(connection, RUN_META, {"last_at": utc_now_iso()})
    web = search_web_origins(config_path)
    if web["erreur"]:
        errors.append(f"Recherche web : {web['erreur']}")
    discovery = discover_accounts(config_path)
    if discovery["erreur"]:
        errors.append(f"Découverte de comptes : {discovery['erreur']}")
    counts = count_all(connection)
    return {
        "criteria": criteria.describe(),
        "fetched": len(listings),
        "accepted_this_run": accepted,
        "database_total": counts["total"],
        "database_accepted": counts["accepted"],
        "database_path": str(db_path),
        "instagram": instagram,
        "web": web,
        "newsletters": newsletters,
        "decouverte": discovery,
        "errors": errors,
    }


RUN_META = "run_"
INSTAGRAM_META = "instagram_"


def last_run_at(config: dict) -> datetime | None:
    value = get_meta(connect(resolve_data_path(config, "database_path")), RUN_META).get("last_at")
    return datetime.fromisoformat(value) if value else None


def instagram_usernames(connection) -> list[str]:
    names = []
    for row in followed_accounts(connection):
        parsed = urlparse(row["url"])
        parts = [part for part in parsed.path.split("/") if part]
        if "instagram.com" in parsed.netloc and parts:
            names.append(parts[0])
    return names


def connect_instagram(config_path: str, token: str, app_id: str = "", app_secret: str = "") -> dict:
    """Enregistre la connexion Instagram (jeton de l'utilisateur) et retrouve son compte professionnel."""
    token = token.strip()
    if not token:
        raise ValueError("Collez le jeton d'accès (access token).")
    config = load_config(config_path)
    api_version = config.get("social_extensions", {}).get("instagram", {}).get("api_version") or DEFAULT_API_VERSION
    expires_at = None
    if app_id.strip() and app_secret.strip():
        token, expires_at = exchange_long_lived_token(token, app_id.strip(), app_secret.strip(), api_version)
    with GraphClient(token, api_version) as client:
        accounts = find_instagram_accounts(client)
        pages = [] if accounts else [p.get("name", "") for p in client.get("me/accounts", fields="name").get("data", [])]
    if not accounts:
        if pages:
            raise ValueError(
                f"Le jeton voit vos Pages Facebook ({', '.join(pages)}), mais aucune n'a de compte Instagram "
                "professionnel relié. Reliez votre Instagram à l'une d'elles (Page Facebook > Paramètres > "
                "Comptes liés > Instagram), puis régénérez le jeton en cochant cette Page et ce compte Instagram."
            )
        raise ValueError(
            "Aucun compte Instagram professionnel trouvé avec ce jeton. Vérifiez que votre compte Instagram "
            "est professionnel, relié à une Page Facebook, et que le jeton a les permissions demandées."
        )
    account = accounts[0]
    set_meta(
        connect(resolve_data_path(config, "database_path")),
        INSTAGRAM_META,
        {
            "access_token": token,
            "ig_user_id": account["id"],
            "username": account["username"],
            "saved_at": utc_now_iso(),
            "expires_at": expires_at,
        },
    )
    return {"username": account["username"], "page": account["page"], "expires_at": expires_at}


def instagram_status(config_path: str) -> dict:
    config = load_config(config_path)
    settings = get_meta(connect(resolve_data_path(config, "database_path")), INSTAGRAM_META)
    return {
        "connecte": bool(settings.get("access_token")),
        "username": settings.get("username", ""),
        "saved_at": settings.get("saved_at"),
        "expires_at": settings.get("expires_at"),
        "hashtags": split_hashtags(settings.get("hashtags", DEFAULT_HASHTAGS)),
    }


def check_instagram(config_path: str) -> str:
    """'' si l'acces Instagram enregistre repond vraiment, sinon le probleme."""
    config = load_config(config_path)
    settings = get_meta(connect(resolve_data_path(config, "database_path")), INSTAGRAM_META)
    settings["api_version"] = config.get("social_extensions", {}).get("instagram", {}).get("api_version")
    return check_connection(settings)


# Meta n'autorise que 30 hashtags differents par semaine : on en garde 10 au maximum.
DEFAULT_HASHTAGS = "castingenfant, castingenfants, castingkids, castingfille, castingparis"
MAX_HASHTAGS = 10


def split_hashtags(text: str) -> list[str]:
    tags = []
    for part in (text or "").replace("\n", ",").replace(" ", ",").split(","):
        tag = part.strip().lstrip("#").lower()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def save_instagram_hashtags(config_path: str, text: str) -> list[str]:
    tags = split_hashtags(text)
    if len(tags) > MAX_HASHTAGS:
        raise ValueError(
            f"{MAX_HASHTAGS} hashtags au maximum (Meta limite à 30 hashtags différents par semaine)."
        )
    config = load_config(config_path)
    set_meta(connect(resolve_data_path(config, "database_path")), INSTAGRAM_META, {"hashtags": ", ".join(tags)})
    return tags


def disconnect_instagram(config_path: str) -> None:
    config = load_config(config_path)
    keys = ("access_token", "ig_user_id", "username", "saved_at", "expires_at")
    set_meta(connect(resolve_data_path(config, "database_path")), INSTAGRAM_META, dict.fromkeys(keys))


WEB_META = "websearch_"
WEB_MONTHLY_LIMIT = 900  # Tavily : 1 000 recherches gratuites par mois, on garde une marge
WEB_PER_RUN = 20


def websearch_status(config_path: str) -> dict:
    config = load_config(config_path)
    settings = get_meta(connect(resolve_data_path(config, "database_path")), WEB_META)
    month = datetime.now().strftime("%Y-%m")
    used = int(settings.get("used", 0)) if settings.get("month") == month else 0
    return {"connecte": bool(settings.get("key")), "utilisees_ce_mois": used, "limite": WEB_MONTHLY_LIMIT}


def connect_websearch(config_path: str, key: str, transport=None) -> None:
    key = key.strip()
    if not key:
        raise ValueError("Collez la clé Tavily (elle commence par tvly-).")
    tavily_search(key, "casting enfant Paris", max_results=1, transport=transport)  # verifie la cle
    config = load_config(config_path)
    set_meta(connect(resolve_data_path(config, "database_path")), WEB_META, {"key": key})


def disconnect_websearch(config_path: str) -> None:
    config = load_config(config_path)
    set_meta(connect(resolve_data_path(config, "database_path")), WEB_META, {"key": None})


def search_web_origins(config_path: str, transport=None) -> dict:
    """Cherche sur le web l'origine des annonces de sites payants ou a inscription (une fois par annonce)."""
    config = load_config(config_path)
    connection = connect(resolve_data_path(config, "database_path"))
    settings = get_meta(connection, WEB_META)
    report = {"active": bool(settings.get("key")), "recherches": 0, "trouvees": 0, "erreur": ""}
    if not settings.get("key"):
        return report
    month = datetime.now().strftime("%Y-%m")
    used = int(settings.get("used", 0)) if settings.get("month") == month else 0
    # Les annonces qui interessent au moins un des profils.
    candidates = {}
    for criteria in all_profiles_criteria(config_path):
        for match in find_matches(config, criteria):
            candidates.setdefault(match["url"], match)
    for match in candidates.values():
        if report["recherches"] >= WEB_PER_RUN or used >= WEB_MONTHLY_LIMIT:
            break
        if match["web"] is not None or match["niveau_acces"] < 2 or match["manual"]:
            continue
        if any(twin["niveau"] <= 1 for twin in match["ailleurs"]):
            continue  # une version gratuite est deja connue
        try:
            results = tavily_search(settings["key"], match["requete"], transport=transport)
        except (WebSearchError, httpx.HTTPError) as error:
            report["erreur"] = str(error)
            break
        used += 1
        report["recherches"] += 1
        report["trouvees"] += bool(keep_relevant(results, match["requete"], match["url"]))
        save_web_origin(connection, match["url"], match["requete"], trim(results))
    set_meta(connection, WEB_META, {"month": month, "used": used})
    return report


DISCOVERY_META = "decouverte_"  # une decouverte par jour (au premier passage de la journee)


def discovery_status(config_path: str) -> dict:
    config = load_config(config_path)
    connection = connect(resolve_data_path(config, "database_path"))
    last = get_meta(connection, DISCOVERY_META).get("last_at")
    return {
        "derniere": datetime.fromisoformat(last) if last else None,
        "proposes": [dict(row) for row in suggestions(connection)],
    }


def discover_accounts(config_path: str, force: bool = False, web_transport=None, ig_transport=None) -> dict:
    """Une fois par jour (ou a la demande) : cherche et verifie de nouveaux comptes de casting enfants."""
    config = load_config(config_path)
    connection = connect(resolve_data_path(config, "database_path"))
    report = {"active": False, "nouveaux": 0, "recherches_web": 0, "verifies": 0, "erreur": ""}
    web = get_meta(connection, WEB_META)
    ig = get_meta(connection, INSTAGRAM_META)
    if not web.get("key") or not ig.get("access_token"):
        return report
    last = get_meta(connection, DISCOVERY_META).get("last_at")
    today = datetime.now().date()
    if not force and last and datetime.fromisoformat(last).date() == today:
        return report  # deja fait aujourd'hui
    queries = discovery_queries_for_day(today.toordinal())
    month = datetime.now().strftime("%Y-%m")
    used = int(web.get("used", 0)) if web.get("month") == month else 0
    if used + len(queries) > WEB_MONTHLY_LIMIT:
        report["erreur"] = "quota mensuel de recherches web presque atteint : découverte reportée"
        return report
    known = set(instagram_usernames(connection)) | {row["username"] for row in suggestions(connection, None)}
    known.add((ig.get("username") or "").lower())
    # Les comptes que citent les directeurs de casting deja suivis dans leurs posts (deja en base).
    cited = mentions_in_texts(
        [row["detail_text"] or row["text"] for row in list_all(connection) if row["source_name"].startswith("Instagram @")]
    )
    ig["api_version"] = config.get("social_extensions", {}).get("instagram", {}).get("api_version")
    found = discover(
        web["key"], ig, known, web_transport=web_transport, ig_transport=ig_transport, queries=queries, cited=cited
    )
    for account in found["comptes"]:
        save_suggestion(connection, account)
    set_meta(connection, WEB_META, {"month": month, "used": used + found["recherches_web"]})
    set_meta(connection, DISCOVERY_META, {"last_at": datetime.now().isoformat(timespec="seconds")})
    report.update(
        active=True,
        nouveaux=len(found["comptes"]),
        recherches_web=found["recherches_web"],
        verifies=found["verifies"],
        erreur=found["erreur"],
    )
    return report


def follow_suggestion(config_path: str, username: str) -> dict:
    config = load_config(config_path)
    connection = connect(resolve_data_path(config, "database_path"))
    row = next((r for r in suggestions(connection, None) if r["username"] == username), None)
    saved = add_followed_account(config_path, f"@{username}", row["name"] if row else "", "Compte suggéré (vérifié)")
    set_suggestion_status(connection, username, "suivi")
    return saved


def ignore_suggestion(config_path: str, username: str) -> None:
    config = load_config(config_path)
    set_suggestion_status(connect(resolve_data_path(config, "database_path")), username, "ignore")


def find_matches(
    config: dict,
    criteria: SearchCriteria,
    include_offline: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Applique les criteres a toutes les annonces en base (les criteres peuvent changer a tout moment)."""
    connection = connect(resolve_data_path(config, "database_path"))
    apply_notes = {source.get("name"): source.get("apply_note", "") for source in config.get("sources", [])}
    rows = list_all(connection)
    origins = OriginFinder(
        [
            {
                "title": row["title"],
                "text": (row["detail_text"] or row["text"] or "").replace(HEADING_MARK, ""),
                "url": row["url"],
                "source_name": row["source_name"],
            }
            for row in rows
        ]
    )
    found_on_web = web_origins(connection)
    matches = []
    for index, row in enumerate(rows):
        if not include_offline and not row["still_online"]:
            continue
        listing = row_to_listing(row)
        decision = evaluate_listing(listing, criteria, exclude_terms(config))
        if not decision.accepted:
            continue
        manual = row["source_url"] == MANUAL_SOURCE_URL
        if manual:
            note = MANUAL_APPLY_NOTE
        elif row["source_url"] == INSTAGRAM_SOURCE_URL:
            note = INSTAGRAM_APPLY_NOTE
        elif row["source_url"] == NEWSLETTER_SOURCE_URL:
            note = NEWSLETTER_APPLY_NOTE
        else:
            note = apply_notes.get(row["source_name"], "")
        text = (row["detail_text"] or row["text"]).replace(HEADING_MARK, "")
        level, label = access_level(row["source_name"], f"{row['title']} {text}")
        twins = origins.twins(index)
        query = origins.search_query(index)
        # None = pas encore cherche sur le web ; [] = cherche, rien de pertinent.
        web = keep_relevant(found_on_web[row["url"]], query, row["url"]) if row["url"] in found_on_web else None
        matches.append(
            {
                "title": row["title"],
                "url": row["url"],
                "source": row["source_name"],
                "text": text,
                "score": decision.score,
                "reasons": "; ".join(decision.reasons),
                "first_seen_at": row["first_seen_at"],
                "last_seen_at": row["last_seen_at"],
                "is_new": bool(row["is_new"]),
                "still_online": bool(row["still_online"]),
                "manual": manual,
                "fiche": build_info(listing, note),
                "types": listing_types(row["title"], text),
                "acces": label,
                "niveau_acces": level,
                "ailleurs": twins,
                "requete": query,
                "recherche": search_links(query),
                "web": web,
                # Postuler sans payer : directement, ou grace a une autre version gratuite trouvee.
                "gratuit": level <= 1 or any(item["niveau"] <= 1 for item in twins + (web or [])),
            }
        )
    # Accessibles gratuitement d'abord, puis nouvelles, puis les plus pertinentes, puis les plus recentes.
    matches.sort(key=lambda m: (m["gratuit"], m["is_new"], m["score"], m["first_seen_at"]), reverse=True)
    return matches[:limit] if limit else matches


MANUAL_APPLY_NOTE = "Suivre les consignes du message d'origine (lien ci-dessus)."


def manual_source_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "instagram.com" in host:
        return "Instagram (ajout manuel)"
    if "facebook.com" in host or host.endswith("fb.com") or "fb.me" in host:
        return "Facebook (ajout manuel)"
    return "Ajout manuel"


def add_manual_listing(config_path: str, text: str, url: str = "", title: str = "") -> dict:
    """Ajoute une annonce copiee a la main (post Instagram, Facebook...). Rien n'est telecharge."""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Collez le texte de l'annonce.")
    url = url.strip()
    if url and urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Le lien doit commencer par http:// ou https://")
    if not url:
        url = "manuel:" + hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()[:12]
    config = load_config(config_path)
    listing = RawListing(
        source_name=manual_source_name(url),
        source_url=MANUAL_SOURCE_URL,
        title=(title.strip() or lines[0])[:160],
        url=url,
        text=" ".join(lines)[:8000],
        fetched_at=utc_now_iso(),
        detail_text="\n".join(lines)[:8000],
    )
    decision = evaluate_listing(listing, SearchCriteria.from_config(config), exclude_terms(config))
    upsert_listing(connect(resolve_data_path(config, "database_path")), listing, decision)
    return {"title": listing.title, "url": url, "source": listing.source_name,
            "correspond_aux_criteres": decision.accepted, "raisons": "; ".join(decision.reasons)}


def delete_manual_listing(config_path: str, url: str) -> bool:
    config = load_config(config_path)
    connection = connect(resolve_data_path(config, "database_path"))
    cursor = connection.execute("DELETE FROM listings WHERE url = ? AND source_url = ?", (url, MANUAL_SOURCE_URL))
    connection.commit()
    return cursor.rowcount > 0


INSTAGRAM_NOT_PROFILES = {"p", "reel", "reels", "stories", "explore", "tv", "accounts", "direct"}


def normalize_account(value: str) -> tuple[str, str]:
    """'@nom', 'instagram.com/nom' ou un lien complet -> (lien propre, libelle '@nom')."""
    value = value.strip()
    if not value:
        raise ValueError("Indiquez le lien du compte ou son @nom.")
    if value.startswith("@"):
        value = f"https://www.instagram.com/{value[1:]}/"
    elif "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or "." not in parsed.netloc:
        raise ValueError("Le lien doit ressembler à https://www.instagram.com/nom_du_compte/")
    parts = [part for part in parsed.path.split("/") if part]
    if "instagram.com" in parsed.netloc.lower():
        if not parts or parts[0].lower() in INSTAGRAM_NOT_PROFILES:
            raise ValueError("Ce lien n'est pas celui d'un compte : ouvrez le profil et copiez son lien.")
        username = parts[0]
        return f"https://www.instagram.com/{username}/", f"@{username}"
    host = parsed.netloc.lower().removeprefix("www.")
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return clean, f"{host}/{parts[0]}" if parts else host


def add_followed_account(config_path: str, link: str, name: str = "", note: str = "") -> dict:
    """Memorise un compte de directeur de casting a consulter (rien n'est telecharge)."""
    url, label = normalize_account(link)
    if name.strip():
        label = f"{name.strip()} ({label})"
    config = load_config(config_path)
    save_followed_account(connect(resolve_data_path(config, "database_path")), url, label, note.strip())
    return {"url": url, "label": label}


def list_followed_accounts(config_path: str) -> list[dict]:
    config = load_config(config_path)
    rows = followed_accounts(connect(resolve_data_path(config, "database_path")))
    return [{"url": row["url"], "label": row["label"], "note": row["note"], "added_at": row["added_at"]} for row in rows]


def remove_followed_account(config_path: str, url: str) -> bool:
    config = load_config(config_path)
    return delete_followed_account(connect(resolve_data_path(config, "database_path")), url)


PROFILE_META = "profil_"  # ancien profil unique : repris automatiquement dans le premier profil
GMAIL_META = "gmail_"


def _db(config_path: str):
    return connect(resolve_data_path(load_config(config_path), "database_path"))


# --- Profils : une personne = infos de candidature + photos + criteres de recherche ---------------
def profiles(config_path: str) -> list[dict]:
    """Tous les profils, le premier d'abord. Cree le premier a partir de l'ancien profil unique."""
    connection = _db(config_path)
    rows = list_profiles(connection)
    if not rows:
        old = get_meta(connection, PROFILE_META)
        criteria = SearchCriteria.from_config(load_config(config_path)).to_config()
        insert_profile(connection, old.get("prenom") or "Profil 1", old, criteria)
        rows = list_profiles(connection)
    return rows


def get_profile(config_path: str, profile_id: int | None = None) -> dict:
    rows = profiles(config_path)
    return next((row for row in rows if row["id"] == profile_id), rows[0])


def is_main_profile(config_path: str, profile_id: int | None) -> bool:
    return profile_id is None or profiles(config_path)[0]["id"] == profile_id


def profile_criteria(config_path: str, profile_id: int | None = None) -> SearchCriteria:
    return SearchCriteria.from_dict(get_profile(config_path, profile_id)["criteria"])


def all_profiles_criteria(config_path: str) -> list[SearchCriteria]:
    return [SearchCriteria.from_dict(row["criteria"]) for row in profiles(config_path)]


def create_profile(config_path: str, label: str) -> int:
    label = label.strip()
    if not label:
        raise ValueError("Donnez un nom au profil (par exemple le prénom).")
    if label.lower() in {row["label"].lower() for row in profiles(config_path)}:
        raise ValueError("Un profil porte déjà ce nom.")
    # Meme lieu que le premier profil, mais ni sexe ni age : a regler pour la nouvelle personne.
    first = profile_criteria(config_path)
    criteria = SearchCriteria(location=first.location, include_unknown_location=first.include_unknown_location)
    return insert_profile(_db(config_path), label, {"prenom": label}, criteria.to_config())


def rename_profile(config_path: str, profile_id: int, label: str) -> None:
    label = label.strip()
    if not label:
        raise ValueError("Le nom du profil ne peut pas être vide.")
    update_profile(_db(config_path), profile_id, label=label)


def delete_profile(config_path: str, profile_id: int) -> None:
    """Supprime le profil (pas le premier). Ses photos restent dans le dossier data\\profil\\photos."""
    if is_main_profile(config_path, profile_id):
        raise ValueError("Le premier profil ne peut pas être supprimé (renommez-le si besoin).")
    delete_profile_row(_db(config_path), profile_id)


def save_profile_criteria(config_path: str, profile_id: int | None, criteria: SearchCriteria) -> None:
    """Criteres d'un profil. Ceux du premier servent aussi a la tache quotidienne et a Claude (config.yaml)."""
    profile = get_profile(config_path, profile_id)
    update_profile(_db(config_path), profile["id"], criteria=criteria.to_config())
    if is_main_profile(config_path, profile["id"]):
        save_criteria(config_path, criteria)


def load_profile(config_path: str, profile_id: int | None = None) -> dict:
    return dict(get_profile(config_path, profile_id)["data"])


def save_profile(config_path: str, values: dict, profile_id: int | None = None) -> None:
    known = {key for key, _ in PROFILE_FIELDS} | {MINOR_KEY}
    profile = get_profile(config_path, profile_id)
    data = {**profile["data"], **{key: str(value).strip() for key, value in values.items() if key in known}}
    update_profile(_db(config_path), profile["id"], data=data)


def photos_dir(config_path: str, profile_id: int | None = None) -> Path:
    """Photos du premier profil : data\\profil\\photos ; des autres : data\\profil\\photos\\<numero>."""
    folder = resolve_data_path(load_config(config_path), "database_path").parent / "profil" / "photos"
    if not is_main_profile(config_path, profile_id):
        folder = folder / str(profile_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def list_photos(config_path: str, profile_id: int | None = None) -> list[Path]:
    folder = photos_dir(config_path, profile_id)
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower().lstrip(".") in PHOTO_TYPES)


def save_photo(config_path: str, filename: str, content: bytes, profile_id: int | None = None) -> Path:
    name = Path(filename).name
    if Path(name).suffix.lower().lstrip(".") not in PHOTO_TYPES:
        raise ValueError("Format accepté : JPG, PNG ou WEBP.")
    target = photos_dir(config_path, profile_id) / name
    target.write_bytes(content)
    return target


def delete_photo(config_path: str, filename: str, profile_id: int | None = None) -> bool:
    target = photos_dir(config_path, profile_id) / Path(filename).name
    if target.exists():
        target.unlink()
        return True
    return False


def gmail_status(config_path: str) -> dict:
    settings = get_meta(_db(config_path), GMAIL_META)
    return {"connecte": bool(settings.get("app_password")), "address": settings.get("address", "")}


def connect_gmail(config_path: str, address: str, app_password: str, imap_factory=None) -> str:
    """Teste la connexion Gmail puis l'enregistre. Retourne le nom du dossier Brouillons."""
    address, app_password = address.strip(), app_password.replace(" ", "")
    if "@" not in address or len(app_password) < 16:
        raise ValueError("Indiquez l'adresse Gmail et le mot de passe d'application (16 lettres).")
    folder = test_gmail(address, app_password, imap_factory) if imap_factory else test_gmail(address, app_password)
    set_meta(_db(config_path), GMAIL_META, {"address": address, "app_password": app_password})
    return folder


def disconnect_gmail(config_path: str) -> None:
    set_meta(_db(config_path), GMAIL_META, {"address": None, "app_password": None})


def application_draft(config_path: str, match: dict, profile_id: int | None = None) -> dict:
    """Brouillon propose pour une annonce : destinataire, objet, message (modifiables avant creation)."""
    profile = load_profile(config_path, profile_id)
    fiche = match["fiche"]
    photos = list_photos(config_path, profile_id)
    return {
        "recipient": (fiche["contact"]["emails"] or [""])[0],
        "subject": build_subject(fiche, profile, match["title"]),
        "body": build_body(profile, match["title"], match["url"], min(len(photos), 3)),
        "photos": [photo.name for photo in photos[:3]],
        "missing": missing_fields(profile),
    }


def create_gmail_draft(config_path: str, listing_url: str, recipient: str, subject: str, body: str,
                       photo_names: list[str], imap_factory=None, profile_id: int | None = None) -> None:
    """Cree le brouillon dans Gmail (jamais envoye automatiquement) et note la candidature."""
    settings = get_meta(_db(config_path), GMAIL_META)
    if not settings.get("app_password"):
        raise ValueError("Gmail n'est pas connecté (onglet « Profil et Gmail »).")
    profile_id = get_profile(config_path, profile_id)["id"]
    folder = photos_dir(config_path, profile_id)
    photos = [folder / Path(name).name for name in photo_names if (folder / Path(name).name).exists()]
    message = build_draft(settings["address"], recipient, subject, body, photos)
    if imap_factory:
        save_gmail_draft(settings["address"], settings["app_password"], message, imap_factory)
    else:
        save_gmail_draft(settings["address"], settings["app_password"], message)
    record_application(_db(config_path), listing_url, recipient.strip(), subject, profile_id)


def prepared_applications(config_path: str, profile_id: int | None = None) -> dict[str, str]:
    return applications_by_listing(_db(config_path), profile_id)


def save_criteria(config_path: str, criteria: SearchCriteria) -> None:
    """Enregistre les criteres comme reglages par defaut (utilises aussi par la tache quotidienne et MCP)."""
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    search = raw.get("search") or {}
    search.update(criteria.to_config())
    raw["search"] = search
    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def criteria_from_args(config: dict, args: argparse.Namespace) -> SearchCriteria:
    criteria = SearchCriteria.from_config(config)
    if args.tout:
        return SearchCriteria()
    if args.sexe is not None:
        criteria.gender = clean_gender(args.sexe)
    if args.age_min is not None:
        criteria.min_age = clean_age(args.age_min)
    if args.age_max is not None:
        criteria.max_age = clean_age(args.age_max)
    if args.lieu is not None:
        criteria.location = clean_location(args.lieu)
    return criteria


def print_matches(config_path: str, criteria_args: argparse.Namespace) -> None:
    config = load_config(config_path)
    criteria = criteria_from_args(config, criteria_args)
    matches = find_matches(config, criteria, criteria_args.anciennes)
    shown = matches[: criteria_args.limit]
    print(f"Criteres : {criteria.describe()} - {len(matches)} annonce(s), {len(shown)} affichee(s)")
    for match in shown:
        flag = "NOUVEAU " if match["is_new"] else ""
        print(f"{flag}[{match['score']}] {match['title']}  ({match['source']})")
        print(f"    {match['url']}")
        print(f"    {match['reasons']}")


def main() -> None:
    # Evite un plantage sur les caracteres que la console Windows ne sait pas afficher.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Veille locale de castings")
    parser.add_argument("--config", default="config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("run", help="Lancer une collecte")
    list_parser = subparsers.add_parser(
        "list",
        help="Lister les annonces correspondant aux criteres (par defaut ceux de config.yaml)",
    )
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--sexe", help="fille, garcon, ou tous")
    list_parser.add_argument("--age-min", help="age minimum, 0 pour aucun")
    list_parser.add_argument("--age-max", help="age maximum, 0 pour aucun")
    list_parser.add_argument("--lieu", help='ex. "Ile-de-France" ou "Paris, Lyon" ; tous pour toute la France')
    list_parser.add_argument("--tout", action="store_true", help="ignorer tous les criteres")
    list_parser.add_argument("--anciennes", action="store_true", help="inclure les annonces qui ne sont plus en ligne")

    args = parser.parse_args()
    if args.command == "run":
        print(json.dumps(run_search(args.config), ensure_ascii=False, indent=2))
    elif args.command == "list":
        print_matches(args.config, args)


if __name__ == "__main__":
    main()
