# Journal de progression

## Checkpoint C0 — provenance et inventaire

Statut : terminé côté source statique, runtime PocketBase non exécuté.

- source : `buma_master_hooks/fix/uv-paani-sup@530018c` ;
- dépôt source vérifié propre au moment du gel ;
- inventaire généré : `docs/contracts/source-inventory.json` ;
- inventaire : 113 routes détectées, 9 hooks, 62 collections référencées et 51 sites transaction/SQL ;
- routes/hooks/collections/SQL/transactions recensés ;
- deux bootstraps (`master.pb.ts` et `index.pb.ts`) conservés dans l'inventaire ;
- schéma live, backup et API runtime restent à obtenir.

Validation : `pytest`, Ruff, format check et `git diff --check` du foundation
passent. Aucun serveur PocketBase ou PostgreSQL n'a été démarré dans C0.

## Checkpoint C1 — foundation PPBase

Statut : terminé.

- registre `app/hooks/register.py` limité à `health` et `poc` ;
- `/` et `/__foundation/poc` enregistrés ; `/api/health` reste fourni par PPBase ;
- configuration, Makefile, Docker Compose et tests structurels présents.
- POC HTTP réel : migration appliquée, création de record et transaction à deux
  écritures validées ; un rollback forcé a retourné HTTP 500 et le nombre de
  records est resté inchangé (3 avant / 3 après).
- Observation : un repository interne ne déclenche pas le hook de requête
  `on_record_create_request`; ce hook concerne le chemin API CRUD. Cette
  différence est documentée avant tout portage métier.

## Checkpoint C2 — POC technique transactionnel PPBase

Statut : POC terminé ; baseline BUMA non commencée.

- PostgreSQL isolé démarré avec Docker sur `localhost:55434` ;
- migration native `0000000001_foundation_poc.py` appliquée ;
- statut migrations : 1 appliquée, 0 en attente ;
- `/api/health` : HTTP 200 ;
- `/__foundation/poc` : HTTP 200 ;
- création CRUD sur `foundation_poc` : HTTP 200 avec `hook_marker` ;
- transaction à deux écritures : HTTP 200, deux records créés ;
- rollback forcé : HTTP 500 attendu, total 3 avant / 3 après.

La migration technique `0000000001_foundation_poc.py` a ensuite été supprimée
du répertoire officiel. Le POC reste une preuve historique du foundation, mais
il ne fait plus partie du schéma appliqué sur une nouvelle base.

La validation a utilisé l'environnement PPBase local
`/Users/omarsisss/Documents/wproject/protech/bumo-hooks/.venv` (PPBase 0.3.0,
source chargée depuis `/Users/omarsisss/Documents/wproject/protech/ppbase`). Le
nouveau projet n'a pas encore son propre `.venv`.

## Checkpoint C2 — Schéma corrigé, baseline et vues

Statut : terminé pour le schéma structurel et la validation SQL ; aucune donnée
de production importée.

Verdict schéma : la source corrigée est cohérente et les 114 définitions sont
installables dans PPBase : 70 collections `base`, 2 collections `auth` et 42
vues. La migration officielle des vues est maintenant versionnée dans
`pb_migrations`.

Fichiers ajoutés ou mis à jour dans ce checkpoint :
`pb_migrations/1789236842_buma_master_views.py`,
`pb_migrations/1789236843_agent_users_username_compat.py`,
`scripts/verify_complete_schema.py`, `scripts/verify_schema_baseline.py`,
`scripts/generate_native_baseline.py`, `tests/test_schema_baseline.py`,
`tests/test_view_verifier.py`, les rapports JSON de schéma/vues, le `Makefile`
et la documentation de suivi/structure.

- export reçu : `/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json` ;
- SHA-256 : `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d` ;
- schéma : 114 définitions, dont 70 collections `base`, 2 collections `auth` et
  42 vues ; IDs et noms uniques ; relations vers des collections existantes ;
- historique analysé : 326 migrations dans `migrationsDir/`, soit 25
  `created_*` et 301 `updated_*` ; toutes les cibles historiques existent dans
  l'export final ;
- artefact : `docs/contracts/schema-migration-reconciliation.json` ;
- constat : 48 définitions finales n'apparaissent dans aucun nom de fichier de
  migration, et 89 n'ont pas de migration `created_*` ; les migrations
  historiques seules ne constituent donc pas une baseline complète ;
- baseline officielle générée :
  `pb_migrations/1789236840_buma_master_schema_baseline.py` ;
- validation sur une base PostgreSQL vide : 72/72 collections, relations et
  métadonnées stables ; second passage : aucune migration en attente ;
- migration officielle des vues générée :
  `pb_migrations/1789236842_buma_master_views.py` ;
- compatibilité auth complétée pour les deux collections PocketBase :
  `users.username` et `agent_users.username` sont matérialisés par les
  migrations `1789236841` et `1789236843` ;
- replay propre depuis le répertoire officiel `pb_migrations` sur la base
  jetable `buma_master_ppbase_complete_test` : 5 migrations appliquées, 0 en
  attente, 0 orpheline ;
- migration `1789236843_agent_users_username_compat.py` rejouée après rollback
  puis réapplication avec succès ;
- validation des requêtes traduites : 42/42 vues PostgreSQL OK, 0 échec ;
- validation complète du schéma sur la base vide
  `buma_master_ppbase_clean_test` : exactement 114 définitions non-système
  présentes, aucune définition supplémentaire, aucun écart de métadonnées et
  aucune relation cassée (`docs/complete-schema-verification-report.json`) ;
