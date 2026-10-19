class SocialConnectorDisabled(RuntimeError):
    pass


# Seuls connecteurs autorises : API officielles. Aucun scraping de pages de reseaux sociaux.
OFFICIAL_CONNECTORS = {"instagram": "official_api"}


def explain_social_limits() -> dict:
    return {
        "instagram": "Uniquement via l'API officielle Meta (Business Discovery) avec le compte professionnel de l'utilisateur.",
        "facebook": "Pas de connecteur : uniquement via Graph API, pages administrees ou exports fournis.",
        "not_supported": "Pas de scraping de comptes connectes, pas de contournement de connexion, pas d'extraction massive non autorisee.",
    }


def collect_social_sources(config: dict) -> list:
    """Verifie que seuls des connecteurs officiels sont actives. Leve SocialConnectorDisabled sinon."""
    for name, settings in (config.get("social_extensions") or {}).items():
        if not isinstance(settings, dict) or not settings.get("enabled"):
            continue
        if OFFICIAL_CONNECTORS.get(name) != settings.get("mode"):
            raise SocialConnectorDisabled(
                f"{name} : seul un connecteur officiel autorise peut etre active "
                f"(mode attendu : {OFFICIAL_CONNECTORS.get(name, 'aucun disponible')})."
            )
    return []


def instagram_enabled(config: dict) -> bool:
    settings = (config.get("social_extensions") or {}).get("instagram") or {}
    return bool(settings.get("enabled")) and settings.get("mode") == OFFICIAL_CONNECTORS["instagram"]
