# Journal de progression

Statut courant au **2026-09-28 : C10 terminé sur décision explicite de
l'utilisateur (D-0020)**, après sa confirmation de la correction PPBase.
Le prochain checkpoint ouvert est C11. Les anciennes mentions C10 ouvert/en
attente ci-dessous sont historiques ; voir la dernière entrée de clôture.

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

## Checkpoint C8 — wallet et caisse — 2026-09-23

Statut : terminé pour le périmètre wallet/caisse autonome. Les routes caisse
qui dépendent des ventes, retours et rapports sont explicitement réservées à
C9 et ne sont pas comptées dans cette fermeture.

Source couverte : `src/entries/master.wallet.ts`,
`master.wallet.functions.ts`, `master.caisse.ts`,
`master.caisse.functions.ts` et `master.caisse.functions2.ts`, sans modifier le
dépôt TypeScript gelé.

Fichiers ajoutés ou modifiés :

- `app/finance/service.py` : wallet, sessions caisse, soldes, transferts,
  annulations, corrections, versements, dépenses, banque, dettes, accès et
  vérification de session ;
- `app/hooks/finance.py` : routes C8 et leurs permissions ;
- `tests/test_finance_contract.py` ;
- `scripts/c8_finance_runtime.py` ;
- `docs/contracts/wallet-caisse.md`, cette tasklist et ce ledger.

Contrats C8 couverts :

- wallet : deposit, withdrawal, transfer, cancel_transaction, updateNote ;
- caisse/session : requestOpen, openCaisse, requestClose, closeCaisse,
  deposit, withdraw, transfer, cancel ;
- opérations internes : correction, versement, dépense, versement bancaire et
  annulation, désallocation, updateAmount, changement de client/note/caissier,
  archive de type de dépense, employee debt/repayment, verifySession et
  accessible.

Les mutations utilisent `run_record_storage_transaction`, les lignes de
soldes/session sont verrouillées avec `FOR UPDATE`, les compteurs de
transactions avec des verrous advisory et les doublons avec les fenêtres de
détection de la source. Les réponses et messages principaux ont été réalignés
sur les enveloppes legacy (`Requete envoyer`, `Transaction reussis`,
`Transaction effectuée`, `Transaction annulée`, etc.).

Preuves exécutées :

- `scripts/c8_finance_runtime.py` :
  `auth=OK wallet_crud=OK wallet_cancel=OK caisse_session=OK
  caisse_crud=OK caisse_transfer=OK caisse_cancel=OK correction=OK
  versement=OK expense=OK bank=OK employee_debt=OK verify=OK accessible=OK
  permission=OK concurrency=OK cleanup=OK` ;
- login/401, permissions, rollback transactionnel, sessions, annulations,
  dettes, changement client, vérification de session et nettoyage PostgreSQL ;
- suite locale : `36 passed`, Ruff/format et `git diff --check` OK ;
- migrations : 7 appliquées, 0 en attente, 0 orpheline ; schéma 114/114 et
  vues 42/42 inchangés ;
- aucune fixture C8 persistée après le scénario.

Routes explicitement reportées à C9 : `/master/caisse/return`,
`/master/caisse/versement_uv`, `/master/caisse/add_payment_to_sale`,
`reportByExpenseType` et `reportByCaisseType`, car elles dépendent des familles
retour, paiement, vente et rapports.

Comportement source/schéma C8 documenté en D-0008 : aucune correction du
schéma PocketBase ou du TypeScript n'est appliquée. PPBase conserve les valeurs
source et le comportement des clés non déclarées ; `DEBT` n'est pas normalisé.

Décision : fermer C8 pour son périmètre autonome et passer à C9, en conservant
la règle de parité stricte. C9 devra intégrer les routes transverses reportées
et les flux vente/recharge/achat/paiement/retour/rapports avant toute clôture.

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

## Note de reprise — C9 interrompu le 2026-09-23

La session précédente a commencé C9 après la fermeture de C8 et a ajouté le
socle `app/commerce/service.py`, `app/hooks/commerce.py`, leur enregistrement,
`tests/test_commerce_contract.py` et le brouillon
`scripts/c9_commerce_runtime.py`. Le dernier point atteint est la création de
ce script de preuve ; le contexte a ensuite dépassé la limite avant toute
exécution de `scripts/c9_commerce_runtime.py`.

État constaté lors de la reprise : les 11 routes cœur vente/recharge/achat/
retour sont enregistrées ; les 18 routes C9 restantes (paiements transverses,
rapports, informations/statistiques et mises à jour retour) ne le sont pas.
`pytest -p no:cacheprovider -q` passe (`38 passed`) et `git diff --check` passe,
mais le script de runtime C9 n'a aucune preuve d'exécution. Le contrôle Ruff
actuel reste ouvert : le nouveau script C9 et quatre tests d'enregistrement
existants nécessitent encore le formatage, et le script C9 contient aussi des
diagnostics de longueur de ligne.

Référence retrouvée : tâche « salut »,
`01a0c3c7-6db7-7233-b918-b5b5c4722e19`, tour interrompu
`01a0ce86-255e-7ec3-bb3e-62aceb7ca46b`. Dernier ajout réussi à 14:14:36 UTC,
erreur `context_length_exceeded` à 14:14:43 UTC. Le dernier résultat historique
à 14:11:33 UTC était `38 passed` et Ruff OK, avant ajout du script runtime.

Les dix fichiers du tour interrompu sont `app/commerce/__init__.py`,
`app/commerce/service.py`, `app/hooks/commerce.py`, `app/hooks/register.py`,
`scripts/c9_commerce_runtime.py`, `tests/test_commerce_contract.py`,
`tests/test_auth_contract.py`, `tests/test_customer_contract.py`,
`tests/test_foundation.py` et `tests/test_poc_contract.py`. Les autres
modifications non commitées préexistaient à ce tour, notamment C8.

Cette session de reconstitution ne modifie que ce ledger et la tasklist.
Commandes/lectures : procédure de handoff, `git status --short --branch`,
`git diff --stat`, lecture ciblée du journal de l'ancienne session,
comparaison AST des routes avec `source-inventory.json`,
`git -C ../buma_master_hooks rev-parse HEAD` et statut source. La source reste
propre sur le commit du manifeste `530018cd20c2acbe88f38b3639e500b5271e2eb2`.

Vérifications locales actuelles :

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  `38 passed in 0.56s` ;
- `.venv/bin/ruff check --no-cache --output-format json app tests scripts` :
  20 diagnostics dans `scripts/c9_commerce_runtime.py` (1 I001, 19 E501) ;