- le vérificateur complet exige désormais explicitement
  `users.username` et `agent_users.username` ; les deux champs sont présents ;
- scripts de preuve : `scripts/verify_schema_baseline.py`,
  `scripts/verify_view_queries.py` et `scripts/verify_complete_schema.py` ;
- traduction mécanique renforcée dans `scripts/generate_native_baseline.py`
  (identifiants camelCase, `GROUP_CONCAT`, `rowid`, égalité d'IDs relationnels,
  `GREATEST`) avec tests dédiés ;
- les 42 vues contenaient du SQL SQLite nécessitant une traduction (backticks,
  `GROUP_CONCAT`, `rowid`, `==`, `= null`, alias dans `WHERE`, agrégats `MAX`,
  etc.) ; ces traductions sont maintenant versionnées et validées dans la
  migration officielle des vues ;
- rapport final des vues : `docs/view-verification-report.json` (42 OK, 0
  échec) ;
- le seul champ métier absent de la collection qui est directement demandé par
  une vue était `buma_pdv.parent`, utilisé par
  `buma_customer_pdv_list_selector_view` et `buma_customer_pdv_list_view` ; les
  deux vues ont été corrigées vers `buma_pdv.agent` dans l'export corrigé ;
- les vues `buma_purchase_list_view`, `buma_sale_list_view`,
  `buma_stockage_no_manager`, `buma_transaction_list_view` et
  `buma_users_list_view` utilisent `users.username`, champ système PocketBase
  confirmé ; elles ne sont pas à corriger dans PocketBase ; leur compatibilité
  physique est assurée côté PPBase ;
- les anciens échecs du draft PostgreSQL concernaient uniquement la traduction
  SQL (`GROUP BY`, types, `GREATEST`) ou la dépendance de `test` à
  `buma_transaction_list_view`, pas un champ métier absent dans PocketBase ;
- divergence résolue : `parent` a été remplacé par `agent` dans les deux vues
  `customer_pdv` ; `username` est demandé par `users` et `agent_users`. Le
  Dashboard confirme que `username` est bien présent comme champ système
  PocketBase ; PPBase 0.3.0 ne le matérialise pas nativement, d'où les
  migrations de compatibilité `1789236841_users_username_compat.py` et
  `1789236843_agent_users_username_compat.py` (la sémantique de connexion reste
  C3).
- clarification utilisateur : les vues qui utilisent `users.username` sont
  fonctionnelles dans PocketBase et ne doivent pas être corrigées dans PocketBase.
  `agent_users.username` est également conservé comme champ système. Leur
  adaptation concerne uniquement la compatibilité auth PPBase.
- preuve runtime PocketBase reçue : `buma_agent_list_view`,
  `buma_purchase_list_view` et `buma_transaction_list_view` retournent chacun un
  record réel. `buma_customer_pdv_list_view` retourne HTTP 200 mais vide avec
  `fields=id` ; cette observation doit être complétée par une lecture sans
  projection et un cas de données correspondant à son filtre.
- décision utilisateur appliquée : les vues ont d'abord été corrigées dans
  PocketBase, puis l'export corrigé est devenu la source de vérité de la
  baseline officielle ; `username` n'a pas été modifié dans les vues et aucun
  champ `rowid` n'a été ajouté.
- les traductions SQL précédemment bloquantes ont été corrigées dans le
  générateur et sont couvertes par la migration des vues ; aucune correction
  supplémentaire du schéma PocketBase n'est requise pour ces cas ;
- vérifications finales : commit source identique au manifeste ; JSON des
  rapports et du manifeste valides ; `ruff check`, `ruff format --check`,
  `pytest -p no:cacheprovider` (12 tests) et `git diff --check` OK ; les deux
  générateurs passent leur contrôle `--check` ; aucun commit n'a encore été
  créé dans le nouveau dépôt.

Revalidation après retrait de `foundation_poc` : 4 migrations officielles
appliquées, 0 en attente, 0 orpheline ; 114 collections/vues non-système,
42 vues, aucune collection nommée `foundation_poc`, 42/42 requêtes de vues OK.

Usage local clarifié : `make migrate-up` applique les migrations en utilisant
`.env` (`PPBASE_DATABASE_URL` et `PPBASE_MIGRATIONS_DIR`) ; `make verify-all`
exécute les deux vérificateurs sans que l'utilisateur répète `--db` ou `--dir`.
Les vérificateurs sont facultatifs après la validation initiale : ils contrôlent
le résultat, mais ne sont pas nécessaires au démarrage normal du serveur.
Vérification PPBase : `serve` appelle `prepare_database` au démarrage et
`auto_migrate=True` par défaut ; `make start` applique donc les migrations en
attente avant de servir les requêtes, sauf si `PPBASE_AUTO_MIGRATE=false` est
configuré explicitement.

## Checkpoint suivant

C3 : authentification et permissions. Le portage des domaines métier reste
bloqué tant que C3 (auth) et C4 (transactions/idempotence métier) ne sont pas
validés. Le backup/import de production reste hors périmètre.

## Reprise par un autre agent

Lire dans cet ordre :

1. `docs/source-baseline.md` ;
2. `docs/implementation-plan.md` ;
3. `docs/migration-ppbase-tasklist.md` ;
4. `docs/contracts/source-inventory.json` ;
5. ce journal ;
6. `git status --short --branch`.

L'agent doit reprendre au premier checkpoint dont le statut n'est pas
« terminé », sans refaire les checkpoints validés et sans modifier la source
TypeScript.
