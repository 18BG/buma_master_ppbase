# Structure du dépôt

Ce dépôt était vide au départ. Il contient maintenant le socle technique et les
preuves nécessaires pour réécrire progressivement `buma_master_hooks`. Les
domaines C3 à C9 sont présents dans le périmètre enregistré dans le ledger ;
aucune donnée de production n'est importée.

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
- `app/hooks/register.py` : point d'entrée des hooks PPBase. Il charge les
  modules foundation, auth et les checkpoints métier validés dans un ordre
  explicite.
- `app/hooks/health.py` : route technique `/` renvoyant l'état du service.
- `app/hooks/auth.py` : routes legacy `/isConnected` et
  `/master/user/health`, plus l'enregistrement des middlewares auth C3.
- `app/auth/` : résolution username/email, contexte live des rôles/permissions
  et garde `requireAdminOrRecordAuth`.
- `app/stockage/service.py` et `app/hooks/stockage.py` : checkpoint C7 pour
  articles, stocks, stockages, transferts, conversions, mouvements batch et
  ponts wallet/UV ; toutes les mutations utilisent le coordinateur
  transactionnel et des verrous PostgreSQL.
- `app/hooks/poc.py` : POC technique, route de test, hook CRUD et endpoints de
  transaction/rollback. Ce n’est pas une route métier BUMA.
- `app/commerce/service.py`, `returns.py`, `reports.py` et
  `app/hooks/commerce.py` : les 29 routes C9 commerce/rapports et les cinq
  routes caisse transverses ; `docs/contracts/commerce.md` précise leurs
  contrats et D-0009 leurs limites transactionnelles.
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
- `pb_migrations/1789236844_auth_compat.py` : aligne les champs d'identité et
  la politique de mot de passe des collections `users` et `agent_users`.
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
- `scripts/c7_stockage_runtime.py` : preuve HTTP PostgreSQL du checkpoint C7,
  avec rollback, idempotence, concurrence, permissions et nettoyage.
- `scripts/c9_commerce_runtime.py` : preuve HTTP PostgreSQL C9 avec contrôles
  persistés, défaut de stockage injecté, concurrence, CRUD natif et nettoyage
  systématique ; résultat dans `docs/c9-runtime-report.json`.
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
- `docs/contracts/README.md`, `routes.json`, `authentication.md`,
  `article-stockage.md`, `collections.json`, `hooks.json` et
  `transaction-invariants.md` : contrats par domaine, avec C3 et C7
  documentés.
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

## Ajouts C10

- `app/commerce/sequential.py` : ordre des sauvegardes non transactionnelles C9,
  correction D-0009.
- `app/hooks/legacy_sql.py`, `legacy_stockage.py`, `legacy_records.py` : routes
  SQL, cinq routes stock legacy et hooks CRUD natifs, plus payDebt.
- `app/legacy/stockage.py` : effets séquentiels du stockage `/api2`.
- `scripts/c10_legacy_sql_runtime.py`, `c10_stockage_runtime.py`,
  `c10_native_hooks_runtime.py` : preuves isolées avec nettoyage et rapports
  `docs/c10-*-runtime-report.json`.
