import argparse
import json

from mcp.server.fastmcp import FastMCP

from casting_watch.cli import (
    add_followed_account,
    add_manual_listing,
    find_matches,
    list_followed_accounts,
    run_search,
)
from casting_watch.config import load_config
from casting_watch.filters import SearchCriteria, clean_age, clean_gender, clean_location


def build_server(config_path: str) -> FastMCP:
    mcp = FastMCP("casting-watch")

    @mcp.tool()
    def run_casting_search() -> str:
        """Lance une collecte des castings sur les sites configures (peut prendre une minute)."""
        return json.dumps(run_search(config_path), ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_casting_matches(
        sexe: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        lieu: str | None = None,
        limit: int = 10,
    ) -> str:
        """Liste les annonces de casting correspondant aux criteres.

        Un parametre omis reprend le reglage par defaut de config.yaml.
        Pour ne pas filtrer : sexe="tous", age_min=0, age_max=0, lieu="toute la France".
        sexe : "fille" ou "garcon". lieu : ex. "Ile-de-France" ou "Paris, Lyon".
        """
        config = load_config(config_path)
        criteria = SearchCriteria.from_config(config)
        if sexe is not None:
            criteria.gender = clean_gender(sexe)
        if age_min is not None:
            criteria.min_age = clean_age(age_min)
        if age_max is not None:
            criteria.max_age = clean_age(age_max)
        if lieu is not None:
            criteria.location = clean_location(lieu)
        matches = find_matches(config, criteria, limit=limit)
        payload = {
            "criteres": criteria.describe(),
            "annonces": [
                {key: match[key] for key in ("title", "url", "source", "reasons", "is_new", "first_seen_at", "fiche")}
                for match in matches
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @mcp.tool()
    def add_casting_listing(texte: str, lien: str = "") -> str:
        """Ajoute a la veille une annonce trouvee ailleurs (post Instagram, Facebook...).

        texte : le texte de l'annonce, copie-colle par l'utilisateur. lien : l'adresse du post (facultatif).
        Rien n'est telecharge depuis le lien : seul le texte fourni est enregistre.
        """
        return json.dumps(add_manual_listing(config_path, texte, lien), ensure_ascii=False, indent=2)

    @mcp.tool()
    def follow_casting_account(compte: str, nom: str = "", note: str = "") -> str:
        """Memorise un compte (Instagram ou autre) de directeur/directrice de casting a consulter.

        compte : lien du profil ou "@nom". Rien n'est telecharge depuis le compte.
        """
        return json.dumps(add_followed_account(config_path, compte, nom, note), ensure_ascii=False, indent=2)

    @mcp.tool()
    def list_followed_casting_accounts() -> str:
        """Liste les comptes de directeurs/directrices de casting memorises, avec leur lien."""
        return json.dumps(list_followed_accounts(config_path), ensure_ascii=False, indent=2)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    build_server(args.config).run()


if __name__ == "__main__":
    main()
