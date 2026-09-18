# Structure du dépôt

Ce dépôt était vide au départ. Il contient maintenant le socle technique et les
preuves nécessaires pour réécrire progressivement `buma_master_hooks`. Il ne
contient pas encore les routes métier ni les données de production.

## Fichiers racine

- `AGENTS.md` : règles obligatoires pour chaque agent, notamment la lecture du
  journal, la validation par checkpoint et la mise à jour du checkpoint avant
  toute fin de session.
- `README.md` : démarrage rapide et rappel du périmètre.
- `pyproject.toml` : nom du projet, dépendances Python, configuration pytest,
  Ruff et mypy.
- `.env.example` : exemple de configuration locale ; le vrai `.env` reste
  ignoré par Git.
- `docker-compose.yml` : PostgreSQL local isolé sur le port `55434`.
- `Makefile` : raccourcis pour installer, tester, démarrer PostgreSQL, lancer
  PPBase et vérifier le schéma.
- `.gitignore` : exclut environnement virtuel, caches, `.env`, données PPBase et
  backups locaux.

## Code d’exécution

- `app/core/config.py` : lit les variables d’environnement PPBase et fournit
  les paramètres de base (URL PostgreSQL, port, dossiers, jobs internes).
- `app/hooks/register.py` : point d’entrée des hooks PPBase. Il charge les
  modules dans un ordre explicite ; seuls les modules foundation sont encore
  enregistrés.
- `app/hooks/health.py` : route technique `/` renvoyant l’état du service.
- `app/hooks/poc.py` : POC technique, route de test, hook CRUD et endpoints de
  transaction/rollback. Ce n’est pas une route métier BUMA.
- `app/__init__.py`, `app/core/__init__.py`, `app/hooks/__init__.py` : fichiers
  Python qui marquent les dossiers comme modules.

## Migrations PPBase

- `pb_migrations/1789236840_buma_master_schema_baseline.py` : baseline native
  officielle générée depuis l'export JSON corrigé ; elle couvre les 70
  collections `base` et les 2 collections `auth`.
- `pb_migrations/1789236841_users_username_compat.py` : ajoute le champ
  physique de compatibilité `users.username`, utilisé par plusieurs vues ;
  `pb_migrations/1789236843_agent_users_username_compat.py` fait de même pour
  la collection auth `agent_users`. La sémantique complète de connexion reste
  le checkpoint auth.
- `pb_migrations/1789236842_buma_master_views.py` : crée les 42 vues avec les
  requêtes traduites et validées pour PostgreSQL, dans leur ordre de dépendance.
- `pb_migrations/.gitkeep` : conserve le dossier avant l'arrivée des migrations.

La collection technique `foundation_poc` n'est pas une migration officielle :
elle a été retirée afin qu'une base vide reçoive exactement les 114 définitions
BUMA.

## Scripts de préparation et de vérification

- `scripts/source_inventory.py` : inventorie la source TypeScript gelée
  (routes, hooks, collections et sites transactionnels).
- `scripts/analyze_migrations.py` : compare les noms des 326 migrations
  historiques avec le schéma final.
- `scripts/generate_native_baseline.py` : génère de façon déterministe une
  migration PPBase depuis le JSON PocketBase ; avec `--include-views` et
  `--views-only`, il régénère la migration officielle des 42 vues.
- `scripts/verify_schema_baseline.py` : compare une base PostgreSQL appliquée
  avec le JSON (IDs, types, champs, relations, règles et indexes).
- `scripts/verify_view_queries.py` : exécute chaque requête de vue traduite
  dans des vues temporaires PostgreSQL et rapporte les échecs sans modifier la
  baseline officielle ; le résultat actuel est 42/42.
- `scripts/verify_complete_schema.py` : vérifie les 114 définitions du JSON
  corrigé, les relations et les champs de compatibilité
  `users.username`/`agent_users.username`.
- `scripts/__init__.py`, `scripts/.gitkeep` : support du package et conservation
  initiale du dossier.

## Documentation de contrat et de suivi

- `docs/source-baseline.md` : commit source, hash du schéma et artefacts
  externes manquants.
- `docs/corrected-export-diff.md` : différences contrôlées entre l’ancien et le
  nouvel export PocketBase.
- `docs/source-manifest.json` : manifeste machine-readable de la provenance et
  de l’état de validation.
- `docs/contracts/source-inventory.json` : inventaire détaillé de la source.
- `docs/contracts/schema-migration-reconciliation.json` : rapport de
  réconciliation du JSON final et des 326 migrations.
- `docs/contracts/README.md`, `routes.json`, `collections.json`, `hooks.json` et
  `transaction-invariants.md` : contrats à compléter par domaine.
- `docs/implementation-plan.md` : ordre des checkpoints C0 à C11.
- `docs/migration-ppbase-tasklist.md` : cases de suivi opérationnel.
- `docs/progress/ledger.md` : journal principal et point de reprise obligatoire.
- `docs/schema-baseline-report.json` : preuve de génération de la baseline
  officielle tables/auth (72 définitions, hash de l’export corrigé).
- `docs/schema-views-baseline-report.json` : avertissements de traduction des
  42 vues de l'export corrigé.
- `docs/view-verification-report.json` : résultat PostgreSQL des vues corrigées
  (42 OK, 0 en échec).
- `docs/complete-schema-verification-report.json` : preuve de la présence des
  114 définitions dans une base PostgreSQL jetable.
- `docs/decisions-and-divergences.md` et `docs/test-matrix.md` : décisions de
  compatibilité et matrice des tests.

## Tests

- `tests/test_project_structure.py` : vérifie la présence du socle.
- `tests/test_foundation.py` : vérifie l’enregistrement des routes foundation.
- `tests/test_poc_contract.py` : vérifie la migration et le contrat du POC.
- `tests/test_schema_baseline.py` : vérifie le rapport et la migration de
  baseline.
- `tests/test_view_verifier.py` : vérifie l’ordre des dépendances de vues.

Les dossiers `__pycache__`, `.pytest_cache`, `.ruff_cache` et `pb_data` sont des
artefacts locaux ignorés ; ils ne constituent pas du code métier à reprendre.
