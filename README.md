# Relais newsletters de la Veille castings

Toutes les heures, GitHub lit la boîte Gmail dédiée aux newsletters de casting
(La Newsletter de l'Acteur, Cinéaste.org), en découpe les annonces et les publie ici :

    https://<compte>.github.io/<dépôt>/annonces.json

Les copies de la Veille castings lisent ce fichier : personne n'a besoin du mot de passe de la boîte.

- Le mot de passe d'application Gmail est rangé dans **Settings > Secrets and variables > Actions**
  (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`). Il n'apparaît jamais dans le code ni dans les journaux.
- Lecture seule de la boîte : rien n'est marqué comme lu, supprimé ni envoyé.
- Lancer à la main : onglet **Actions** > « Relais newsletters » > **Run workflow**.
- Ce dépôt est généré par `scripts\preparer_relais.ps1` dans le projet Veille castings : ne pas le modifier ici.