- `.venv/bin/ruff format --no-cache --check app tests scripts` : 5 fichiers
  à formater (script C9 et les quatre tests d'enregistrement ci-dessus),
  60 déjà formatés ; `git diff --check` : OK.

Contrats comparés dans cette reconstitution : méthodes et chemins de
`master.sale.ts`, `master.purchase.ts`, `master.retour.ts` et des cinq routes
transverses de `master.caisse.ts` via l'inventaire gelé. Les payloads,
enveloppes, messages/codes, permissions, effets hooks, persistance, rollback,
concurrence et opérations SDK/CRUD C9 restent à vérifier contre la source et
en runtime. Les deux tests C9 existants inspectent surtout le texte source ;
le brouillon runtime vérifie principalement des succès HTTP et ne couvre pas
encore la gate complète. Aucun serveur ni fixture C9 lancé dans cette session.

Décision : arrêter ici la reconstitution demandée ; C9 reste ouvert.
Prochaine action d'implémentation : corriger les diagnostics statiques,
compléter les assertions et le nettoyage garanti du script runtime, vérifier
le socle en PostgreSQL/HTTP isolé, puis porter les 18 routes manquantes et
exécuter la gate C9 complète avant clôture. C10 et l'import restent ouverts.

## Checkpoint C9 — commerce et rapports — 2026-09-23

Statut : portage et gate locale terminés pour les 29 routes listées dans
`docs/c9-runtime-report.json`. Clôture limitée aux contrats et preuves ci-dessous,
avec la différence D-0009 explicitement documentée. La parité absolue sur les
échecs des anciens flux non transactionnels, et le cutover, restent ouverts.

### Périmètre et provenance

Reprise du code interrompu après C8 : 11 routes existaient mais n'avaient
aucune preuve runtime. Les 18 routes restantes sont maintenant portées :
4 retour, 9 vente/recharge/statistiques/rapports et 5 caisse transverses.

Source lue et comparée : `master.sale.ts`, `master.sale.functions.ts`,
`master.sale.types.ts`, `master.purchase.ts`, `master.purchase.functions.ts`,
`master.retour.ts`, `master.retour.functions.ts`, `master.retour.type.ts`,
`master.caisse.ts`, `master.caisse.functions.ts`, `master.caisse.functions2.ts`,
`master.utility.ts`, `master.constants.ts`, inventaire et baseline corrigée.
Le dépôt TypeScript reste propre sur la branche `fix/uv-paani-sup`, commit
`530018cd20c2acbe88f38b3639e500b5271e2eb2` identique au manifeste. Aucun
inventaire source ni migration officielle n'a été régénéré.

Fichiers repris/ajoutés/modifiés pendant cette session :

- `app/commerce/service.py`, nouveaux `app/commerce/returns.py` et `reports.py` ;
- `app/hooks/commerce.py` ; l'enregistrement `app/hooks/register.py` et
  `app/commerce/__init__.py` de la session précédente sont conservés ;
- `tests/test_commerce_contract.py` ; formatage des listes de modules dans
  `tests/test_auth_contract.py`, `test_customer_contract.py`,
  `test_foundation.py`, `test_poc_contract.py` ;
- `scripts/c9_commerce_runtime.py`, `docs/c9-runtime-report.json` ;
- `docs/contracts/commerce.md`, `README.md`, `routes.json`,
  `transaction-invariants.md` ;
- `docs/decisions-and-divergences.md`, `docs/test-matrix.md`,
  `docs/project-structure.md`, `docs/implementation-plan.md`,
  `docs/migration-ppbase-tasklist.md`, ce ledger et `AGENTS.md`.

Les changements C8 déjà présents (`app/finance/*`, `app/hooks/finance.py`,
`scripts/c8_finance_runtime.py`, etc.) n'ont pas été annulés ou réécrits.
Aucun commit, staging, reset ou import de production.

### Contrats comparés et corrigés

- 29 méthodes/chemins comparés à l'inventaire gelé ; noms de paramètres legacy
  préservés : `articleStock`/`article_stock`, `sale_id`/`saleId`,
  `buyId`/`purchaseId`, `walletId`, `returnId` et IDs de transaction/vente/retour
  conservés distincts.
- Vente : `200 {message:"Operation effectuee"}` ; `kind=AUTRES` conservé,
  état par défaut, dette/paiement initial et timestamps autorisés. Recharge :
  `{id}` de transaction, supplément agent et bonus PAANI distincts, annulation
  sans état `CANCELED` ajouté et préfixe d'erreur exact.
- Achat : cinq réponses/messages, réception NAFAMA, snapshots wallet,
  changement fournisseur, refus d'annuler un achat payé et compensations.
- Retour : `{id}` transaction, lecture avec sérialisation native et relations,
  `updatePDV` 200 vide, stock/due/statuts legacy, création « Client Inconnu »
  uniquement prévue par le source, interdiction non-admin d'annuler un retour
  payé et 404 métier explicites.
- Caisse transverse : retour `{message:"Ok"}`, versement UV simple/groupé,
  compensation caisse et crédit cible, snapshots/notes/numéros, paiement vente
  avec enveloppe `{success,message,caisse_transaction,payment,sale}`.
- Permissions/enveloppes : 401 anonyme, refus 403/400 selon la route, flag absent
  distinct de `=== false`, permission wallet supplémentaire pour achat payé,
  rapports publics et `retour/info` sans garde de lecture ajouté.
- Rapports : JSON camelCase, GROUP BY, filtres, pagination, bornes temporelles,
  revenus/bonus/coûts et distinction du calcul `totalMissing` source conservés.
- Effets hooks/CRUD : les routes custom portent leurs propres agrégats ; CRUD
  natif retour ne déclenche pas implicitement les mutations de stock/dette.
  Login, GET, liste filtrée, expand, PATCH, POST et DELETE natifs exercés.

### Commandes et preuves exécutées

- `docker compose ps` : PostgreSQL isolé `healthy` sur 55434 (accès socket
  initial refusé par le sandbox puis obtenu via exécution autorisée).
- `make start` : PPBase 0.3.1 sur `127.0.0.1:8090`, jobs internes désactivés.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **69 passed** ; Ruff check : OK ; format check : **67 files already formatted** ;
  `git diff --check` : OK.
- `.venv/bin/python scripts/c9_commerce_runtime.py` :
  **C9 runtime PASSED; 29 distinct called paths**, **cleanup=OK**.
  Contrôles : effets financiers/stock, antidatage, bonus PAANI/supplément agent,
  annulations, idempotence/retry et concurrence recharge/retour, paiement
  concurrent sans dépassement, réception NAFAMA, identités nouvelles prévues
  par la source, retour payé/groupé, rollback de groupe, rapports non vides,
  permissions et CRUD natif/filtre/expand.
- Injection d'une erreur de stockage après création d'une vente et d'un premier
  mouvement : aucun effet résiduel vente/client/stock ; trigger et fonction
  de test supprimés en `finally`. Cette atomicité est l'écart D-0009, pas une
  prétention que toutes les fonctions TypeScript avaient cette atomicité.
- `.venv/bin/python scripts/c8_finance_runtime.py` rejoué avec C9 chargé :
  tous les contrôles C8 passent (auth, wallet, sessions, caisse, annulations,
  correction, versement, dépenses, banque, dettes, vérification, accès,
  permissions, concurrence, nettoyage).
- `make migrate-status` : **7 applied, 0 pending, 0 orphaned**.
- `make verify-all` : **114/114 définitions**, aucun mismatch/relation manquante,
  **42/42 vues**, aucun échec. Rapports de schéma/vues inchangés.
- Serveur de cette session arrêté proprement après la gate ; fixtures C9/C8,
  config synthétique éventuellement créée et trigger de faute nettoyés.

Les premiers essais ont identifié puis corrigé : identifiants camelCase non
quotés dans l'ancien brouillon de fixtures ; bonus recharge absent dans le
processus serveur encore chargé ; deux guillemets SQL manquants dans les
nouveaux rapports ; casse physique PostgreSQL `authorname`. Aucun de ces essais
échoués n'est compté comme preuve passante. La preuve finale ci-dessus porte
sur le code rechargé et les assertions complètes.

### Écarts, limites et décision

D-0009 décrit l'atomicité ajoutée aux anciens agrégats vente/achat non
transactionnels, son impact client et sa régression. Les écritures historiques
`AUTRES`/`DEBT` et les champs inconnus sont conservés sans migration correctrice
ni changement du dépôt PocketBase. Une lecture comparée du TypeScript et une
gate PPBase locale ne remplacent pas une comparaison des deux serveurs avec
fixtures identiques.

Restent non prouvés : parité exhaustive des erreurs techniques moteur,
toutes les formes de payload malformé, toutes les combinaisons de règles/SDK,
files/realtime et comportement de production. C10 `/api2/*`/legacy et
backup/import restent ouverts. Décision : clôturer le périmètre C9 porté et
testé avec ces limites explicites ; prochaine famille de code C10. Avant un
cutover exigeant une stricte parité des erreurs tardives, traiter D-0009.

## Correction C9 — D-0009 refusé, sauvegardes legacy rétablies — 2026-09-23

L'utilisateur précise que la réécriture doit conserver les problèmes éventuels
de l'ancien backend. La différence D-0009 n'est donc pas une option acceptée.
Les sept fonctions vente/achat concernées sont reprises dans
`app/commerce/sequential.py`, appelées depuis `app/commerce/service.py` :
vente/création et annulation, achat/création, réception, annulation, ajout de
paiement et annulation de paiement wallet. Chaque sauvegarde persiste à l'endroit
prévu par `master.sale.functions.ts` / `master.purchase.functions.ts`, sans
transaction ou verrou ajouté autour de l'ensemble.

La source a été relue pour les chemins d'échec : fournisseur débité avant
validation wallet, achat marqué reçu avant lecture des articles, transaction
wallet persistée avant débit, annulation enregistrée avant remboursement et
mouvements stock sauvegardés avant leur transaction associée. Recharges,
retours et paiements caisse explicitement transactionnels restent atomiques.

Preuve : `scripts/c9_commerce_runtime.py` modifié et rejoué avec succès sur
PPBase/PostgreSQL local : **29 routes, cleanup=OK**. L'échec de la seconde
transaction d'article laisse désormais la vente de 150, la dette de 150,
les deux débits stock (2+13) et seulement la première transaction, comme
l'ordre source. L'achat sans wallet conserve le débit fournisseur de 6 avant
son HTTP 400. La réception sans article conserve `received=true` ; le retry
répond qu'il est déjà reçu. Ces effets étaient auparavant annulés à tort.

Commandes : lecture des documents de handoff/source, `git status`, source HEAD
`530018c` propre, `.venv/bin/ruff check app tests scripts`, format,
`PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` (**69 passed**),
`make start`, `.venv/bin/python scripts/c9_commerce_runtime.py` (passant).
Fichiers métier changés : `service.py`, nouveau `sequential.py`, script C9.
Rapport C9 actualisé ; documentation D-0009/tasklist/contrats actualisée.

Décision : D-0009 corrigé, aucune acceptation d'un durcissement du legacy.
Poursuivre C10. Les preuves précédemment listées restent limitées à leurs
scénarios ; l'ancienne assertion de rollback global C9 est remplacée par la
preuve des sauvegardes partielles. Source TypeScript, baseline et production
inchangées.

## Checkpoint C10.1 — SQL legacy — 2026-09-23

Statut : terminé pour le sous-lot SQL uniquement ; C10 global reste ouvert.

Source couverte : `src/entries/index.pb.ts` (`/sql`, `/api/sql2`) et
`src/entries/master.article.ts` (`/master/article/executeSql`,
`/master/article/deleteSql`). Le commit source reste
`530018cd20c2acbe88f38b3639e500b5271e2eb2`, dépôt TypeScript propre.

Fichiers : `app/hooks/legacy_sql.py`, `app/hooks/register.py`,
`tests/test_legacy_sql_contract.py`, `scripts/c10_legacy_sql_runtime.py`,
`docs/contracts/legacy-sql.md`, `docs/c10-sql-runtime-report.json`,
`docs/contracts/routes.json`, `docs/decisions-and-divergences.md`, cette
migration/tasklist et les contrats C10.

Contrats comparés : méthodes/chemins publics, mot de passe `SuperSakone`,
projection/type coercion DynamicModel, erreurs de requête, suppression et
mutation de collection. PocketBase 0.22.27 jetable a confirmé : `/api/sql2`
répond HTTP 200 JSON `null` sans exécuter SQL ; `/sql` façonne les résultats
selon `fields`; les routes article mutent/suppriment réellement la collection.
Ce défaut est conservé en PPBase ; aucune authentification ou validation SQL
supplémentaire n'a été ajoutée.

Preuves : `.venv/bin/python scripts/c10_legacy_sql_runtime.py` passe
(`raw_projection=OK password=OK broken_sql2_null=OK view_update=OK
collection_delete=OK restore=OK`). La vue `buma_sql_table` est restaurée et
la collection de test aléatoire supprimée. `pytest` global passe ensuite à
`71 passed`, Ruff/format et `git diff --check` OK. Migrations/schema/vues
restent `7/0/0`, `114/114`, `42/42`.

Limites : SQL SQLite arbitraire, toutes les erreurs moteur, inférences de
colonnes et routes `/api2/*`/hooks legacy restent ouverts. Décision : poursuivre
C10 avec les routes legacy `/api2` et les hooks de middleware, sans corriger les
problèmes du backend source.

## Checkpoint C10.2 — `/api2/stockage` legacy — 2026-09-23

Statut : terminé pour les cinq routes stockage legacy ; C10 global reste ouvert.

Source couverte : `src/entries/stockage.ts`, `getStockageRole`,
`transferStockage`, `getStockInfo`, `addArticle`, `removeArticle` et
`convertStock`. Portage : `app/legacy/stockage.py`,
`app/hooks/legacy_stockage.py`, registration/tests et
`scripts/c10_stockage_runtime.py`.

Contrats et défauts préservés : transfer séquentiel et stock négatif, absence de
garde transfer/remove, `stock_info` qui lit `stockId` alors que le chemin expose
`:name`, add_article avec status false omis par le Record interne, remove_article
silencieux après exception et convert qui renvoie 200 `okay` après erreur.
Permission explicite `update=false` produit 401 pour add ; conversion exige
`canManage && canConvert` et NAFAMA applique `ceil(5%)`.

Preuve : `make start`, `.venv/bin/python scripts/c10_stockage_runtime.py` :
**5 routes, effets persistés, échecs partiels, permissions et cleanup OK**.
Tests globaux : **78 passed**, Ruff/format/diff OK. Les fixtures/collections de
ce scénario sont supprimées. Rapport : `docs/c10-stockage-runtime-report.json`.

Décision : poursuivre C10 avec les routes `/api2/caisse`, customer/PDV,
sale/purchase/return et les hooks legacy de middleware. Aucune correction du
source n'est acceptée.

## Checkpoint C10.3 — hooks CRUD natifs legacy — 2026-09-23

Statut : terminé pour les hooks natifs portés ; C10 global reste ouvert.

Source couverte : `index.pb.ts`, `caisse.ts`, `master.employee.ts` et
`master.middleware.ts`. Fichiers : `app/hooks/legacy_records.py`,
`tests/test_legacy_records_contract.py`, `scripts/c10_native_hooks_runtime.py`,
`docs/contracts/legacy-records.md`, D-0012 et cette tasklist/ledger.

Contrats : auteur/statut sur le chemin CRUD pour les collections source,
`buma_caisse.state=CLOSE`, dette employé (débit avant création, UNPAID),
paiement dette partiel, protection des champs SIM avec transaction, et neuf
gardes de suppression (SIM, caisse, article, stockage, client, wallet,
fournisseur, employé, agent). Les messages et le statut HTTP 400/204 sont
conservés ; les écritures internes hors API ne reçoivent pas ces hooks.

Preuve : `make start`, `.venv/bin/python scripts/c10_native_hooks_runtime.py` :
**create defaults, debt, SIM updates, deletion guards and cleanup OK**.
Le scénario couvre login/CRUD natif, création forcée auteur/statut, caisse,
dette et retries partiels, SIM protected/unprotected, relations actives/inactives
et nettoyage. `pytest` global : **89 passed** ; Ruff/format/diff OK.

Décision : conserver les défauts source et poursuivre C10 avec les routes
`/api2/caisse`, customer/PDV, sale/purchase/return, master pilotage/rupture et
les hooks restants. Aucun import de production ni modification TypeScript.


### Correction C10.3 — validation ID avant hook — 2026-09-23

Une référence PocketBase 0.22.27 a montré qu'un ID CRUD fourni de longueur
incorrecte est rejeté avant le hook dette (400 `validation_length_invalid`) et
ne débite pas l'employé. PPBase acceptait initialement cet ID.
`app/hooks/legacy_records.py` ajoute le garde générique exact pour les IDs
fournis, avant les hooks métier ; les IDs internes restent générés comme avant.
La preuve native C10 a été rejouée avec le garde et le rollback de l'INSERT
dette forcé : passante, nettoyage OK. C9 et C8 rejoués sans régression.

### Gate de fin de session — C9 corrigé, C10.1 à C10.3 — 2026-09-23

Périmètre final : suppression de l'atomicité ajoutée D-0009 sur sept fonctions
C9, puis quatre routes SQL, cinq routes stockage legacy, les hooks CRUD natifs
et `/master/employee/payDebt`. C10 global n'est pas clôturé.

Validation finale :

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **104 passed** ; Ruff : OK ; format : **79 files already formatted** ;
  `git diff --check` : OK.
- `scripts/c10_legacy_sql_runtime.py`, `c10_stockage_runtime.py` et
  `c10_native_hooks_runtime.py` : scénarios passants, fixtures/collections
  jetables nettoyées et vue SQL restaurée.
- Après correction du hook dette : C10 natif, C9 complet (29 routes) et C8
  complet rejoués avec succès et nettoyage vérifié.
- PocketBase **0.22.27** officiel téléchargé dans un dossier temporaire :
  handlers SQL extraits du commit gelé et transpilation TypeScript en modules
  isolés. Comparaisons supplémentaires de `DynamicModel` : les NULL typés
  produisent une erreur 500 de scan, `"false"`/`"0"` booléens deviennent false,
  les colonnes absentes conservent la valeur du modèle. Régressions unitaires
  explicites ajoutées. Ce runtime est une référence locale de la famille
  d'API source, pas une preuve de la version actuellement en production.
- Hook dette source exécuté dans la référence : échec de longueur d'ID sans
  débit, échec d'INSERT forcé sans débit résiduel, succès avec débit réel.
  PPBase utilise `event.engine` pour conserver ce résultat sur erreur native.
  La règle d'ID est placée avant les hooks métier. Triggers de faute supprimés.
- `make migrate-status` : **7 appliquées, 0 pending, 0 orphaned**.
  `make verify-all` : **114/114** et **42/42**, aucun écart de métadonnées/relation.
- Les serveurs PPBase et PocketBase de cette session sont arrêtés ; le dépôt
  TypeScript est toujours propre sur `530018cd20c2acbe88f38b3639e500b5271e2eb2`.
  Aucune donnée de production, migration officielle ou intégration externe
  métier modifiée. Aucun commit/staging/reset.

Documentation actualisée : AGENTS, plan, tasklist, ledger, matrice de tests,
structure, contrats commerce/SQL/stockage/hooks, routes et D-0009 à D-0012.
Les sections historiques qui décrivent un rollback C9 renforcé sont
supplantées par la correction D-0009, conformément à la demande utilisateur.

Limites encore ouvertes : SQL arbitraire SQLite/PostgreSQL, toutes les erreurs
moteur/formes de champs, toutes les combinaisons de permissions CRUD, files et
realtime. Les contrats source actifs des sous-lots restants n'ont pas été
remplacés par des alias vers les routes master.

Prochaine action exacte : reprendre **C10.4 `/api2/caisse`** à partir de
`src/entries/caisse.ts` (`getCaisseRole`, open/reject/reject_closing/openAgain,
report/rangeReport/reportByExpenseType, cancel/cancel2 et withdrawal), avec
l'ordre des sauvegardes, réponses vides et anomalies source conservés. Puis
customer/PDV, sale/purchase/return et routes master PDV/pilotage/rupture.

### Complément D-0009 — préparation d'identités retour — 2026-09-23

`app/commerce/returns.py::_sim_customer` ne regroupe plus les sauvegardes
SIM/PDV/client dans une transaction ajoutée. La source réalise cette préparation
séquentiellement avant sa transaction stock/retour. Régression PostgreSQL :
un trigger de test fait échouer l'INSERT du PDV, le client précédent reste
persisté comme dans l'ordre source, puis toutes les fixtures sont nettoyées.
`c9_commerce_runtime.py` passe à nouveau pour les 29 routes ; rapport actualisé.
Les vraies transactions source (stock/retour/recharge/paiement) sont conservées.
Le serveur local a été arrêté après cette dernière preuve.

## C10 — point d'avancement et portage caisse legacy — 2026-09-24

Statut : C10 global reste ouvert. La demande de point d'avancement a confirmé
que le dépôt était propre, et que la reprise précédente s'était arrêtée à
l'inventaire des routes restantes. Les libellés historiques C10.1/C10.2/C10.3
et « prochaine action C10.4 » ci-dessus sont des subdivisions de travail ; le
plan officiel n'a qu'un checkpoint C10.

Handoff relu : baseline, plan, tasklist, ledger, README contrats/inventaire,
git status et source HEAD propre sur
`530018cd20c2acbe88f38b3639e500b5271e2eb2`. Aucun inventaire régénéré. Le relevé
AST des méthodes/chemins confirme après portage 18 routes actives manquantes.
Les déclarations `return.ts` ne sont pas appelées par `index.pb.ts` ou
`master.pb.ts` : les deux routes retour actives viennent de `sale.ts`, et
`/api2/test` est dormant dans ces bootstraps.

Périmètre implémenté : les dix routes actives `/api2/caisse` de `caisse.ts`.
Fichiers métier : `app/legacy/caisse.py`, `app/hooks/legacy_caisse.py`, registre
de hooks ; tests : nouveau `tests/test_legacy_caisse_contract.py`, mise à jour
des registres attendus dans tests auth/customer/foundation/poc ; preuve runtime
`scripts/c10_caisse_runtime.py` et `docs/c10-caisse-runtime-report.json`.
Documentation : contrat legacy-caisse, matrice routes/tests, plan, tasklist,
AGENTS, README contrats, D-0013 et ce ledger.

Contrats comparés au TypeScript : méthodes/chemins, données caisse/wallet/amount,
ordre auth puis rôle caisse, rôle isAdmin absent pour non-admin, 401, erreurs
400/403, enveloppes `{message}`, `{error}`, `{success,...}` et corps 200 vide,
rapports publics `{caisse,report,cashier,transactions}`/`{caisse,transactions}`,
agrégat `{datas}`. Dates comparées lexicalement comme SQLite, expand auteur et
type de dépense, status actif/non actif et relations wallet/session conservés.
Lecture complémentaire du code officiel PocketBase 0.22.27
`models/record.go:518-568` : emailVisibility et PublicExport. Régressions pour
email non visible, dates vides et scan entier du montant agrégé.

Preuve PostgreSQL/HTTP : dix routes passantes, ouverture/rejet/réouverture,
permissions avec rôles modifiés à chaud, rapports non vides anonymes, retrait
fractionnaire avec soldes tronqués, compteur wallet toujours 1, mouvement wallet
restant actif après annulation WITHDRAWAL, double reversal non-admin, rejet
d'une transaction inactive. Une faute sur UPDATE caisse laisse la session OPEN.
Une faute sur INSERT transaction caisse laisse le mouvement wallet précédent
mais aucun changement de solde. Fixtures et triggers supprimés en finally et
absence des fixtures recontrôlée. Aucune atomicité englobante ajoutée.

Commandes et résultats :

- `make start` a d'abord échoué : PostgreSQL/Docker arrêtés. Relance de Docker
  Desktop et `make postgres-up`, puis démarrage PPBase réussi, jobs désactivés.
- `.venv/bin/python scripts/c10_caisse_runtime.py` : passant après correction
  de la fixture session (REQUEST_OPEN n'appartient pas à son select), transaction
  unitaire requise par PPBase pour la suppression et sérialisation PublicExport.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **115 passed** ; Ruff OK ; format : **83 files already formatted**.
- `make migrate-status` : **7 applied / 0 pending / 0 orphaned**.
  `make verify-all` : **114/114**, **42/42**, aucune erreur de relation/métadonnées.

Limites : tests source + PPBase, sans replay des dix handlers caisse sur un
serveur PocketBase dans cette session. Toutes les branches d'annulation et
formes malformées, erreurs moteur/dialecte SQL, concurrence, SDK files/realtime
restent non prouvées. La preuve de caisse ne clôture pas C10. Aucun import de
production, intégration externe métier, modification TS/framework, migration,
commit ou staging.

Prochaine action exacte : poursuivre C10 avec les quatre routes de
`src/entries/customer.ts` (conserver `pvd`, permission create absente pour le
manager, sauvegardes partielles et champ `parent` défectueux), puis quatre
purchase, cinq sale/return et cinq master PDV/pilotage/rupture. La gate C10
globale et la comparaison des comportements encore ouverts restent à faire.

Validation finale de cette reprise : le script caisse a été rejoué après le
dernier ajustement SQL avec succès, y compris la date contenant une apostrophe
(400 `{message:"Error",error:{}}`). Suite 115 tests, Ruff, format et diff check
passants. Le serveur PPBase démarré pour la preuve est arrêté ; le service
PostgreSQL local reste disponible. La source TypeScript est toujours propre.

## C10 — 18 routes restantes portées, comparaison et SDK — 2026-09-24

Statut : toutes les routes actives inventoriées sont implémentées ; **C10 reste
ouvert**, car des écarts SQL/projection ont été mis en évidence. L'utilisateur
demande d'avancer avec prudence ; les résultats ci-dessous remplacent le relevé
précédent de 18 routes manquantes, sans annoncer une clôture prématurée.

Handoff relu, état Git existant conservé, source propre confirmée sur
`530018cd20c2acbe88f38b3639e500b5271e2eb2`. Pas de régénération de l'inventaire
source ni de modification TS/framework. Les changements de la session caisse
précédente étaient encore non commités et ont été conservés.

### Code et contrats

Portage de `customer.ts` (4 routes), `purchase.ts` (4), `sale.ts` (5) et
`master.pdv.ts`/`master.pilotage.ts`/`master.rupture.ts` (5). Nouveaux services
`app/legacy/{records,customer,commerce,master,http}.py`, hooks
`app/hooks/{legacy_people,legacy_commerce,legacy_master}.py`, registre et tests
de contrat. `return.ts` est dormant dans les deux bootstraps ; `/api2/test`
n'est pas ajouté et ses déclarations retour ne remplacent pas celles de sale.ts.

Les sauvegardes internes conservent leur ordre et leurs effets partiels :
personne/PDV avant erreur SIM, achat/dette/stock avant article absent, paiement
vente avant session, annulations répétées et confirmation retour répétée.
Le manager customer ne gagne pas create, purchase/add et return ne reçoivent
pas de garde métier inventée, la numérotation facture défectueuse reste à 1,
le mauvais champ parent et la compensation vente filtrant buy restent inchangés.

Une référence PocketBase 0.22.27 officielle a été téléchargée et lancée sur
127.0.0.1:8098. `scripts/c10_prepare_reference.py` transpile les modules gelés
dans un dossier temporaire, importe l'export corrigé avec `new Collection`,
puis enregistre seulement les modules de test. L'ordre initial de migration
et l'import de simples objets JS ont échoué ; l'import correct a été exécuté
sans retirer d'index, relation, vue ou champ de l'export.

Le contrôle automatique avait refusé un lancement en interprétant le log
rupture « cron configurée » comme un job actif. Vérification du JS compilé :
cronAdd et fetchAndPrintReports sont entièrement commentés dans run(). Après
présentation de cette preuve, le lancement local a été approuvé. Aucun cron ni
appel Orange n'a démarré ; le test pilotage confirme son échec avant config.

Comparaisons exécutées : méthodes/chemins, guard/middleware, 401/403/404/500,
content type, enveloppes message/error/data/success et corps 200 vides,
relations stock/caisse/session/personne/SIM/PDV/agent, soldes/statuts et retries.
Les différences découvertes ont été corrigées : 401 guard complet, 404 DAO
non capturé, GoError capturé `{value:{}}` distinct d'Error JS `{}`, et 404 d'un
rapport caisse absent. Le message SQL caisse est maintenant `{error:{value:{}}}`.
Les guards correspondants des anciens modules ont reçu l'enveloppe exacte ;
les permissions elles-mêmes n'ont pas été élargies.

### Vues et SDK

Le scénario PDV non vide a révélé une erreur cible : 27 alias de neuf vues
avaient été convertis en minuscules par PostgreSQL. Migration
`1789236847_c10_view_alias_case.py`, traducteur et vérification physique des
colonnes corrigés. Rollback puis réapplication de cette seule migration testés.
La requête C9 `transaction_stats` est adaptée à `v."authorName"`. Un redémarrage
après migration renouvelle les plans préparés ; le cached-plan error observé
pendant les essais n'est pas masqué par un retry métier.

Le SDK JS PocketBase installé dans le dépôt web est utilisé réellement via
Node 26 et EventSource natif : login, refresh, CRUD, filtre, expand, fichier PNG
uploadé puis retéléchargé octet pour octet, et événement realtime update.
Le premier test realtime PPBase a échoué : sa règle ne chargeait pas la relation
role de l'abonné et comparait une chaîne vide à un booléen. Le nouvel adaptateur
`app/auth/realtime.py` recharge le contexte propre à l'abonné à chaque événement,
isolé par ContextVar ; `context.py` partage le chargeur HTTP/realtime. Le
framework sibling n'est pas édité. Une révocation de lecture supprime l'événement
puis son rétablissement réactive la réception. Un test concurrent vérifie que
deux abonnés ne partagent ni contexte ni droit de l'émetteur.

### Commandes et preuves finales

- `scripts/c10_remaining_runtime.py` : **18 routes, 60 comparaisons égales**,
  fixtures nettoyées et absence recontrôlée sur les deux serveurs.
- `scripts/c10_caisse_reference_runtime.py` : **10 routes, 34 comparaisons égales**,
  dont EXPENSE/VERSEMENT/DEPOSIT/WITHDRAWAL, rapports publics et emailVisibility.
- `scripts/c10_sdk_runtime.py` : **passant sur les deux serveurs**, y compris
  permission de l'abonné révoquée/rétablie et suppression de la fixture/fichier.
- `c10_legacy_sql_runtime.py`, `c10_stockage_runtime.py`,
  `c10_native_hooks_runtime.py`, `c10_caisse_runtime.py` : passants et cleanup OK.
- `c8_finance_runtime.py` : passant, concurrence et cleanup OK.
  `c9_commerce_runtime.py` : **29 routes, assertions et cleanup OK**, rejoué après
  correction authorName de la requête de statistiques.
- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **126 passed**. Ruff OK ; **98 fichiers formatés** ; git diff check OK.
- `make migrate-status` : **8 appliquées / 0 pending / 0 orphaned**.
  `make verify-all` : **114 définitions**, **42/42 vues** ; aucun alias camelCase
  manquant. Deux pseudo-colonnes rowid de D-0006 sont signalées séparément.

Documentation et artefacts : rapports remaining/caisse-reference/SDK/global C10,
contrat legacy-remaining, legacy-caisse actualisé, routes, matrice de tests,
AGENTS, plan, tasklist, D-0014/D-0015 et ce ledger. Le rapport global a le statut
**incomplete**, même si ses scénarios de routes et SDK sont passants.

### Blocages et prochaine action exacte

`/sql` reste incompatible avec certaines expressions SQLite : la même requête
constante strftime renvoie 200 sur PocketBase et 500 UndefinedFunction sur
PPBase (`docs/c10-sql-dialect-gap.json`). Ce n'est pas un défaut source à
conserver ; un client existant peut casser après changement d'URL.
Les vues `buma_article_with_no_stock_list_view` et `buma_stockage_no_manager`
ont aussi perdu la projection rowid lorsque D-0006 a mappé rowid vers id puis
retiré la projection en double. Le nouveau contrôle l'expose, sans fallback.

Prochaine action : traiter la compatibilité SQL SQLite des routes publiques
SQL et la projection de ces deux vues, avec preuve comparée, puis clôturer
uniquement si la gate globale passe. Aucun nouveau groupe de routes ne reste
à porter. Les variantes SSE delete/options, fichiers protégés et autres formes
non exercées restent des limites de preuve distinctes. Aucun import production,
cutover, commit, staging ou changement du dépôt TypeScript/framework.

État de fin de cette reprise : rapports globaux/routes mis à jour ; source
toujours propre ; diff check et Ruff/format passants. Les serveurs PPBase
8090 et PocketBase de référence 8098 ont été arrêtés après les preuves. Docker/
PostgreSQL local reste disponible. Le dossier temporaire de référence contient
uniquement son schéma et ses fixtures synthétiques ; les fixtures des scripts
comparatifs et SDK ont été nettoyées et leur absence recontrôlée.

## C10 — clarification des usages rowid/strftime — 2026-09-24

Demande utilisateur : expliquer concrètement les champs/fonctions, leurs
équivalents PostgreSQL et les fichiers/routes où ils sont utilisés. Session
d'analyse et correction du suivi uniquement ; aucun code métier modifié,
serveur démarré ni test runtime rejoué.

Handoff relu, état Git conservé, HEAD source confirmé à 530018c. Commandes :
git status/rev-parse, git grep puis grep récursif src/ et migrationsDir/, lecture
JSON des 114 définitions et de source-inventory, lectures numérotées du
traducteur, de la migration vues et des deux handlers SQL. Recherche auxiliaire
dans le dépôt Boutique : des usages strftime existent dans ses propres hooks,
mais ils n'établissent pas un usage du backend Master gelé.

Résultat : **aucune occurrence SQL strftime dans le code/migrations/schéma
Master examinés**. L'expression du rapport c10-sql-dialect-gap était une sonde
synthétique envoyée par l'agent à POST /sql. Le handler source index.pb.ts:37
exécute requestData.query ; son portage legacy_sql.py:72 envoie cette requête
sans traduction à PostgreSQL. PostgreSQL peut produire le même texte via
`to_char(DATE '2026-09-24', 'YYYY-MM-DD')`. La différence de dialecte mesurée est
réelle, mais son utilisation par une fonctionnalité cliente n'avait pas été
établie et ne doit pas être présentée comme telle.

Les deux rowid manquants sont dans l'export corrigé :
buma_article_with_no_stock_list_view (ligne 10311) lit a.rowid depuis
buma_article ; buma_stockage_no_manager (15705) le lit depuis users. Le
traducteur generate_native_baseline.py:98 remplace a.rowid par a.id puis supprime
le doublon ; migration vues aux lignes 1077/6213. Les tables et les IDs existent,
mais la valeur rowid attendue par le schéma de ces vues n'est plus projetée.
Accès : API/SDK native de collection, aucun handler métier explicite lisant
rowid trouvé dans src/entries (seulement les types de tables).

La même règle affecte une troisième vue, buma_stock_transaction_list_view :
`a.rowid AS article_id` dans l'export/migration source
1774706510_updated_buma_stock_transaction_list_view.js:7 devient `a.id AS article_id`
dans la migration Python:6148. Le nom reste présent, mais sa valeur change de
sens (identifiant entier SQLite contre ID PocketBase texte).

Tasklist, plan, AGENTS, D-0015 et rapports globaux sont précisés. C10 reste
ouvert pour les traductions rowid ; le volet SQL libre doit être cadré selon
ses appels réels. Prochaine action : corriger ces projections en préservant leur
contrat, puis vérifier les requêtes des clients SQL. Aucun changement source,
staging, commit ou modification de données.

## C10 — recherche rowid, correction sourcée et clôture locale — 2026-09-24

L'utilisateur exige une relecture de `/Users/omarsisss/Documents/wproject/protech/buma_master_hooks`
et une recherche PocketBase/SQLite fondée, puis demande où rowid est réellement
utilisé. Handoff relu, Git/source HEAD revérifiés, changements existants conservés.
Le source demeure propre sur `530018cd20c2acbe88f38b3639e500b5271e2eb2`.

### Provenance et recherche

L'usage SQL dans le dépôt BUMA est la migration
`migrationsDir/1774706510_updated_buma_stock_transaction_list_view.js:7`,
`a.rowid AS article_id`, ainsi que ses versions antérieures. Dans src/entries,
les occurrences rowid sont uniquement les types aux lignes 1013/1450 de
master.tables.types.ts. Les requêtes des deux autres vues proviennent de
l'export corrigé (article_with_no_stock et stockage_no_manager), distinction
explicitement expliquée à l'utilisateur. Aucun handler métier rowid inventé.

Sources primaires téléchargées/lues : sqlite.org/rowidtable.html, autoinc.html,
lang_createtable.html, lang_vacuum.html ; PocketBase v0.22.27
daos/record_table_sync.go, base.go, record.go, view.go, models/record.go et
models/schema/schema_field.go. Références exactes dans docs/contracts/rowid.md.
La création officielle utilise `id TEXT PRIMARY KEY`, sans WITHOUT ROWID.
Le DDL réel d'une base créée par le binaire officiel a confirmé l'absence de
colonne déclarée masquant rowid pour users/article. Le moteur du serveur donne
sqlite_version()=3.46.0 ; la version sqlite3 du lecteur Python n'est pas confondue
avec celle du serveur. La version effectivement déployée en production reste
inconnue, à identifier avec le backup C11.

La preuve PocketBase a observé les valeurs [1,2,2,3,3,4] après créations,
update, suppression d'un trou, suppression du maximum et rollback. Les trois
API renvoient rowid/article_id sous forme de nombres JSON ; le CRUD de base ne
les expose pas. L'affectation SQL explicite du maximum signé provoque ensuite
l'allocation d'un entier positif libre. PocketBase utilise son inférence JSON
pour un champ SQL inconnu, sans transformer ce nombre en objet JSON.

### Correction et périmètre

Migration `pb_migrations/1789236848_c10_sqlite_rowid.py` : bigint interne dans
users/article, allocation max+1 sous verrou transactionnel, réutilisation après
suppression/rollback et gestion du maximum signé. Elle restaure les expressions
a.rowid des trois vues au lieu de les remplacer par l'ID texte. Aucun champ
public n'est ajouté aux collections de base ; le contexte auth exclut rowid.
Le schéma PocketBase et le code TypeScript ne sont pas modifiés. Les deux tables
locales étaient vides (comptage avant migration). La migration refuse tout
backfill arbitraire si elles contiennent des lignes ; C11 devra importer les
valeurs SQLite originales explicitement. Son rollback exige également une cible
vide pour ne pas perdre ces identifiants.

`scripts/generate_native_baseline.py` conserve les expressions source ;
`verify_complete_schema.py` ne tolère plus les deux projections manquantes.
`app/legacy/views.py`, installé par legacy_records, reproduit PublicExport sur
ces trois vues : dates système absentes omises et NULL de LEFT JOIN préparés
suivant le type source. Le stockage SQL reste entier, ce qui conserve les filtres
numériques natifs. Fichiers framework sibling inchangés.

Reproduction ajoutée à `c10_prepare_reference.py --rowid-only` : copie de l'export
et helper temporaire seulement, aucun module métier/cron/intégration. Nouveau
`scripts/c10_rowid_runtime.py`, tests rowid/migration et régression traduction.
Artefacts : docs/c10-rowid-reference-research.json, c10-rowid-runtime-report.json,
contrat rowid.md, D-0016/D-0017, tasklist/plan/AGENTS/matrice/rapport global.

### Résultats

- Référence PocketBase 0.22.27 sur 8099, fraîche et jetable : trois vues natives,
  filtres/fields, type numérique, champ caché et cycle d'allocation comparés.
- `c10_rowid_runtime.py` : **12 comparaisons passantes**, 20 insertions PG
  concurrentes avec 20 valeurs uniques, rollback et cas du maximum signés,
  fixtures nettoyées et absence recontrôlée.
- Rollback puis replay de la seule migration rowid sur cible vide : passants.
- `c10_remaining_runtime.py` : 60 comparaisons ; caisse-reference : 34 ;
  SDK : login/refresh/CRUD/filter/expand/PNG/realtime et révocation/rétablissement
  passent sur les deux serveurs. Toutes les gates C10 antérieures rejouées.
- C8 complet et C9 complet (29 routes) : passants, cleanup OK.
- pytest sans cache : **131 passed** ; Ruff et format **101 fichiers** OK ;
  diff check OK. `make migrate-status` : **9 applied / 0 pending / 0 orphaned**.
  `make verify-all` : **114 définitions / 42 vues**, zéro projection manquante.

### Décision

**C10 clôturé pour le code gelé, le schéma fourni et la gate locale documentée.**
La sonde strftime était synthétique et n'est pas transformée en fonctionnalité
BUMA à inventer. D-0017 fixe explicitement la limite du SQL fourni librement :
exécution native PostgreSQL, aucune émulation complète de SQLite revendiquée.
L'artefact de cette différence est conservé et son impact potentiel sur des
requêtes ad hoc doit être contrôlé avant cutover ; il n'est pas présenté comme
un appel métier retrouvé. Les comportements de maintenance SQLite comme VACUUM
ne sont pas une promesse de stabilité du rowid ni une émulation implémentée.

Le prochain checkpoint est C11, **non commencé** : backup/version source,
diagnostic d'import, IDs/relations/fichiers et rowid historiques à conserver,
tests clients de staging, rollback et décision de bascule. Aucun import
production, cutover, commit ou staging n'a été effectué pendant C10.

Fin de cette reprise : source toujours propre, diff check passant, absence des
fixtures users/article recontrôlée. Les serveurs PPBase 8090, référence source
8098 et référence rowid 8099 ont été arrêtés. PostgreSQL local reste disponible.

## Rectification — SQL libre et clôture C10 retirée — 2026-09-24

L'utilisateur demande ce que signifie la « limite du SQL libre exécuté sur
PostgreSQL ». Relecture de index.pb.ts:37 et master.article.ts:30, ainsi que
legacy_sql.py:72/115 : les deux handlers reçoivent une chaîne query. Le portage
l'envoie directement au moteur PostgreSQL ; cela ne garantit pas l'acceptation
des requêtes que l'exécution SQLite acceptait auparavant.

L'agent avait transformé cette différence en limite supposée acceptable et
annoncé la clôture C10. Cette clôture est retirée : la documentation ne constitue
ni une correction technique ni une acceptation de changements côté client.
D-0017 est marquée retirée ; tasklist, plan, AGENTS, README, matrice et rapport
global reprennent le statut ouvert/incomplete. Les 131 tests, 106 comparaisons,
SDK et correction rowid restent acquis ; aucun code métier n'a changé ici.

Commandes : git status, lectures source/portage/suivi et correction documentaire,
git diff --check. Aucun test runtime rejoué ni serveur démarré pour cette
clarification. Prochaine action : finaliser la compatibilité des deux routes
SQL à partir de leur contrat source, sans imposer un dialecte différent aux
clients par décision unilatérale. C11 reste non commencé.

## Reconstitution de la session C10 SQL interrompue — 2026-09-25

Demande actuelle : relire le projet et retrouver précisément le point d'arrêt.
Cette session effectue la reconstitution et les contrôles statiques ; elle ne
modifie que ce ledger et la tasklist. Aucun code applicatif, script, migration
ou rapport de preuve antérieur n'est modifié. Les changements non commités de
la session précédente sont conservés.

### État confirmé et provenance

Handoff relu : baseline, plan, tasklist, ledger, README des contrats et inventaire.
La source est propre sur `fix/uv-paani-sup`, HEAD
`530018cd20c2acbe88f38b3639e500b5271e2eb2`, conforme au manifeste. Le SHA-256
de l'export corrigé reste
`9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`.
C9 est fermé pour sa gate locale corrigée D-0009. C10 reste ouvert ; la
clôture D-0017 a été retirée. Les rapports du 24 septembre conservent leurs
106 comparaisons ciblées (60 + 34 + 12), les preuves SDK/rowid et les validations
9 migrations / 114 définitions / 42 vues. Ces gates runtime ne sont pas
rejouées dans cette reconstitution. C11 et l'import restent non commencés.

### Travaux effectivement laissés par la session précédente

Tâche retrouvée : « Reprendre le travail C9 »,
`01a0ced1-b5b1-78c0-be9b-e04c1ef22195`. Son tour
`01a0d83d-d808-7852-901e-b782e0f3bf7f` se termine le 25 septembre à
11:25:53 UTC par `context_length_exceeded`. Les sorties des outils du journal
de session permettent de distinguer préparation, essai échoué et preuve.

- `scripts/c10_sql_usage_audit.py` a été exécuté : rapport
  `docs/c10-sql-usage-audit.json` daté de 11:13:27 UTC. Il examine 416 fichiers
  backend et 2 205 fichiers dans huit chemins clients candidats. Aucun motif
  d'appel n'est retrouvé dans ces clients. Le frontend Master n'est pas fourni ;
  ce résultat ne prouve pas l'absence d'utilisation des routes. La recherche
  n'est pas réexécutée aujourd'hui ; son rapport et sa méthode sont relus.
- `scripts/c10_prepare_reference.py --sql-only` prépare une référence
  PocketBase 0.22.27 avec les handlers gelés et un helper de fixtures/restauration.
- `scripts/c10_stored_sql_runtime.py` doit comparer deux requêtes exactes :
  la somme par `type` de `buma_caisse_transactions`, filtrée sur la session
  `bra4vsynv2vrpih` et `status=true` dans l'export ; puis
  `SELECT (ROW_NUMBER() OVER()) as id` dans
  `migrationsDir/1736833029_created_buma_sql_table.js`.
  Ces requêtes sont sourcées ; les lignes de test et le paramètre `fields`
  du script restent synthétiques, et aucun appel frontend n'est établi.

Un essai HTTP isolé antérieur au script, consigné à 11:17:14 UTC, obtient pour
la requête ROW_NUMBER via `/sql` : `200 [{"id":"1"}]` sur les deux serveurs
alors interrogés. Ce seul succès ne valide pas les deux requêtes/deux routes.

Le script complet a bien été lancé deux fois. Les sorties de 11:20:27 et
11:25:27 UTC échouent toutes deux dans `replay(True)`, sur
`fixture_api(action="restore_collection", table="buma_sql_table")` : HTTP 400
`{"code":400,"message":"Something went wrong while processing your request.","data":{}}`.
`replay(False)` n'est donc pas atteint. La restauration manuelle intermédiaire
a annoncé 200 et un nettoyage, mais l'essai suivant échoue encore. La cause
interne du 400 n'est pas établie par ces sorties. Ne pas le présenter comme
une incompatibilité SQL métier. Aucun fichier
`docs/c10-stored-sql-runtime-report.json` n'a été produit.

### Vérifications effectuées dans cette reprise

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **131 passed in 0.82s**, code de sortie 0.
- `.venv/bin/ruff check --no-cache app tests scripts` : **5 diagnostics**
  dans les deux nouveaux scripts SQL : deux E501 dans l'audit, un I001 et
  deux F401 dans le script runtime. Le lint historique passant du rapport
  du 24 septembre ne décrit donc plus l'ensemble des fichiers actuels.
- `.venv/bin/ruff format --no-cache --check app tests scripts` :
  **103 files already formatted** ; `git diff --check` : passant.
- Lectures des handlers `index.pb.ts:37`, `master.article.ts:30/87`, du portage
  `app/hooks/legacy_sql.py`, des trois scripts et des rapports C10. `/sql`
  exécute `query` et façonne les résultats avec `fields` ; `executeSql` modifie
  la définition persistante de `buma_sql_table` puis renvoie la collection.
  Les statuts/enveloppes, inférences de champs et lectures natives attendues
  sont identifiés dans le source et le script, sans nouvelle preuve HTTP.
- `lsof` identifie une référence PocketBase toujours en écoute sur 8098 dans
  le dossier temporaire `buma-c10-reference-lqk8119d`. Le processus qui écoute
  actuellement sur 8090 a pour cwd `mali_foot/backend_mf`, **pas BUMA**. Aucun
  de ces services n'est arrêté, relancé ou utilisé pour une mutation ici.
  La commande `ps` a été refusée par le sandbox ; `lsof -d cwd` suffit pour
  ce constat. Ne pas lancer le script tel quel contre le port 8090 actuel.
- Lecture SQLite en mode `ro` de cette seule référence jetable : zéro ligne
  pour la session de test et zéro ligne avec la note `C10 source query proof`.
  La requête agrégée est présente. Cette lecture ne remplace pas la comparaison
  au snapshot initial du script : la restauration exacte des métadonnées
  reste non certifiée après l'échec du helper. Le PostgreSQL BUMA n'a pas été
  réinterrogé dans cette session.

### Décision et prochaine action exacte

Reconstitution terminée ; C10 reste ouvert. La suite précise est de corriger
les diagnostics des scripts, diagnostiquer le helper PocketBase de restauration
et vérifier son snapshot aller/retour sur une référence fraîche, puis démarrer
BUMA sur un port libre explicitement identifié et rendre ce port configurable
dans le scénario. Rejouer les deux requêtes sans les réécrire, comparer HTTP,
enveloppes, métadonnées de vue, lectures natives et restauration/nettoyage,
et produire le rapport avant de conclure sur ces cas. Le frontend ou des
captures de ses appels restent nécessaires pour établir les usages clients.
La sonde `strftime` demeure synthétique. Aucun changement client, clôture C10,
cutover, import, staging ou commit n'est décidé par cette reprise.

## C10 — recherche des appels dans le desktop fourni — 2026-09-26

Demande : explorer en lecture seule le dossier
`/Users/omarsisss/Documents/wproject/protech/buma-master-desktop-2.3.1` pour
retrouver les appels aux deux routes SQL. Handoff relu avec les constats de
la reprise précédente ; inventaire/source confirmés sur le commit gelé
`530018cd20c2acbe88f38b3639e500b5271e2eb2`, source TypeScript propre. Le desktop
est une copie sans métadonnées `.git` ; aucune correspondance avec une version
effectivement déployée n'est présumée. Son AGENTS.md a été lu.

### Preuve d'appel retrouvée

`qml/BuMa/Apps/UserSetting/Component_SQL.qml:412-441` contient un appel actif :
`PocketBaseClient.collection("any").send("/master/article/executeSql", "POST", data)`.
`data` contient uniquement `{query: editor.text}` ; le texte par défaut de
l'éditeur est **`SELECT * FROM users`** (ligne 170). Ni `fields` ni mot de passe
SQL ne sont ajoutés au corps de cet appel. Le client C++ joint le token de
session dans l'en-tête Authorization et sérialise ce corps en JSON.

Après le succès du POST, le client appelle
`PocketBaseClient.collection("buma_sql_table").getList(1, 1000)`, soit le GET
natif `/api/collections/buma_sql_table/records` avec page 1/perPage 1000.
Le corps du POST n'est pas exploité pour l'affichage ; le JSON du GET doit
contenir `items`. Le tableau retire `collectionId`/`collectionName`, puis tire
ses colonnes de la première ligne. Ce parcours est donc à comparer dans son
ensemble, pas uniquement par le statut HTTP du POST.

Chaîne d'accès vérifiée dans les fichiers :

- `qml/BuMa/Apps/CMakeLists.txt:19/40` inclut et lie UserSetting ; son
  CMakeLists ajoute les fichiers QML au module `BuMa.Apps.UserSetting`.
- `qml/BuMa/Pages/ConstantBackends.qml:180/192` enregistre le module
  « Administration » ; `Model/AppsModel.qml:236` utilise `role.isAdmin`.
- `Apps/UserSetting/Main.qml` charge `List` et affiche NotAllowed hors admin.
- `Apps/UserSetting/List.qml:368/383/407/720` présente « Console administrateur »,
  ouvre une vérification locale de mot de passe et instancie `Component_SQL`.
- `3rd/PocketBase/src/PocketBaseCollection.cpp:80-87` transmet l'appel en POST ;
  `PocketRequest.cpp:188-190/433-445` construit l'URL racine + chemin, sans
  ajouter `/api` au chemin custom, avec JSON et Authorization.
  Le GET de collection emprunte le chemin natif avec `/api`.

La requête dépend de la saisie libre de l'administrateur. Aucun catalogue
exhaustif de SQL métier n'est fourni. Une liste de requêtes sauvegardées est
prévue dans l'interface (`Settings.sqlQuery`, `List.qml:347-350`), mais ses
valeurs runtime ne sont pas dans cette copie et le flux sauvegarder/recharger
n'a pas été exécuté. Il présente aussi des références `query.text`/`field.text`
dans le bouton sauvegarder alors que l'éditeur utilisé pour envoyer est
`editor.text` ; aucune correction du frontend n'est faite dans cet audit.

### Étendue de recherche et résultats négatifs

Recherche par `grep -RInE` dans QML/C++/JS, puis balayage Python de **26 659
fichiers / 193 435 093 octets**, sans exclusion d'extension, avec recherche des
routes, noms de fonctions et segments SQL. Les fichiers binaires sont parcourus
comme octets ; les archives ne sont pas décompressées. Le rapport
`docs/c10-desktop-sql-usage-audit.json` contient motifs, chemins, lignes,
SHA-256 des fichiers avec correspondance et empreinte du manifeste parcouru.

- `/master/article/executeSql` : un site d'appel explicite, ci-dessus.
- `buma_sql_table` : une lecture explicite, au même endroit.
- `/sql` : aucune requête HTTP identifiée ; cinq correspondances concernent
  uniquement l'icône `sql.svg`/`sql.qml` de Qaterial.
- `/api/sql2`, `/master/article/deleteSql`, segments littéraux isolés
  `sql`/`executeSql`/`deleteSql` : aucune autre correspondance trouvée.
- `strftime` : aucune occurrence littérale retrouvée dans ce balayage.
- Les autres `executeSql` de `Model/StorageModel.qml` et
  `Providers/StorageModel.qml` passent par les drivers de stockage local
  (Database QSQLITE/QtQuick.LocalStorage), pas par ces routes HTTP.

Ces résultats remplacent le constat « frontend Master non fourni » pour
**cette copie** seulement. Une absence de résultat ne prouve pas l'absence
d'usage dans une autre version, une construction encodée ou du trafic réel.
La console trouvée établit un appel prévu par le frontend ; elle ne démontre
ni son utilisation effective en production ni les requêtes saisies dans le passé.

### Validation, périmètre et suite

Fichiers ajoutés/modifiés dans le dépôt PPBase : nouveau rapport d'audit,
tasklist et présent ledger uniquement. Aucun fichier desktop, source gelée,
code applicatif, script de test ou migration modifié. Pas de build Qt, appel
HTTP, lancement de serveur, fixture ou test runtime pendant cette exploration.
Validation documentaire : JSON du rapport relu/parsable, liens de preuve
contrôlés dans les sources, `git diff --check` passant. Les statuts/réponses,
permissions effectives, mutations de vue, hooks, rollback et concurrence du
parcours desktop restent à comparer en runtime.

Décision : exploration demandée terminée, **C10 reste ouvert**. Prochaine
action : réparer la restauration du scénario de test déjà bloqué, utiliser
une référence PocketBase fraîche et un port BUMA identifié, puis reproduire
le parcours desktop exact à partir de sa requête initiale et des deux requêtes
stockées déjà identifiées. Vérifier les réponses et la liste native obtenue.
La saisie SQL libre est désormais une fonction cliente concrètement retrouvée ;
ne pas supposer que quelques requêtes statiques couvrent tous ses usages,
ni imposer une modification de dialecte au client sans décision utilisateur.
Les requêtes réellement utilisées/sauvegardées restent à recueillir si une
validation des usages historiques est requise. C11 n'est pas commencé.

## C10 — décision utilisateur sur la console et /sql — 2026-09-26

Après l'audit desktop, l'utilisateur décide explicitement : « comme le backend
est ppbase, quand l'admin écrit des requêtes postgresql ça passera normalement »
et, sans usage `/sql` trouvé, « le mettre de côté pour le moment ».

Décision enregistrée sous **D-0018** : la console `executeSql` accepte le SQL
PostgreSQL saisi par l'administrateur. Son transport `{query}`, sa réponse
collection, sa modification de vue et le GET natif de `buma_sql_table` restent
le contrat à préserver. La requête doit rester utilisable comme définition de
vue avec `id`, comme l'exige cette route ; toutes les commandes SQL ne sont
pas promises exécutables. La route `/sql` reste dans le code ; ses recherches
d'usage et sa compatibilité SQLite sont reportées et ne bloquent plus C10.
Il ne s'agit ni de la supprimer ni de déclarer qu'elle est inutilisée partout.

Handoff et état Git relus ; source toujours propre au commit gelé `530018c`.
Lecture du handler `app/hooks/legacy_sql.py` : il utilise déjà PostgreSQL,
donc cette décision ne nécessite aucune modification de code. L'ancienne
décision D-0017 reste retirée : D-0018 est une autorisation nouvelle donnée
après découverte de l'appel frontend, pas une clôture rétroactive.

Fichiers documentaires mis à jour : `AGENTS.md`,
`docs/decisions-and-divergences.md`, `docs/contracts/legacy-sql.md`,
`docs/contracts/README.md`, `docs/implementation-plan.md`,
`docs/migration-ppbase-tasklist.md`, `docs/test-matrix.md`,
`docs/c10-runtime-report.json` et ce ledger. Le rapport global conserve la date
et les résultats de la gate historique ; son périmètre/décision/blocage actuels
sont actualisés séparément. Aucun rapport d'audit ou runtime antérieur n'est
présenté comme réexécuté.

Commandes : git status/rev-parse, lectures ciblées des documents et des tests
SQL existants, mise à jour documentaire, parse JSON et `git diff --check`.
Aucun test applicatif, serveur, requête HTTP, migration ou fixture lancé ;
aucun changement de frontend/source/backend, commit ou staging. Le contrat
méthode/chemin/{query}/vue/GET natif est relu ; réponses, permissions, champs,
persistance, rollback et comportement Qt ne sont pas nouvellement validés.

Décision de fin : cadrage demandé enregistré ; **C10 reste ouvert** uniquement
pour les validations de son périmètre retenu. Prochaine action exacte : préparer
un scénario console isolé sur un port BUMA identifié, garantir la restauration
de la vue, puis tester `SELECT * FROM users`, un cas PostgreSQL spécifique,
le POST executeSql → GET natif page 1/perPage 1000, les champs/types/enveloppes
et les effets des erreurs, avant la gate finale. Reprendre le helper de référence
si nécessaire à la comparaison du contrat ; ne pas relancer l'audit SQLite
global ni faire de `/sql` un blocage tant que son report n'est pas levé.
Les cinq diagnostics Ruff constatés le 25 septembre sur les scripts préparatoires
ne sont pas corrigés par cette décision. C11/import/cutover non commencés.

## Coordination C10 en attente et préparation C11 autorisée — 2026-09-26

L'utilisateur demande de suspendre le travail C10 dans cette session jusqu'à
disponibilité du build BUMA Master desktop pour les tests réels. C10 reste
ouvert et sa validation/clôture restent à la charge de cette session
(`01a0d862-ed57-7083-a12c-cfca5f38235b`). Aucun suivi automatique n'est demandé
ou créé ; la reprise dépendra du retour utilisateur avec le build.

L'utilisateur souhaite poursuivre la suite dans une nouvelle session et
demande uniquement un mini prompt de transmission. La préparation C11 est
donc explicitement autorisée malgré C10 ouvert : diagnostic des artefacts,
plan/outillage d'import et vérifications sur environnement isolé. Ne pas
reprendre ou clôturer C10 dans cette autre session. D-0018 demeure applicable
(console PostgreSQL, `/sql` reporté mais présent). Préserver les changements
existants, la source gelée, les IDs/relations/fichiers et les rowid historiques.
Aucune autorisation d'import de production ou de cutover n'est déduite.

Périmètre de ce tour : coordination uniquement ; `AGENTS.md`, plan, tasklist
et ledger mis à jour. Commandes : `git status --short --branch`, lectures des
consignes/suivi et `git diff --check`. Aucun code, route, enveloppe, hook,
permission ou comportement SDK changé ; aucun test/runtime exécuté.
Décision : attendre sur C10 et remettre le prompt demandé. C11 n'est pas
exécuté ici et aucune nouvelle session n'est créée par cet agent.

## C11 — préparation et répétition synthétique isolée — 2026-09-26

Statut : préparation effectuée ; **C11 ouvert**, backup réel en téléchargement
chez l'utilisateur. C10 reste volontairement en attente du build desktop,
réservé à sa session d'origine. Aucune reprise des scénarios console, aucune
clôture C10 et aucune modification de D-0018 ou de `/sql`.

### Périmètre et artefacts

Handoff complet relu avant les changements : AGENTS, baseline, plan, tasklist,
ledger, contrats/inventaire. `git status --short --branch` a montré les
changements C10 déjà présents ; ils sont conservés. Source TypeScript propre
sur `fix/uv-paani-sup`, HEAD `530018cd20c2acbe88f38b3639e500b5271e2eb2`, égal au
manifeste. Aucune régénération d'inventaire, aucun changement TS/framework.
L'export corrigé possède toujours l'empreinte `9d99e7b5…b2313d` attendue.

Recherche bornée par noms dans le projet, source, Downloads et temporaire :
export corrigé disponible ; sauvegardes nommées BUMO/ACME/PPBase non identifiées
comme Master, donc non ouvertes. Après demande de chemin, l'utilisateur
précise qu'il télécharge le backup, volumineux. Aucun contenu de backup réel
lu, copié ou importé. Le binaire PB 0.22.27 de référence locale antérieure
est réutilisé uniquement pour créer de nouvelles fixtures synthétiques.
Rapport d'artefacts : `docs/c11-artifact-diagnostic.json`.

Fichiers ajoutés : `scripts/c11_import.py`, `scripts/c11_fixture.py`,
`scripts/c11_rehearsal.py`, `tests/test_c11_import.py`,
`docs/c11-preparation.md`, `docs/c11-artifact-diagnostic.json` et
`docs/c11-rehearsal-report.json`. Fichiers de suivi actualisés : AGENTS,
tasklist, D-0019 dans `docs/decisions-and-divergences.md`, présent ledger.
Aucun fichier applicatif ou migration officielle modifié dans cette session.

### Outils et contrats conservés

Le diagnostic propose un inventaire ZIP sans extraction (empreinte en flux,
taille décompressée déclarée, chemins/doublons/WAL, aucun CRC/donnée prétendu
validé) et une lecture SQLite hors ligne `ro&immutable`, jamais sur un serveur.
Il compare les 114 définitions avec l'export, puis comptes, valeurs, relations,
fichiers et doublons d'idempotence. Rapport privé sans valeurs des records.
Les snapshots/version inconnus, WAL actifs, schémas dérivés, relations
orphelines et conversions avec perte sont bloqués.

La répétition n'accepte aucune URL de DB ni source externe : création d'un
conteneur PostgreSQL local jetable, neuf migrations officielles, fixtures PB
nouvelles et copie privée vérifiée. L'import SQL lit cette copie, garde IDs,
relations, dates, booléens, JSON, listes, fichiers, hashes auth/token keys,
statuts et soldes, sans hook métier/recalcul. Rowids users/article natifs
préservés ; tous les autres rowids et dates auth legacy également archivés
dans une table technique privée. Le SQLite complet original est conservé.
Refus de cible non vide et verrouillage avant contrôle ; aucune fusion.
Les 24 seeds C5 ne sont retirés que dans la cible neuve possédée par le harness.

Source de comparaison : export corrigé, contrat rowid et migration 1789236848,
contraintes C4/seed C5, stockage/auth PPBase lus sans modification, véritable
DDL et records créés par PB 0.22.27. Le source TS n'est pas réexécuté et aucun
nouveau contrat de route métier n'est porté.

### Commandes et résultats

- `.venv/bin/pytest -p no:cacheprovider tests/test_c11_import.py -q` :
  **23 tests C11** (conversions, rowid obligatoire, refus tableaux non
  représentables, lecture seule, fichiers/empreintes, chemins, ZIP, non-écrasement).
- `.venv/bin/python -m scripts.c11_rehearsal --pocketbase <binaire local
  0.22.27> --schema /Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json
  --report /private/tmp/buma-c11-rehearsal-attempt5.json` : **passant**.
  Copie du résultat et limites dans `docs/c11-rehearsal-report.json`.
- **9 migrations ; 114 définitions / 42 vues** contrôlées avant et après.
  **9 records / 8 collections, 6 relations, 3 fichiers / 38 octets** conservés.
  users rowids 41/93, article 7001 et fournisseur -17 archivé ; dates
  millisecondes, statut false, solde -17.25 et JSON imbriqué vérifiés.
- Erreur forcée après trois insertions : rollback des 72 tables, valeurs
  initiales des **24 seeds** exactement restaurées, table technique annulée,
  fichiers de staging supprimés. Deux importeurs concurrents : **9** records
  une seule fois ; second résultat **target_not_empty**, sans duplication.
- Scénarios négatifs passants : relation orpheline, clé C4 dupliquée, schéma
  dérivé, nombre invalide, fichier absent et relation multiple non représentable.
- HTTP cible après import : `users` login/refresh 200 `{token,record}`,
  `agent_users` login avec hash PB et lecture owner 200 ; mauvais mot de passe
  400 ; accès caisse admin 200/non-admin 404 ; fournisseur anonyme 404 ; article
  public 200 selon sa règle source. Liste native avec enveloppe
  `{page,perPage,totalItems,totalPages,items}`, filtre article, expand
  article/auteur ; trois GET fichiers identiques octet pour octet. Aucun hash,
  tokenKey ou rowid interne dans le record public. Relecture SQL/fichiers après
  HTTP sans modification métier. **Pas de nouveau replay HTTP PocketBase
  comparé, SDK JS, desktop ni E2E de production.**
- Suite finale : **154 passed in 0.82s** ; Ruff et format des quatre fichiers
  C11 passent ; `git diff --check` passe. Ruff global retrouve uniquement les
  **cinq diagnostics préexistants C10** documentés le 25 septembre ; ils ne
  sont pas corrigés dans cette session réservée à C11.
- Serveur de répétition arrêté ; conteneur et dossiers temporaires supprimés.
  Le conteneur exploratoire C11 initial et ses deux fixtures préparatoires
  sont également supprimés. Aucun service existant arrêté/modifié, aucun
  commit, staging ou reset.

Les premiers essais ne sont pas comptés comme preuves passantes : accès Docker
sandbox refusé puis autorisé ; une mutation de schéma de test initialement
identique au source a été remplacée par une vraie dérive ; le test HTTP initial
attendait à tort 404 pour l'article public et a été réaligné sur les règles
exportées, avec contrôle séparé d'une collection réellement privée.

### Blocages et décision

**D-0019** : PB traite `maxSelect: null` comme une relation multiple ; les
colonnes cibles observées `buma_customer.pdv`, `buma_pilotage_ticket.tag` et
`buma_wallet.managers` sont scalaires. Le diagnostic refuse leurs records,
y compris avec `[]`, au lieu d'aplatir ou de stocker une fausse référence.
Le scénario positif exclut ces trois tables et un scénario négatif teste
explicitement deux IDs. Cette preuve limitée ne valide donc pas l'import
d'un Master complet. Aucun correctif de domaine ou reprise C10 engagé ici.

Restent ouverts : backup/version réels et anomalies effectives, import de
toutes les tables peuplées, performance gros volumes, configuration/admins/
OAuth et anciens tokens, fichiers protégés/S3, récupération SIGKILL/disque
plein et validations staging/client. La copie SQLite conserve les informations
internes, mais elles ne sont pas activées arbitrairement dans PPBase.

Décision : préparation et première répétition synthétique terminées, C11
non clôturé. **Prochaine action exacte** : après réception du chemin du backup
téléchargé, lancer l'inventaire ZIP en lecture seule, identifier provenance et
version, vérifier l'espace disque puis diagnostiquer une copie hors ligne.
Présenter les anomalies sans corriger les données. Aucun import de production
ni bascule n'est autorisé par cette préparation.

## C10 — build reçu et instructions de démarrage local — 2026-09-26

L'utilisateur indique avoir reçu le build desktop et demande les commandes
pour lancer le serveur local puis configurer son tunnel. Le préalable matériel
à l'attente est donc levé ; aucun test desktop n'est encore déclaré effectué.

Vérifications : lecture Makefile/README/Compose, configuration `.env` limitée
aux paramètres utiles sans divulgation de secrets, `ppbase serve --help`,
`git status`, HEAD source toujours `530018c`, `lsof` ports et cwd. Le port 8090
est occupé par `mali_foot/backend_mf` ; aucun listener trouvé sur 8091 au moment
du contrôle. Le venv existe et le CLI répond. La cible `.env` est PostgreSQL
local sur 55434/base `buma_master_ppbase`, avec jobs internes désactivés.

Instructions à remettre : ouvrir Docker Desktop, `make postgres-up`, puis
`make start HOST=127.0.0.1 PORT=8091 BUMA_MASTER_ENABLE_INTERNAL_JOBS=false`.
Le Makefile charge `.env`, les hooks BUMA et les migrations ; celles en attente
sont appliquées au démarrage. Vérifier `/api/health`, diriger le tunnel vers
`http://127.0.0.1:8091`, puis saisir l'URL HTTPS racine du tunnel dans l'app.

Fichiers changés : ledger et tasklist uniquement. `git diff --check` exécuté.
Aucun serveur/tunnel lancé par l'agent, aucune migration appliquée ici, aucun
contrat HTTP/permission/persistance/SDK ou comportement desktop validé dans ce
tour. Décision : C10 reste ouvert, prochaine action utilisateur démarrage puis
test du build ; cette session accompagnera les résultats avant clôture.

## C10 — absence de lien de premier administrateur — 2026-09-26

L'utilisateur confirme la connexion réseau du desktop au serveur, mais ne
connaît pas l'administrateur et attend le lien de création initiale PPBase.
Lecture du framework editable (`ppbase/app.py:174-196`) : le lien unique
`/_/setup?token=...` est généré/affiché uniquement si `count_admins == 0`.

Vérification en transaction PostgreSQL READ ONLY sur la base locale configurée :
un superuser `admin@admin.ad` existe ; `users=0`, `roles=0`, `buma_config=0`.
Cette base n'est donc pas neuve au niveau des comptes techniques. GET `/` et
`/_/` sur 8091 répondent 200 (JSON BUMA et HTML dashboard). L'accès réseau
sandbox initial a échoué ; la vérification locale autorisée a réussi ensuite.
Un unique essai avec les identifiants d'initialisation présents dans le source
desktop a retourné 400 ; aucun mot de passe valide n'est connu et aucun jeton
n'a été affiché. Aucun compte/mot de passe n'a été créé, supprimé ou modifié.

Le desktop s'authentifie via la collection `users` et lit `roles.permission`.
Son contrôle initial lit également `buma_config.CONFIGURATION_1.adminSetup` :
le superuser du dashboard seul ne suffit donc pas à initialiser le desktop.
Les données de bootstrap absentes sont signalées, sans inventer de configuration
ni importer le schéma desktop sur la baseline officielle.

Commandes : CLI `--help` et `create-admin --help`, lectures framework/frontend,
HTTP local, comptages SQL READ ONLY, git status/source HEAD et diff check.
La commande interactive `.venv/bin/python -m ppbase create-admin` lit `.env`,
demande email/mot de passe et refuse de remplacer un email existant. Elle est
indiquée à l'utilisateur pour créer son propre accès technique avec un autre
email sans effacer la base. Pas de test de parité métier/console dans ce tour.

Fichiers modifiés : ledger et tasklist uniquement ; les nouveaux fichiers C11
apparus dans le checkout sont laissés intacts. C10 reste ouvert. Prochaine
action : accès dashboard personnel, puis initialisation explicite du compte
`users`, rôle administrateur et configuration métier avant le test desktop.

## C10 — clarification des champs système users — 2026-09-26

L'utilisateur a accédé au dashboard et demande la provenance des champs
`id` et `tokenKey`. Vérification du schéma local en SQL READ ONLY : aucun
champ métier `tokenKey` ni `rowid` ajouté à users ; password est matérialisé
avec min=5 et username par la migration de compatibilité prévue. Les relations
métier correspondent à l'export corrigé.

Le framework `record_service.py` génère l'id si absent et la token_key lors de
la création auth. L'UI `system-fields-display.tsx` affiche id/tokenKey dans
la description des champs système, avec le badge Nonempty ; ce n'est pas une
demande de fournir une valeur technique. Réponse : ne pas les renseigner,
ils sont gérés automatiquement. Aucun identifiant/secret n'est inventé.

Commandes : lectures export/migrations/record-editor/system-fields-display et
record_service, schéma SQL READ ONLY, git status/source HEAD et diff check.
Aucun compte, collection, donnée, frontend ou framework modifié. Ledger et
tasklist seuls actualisés ; nouveaux travaux C11 préservés. Pas de test runtime
de création exécuté ; C10 reste ouvert, création de l'utilisateur par le
dashboard puis validation desktop toujours attendues.

## Rectification — id et tokenKey réellement affichés en création — 2026-09-26

L'utilisateur précise l'écran exact : « New users record », avec Email,
Password, Confirm, **id*** et **tokenKey***. L'explication précédente supposant
qu'il s'agissait seulement de la description des champs système était erronée.

Cause identifiée en lecture du framework/dashboard PPBase :
`ppbase/models/collection.py::CollectionResponse.from_record` ajoute les champs
système auth au contrat public, dont `id` required et `tokenKey` required,
system/hidden, avec génération automatique. `admin-ui/src/components/record-editor.tsx`
prend `collection.fields` en priorité et, lors de la création auth, ne retire
que `email` de la liste générique ; id/tokenKey sont donc rendus par
`RecordFieldInput` comme des champs texte. `record_service.py` génère pourtant
ces valeurs à la création. Le problème est l'édition de ces champs internes
dans le dashboard, pas l'absence de données métier BUMA.

Correction attendue dans le framework PPBase : ne pas demander tokenKey dans
le formulaire, traiter id comme généré automatiquement (éventuelle édition
explicite/avancée conforme au dashboard de référence), exclure ces valeurs
internes du payload générique et éviter les doublons de champs auth dédiés.
Ne pas supprimer les métadonnées système du contrat API ni modifier le schéma
BUMA pour contourner le formulaire. Cette correction n'est pas réalisée ici.

Commandes : lectures ciblées record-editor/record-field-input, sérialisation
CollectionResponse et génération côté record_service ; `git -C ../ppbase status`
(checkout propre au contrôle), diff check. Aucun test runtime de création,
compte, secret, donnée, code BUMA/frontend/framework modifié ; ledger/tasklist
seuls actualisés. C10 reste ouvert ; le formulaire dashboard est un blocage
concret supplémentaire à résoudre pour l'initialisation manuelle du test.

## C10 — origine historique de la régression dashboard auth — 2026-09-26

L'utilisateur relève que les anciennes versions PPBase n'affichaient pas id et
tokenKey en création, et suspecte le travail `pocketbase-auth-parity-v0404`.
Audit en lecture seule du framework : hypothèse confirmée. Aucun code corrigé.

Git blame/log/show identifient le commit
`8aec0617e0af5a55b1f0374e60e01eca6a9ed72f`, daté du **2026-09-21 à 14:35:03 UTC**,
`fix(auth): align password field resolution with PocketBase`. Il ajoute onze
lignes dans `ppbase/models/collection.py::CollectionResponse.from_record`,
incluant id/password/tokenKey/email/emailVisibility/verified lorsque
`record.type == "auth"`. id et tokenKey sont marqués required et ajoutés aux
deux représentations publiques schema/fields, sans modifier le stockage.

Le journal de la tâche PPBase `01a0ba03-d5cd-7b61-8245-43cfcc3b7330` date
l'édition agent originale du **2026-09-19 à 15:27:13 UTC** (apply_patch réussi ;
la tentative de 15:26:44 avait échoué). Cette édition faisait partie du travail
de parité auth utilisé par le handoff C3 BUMA, puis a été incluse dans le commit
du 21 septembre. Ce n'est pas une modification du formulaire desktop BUMA.

`admin-ui/src/components/record-editor.tsx` est inchangé entre le parent
`54a3b99`, le commit fautif et HEAD PPBase `5cc3dc0`. Le filtre générique ne
retire qu'email en création auth ; les nouveaux champs serveur apparaissent
donc comme des entrées à renseigner. Le `main` local (`aaa7c5a`) ne contient pas
cet ajout. Aucune consultation distante/fetch n'a été réalisée ; les références
origin citées sont les références locales connues.

Preuve comparative exécutée sans serveur ni base : chargement en mémoire du
modèle CollectionResponse depuis quatre révisions Git, mêmes métadonnées
synthétiques, puis application de la sélection de champs du formulaire.
Parent et main local : zéro entrée id/tokenKey ajoutée. Commit 8aec061 et HEAD :
`[id, tokenKey]` pour chacune des collections auth `_superusers`, `users` et
`agent_users`. Une collection base `roles` reste inchangée dans les quatre cas.
Assertions passantes ; ce test ne prétend pas être un rendu navigateur.
Artefact : `docs/c10-auth-dashboard-regression-audit.json`.

Le test existant `test_auth_collection_response_preserves_normalized_password_minimum`
contrôle password.min, mais ne vérifie pas l'affichage du formulaire. Les
preuves auth unitaires précédentes ne constituaient donc pas une validation
de non-régression du dashboard.

Fichiers changés ici : rapport d'audit, ledger et tasklist uniquement.
Commandes : git log -S/blame/show/diff/branch, lecture ciblée du journal de
session, comparaison Python des quatre révisions, parse JSON/diff check et
vérification du checkout PPBase propre. Aucun compte, donnée, endpoint,
frontend ou fichier du framework modifié ; travaux C11 préservés.

Décision : C10 reste ouvert. Corriger ensuite cette interaction métadonnées
auth/formulaire dans PPBase et vérifier les trois collections auth, sans
effacer la base ni annuler globalement les corrections de mot de passe.
L'historique confirme une régression de notre changement serveur de parité,
pas un comportement ancien normal du dashboard.

## Clôture définitive C10 sur décision utilisateur — 2026-09-28

Demande explicite : « ppbase a été correctement corrigée », « la C10 va être
considérée comme totalement finie maintenant, tu peux la fermer ».
**C10 est terminé**, décision D-0020. Ce statut remplace les attentes et gates
supplémentaires des dernières reprises ; il ne rétablit pas la clôture
unilatérale D-0017 retirée. D-0018 reste inchangée : PostgreSQL accepté dans
la console administrateur, `/sql` reporté et conservé.

### Portée et fichiers

Mise à jour de suivi uniquement : `AGENTS.md`, `docs/implementation-plan.md`,
`docs/migration-ppbase-tasklist.md`, ce ledger,
`docs/decisions-and-divergences.md`, `docs/c10-runtime-report.json`,
`docs/contracts/README.md`, `docs/contracts/routes.json`,
`docs/contracts/legacy-sql.md`, `docs/contracts/legacy-remaining.md`,
`docs/test-matrix.md` et la note de coordination de `docs/c11-preparation.md`.
Aucun code, migration, compte, donnée ou rapport runtime détaillé modifié.
Les changements des autres sessions dans BUMA/PPBase sont conservés.

Handoff relu avec les nouveaux documents C11. Source TypeScript toujours
propre sur `fix/uv-paani-sup@530018cd20c2acbe88f38b3639e500b5271e2eb2`, conforme
au manifeste. Aucun inventaire source régénéré. PPBase contient des changements
locaux de correction ; ils ne sont ni modifiés ni retestés ici. La confirmation
de correction est celle de l'utilisateur.

### Preuves conservées et limites

Le rapport global garde sa date de gate historique et ajoute une date/base de
clôture distincte : **acceptation explicite utilisateur**, pas nouvel essai.
Preuves précédemment enregistrées : 60 comparaisons des 18 routes restantes,
34 comparaisons caisse et 12 comparaisons rowid, soit 106 ; SDK réel
auth/refresh/CRUD/filter/expand/fichier/realtime avec révocation des droits ;
131 tests dans la gate C10 enregistrée ; 9 migrations, 114 définitions, 42 vues.
Les statuts/messages/enveloppes HTTP, guards, effets séquentiels/hooks natifs,
relations persistées, rollback/concurrence/idempotence applicables et les
opérations SDK sont ceux des scénarios décrits dans leurs rapports existants.

Aucune nouvelle comparaison HTTP, création native, opération SDK, exécution
desktop Qt, test PostgreSQL ou validation de persistance/rollback/concurrence
n'est exécutée dans ce tour. Le rapport du script SQL interrompu demeure absent
et n'est pas fabriqué. Les tests non enregistrés et les diagnostics historiques
des scripts ne deviennent pas passants du seul fait de cette clôture. Ils ne
constituent plus des conditions bloquantes C10 après la décision utilisateur.

Commandes/contrôles de ce tour : lectures des documents/rapports, Git status
dans BUMA et PPBase, rev-parse/status source, lecture du dernier log PPBase,
parse JSON des rapports et de l'inventaire, contrôle de cohérence des nouveaux
statuts et `git diff --check`. Aucun test applicatif lancé ; la modification
documentaire ne nécessite pas de rejouer toute la gate.

### Suite

Décision : clôturer C10 et reprendre au premier checkpoint ouvert, **C11**.
Sa préparation synthétique du 26 septembre (9 records, 3 fichiers, rollback,
concurrence, auth et lectures HTTP) reste acquise pour son périmètre. Il reste
à recevoir/identifier le backup Master terminé, exécuter son diagnostic en
lecture seule, puis traiter les blocages d'import, notamment D-0019 concernant
les relations multiples. Ne jamais aplatir leurs listes ni deviner les rowids.
Cette clôture n'autorise aucun import de production ni cutover. Aucun commit,
staging, reset ou nettoyage n'a été effectué.

## C11 — chemin du backup reçu et clarification du périmètre — 2026-09-28

L'utilisateur annonce le ZIP à
`/Users/omarsisss/Documents/wproject/protech/master-backup.zip` et demande
exactement son usage prévu et ce que couvre C11. Ce tour répond à cette
question ; il ne lance pas le diagnostic du backup. Archive non ouverte,
non extraite, non hashée ; existence/taille/contenu non vérifiés.

Handoff relu : consignes, baseline, périmètre C11 du plan/tasklist, dernières
entrées ledger, README des contrats, provenance/comptes de l'inventaire et
rapport d'artefacts C11. `git status --short --branch`, lecture du manifeste
et `git -C ../buma_master_hooks rev-parse HEAD` confirment toujours le commit
gelé `530018cd20c2acbe88f38b3639e500b5271e2eb2`. Modifications existantes
conservées ; aucun inventaire régénéré.

Explication remise : C11 couvre diagnostic du backup, préparation de l'import
fidèle, répétition sur cible fraîche isolée, comparaison des données/fichiers
et comportements clients, procédure de rollback puis éventuelle bascule sous
autorisation distincte. L'étape immédiate prévue avec le ZIP est un rapport
de compatibilité : intégrité/provenance, schéma, IDs, relations, rowids,
fichiers et anomalies, sans réécriture de la sauvegarde. Le prototype actuel
n'importe que ses fixtures synthétiques ; aucune restauration réelle n'est
lancée du seul fait que le chemin vient d'être fourni.

Fichiers modifiés : tasklist (chemin reçu séparé du diagnostic encore ouvert)
et ce ledger. Validation documentaire : `git diff --check`. Aucun test,
serveur, appel API/SDK, hook, permission, persistance, rollback ou concurrence
réexécuté ; les preuves synthétiques du 26 septembre ne deviennent pas des
preuves sur ce backup. Aucun import de données de production ni cutover.

Les documents locaux comportent une nouvelle clôture C10 D-0020 datée du
28 septembre, tandis que les consignes fournies dans le contexte de ce tour
reprennent encore son état en attente. Ce tour ne modifie aucun statut C10
et ne reprend aucun de ses travaux ; cette différence n'affecte pas
l'explication du périmètre C11 demandé.

Décision : terminer la clarification ; C11 reste ouvert. Prochaine action
technique prévue : inventaire en lecture seule du ZIP indiqué, puis diagnostic
d'une copie hors ligne, en conservant l'archive originale. Import réel et
bascule restent hors du périmètre exécuté dans ce tour.

## C11 — diagnostic réel du backup, étapes 1 et 2 — 2026-09-28

Statut : **diagnostic terminé ; import bloqué ; C11 ouvert**. Après l'explication
du périmètre, l'utilisateur répond « okay cool alors, vas y ». Autorisation
appliquée aux deux étapes annoncées : examen du ZIP et diagnostic hors ligne,
sans import PostgreSQL. Il fournit ensuite **PocketBase v0.22.34** comme
version source. Aucun test ou changement C10 ; aucun accès à un serveur réel.

### Provenance et fichiers

Handoff et mémoire C11 relus, état Git existant préservé ; source gelée et
inventaire conformes au manifeste `530018cd20c2acbe88f38b3639e500b5271e2eb2`.
Le ZIP fourni `/Users/omarsisss/Documents/wproject/protech/master-backup.zip`
contient un README `buma_master_database`, `data.db`, `logs.db`, WAL vides,
SHM, types.d.ts et storage. Aucun binaire/manifeste ne prouve la version ;
0.22.34 est explicitement déclarée par l'utilisateur, jamais inférée.

Fichiers nouveaux : `scripts/c11_snapshot_audit.py`,
`tests/test_c11_snapshot_audit.py`, `docs/c11-master-diagnostic.md` et rapports
`c11-master-{archive-inventory,extraction-report,initial-diagnostic,detailed-audit,
duplicate-analysis,legacy-clues,multirelation-field-check,preservation-report,
diagnostic-report}.json`. Fichiers de suivi actualisés : AGENTS (C11 seulement),
tasklist, ledger, D-0021, `docs/c11-preparation.md` et ajout d'une mise à jour
datée dans `docs/c11-artifact-diagnostic.json`. Aucun handler, migration,
framework ou source TypeScript modifié.

Copie privée dans `backups/c11-master-20260928/`, exclue de Git : répertoires
0700/fichiers 0600, manifeste de chaque membre et détails privés des chemins
fichiers. Aucune donnée de record, secret ou chemin de fichier personnel
n'est publié dans les rapports de documentation.

### Commandes et preuves

- `git status --short --branch`, source `rev-parse`, lecture manifeste et
  inventaire ; espace disque environ **260 Go libres** avant extraction.
- `.venv/bin/python -m scripts.c11_import --archive
  /Users/omarsisss/Documents/wproject/protech/master-backup.zip --report
  docs/c11-master-archive-inventory.json` : inventaire passant, aucun chemin
  dangereux/doublon/lien symbolique/chiffrement/WAL non vide.
- Extraction Python `zipfile` en flux vers un nouveau dossier privé, sans
  exécuter le contenu : **1 290 fichiers**, chaque CRC vérifié par lecture
  complète, SHA-256 calculé ; **8 526 465 264 octets**, environ **11,18 s**.
  ZIP **1 555 849 147 octets**, SHA-256
  `541ca900839207c53dedb19b51e329bbec89c8e98774043c7b9dab2bbcd61295`.
- `.venv/bin/python -m scripts.c11_import --data backups/c11-master-20260928
  --schema /Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json
  --report docs/c11-master-initial-diagnostic.json` : code 1 attendu,
  **ready_for_isolated_rehearsal=false**. Version non fournie à ce premier
  lancement ; réponse utilisateur 0.22.34 consignée dans les rapports suivants.
- SQLite ouvert `mode=ro&immutable=1`, query_only : intégrité data.db **OK**,
  empreinte **3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd**.
  Schéma **114/114**, 70 base/2 auth/42 vues, aucun écart avec l'export corrigé.
  **1 188 247 records** lus, rowids et contenus hashés, aucune réécriture.
- Le premier diagnostic s'arrête au premier défaut par record et masquait
  des fichiers/relations supplémentaires. Ajout d'un **audit séparé**, sans
  relâcher les refus de l'importeur : `.venv/bin/python -u -m
  scripts.c11_snapshot_audit --data backups/c11-master-20260928
  --source-version 0.22.34 --report docs/c11-master-detailed-audit.json
  --private-report backups/c11-master-20260928/c11-private-file-findings.json`
  → **10,84 s**, **191 champs relationnels / 4 896 135 références**, tous les
  **20 champs fichiers** contrôlés, même après un premier défaut.
- Agrégats SQLite complémentaires en lecture seule : clés idempotentes par
  type/auteur, hashes des records hors id/created/updated, correspondances
  ancienne/nouvelle collection client, distribution des longueurs de listes,
  sentinelles et catégories des objets storage. Résultats dans les rapports
  duplicate-analysis, legacy-clues et multirelation-field-check.
- Comparaison de préservation finale : ZIP et **1 290 membres extraits**
  rehashés, tailles/empreintes toutes identiques au manifeste. Le data.db
  archivé est daté du 15 septembre, logs.db du 16 septembre ; ces timestamps
  ZIP sans fuseau ne constituent pas une preuve de capture cohérente.
- `.venv/bin/pytest -p no:cacheprovider -q` : **157 passed in 1.12s**.
  Ruff/format ciblés de l'outil C11 et tests : passants. Trois nouveaux tests
  prouvent l'audit des arrays, le comptage record/cible distincte et la poursuite
  après un fichier absent. `git diff --check` et parse JSON : passants.

### Résultats et portée des comparaisons

- **D-0019 confirmé sur 5 841 records** : customer.pdv 5 826 (331 listes non
  vides), ticket.tag 4, wallet.managers 11 (une liste non vide). Aucun JSON
  invalide ; les listes restent des listes même avec un seul élément. Lecture
  du DDL/validateur PPBase actuel confirme `maxSelect null -> scalaire` ; aucune
  nouvelle base PostgreSQL n'est lancée pour répéter la preuve physique ancienne.
- **877 groupes mobile_message_id RETURN / 2 391 transactions**, max 16/groupe,
  **tous différents sur des valeurs métier** hors IDs/timestamps. Aucune clé
  system_id non vide dupliquée. L'index unique C4 rejetterait cet historique ;
  aucune suppression des 1 514 lignes excédant une par clé, aucune réécriture
  de clé ni désactivation d'index. `master.idempotency.ts` et
  `master.retour.functions.ts` relus dans la source gelée.
- **195 795 références sans cible / cinq champs** : vente.customer 115 325
  vers ancienne table vide (115 168 existent dans la nouvelle, 157 non) ;
  sim_balance_activity.author 80 461 valeurs `1` ; article.categorie sept
  valeurs `0` ; customer.type une valeur `PERSONNE` aussi présente dans le setup
  gelé ; wallet.managers un élément contenant deux IDs joints par virgule.
  Ces valeurs sont source, pas des effets d'un import. Aucune cible inventée,
  relation redirigée ou normalisation.
- **2 899 fichiers référencés uniques : 596 présents, 2 303 absents**, dont
  2 302 ressources et une image wallet. Aucun nom retrouvé ailleurs dans
  storage, S3 désactivé dans la configuration archivée. **686 objets extra**
  conservés : 601 `.attrs`, 80 miniatures et cinq autres ; ils ne sont pas
  supprimés comme des fichiers supposés inutiles. Total storage **1 282 objets /
  4 890 458 144 octets**. Aucun ancien serveur/autre backup consulté.
- **20 users, 3 agent_users, un admin**, hashes bcrypt non vides et tokenKeys
  de 50 caractères pour les 23 records auth ; présence contrôlée, aucun login.
  Paramètres/secrets conservés dans le SQLite privé, jamais activés ; aucun
  auth externe enregistré. Le contenu de logs.db n'est pas interrogé.

Contrats comparés : métadonnées des 114 collections, IDs/rowids, formes
relationnelles, fichiers natifs et correspondance champs auth. La comparaison
est statique/offline sur les vraies données. **Aucune enveloppe HTTP, statut,
permission effective, hook, CRUD/SDK, desktop, persistance PostgreSQL,
rollback ou concurrence n'est exercé avec ces données.** Les preuves
synthétiques antérieures ne sont pas réattribuées au backup réel.

### Décision et prochaine action

Les deux étapes demandées sont terminées, le backup original reste intact,
la copie privée reste disponible pour vérification. D-0021 distingue les
incompatibilités cible des anomalies historiques et des fichiers absents.
Aucune différence/perte n'est acceptée et C11 reste ouvert.

Prochaine action de préparation : concevoir une migration préservant les
listes et toutes les transactions aux clés dupliquées, documenter le maintien
des valeurs orphelines, rechercher ou qualifier explicitement les fichiers
absents. Ensuite seulement préparer une répétition complète sur données
réelles dans le périmètre autorisé, avec référence 0.22.34. **Aucun import
PostgreSQL, bascule, service/external integration, commit, stage ou reset**
effectué dans cette session ; les changements existants sont conservés.

## Handoff du 2026-10-22

Le dépôt local est prêt pour un agent suivant, mais l'état complet n'est pas
encore publié sur GitHub : branche `features/auth`, HEAD local et distant
`46fc6cd`, **33 fichiers suivis modifiés et 60 fichiers non suivis**. Le backup
extrait `backups/c11-master-20260928/` est ignoré par Git et reste local.
Une note de passation synthétique a été ajoutée dans
`docs/next-agent-handoff.md`. Elle pointe vers le diagnostic C11 réel, les
rapports, les blocages D-0019/D-0021 et les actions suivantes.

Cette passation ne commit, ne stage, ne pousse et ne nettoie aucun changement.
L'agent suivant doit travailler dans cet état local ou effectuer d'abord une
revue contrôlée des changements avant toute publication GitHub. Il ne doit pas
reprendre C10 ; le prochain checkpoint reste C11, avec stratégie d'import
préservant arrays, doublons, références orphelines, rowids et fichiers absents.

## Publication du travail local autorisée — 2026-10-22

L'utilisateur demande explicitement « commit et push les sur github » après
le relevé des changements locaux. Périmètre de livraison : **94 fichiers**,
soit 33 fichiers suivis modifiés et 61 nouveaux fichiers, incluant la note de
passation. Branche cible `features/auth`, remote
`git@github.com:18BG/buma_master_ppbase.git` ; aucun merge vers `main` demandé.
`git ls-remote --heads origin features/auth main` confirme avant publication
`features/auth=46fc6cd` et `main=2ce54e4`.

Le commit regroupe les travaux déjà autorisés C10/C11 : hooks/routes legacy,
contexte realtime, corrections vues/rowids (migrations 1789236847/48), tests
et scripts de preuve, diagnostics C11, contrats, décisions et suivi. Les
rapports des sessions antérieures conservent leur portée/date ; aucune preuve
runtime n'est régénérée ou inventée. Les données de production ne sont ni
modifiées, ni importées, ni publiées.

Changements effectués pour cette livraison uniquement :

- `scripts/c10_sql_usage_audit.py` : deux chaînes longues réparties sur
  plusieurs lignes, sans changement de valeur.
- `scripts/c10_stored_sql_runtime.py` : tri des imports, retrait des imports
  inutilisés `httpx` et `create_async_engine` ; aucun changement fonctionnel.
- `docs/next-agent-handoff.md` : livraison sur la branche explicite, compte
  initial historisé et dépendances externes détaillées. Le venv charge
  `../ppbase` 0.3.1 editable, dont les changements locaux ne sont pas inclus.
  Le clone GitHub ne contient pas le backup, l'export corrigé externe ni
  la source TypeScript sibling. Tasklist et présent ledger actualisés.

Vérifications exécutées avant commit :

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **157 passed in 0.68s** après nettoyage statique.
- `.venv/bin/ruff check --no-cache app tests scripts` : **passant**.
- `.venv/bin/ruff format --no-cache --check app tests scripts` :
  **109 files already formatted**.
- `git diff --check` et `git diff --cached --check` : passants.
- Contrôle des 94 chemins et parsing des 27 fichiers JSON concernés :
  passants ; recherche ciblée de clés privées, tokens GitHub/AWS/JWT et hashes
  bcrypt littéraux sans résultat. Ce contrôle ne remplace pas un audit exhaustif.
- `git check-ignore -v` confirme l'exclusion de `backups/`, `.env` et
  `pb_data/.jwt_secret`. Le ZIP `../master-backup.zip` est hors dépôt.
  Aucun fichier DB, archive ZIP, `.env` ou secret candidat dans l'index.
- `git add -A` exécuté dans ce dépôt uniquement : 94 fichiers staged.

Les comparaisons HTTP/SDK, permissions et effets hooks, rollback/concurrence
restent les preuves historiques C10 et synthétiques C11. La présente validation
est un contrôle de tests locaux, de style et de contenu à publier, pas un
replay de serveur ni une validation de données réelles.

Décision : créer le commit de livraison puis pousser normalement sur
`origin/features/auth`, sans force push. La référence Git distante sert de
confirmation finale. C10 reste fermé sous D-0020, C11 ouvert ; l'agent suivant
reprend la stratégie de conservation D-0019/D-0021, sans import/cutover.

## Reprise par un nouvel agent — analyse de passation — 2026-10-23

L'utilisateur annonce le passage d'un agent GPT à un nouvel agent (Claude) et
demande d'analyser le projet et de tout lire avant le moindre changement, en
particulier `docs/next-agent-handoff.md`. Session d'**analyse uniquement** :
aucun code, script, test, migration, rapport de preuve ou fichier du framework
modifié ; seuls ce ledger et la tasklist reçoivent cette entrée. Aucun commit,
stage ou push.

### Procédure de handoff et état constaté

- Lus : note de passation, AGENTS, baseline, plan, tasklist, ledger complet,
  README des contrats et `source-inventory.json`, décisions D-0001 à D-0021,
  matrice de tests, structure, `rowid.md`, `transaction-invariants.md`,
  `c11-preparation.md`, `c11-master-diagnostic.md` et ses rapports JSON,
  `scripts/c11_import.py`, `c11_snapshot_audit.py`, `c11_rehearsal.py`,
  `tests/test_c11_import.py`, `app/hooks/register.py`,
  `app/transactions/idempotency.py`, migrations `1789236845` et `1789236848`.
- Git : `features/auth...origin/features/auth`, propre, HEAD `3d20322`. Un
  worktree `.kilo/worktrees/exclusive-aerosteon` (HEAD détaché `46fc6cd`,
  instantané d'époque C9) existe ; il n'est ni utilisé ni modifié.
- Source gelée propre sur
  `fix/uv-paani-sup@530018cd20c2acbe88f38b3639e500b5271e2eb2`, conforme au
  manifeste ; inventaire non régénéré.
- PPBase sibling : `5cc3dc0`, `pocketbase-auth-parity-v0404`, 15 fichiers
  suivis modifiés et 3 non suivis, comme la note. Le venv charge PPBase 0.3.1
  editable depuis `../ppbase` (Python 3.14.6).
- ZIP et copie privée `backups/c11-master-20260928/` présents ; ni ouverts,
  ni hashés, ni interrogés dans cette session.

### Commandes et résultats

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **150 passed, 7 failed**. Les sept échecs (`test_schema_baseline` ×2,
  `test_view_translation` ×3, `test_view_verifier` ×2) ont la même cause :
  `PermissionError: [Errno 1] Operation not permitted` à la lecture de
  `/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json`. `ls` voit
  le fichier (420 423 octets), mais sa lecture est refusée, y compris hors
  sandbox : protection de confidentialité macOS sur `~/Downloads` pour ce
  processus. Ce n'est pas une régression de code, mais le résultat de 157
  tests du 22 octobre n'est **pas reproduit** dans cet environnement et le
  SHA-256 de l'export n'a pas pu être vérifié.
- `.venv/bin/ruff check --no-cache app tests scripts` : passant ;
  `.venv/bin/ruff format --no-cache --check app tests scripts` : 109 fichiers
  déjà formatés.
- Lectures seules de la source gelée (`master.idempotency.ts`,
  `master.retour.functions.ts:250-400`, `master.stockage.functions.ts:178-300`,
  `master.utility.ts:72-78`, `migrationsDir/1785599959_updated_buma_transaction.js`)
  et recherche `maxSelect` dans le framework PPBase. Aucune donnée réelle lue,
  aucun Docker, serveur, migration ni import lancé.

### Constats d'analyse (aucune décision prise)

1. **Index C4 différent de la source et de la production, non documenté.**
   La migration source `1785599959` (introduite par `530018c`) crée seulement
   `UNIQUE (system_id) WHERE system_id LIKE 'idem:%'` et garde un index **non
   unique** sur `mobile_message_id`. Le schéma de production (export corrigé,
   baseline `1789236840` lignes 7931-7932, backup sans écart d'index selon
   D-0021) ne contient que ces deux index non uniques sur `buma_transaction`,
   sans même l'index unique source. La migration PPBase `1789236845` impose
   `UNIQUE (type, author, system_id) WHERE system_id <> ''` et
   `UNIQUE (type, author, mobile_message_id) WHERE mobile_message_id <> ''`.
   Aucune entrée D-xxxx ne compare ces index à la source ;
   `transaction-invariants.md` les décrit sans cette comparaison. C'est l'index
   mobile, propre à PPBase, qui rejette les 877 groupes de D-0021. Dans la
   source, `newReturn` ne crée qu'une transaction RETURN par appel après lookup ;
   la conversion écrit OUT/IN avec le même `mobile_message_id`, mais sous deux
   types. Le blocage des doublons D-0021 provient donc d'une contrainte qui
   n'existe ni dans la source gelée ni dans le schéma de production.
2. **Lecture de « 877 groupes aux valeurs différentes ».** La comparaison
   excluait seulement id/created/updated. Des retries de l'ancien flux créent
   chacun un `return` distinct et un `old_quantity` différent : elle ne
   distingue donc pas des retries d'opérations distinctes. Dans les deux cas,
   chaque ligne porte des effets stock/dette réels ; la conclusion « ne pas
   dédupliquer » reste valable. Non vérifié sur les données dans cette session.
3. **Ordre du lookup.** La source renvoie `records?.[0]` de `findRecordsByExpr`
   sans ORDER BY explicite ; PPBase trie `ORDER BY created ASC LIMIT 1`. Avec
   l'historique dupliqué importé, un retry d'une ancienne clé pourrait renvoyer
   un record différent. À comparer si ces anciennes clés peuvent être rejouées.
4. **Périmètre de D-0019.** Le framework calcule `opts.get("maxSelect", 1) or 1`
   dans `db/schema_manager.py`, `models/field_types.py`, `api/files.py`,
   `backup/canonical.py` et `services/filter_parser.py` : `null` y vaut 1,
   alors que PocketBase 0.22 le traite comme multiple. Une correction fidèle
   touche le framework sibling (qui a des changements locaux non commités) ou
   exige une alternative BUMA explicitement documentée.
5. **Version.** `diagnose()` bloque toute version autre que 0.22.27
   (`source_pocketbase_version_unvalidated`) : le backup réel 0.22.34 restera
   bloqué sans référence 0.22.34 validée.
6. **Volume.** `load_isolated` exécute un INSERT, une archive rowid et une
   relecture par record (environ 3,6 millions d'instructions pour 1 188 247
   records) ; la durée d'une répétition complète reste à mesurer.
7. **Dépendance externe.** L'export est référencé par chemin absolu dans trois
   modules de test et sept scripts, hors dépôt.

### Décision et prochaine action exacte

Analyse terminée ; **C11 reste ouvert**, aucun statut de checkpoint modifié,
aucune divergence acceptée. Prochaines actions, dans l'ordre :

1. Rendre l'export lisible par le processus de l'agent (autorisation macOS
   « Fichiers et dossiers » ou accès complet au disque pour le terminal), puis
   rejouer pytest (157 attendus) et vérifier le SHA-256 `9d99e7b5…b2313d`.
2. Obtenir la décision de l'utilisateur sur l'index C4 (constat 1) :
   réaligner sur l'index source ou conserver l'index strict en le documentant.
   L'option retenue exige une entrée D-0022, sa migration, ses tests et le
   rejeu des preuves C4/C7/C9 concernées avant la stratégie d'import des
   2 391 transactions.
3. Si l'utilisateur l'autorise, agrégats read-only sur la copie privée :
   unicité globale de `system_id LIKE 'idem:%'` et dates des 877 groupes par
   rapport au 31 août 2026, date du commit d'idempotence.
4. Choisir la voie de correction D-0019 (framework ou BUMA), sans aplatir.

## C11 — D-0022 : idempotence C4 réalignée sur la source — 2026-10-23

Suite de la reprise du même jour. L'utilisateur demande si le ZIP est
« l'export » : non, le ZIP est le backup de données, l'export est le JSON du
schéma dans `~/Downloads`. Il retient l'option (a) recommandée (« si tu
recommandes le a, on part sur cela ») et autorise le démarrage de Docker
Desktop pour une preuve dans un conteneur jetable, sans toucher la base locale.
Les agrégats read-only sur la copie privée du backup lui ont été expliqués
(aucun déploiement ni serveur), puis exécutés.

### Provenance en lecture seule

- Copie privée `backups/c11-master-20260928/data.db` : SHA-256 `3539f3eb…`
  identique avant et après, `mode=ro&immutable=1`, Python lancé avec `-I`.
- `scripts/c11_idempotency_source_check.py` →
  `docs/c11-idempotency-source-check.json` : 325 des 326 migrations source
  appliquées en production, seule `1785599959_updated_buma_transaction.js`
  manque ; index physiques `idx_Yo33dyU` et `idx_bEWTmoj` ; 93 220 `system_id`
  non vides, aucun préfixé `idem:` ; 877 groupes, 2 391 lignes, du 2024-11-18 au
  2025-01-10 ; première ligne par rowid et par created : 843 identiques,
  30 égalités de date, 4 inversions.

### Changements

- Nouvelle migration `pb_migrations/1789236849_c11_source_transaction_idempotency.py`.
- `scripts/verify_schema_baseline.py` : `SOURCE_INDEX_DELTAS`, lu dans la
  migration ; l'attendu devient l'export plus l'index source.
- `scripts/c11_import.py` : `idempotency_key_conflicts()` (index source)
  remplace les deux contrôles C4 de `diagnose()`.
- `scripts/c9_commerce_runtime.py` : `ROOT`, `TARGET` et `REPORT` au niveau
  module, valeurs par défaut inchangées, pour rejouer sans écraser
  `docs/c9-runtime-report.json`.
- Nouveaux scripts `scripts/c11_idempotency_source_check.py` et
  `scripts/c11_idempotency_runtime.py`.
- Tests : `test_idempotency.py` (+15), `test_c11_import.py` (+1),
  `test_schema_baseline.py` (+1 et deux tests adaptés), `test_view_verifier.py`
  (adapté).
- Documentation : D-0022, `transaction-invariants.md`,
  `c11-master-diagnostic.md`, AGENTS, note de passation, plan, tasklist,
  matrice de tests, structure et ce ledger ; rapports
  `docs/c11-idempotency-source-check.json` et
  `docs/c11-idempotency-runtime-report.json`.

### Commandes et résultats

- `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
  **167 passed, 7 failed**. Les sept échecs sont les mêmes tests qui lisent
  l'export (PermissionError macOS). Les deux tests du vérificateur adaptés à
  D-0022 en font partie et restent donc **non rejoués**.
- Ruff check et format check sur app, tests, scripts et la nouvelle migration :
  passants, 112 fichiers formatés ; `git diff --check` passant.
- Docker Desktop démarré avec l'accord de l'utilisateur ; le démon était
  arrêté et le PATH de session a été complété par `/usr/local/bin`.
- `PATH=/usr/local/bin:$PATH .venv/bin/python -m scripts.c11_idempotency_runtime
  --report /private/tmp/buma-d0022-runtime-attempt{1,2}.json` : **passant deux
  fois**, résultats identiques hors horodatage. La seconde exécution porte sur
  la version finale du script (longueur de ligne corrigée sans changement
  logique) ; sa copie est `docs/c11-idempotency-runtime-report.json`.
  - 10 migrations sur PostgreSQL 16 vierge : index source unique présent,
    index C4 absents, `idx_bEWTmoj` non unique, métadonnées égales à la
    baseline plus l'index source.
  - Matrice source : mobile identique et clé non `idem:` dupliquée acceptés ;
    clé `idem:` d'un autre type/auteur et préfixe `IDEM:` rejetés par
    `idx_transaction_idempotency` ; préfixe non ASCII et valeurs différant par
    la casse acceptés.
  - Après `run_migrations_down` : index et métadonnées C4 restaurés, matrice C4
    inverse (mobile et clé legacy rejetés, clé `idem:` multi-type acceptée) ;
    `run_migrations_up` rétablit l'état source.
  - Avec deux lignes au même `mobile_message_id`, le rollback échoue sur
    `idx_buma_transaction_idempotency_mobile` et s'annule : index source,
    métadonnées et lignes conservés.
  - Huit tentatives concurrentes sur une clé mobile seule (verrou et lookup,
    sans index) : une seule ligne.
  - `c4_idempotency_runtime` : rollback, concurrence, lookup et nettoyage OK.
  - Serveur PPBase isolé (port aléatoire, cwd temporaire, jobs désactivés) :
    `c7_stockage_runtime` passant ; `c9_commerce_runtime` **29 routes**,
    idempotence et concurrence recharge/retour, nettoyage OK, rapport écrit
    en temporaire.
  - Aucune ligne `d0022-%` restante ; conteneur supprimé (0 par label) ; autres
    conteneurs inchangés ; `docs/c9-runtime-report.json` inchangé.

### Comparaisons et limites

Comparés à la source gelée : la migration `1785599959` (nom, colonne,
prédicat, absence de type et d'auteur), `master.idempotency.ts` (lookup
inchangé), `newReturn`, `newRecharge` et la conversion (seules écritures de
`system_id`, toujours préfixées `idem:` en minuscules), et la sémantique du
`LIKE` SQLite, testée contre SQLite. Aucune route, payload, enveloppe, statut,
permission ou hook n'est modifié. Les preuves HTTP C7/C9 sont un rejeu PPBase,
pas une nouvelle comparaison avec un serveur PocketBase. Non rejoués : la
répétition C11 synthétique (`c11_rehearsal.py` exige l'export et le binaire
PocketBase), `make verify-all` et les deux tests du vérificateur qui lisent
l'export. La base locale `buma_master_ppbase` n'a été ni démarrée ni migrée :
`make start` y appliquera `1789236849` au prochain démarrage.

### Décision et prochaine action

D-0022 réalisée ; **C11 reste ouvert** (D-0019, références orphelines, fichiers
absents, import réel non autorisé). En attente de l'utilisateur : (1) accès
macOS à `~/Downloads` pour rejouer les sept tests, `verify-all` et la
répétition ; (2) décision sur l'ordre du lookup de l'historique dupliqué
(34 groupes) ; (3) voie de correction D-0019. Docker Desktop reste démarré.
Aucun commit.

## Préparation de la session serveur — D-0023, PPBase poussé — 2026-10-24

L'utilisateur veut poursuivre sur son serveur dans une nouvelle session pour
pouvoir fermer le Mac, avec un objectif : BUMA Master PPBase presque complet
sous 12 heures, en orchestrant agents et sous-agents. Il accepte l'option 2 sur
l'ordre du lookup et demande s'il ne reste que D-0019. Réponse : non. Restent
aussi les orphelins, les fichiers absents, comptes/paramètres/sessions, la
version 0.22.34, la répétition réelle et le runbook.

### Décisions et autorisations de l'utilisateur

- D-0023 : écart d'ordre du lookup accepté, avec test de non-régression.
- Commit et push du travail BUMA sur `origin/features/auth`.
- PPBase : « si la branche actuelle a été créée pour ces changements, commit
  directement dessus, sinon une branche séparée et push ». Les 15 fichiers
  modifiés et 3 non suivis traitent des champs système auth et du formulaire
  du dashboard, suite du commit `8aec061` de `pocketbase-auth-parity-v0404` :
  commit sur cette branche.
- Session serveur : orchestration multi-agents ; commits et push au fil de
  l'eau sur branches dédiées ; correction de D-0019 dans PPBase avec la version
  actuelle de PocketBase (documentation et code source) comme référence ;
  backup copié sur le serveur par l'utilisateur, importé seulement dans un
  PostgreSQL jetable.

### Actions et preuves

- Constats : PPBase ne crée aucune clé étrangère pour les relations, donc les
  orphelins sont stockables ; relations en `VARCHAR(15)` ou `VARCHAR(15)[]`, où
  l'élément `managers` de 31 caractères ne passerait pas ; PPBase accepte les
  jetons `authRecord` signés avec `token_key` plus le secret de collection, donc
  la continuité des sessions est possible si l'import reprend le secret.
- PPBase : `git fetch` sans retard ; `tests/test_auth_parity_core.py`
  16 passants ; `node --test tests/test_admin_ui_record_editor_fields.mjs` 6 sur
  6 ; recherche de secrets négative ; commit `c6821aa` « fix(auth): keep auth
  system fields out of the record editor », push `8aec061..c6821aa`. Plus aucun
  changement local.
- BUMA : test D-0023 ; `BUMA_SCHEMA_EXPORT` dans 3 tests et 7 scripts, défaut
  inchangé, vérifié avec un chemin factice ; plan `docs/server-session-plan.md` ;
  note de passation, AGENTS, README, tasklist, matrice et décisions à jour.
- `pytest` : 168 passants et les 7 échecs liés à l'export illisible ; Ruff,
  format (112 fichiers) et `git diff --check` passants.

### Décision et prochaine action

Ce travail est commité et poussé sur `origin/features/auth` dans le commit qui
contient cette entrée. Prochaine action : session serveur selon
`docs/server-session-plan.md`, en commençant par sa phase 0. C11 reste ouvert ;
aucune donnée réelle importée, aucune action sur la production.
