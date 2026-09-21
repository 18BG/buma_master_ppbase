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

## Transition vers C3

C3 a démarré. Le portage des domaines métier reste bloqué tant que C3 (auth)
et C4 (transactions/idempotence métier) ne sont pas validés. Le backup/import
de production reste hors périmètre.

## Checkpoint C3 — Authentification et permissions

Statut : en cours avancé ; login/JWT/permissions validés, fermeture bloquée par
une divergence de longueur minimale de mot de passe à la création.

Périmètre source couvert :

- `src/entries/index.pb.ts` : `GET /isConnected` et contexte auth des hooks ;
- `src/entries/master.user.ts` : `GET /master/user/health` ;
- `src/entries/master.utility.ts` et `src/entries/master.access.ts` :
  `users.role`, `roles.permission`, `isAdmin`, `buma_apps_roles.info.actions` ;
- migrations PocketBase `1738201707_created_agent_users.js` et règles
  `1741613060_updated_agent_users.js` ;
- règles de collections qui utilisent `@request.auth.role.permission.*`.

Fichiers ajoutés ou modifiés :

- `app/auth/context.py`, `app/auth/login.py`, `app/auth/middleware.py`,
  `app/auth/permissions.py`, `app/auth/__init__.py` ;
- `app/hooks/auth.py`, `app/hooks/register.py` ;
- `pb_migrations/1789236844_auth_compat.py` ;
- `tests/test_auth_contract.py`, adaptations de `tests/test_foundation.py` et
  `tests/test_poc_contract.py` ;
- `docs/contracts/authentication.md`, `docs/contracts/routes.json`,
  `docs/decisions-and-divergences.md` et cette tasklist.

Contrats et implémentation :

- `/isConnected` conserve `400 {"message":"false"}` sans authRecord et
  `200 {"message":"true"}` avec authRecord ; un token admin ne compte pas ;
- `/master/user/health` conserve `200` texte `true`/`false` ;
- `users` accepte username/password ; `agent_users` accepte email/password et
  username/password ; le format JWT/record est celui de PPBase/PocketBase ;
- `requireAdminOrRecordAuth` reproduit `401` sans token et `403` pour un modèle
  d'authentification non autorisé ;
- les règles natives reçoivent maintenant le rôle et les permissions live,
  avec des valeurs booléennes sûres pour éviter une requête PostgreSQL invalide
  quand un agent n'a pas de rôle ;
- les probes et le contexte ne retournent jamais `password_hash` ni `token_key`.

Preuves exécutées :

- `.venv/bin/pytest -p no:cacheprovider -q` : `18 passed` ;
- `.venv/bin/ruff check app tests scripts` : OK ;
- `.venv/bin/ruff format --check app tests scripts` : OK ;
- `make check` : OK (`18 passed`, Ruff et format OK) ;
- `make migrate-status` : 5 appliquées, 0 en attente, 0 orpheline ;
- `make verify-all` après C3 : 114/114 définitions et 42/42 vues OK ;
- `make migrate-up` sur PostgreSQL isolé : migration `1789236844_auth_compat.py`
  appliquée ;
- serveur PPBase réel : login users username, login agent_users username/email,
  JWT, refresh sans token `401`, probes legacy, owner `agent_users` `200` et
  autre token `404`, permission de rôle autorisée `200`, permission refusée
  `400` avec création non persistée ; fixtures synthétiques supprimées après
  validation.

Divergence et blocage :

- `PPBase 0.3.1` impose huit caractères dans `create_record()` pour toutes les
  collections auth, alors que la source configure `users` à cinq caractères.
  La migration fixe bien les options d'identité, mais l'écriture d'un mot de
  passe users de 5 à 7 caractères n'est pas encore fidèle.

Décision :

- continuer uniquement sur la correction ciblée de la validation auth users ;
- ne commencer aucun domaine métier ni C4 tant que cette divergence n'est pas
  résolue et testée en runtime ;
- backup/import de production toujours hors périmètre.

### Ré-audit editable PPBase — 2026-09-19

Le venv de BUMA charge maintenant bien le dépôt PPBase editable :

`/Users/omarsisss/Documents/wproject/protech/ppbase/ppbase/__init__.py`.

La correction PPBase est donc visible par BUMA (`validate_password_value` est
importable depuis ce runtime), et les tests unitaires PPBase
`tests/test_auth_parity_core.py` passent : `4 passed`.

La fermeture de C3 reste toutefois non validée par la preuve fonctionnelle :

- une collection auth avec `options.minPasswordLength=5`, mais sans champ
  système `password` dans `collection.schema`, rejette encore un mot de passe
  de 5 caractères avec `validation_min_text_constraint` / minimum 8 ;
- la migration BUMA `1789236844_auth_compat.py` écrit `minPasswordLength=5`,
  mais `ppbase.services.auth_service.get_password_field()` retombe sur le
  défaut 8 lorsque le champ système n'est pas présent dans le schéma persisté ;
- les tests d'intégration PPBase complets n'ont pas pu démarrer leur conteneur
  Docker dans ce contrôle (`PermissionError` sur le socket Docker).

Prochaine action exacte : corriger génériquement PPBase pour que les contraintes
du champ système auth soient dérivées de `collection.options.minPasswordLength`
quand le champ `password` n'est pas matérialisé, puis rejouer la création et la
mise à jour runtime BUMA avec 5/4 caractères sur une base PostgreSQL isolée.

### Ré-audit après correction PPBase — 2026-09-21

Le dépôt PPBase editable chargé par BUMA pointe toujours vers :

`/Users/omarsisss/Documents/wproject/protech/ppbase/ppbase/`.

La correction générique est maintenant présente dans PPBase :

- `normalize_auth_password_schema()` consomme `minPasswordLength` une seule
  fois et matérialise le champ système `password` ;
- le champ existant gagne sur l'ancienne option ;
- le `MigrationApp` et la préparation de base appliquent cette normalisation ;
- la validation runtime utilise ensuite `password.min` ;
- les tests PPBase `tests/test_auth_parity_core.py` passent (`4 passed`) ;
- les tests BUMA passent (`18 passed`) et Ruff/format sont OK.

Vérification déterministe sur les définitions BUMA :

- `users` est normalisé en champ `password.min = 5` ;
- `agent_users` est normalisé en champ `password.min = 8` ;
- `minPasswordLength` est retiré après conversion.

Blocage de la preuve finale : le runtime PostgreSQL ne peut pas être démarré
dans cette session, car le daemon Docker n'est pas lancé (`Cannot connect to the
Docker daemon`). La création/mise à jour HTTP réelle avec 5/4 caractères n'a
donc pas encore été rejouée après la correction PPBase.

Verdict C3 : correction source PPBase intégrée et vérifiée statiquement, mais
C3 BUMA reste ouvert jusqu'à la preuve runtime PostgreSQL suivante :

- `users` : 5 accepté, 4 refusé ;
- mise à jour `users` avec 5 accepté ;
- `agent_users` : 8 accepté, 7 refusé ;
- login/JWT/refresh/401/403/permissions sans régression.

### Reprise de session — audit du 2026-09-21

Le handoff a été repris sans modifier le dépôt TypeScript source. Le commit
source courant et la branche `fix/uv-paani-sup` restent
`530018cd20c2acbe88f38b3639e500b5271e2eb2`, identiques au manifeste.

Vérifications locales exécutées :

- dépôt BUMA : `.venv/bin/pytest -p no:cacheprovider -q` → `18 passed` ;
- dépôt BUMA : Ruff check et format check → OK ;
- dépôt PPBase editable : `tests/test_auth_parity_core.py` → `11 passed` avec
  un avertissement JWT de longueur de clé de test ;
- runtime Python chargé par BUMA :
  `/Users/omarsisss/Documents/wproject/protech/ppbase/ppbase/` ;
- normalisation déterministe : `users` accepte 5 et rejette 4, `agent_users`
  accepte 8 et rejette 7 au niveau de la validation du champ `password`.

Blocage runtime inchangé : Docker Desktop a été lancé, mais le démarrage du
conteneur `buma-master-ppbase-postgres` via `docker compose up -d postgres` a
été refusé par l’autorisation d’exécution. Aucun test HTTP/PostgreSQL n’a donc
été exécuté après la correction PPBase et aucune fixture n’a été créée.

Prochaine action exacte : avec Docker autorisé et le conteneur PostgreSQL
isolé démarré, appliquer `pb_migrations`, démarrer PPBase, puis vérifier par
HTTP la création/mise à jour `users` (5 accepté, 4 refusé), `agent_users`
(8 accepté, 7 refusé) et la non-régression login/JWT/refresh/401/403.
Jusque-là, C3 reste ouvert et C4/domaines métier restent bloqués.

### Tentative runtime PostgreSQL — 2026-09-21

La vérification runtime a finalement été exécutée avec Docker autorisé :

- `docker compose up -d postgres` → conteneur démarré et `healthy` ;
- migrations BUMA : 5 appliquées, 0 en attente, 0 orpheline ;
- PPBase démarré sur `127.0.0.1:8090` avec le dépôt editable ;
- lecture directe des métadonnées : `users.password.min=5`,
  `agent_users.password.min=8`, sans `minPasswordLength` résiduel ;
- création d'une fixture admin synthétique réussie ; elle a été supprimée
  après l'essai ; aucune fixture utilisateur n'a été persistée.

Résultat bloquant : l'appel runtime de `create_record()` pour `users` avec
`password=abcde`/`passwordConfirm=abcde` échoue avec
`validation_min_text_constraint` indiquant un minimum de 8 caractères, malgré
le champ persisté à 5. L'inspection du même objet chargé par PPBase montre que
`get_password_field(users)` renvoie le premier champ métier `users.name`,
renommé artificiellement `password`, au lieu du champ `password_field`.

Cause précise dans le dépôt PPBase editable :
`ppbase/services/auth_service.py:128-134` appelle
`_normalise_schema_field(raw)` avant de vérifier `name=password` et
`type=password`, alors que `_normalise_schema_field()` impose ces deux valeurs
à toute définition reçue. Les tests unitaires existants ne contiennent pas de
champ métier avant le champ password et ne détectent donc pas cette régression.

Fixtures et serveur nettoyés après le test. Décision : ne pas fermer C3 et ne
pas commencer C4/domaines métier. Prochaine action exacte dans le dépôt PPBase
editable : filtrer d'abord les définitions `name=password`/`type=password`,
ajouter une régression avec un champ métier placé avant password, puis rejouer
la vérification HTTP complète.

### Revalidation après correction PPBase — 2026-09-21

La correction PPBase editable est présente et vérifiée :

- `tests/test_auth_parity_core.py` : `14 passed` ;
- le champ password est recherché par `name=password` et `type=password` avant
  `_normalise_schema_field()` ;
- runtime direct : `users` 5 accepté, 4 rejeté ; mise à jour 5 acceptée, 4
  rejetée ; `agent_users` 8 accepté, 7 rejeté ;
- runtime HTTP avec fixtures synthétiques : créations et mises à jour 5/4 et
  8/7 conformes, login username HTTP 200, refresh HTTP 200 ;
- les fixtures admin/users/agent et le serveur ont été nettoyés après test.

Nouvelle divergence détectée côté adaptation BUMA : les tokens PPBase portent le
claim canonique `type=auth`, mais `app/hooks/auth.py` et
`app/auth/context.py` exigent littéralement `type=authRecord`. Les routes
`/isConnected` et `/master/user/health` répondent donc respectivement HTTP 400
`{"message":"false"}` et HTTP 200 `false` avec un token utilisateur pourtant
valide. `authRecord` est le contexte PocketBase, pas le type du claim JWT.

Décision : la correction PPBase est validée ; C3 BUMA reste ouvert uniquement
pour l'adaptation de ce contexte et la revalidation des probes/permissions.
Ne pas modifier PPBase pour contourner cette divergence ; corriger l'adaptateur
du dépôt BUMA lors du prochain lot autorisé.

### Correction de l'adaptateur auth BUMA — 2026-09-21

Fichiers BUMA modifiés : `app/auth/context.py`, `app/hooks/auth.py` et
`tests/test_auth_contract.py`.

- ajout de `is_record_auth()` qui accepte `type=auth` (claim PocketBase/PPBase)
  et `type=authRecord` (compatibilité legacy) ;
- `load_live_auth_context()`, `/isConnected` et `/master/user/health` utilisent
  maintenant ce même test ;
- test de middleware ajouté pour un token standard `type=auth` ;
- BUMA : `19 passed`, Ruff/format et `git diff --check` OK.

Preuve HTTP locale après correction avec fixtures synthétiques nettoyées :

- login `users` par username : HTTP 200 ;
- `/isConnected` : HTTP 200 `{"message":"true"}` ;
- `/master/user/health` : HTTP 200 `true` ;
- refresh avec token : HTTP 200 ;
- refresh sans token : HTTP 401.

La source PocketBase v0.40.4 confirme que le JWT standard utilise
`TokenTypeAuth = "auth"`; l'ancien BUMA ne testait pas le claim JWT mais lisait
`requestInfo(...).authRecord`. La correction est donc bien dans l'adaptateur
BUMA, pas dans PPBase.

### Clôture C3 — 2026-09-21

Statut : terminé. Aucun blocage C3 restant.

Preuve finale de permissions avec des JWT standards `type=auth` et fixtures
synthétiques :

- rôle avec `role.create=true` : création `roles` HTTP 200 et ligne persistée ;
- rôle avec `role.create=false` : HTTP 400 `validation_rule_failed`, aucune
  ligne persistée ;
- requête anonyme : HTTP 400 contrôlé, aucune ligne persistée (plus de HTTP 500
  PostgreSQL après la fourniture de permissions booléennes `false`) ;
- règle owner `agent_users` : propre record HTTP 200, autre record HTTP 404 ;
- refresh sans token : HTTP 401 ; middleware `requireAdminOrRecordAuth` : 401
  anonyme et 403 modèle non autorisé dans les tests de contrat.

Gate finale C3 :

- BUMA : `21 passed`, Ruff, format et `git diff --check` OK ;
- PPBase auth ciblé : `14 passed` ;
- migrations PostgreSQL : 5 appliquées, 0 en attente, 0 orpheline ;
- schéma complet : 114/114 définitions, 0 mismatch après prise en compte de la
  matérialisation runtime du champ auth `password` ;
- vues PostgreSQL : 42/42 OK ;
- fixtures et serveur local nettoyés ; aucune donnée de production importée.

Décision : passer à C4 — transactions, idempotence, rollback et concurrence.
Le backup/import de production reste hors périmètre.

## Checkpoint C4 — fondation transactions/idempotence

Statut : terminé pour la fondation générique ; aucun flux métier financier
porté dans ce checkpoint.

Source couverte : `src/entries/master.idempotency.ts`, usages de
`system_id`, `mobile_message_id` et `dao.runInTransaction` dans les flux sale,
return et stockage/conversion.

Fichiers ajoutés ou modifiés :

- `app/transactions/__init__.py` ;
- `app/transactions/idempotency.py` ;
- `pb_migrations/1789236845_c4_transaction_idempotency.py` ;
- `scripts/c4_idempotency_runtime.py` ;
- `tests/test_idempotency.py` ;
- `docs/contracts/transaction-invariants.md` et cette tasklist/ledger.

Comportement porté :

- `build_idempotency_key()` normalise `idem:<operation>:<key>` comme la source ;
- lookup par `system_id`, ancienne clé et `mobile_message_id`, borné par
  `type`/`author` ;
- index uniques partiels pour les clés non vides ;
- verrou `pg_advisory_xact_lock` par clé, type et auteur pour sérialiser les
  tentatives concurrentes.

Preuves exécutées :

- migration C4 appliquée : 6 migrations, 0 en attente, 0 orpheline ;
- tests unitaires idempotence : `3 passed` ;
- runtime `scripts/c4_idempotency_runtime.py` via le coordinateur PPBase :
  rollback=OK, concurrency=OK, lookup=OK (clé courante/legacy/mobile),
  cleanup=OK ;
- suite BUMA : `24 passed` ; Ruff, format et `git diff --check` OK.

Le checkpoint C4 fondation est fermé. Les flux vente, retour, conversion,
stock, wallet et caisse nécessitent chacun leur checkpoint métier avec tests
de solde/quantité, rollback et concurrence dédiés.

## Checkpoint C5 — settings et référentiels

Statut : terminé pour le seed de référentiels et le lecteur settings ; aucune
configuration métier inventée.

Source couverte : `master.settings.ts`, `master.setup.ts`,
`master.constants.ts`, `master.tables.ts` et `buma.tables.ts`.

Fichiers ajoutés :

- `app/settings/__init__.py` ;
- `app/settings/service.py` ;
- `pb_migrations/1789236846_c5_reference_defaults.py` ;
- `tests/test_c5_referentials.py` ;
- `docs/contracts/settings-referentials.md`.

Defaults portés de manière idempotente :

- `STOCK_PRINCIPAL` dans `buma_article_stockage` ;
- `PAYMENT_CASH_12`, `PAYMENT_MOBILE_`, `PAYMENT_CARTE_1` ;
- `CUSTOMER_DIVERS`, `SUPPLIER_DIVERS` ;
- les 18 entrées de `buma_apps_roles` avec les actions source.

Preuves : migration C5 appliquée, second passage sans migration en attente,
IDs retrouvés, 18 rôles présents, settings absent rejeté explicitement,
`26 passed`, Ruff/format OK, schéma complet 114/114 intact.

Divergence documentée : le bootstrap PocketBase source supprime les rôles
applicatifs existants avant de les recréer ; le portage PPBase est non destructif
(`ON CONFLICT DO NOTHING`) pour ne pas effacer des données hors bootstrap.
`CONFIGURATION_1` n’est pas créé sans valeur source explicite.

Décision corrigée : C5 se limite à settings/référentiels et est terminé. Le
groupe clients/fournisseurs/employés/PDV/SIM/agents appartient à C6.

### Sous-checkpoint C6.1 — clients — 2026-09-21

Statut : terminé.

Source couverte : `master.customer.ts`, `master.customer.functions.ts` et les
règles de `buma_customer`.

Fichiers ajoutés ou modifiés :

- `app/customers/__init__.py` ;
- `app/customers/service.py` ;
- `app/hooks/customer.py` et `app/hooks/register.py` ;
- `tests/test_customer_contract.py` ;
- `docs/contracts/customer.md`.

Comportement porté :

- `POST /master/customer/creates` ;
- refus `401` sans authentification et `403` sans `customer.create`/admin ;
- prénom obligatoire ;
- `status=true`, `author=<utilisateur>` ;
- `customer_no` séquentiel sous verrou advisory ;
- batch atomique via le coordinateur transactionnel PPBase.

Preuve HTTP PostgreSQL avec fixtures synthétiques :

- batch autorisé : HTTP 200 et client persisté ;
- batch avec un prénom vide : HTTP 400, aucune écriture partielle ;
- rôle refusé : HTTP 403 ;
- `customer_no=1` et auteur correct sur la fixture ;
- fixtures et serveur nettoyés après test.

Gate : suite BUMA `28 passed`, Ruff/format OK, schéma 114/114 intact.

Décision : conserver C6 ouvert et poursuivre les fournisseurs, employés, PDV,
SIM et agents dans le même checkpoint métier.

### Sous-checkpoint C6.2 — fournisseurs — 2026-09-21

Statut : terminé.

Source couverte : `src/entries/supplier.ts`, route `/api2/supplier/creates`,
permission `supplier.create` et création liée `buma_person_entity`/
`buma_supplier`.

Fichiers ajoutés ou modifiés :

- `app/suppliers/__init__.py` ;
- `app/suppliers/service.py` ;
- `app/hooks/supplier.py` et `app/hooks/register.py` ;
- `tests/test_supplier_contract.py` ;
- `docs/contracts/supplier.md`.

Preuve HTTP PostgreSQL avec fixtures synthétiques :

- batch autorisé : HTTP 200 et relation `person_entity` persistée ;
- type invalide : HTTP 400, transaction rollbackée, aucune écriture partielle ;
- rôle refusé : HTTP 403 ;
- fixtures et serveur nettoyés après test.

Gate : suite BUMA `29 passed`, Ruff/format OK, schéma 114/114 intact.

Décision : conserver C6 ouvert et poursuivre employés, PDV, SIM et agents dans
le même checkpoint métier.

### Sous-checkpoint C6.3 — employés, PDV, SIM et agents — 2026-09-21

Statut : terminé. Le checkpoint C6 complet est maintenant fermé.

Portage ajouté :

- `app/employees/service.py`, `app/hooks/employee.py` :
  `/master/employee/create` et `/api2/employee/create` ;
- `app/pdv/service.py`, `app/hooks/pdv.py` : `/master/pdv/newSim` ;
- `app/hooks/register.py` et `tests/test_employee_pdv_contract.py` ;
- `docs/contracts/people-pdv-agents.md`.

Preuve HTTP PostgreSQL groupée :

- employé autorisé : HTTP 200, relation personne/employé persistée ;
- type employé invalide : HTTP 400, rollback sans écriture partielle ;
- PDV/SIM autorisé : HTTP 200, SIM liée au PDV ;
- PDV refusé : HTTP 403 ;
- agent natif `POST /api/collections/buma_agent/records` : HTTP 200 ;
- fixtures et serveur nettoyés.

La source ne contient pas de route métier agent dédiée ; sa collection est donc
couverte par le CRUD natif PPBase et ses règles de collection.

Gate C6 complète : suite BUMA `30 passed`, Ruff/format OK, schéma 114/114
intact.

Décision corrigée : fermer C6 et passer à C7 article/stock/stockage.

## Checkpoint C8 — Wallet et caisse — commencé prématurément, suspendu

Routes et services portés :

- `app/finance/service.py` ;
- `app/hooks/finance.py` ;
- wallet deposit, withdraw, transfer ;
- caisse deposit, withdraw, transfer.

Comportement : soldes verrouillés avec `FOR UPDATE`, mutations et transactions
liées dans le coordinateur transactionnel PPBase, permissions `wallet.*` et
`caisse.transaction`, rollback automatique en cas d’exception.

Preuve HTTP PostgreSQL avec fixtures synthétiques :

- wallet deposit/withdraw/transfer : HTTP 200 ;
- caisse deposit/withdraw/transfer : HTTP 200 ;
- soldes finaux cohérents et conservation des montants vérifiée ;
- utilisateur sans permission : HTTP 403 ;
- fixtures et serveur nettoyés après test.

Gate provisoire : suite BUMA `31 passed`, Ruff/format OK.

Ce code a été commencé avant C7 par erreur de lecture du plan. Il est conservé,
mais C8 est suspendu et ne sera ni complété ni fermé avant C7.

## Transition vers C7

C7 a été réalisé et fermé dans la section suivante. C8 reste suspendu jusqu'à
la fin de ce checkpoint, conformément à l'ordre officiel du plan.

## Checkpoint C7 — article, stock et stockage — 2026-09-21

Statut : terminé. Le groupe complet est fermé ; C8 reste suspendu pour ses
sessions de caisse, annulations et compensations.

Source couverte : `src/entries/master.article.ts`,
`master.article.functions.ts`, `master.stockage.ts`,
`master.stockage.functions.ts` et `master.stockage.types.ts`, au commit gelé
`530018cd20c2acbe88f38b3639e500b5271e2eb2`.

Fichiers ajoutés ou modifiés :

- `app/stockage/service.py` et `app/stockage/__init__.py` ;
- `app/hooks/stockage.py`, enregistré dans `app/hooks/register.py` ;
- `tests/test_c7_stockage_contract.py` ;
- `scripts/c7_stockage_runtime.py` ;
- `docs/contracts/article-stockage.md`, `docs/contracts/routes.json`,
  `docs/contracts/transaction-invariants.md` et `docs/project-structure.md` ;
- cette tasklist et ce ledger.

Routes et comportement portés :

- `/master/article/creates` : validation du nom/prix, création de l'article et
  ligne de stock principale à quantité zéro ;
- `/master/stockage/movement` et `batch-movement` : entrées/sorties,
  validation du stock disponible et rollback complet du batch ;
- `/master/stockage/transfer` et `cancel_transfer` : stocks source/destination,
  `buma_transfer`, `buma_transfer_transaction` et transactions de stock ;
- `/master/stockage/convert` et `cancel_conversion` : quantité, bonus NAFAMA,
  verrouillage, idempotence C4 et restauration ;
- `/master/stockage/wallet_uv` et `uv_wallet` : mutation atomique wallet/UV.

Les opérations de quantité passent par le coordinateur de transaction PPBase.
Les lignes de stock sont verrouillées avec `FOR UPDATE`, les couples
stockage/article avec un verrou advisory et les conversions avec le verrou
idempotent `system_id`/`mobile_message_id`.

Preuves exécutées :

- `docker compose up -d postgres` : conteneur healthy ;
- migrations : 7 appliquées, 0 en attente, 0 orpheline ;
- `.venv/bin/pytest -p no:cacheprovider -q` : `34 passed` ;
- Ruff check et format check : OK ; `git diff --check` : OK ;
- `scripts/c7_stockage_runtime.py` :
  `article=OK movement=OK batch_rollback=OK transfer=OK
  conversion_idempotency=OK cancellation=OK wallet_uv=OK permission=OK
  concurrency=OK cleanup=OK` ;
- aucune fixture C7 ne reste en base après le scénario ; aucune donnée de
  production n'a été importée.

Écarts documentés dans `docs/contracts/article-stockage.md` : l'export corrigé
ne liste pas les valeurs historiques `WALLET_TO_UV`, `UV_TO_WALLET` et
`CONVERSION_CANCEL_IN/OUT` du select `buma_transaction`, ni les champs
`cancel_author`/`cancel_date` de `buma_conversion`. PPBase conserve les types
historiques via une insertion interne contrôlée, marque la conversion inactive
et garde l'auteur/date dans les lignes d'audit/timestamps, sans modifier la
baseline officielle.

Décision : fermer C7 et reprendre ensuite C8 avec ses sessions ouverture/
fermeture de caisse et annulations/compensations. C9 reste bloqué jusqu'à la
fermeture de C8.

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
