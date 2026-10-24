# Contrat C11 — comptes, paramètres et continuité des sessions (WS-C)

Chantier WS-C de la session serveur du 2026-10-24. Source gelée
`buma_master_hooks@530018c` ; données produites par PocketBase **0.22.34** ;
cible PPBase `pocketbase-auth-parity-v0404@c6821aa`. Projet de décision : D-XXXX
(numéro attribué par l'orchestrateur).

Fichiers : `scripts/c11_auth_import.py` (import et diagnostic),
`scripts/c11_auth_runtime.py` (preuve synthétique),
`tests/test_c11_auth_import.py`, `docs/c11-auth-runtime-report.json`.

## 1. Jetons PocketBase 0.22.34 et vérification PPBase

Les jetons sont des JWT HS256 sans état : rien n'est stocké côté serveur et un
refresh n'invalide pas les jetons précédents (documentation actuelle,
https://pocketbase.io/docs/authentication/). La continuité ne dépend donc que
du matériel de signature : `tokenKey` du compte + secret du type de jeton.

| Jeton 0.22.34 | Claims | Clé de signature | Source |
| --- | --- | --- | --- |
| admin (session) | `exp, id, type=admin` | `admin.tokenKey + adminAuthToken.secret` | `tokens/admin.go:10-17` |
| admin (reset) | `exp, id, type=admin, email` | `tokenKey + adminPasswordResetToken.secret` | `tokens/admin.go:19-26` |
| admin (fichier) | `exp, id, type=admin` | `tokenKey + adminFileToken.secret` | `tokens/admin.go:28-35` |
| record (session) | `exp, id, type=authRecord, collectionId` | `record.tokenKey + recordAuthToken.secret` | `tokens/record.go:12-27` |
| record (vérification) | idem `+ email` | `tokenKey + recordVerificationToken.secret` | `tokens/record.go:29-45` |
| record (reset) | idem `+ email` | `tokenKey + recordPasswordResetToken.secret` | `tokens/record.go:47-63` |
| record (changement d'email) | idem `+ email, newEmail` | `tokenKey + recordEmailChangeToken.secret` | `tokens/record.go:65-78` |
| record (fichier) | `exp, id, type=authRecord, collectionId` | `tokenKey + recordFileToken.secret` | `tokens/record.go:80-95` |

Références `v0.22.34` : `tools/security/jwt.go:45-58` (`NewJWT` : `exp` puis
claims, HS256), `:27-43` (`ParseJWT` n'accepte que HS256), `:10-25`
(`exp/iat/nbf` contrôlés) ; `apis/middlewares.go:212-254` (`LoadAuthContext`
choisit le secret selon `type`) ; `daos/admin.go:49-78` et
`daos/record.go:411-444` (recherche du compte par `id`/`collectionId`, puis
vérification avec `tokenKey + secret`). Les secrets sont **globaux** dans
`_params.settings` (`models/settings/settings.go:24-70`), JSON en clair ou
chiffré si `--encryptionEnv` (`daos/settings.go:12-56`).

PPBase vérifie la même formule par collection : `ppbase/api/deps.py:79-101`
(`type=admin` : `_superusers.token_key + options.authToken.secret`) et
`:103-150` (`type` `auth` ou `authRecord` : `collectionId`, `token_key` du
record, `options.authToken.secret`), HS256 et `exp` vérifiés par PyJWT
(`ppbase/services/auth_service.py:398-423`). Les types hérités sont acceptés
explicitement (`auth_service.py:18-19`), refresh compris
(`record_auth_service.py:420-462`, `:507-564`).

PocketBase lui-même ne garde pas ces sessions lors de sa mise à niveau : sa
migration 0.22 → 0.23 recopie les secrets, mais la version actuelle n'accepte
plus que `type=auth` (`v0.40.6 apis/middlewares.go:199`,
`core/record_query.go:483-528`). PPBase les garde, conformément au choix de
l'utilisateur (reprendre les secrets pour que les sessions continuent).

## 2. Correspondance (logique de `v0.23.0 migrations/1717233556_v0.23_migrate.go`)

### `_admins` → `_superusers`

| PocketBase 0.22 | PPBase | Traitement |
| --- | --- | --- |
| `id`, `email`, `created`, `updated` | mêmes colonnes | copiés (dates relues en UTC, millisecondes) |
| `passwordHash` | `password_hash` | copié tel quel, aucun mot de passe régénéré |
| `tokenKey` | `token_key` | copié tel quel (continuité des jetons admin) |
| `avatar` | `avatar` | copié : PPBase le sérialise dans `/api/admins` comme 0.22 (`admin_service.py:37-45`) |
| `lastResetSentAt` | `last_reset_sent_at` | **non activé** : colonne laissée NULL, valeur archivée |
| rowid SQLite | — | archivé |

La migration officielle copie `id, email, passwordHash→password, tokenKey,
created, updated` et supprime `_admins` (`migrate.go:114-127`). Ici `avatar`
est conservé parce que PPBase expose encore l'API `/api/admins` de 0.22.
`lastResetSentAt` ne sert en 0.22 qu'au délai de 120 s de
`/api/admins/request-password-reset` (`forms/admin_password_reset_request.go:73-77`),
route absente de PPBase ; PPBase a la colonne mais aucun lecteur.

Archive : table technique `_buma_c11_auth_archive(source_table, record_id,
source_rowid, legacy)`, sans collection publique : `_admins` (rowid,
`lastResetSentAt`) et `_params` (rowid, clé). Elle n'est pas mélangée à
`_buma_c11_rowids`, dont les lignes désignent des collections de l'export.

### `_params.settings` → options des collections auth

| Réglage 0.22 | Cible PPBase |
| --- | --- |
| `recordAuthToken`, `recordPasswordResetToken`, `recordEmailChangeToken`, `recordVerificationToken`, `recordFileToken` | `authToken`, `passwordResetToken`, `emailChangeToken`, `verificationToken`, `fileToken` de **`users` et `agent_users`** (`migrate.go:338-357`) |
| `adminAuthToken`, `adminPasswordResetToken`, `adminFileToken` | `authToken`, `passwordResetToken`, `fileToken` de **`_superusers`** (`migrate.go:86-109`) |
| — | `_superusers.verificationToken/emailChangeToken` : secrets PPBase conservés (0.22 n'en a pas) |
| `meta.verificationTemplate`, `resetPasswordTemplate`, `confirmEmailChangeTemplate` | mêmes options de `users`/`agent_users`, `{ACTION_URL}` remplacé par `actionUrl` (`migrate.go:441-463`) |

Secret vide ou durée nulle : la valeur cible est gardée (`zeroFallback`,
`migrate.go:904-912`). Les autres options PPBase (`passwordAuth`, `oauth2`,
`mfa`, `otp`, `authAlert`…) ne sont pas touchées. Les ID des collections auth
doivent être ceux de l'export (le claim `collectionId` les désigne).

### `_params.settings` → `_params.settings` PPBase

Document par défaut de PPBase (`ppbase/api/settings.py:37-102`) recouvert par :
`meta.appName`, `meta.appUrl` → `meta.appURL` (un seul `/` final retiré,
`migrate.go:193`), `meta.hideControls`, `meta.senderName`,
`meta.senderAddress`, `logs.maxDays`, `logs.minLevel`, `logs.logIp` →
`logs.logIP`, et `smtp.*` (`enabled, host, port, username, password,
authMethod, tls, localName`) (`migrate.go:192-209`). ID et dates de la ligne
source conservés. Les clés propres à PPBase (`batch`, `rateLimits`,
`trustedProxy`, `logs.logAuthId`) gardent leurs valeurs PPBase.

## 3. Non importé, refusé, et impact client

| Élément | Décision | Impact client / exploitation |
| --- | --- | --- |
| `s3`, `backups.s3` | non importés ; `s3.enabled=true` **bloque** | PPBase applique `_params.settings.s3` à son stockage au démarrage (`ppbase/app.py:164-171`) : l'importer ferait lire/écrire le bucket de production. Backup BUMA : S3 désactivé, champs vides (D-0021) : aucun impact. |
| `backups.cron`, `cronMaxKeep` | non importés (avertissement) | Le cron PPBase lance ses propres sauvegardes natives (`ppbase/backup/automation.py:144-172`), format différent du ZIP `pb_data`. Le backup BUMA a un cron : la politique de sauvegarde PostgreSQL/PPBase est à définir au runbook (WS-F). |
| fournisseurs OAuth2 (25), `_externalAuths` | non importés ; un fournisseur activé ou une ligne `_externalAuths` **bloque** | Backup BUMA : aucun identifiant client, 0 ligne : aucun impact. |
| `emailAuth` | ignoré | Réglage obsolète, ignoré par PocketBase 0.22 lui-même. |
| `logs.db` | jamais importé | Journaux de requêtes 0.22 non migrés (PocketBase 0.23 supprime aussi `logs.db`, `migrate.go:62-65`). |
| Paramètres chiffrés | **bloquent** (`settings_not_plain_json`) | Aucune clé de chiffrement n'est acceptée. Backup BUMA : JSON en clair. |
| Clé `_params` inconnue, colonne `_admins` inconnue, valeur non représentable, collection auth cible différente | **bloquent** | Jamais de réparation ni de troncature. |

Effets runtime PPBase à connaître (hors import) : `logs.*` est stocké mais le
journal de requêtes PPBase ne l'applique pas ; l'envoi des e-mails utilise
`PPBASE_SMTP_HOST/PORT/USERNAME/PASSWORD/FROM` (`ppbase/config.py:87-91`,
`services/mail_service.py:15-30`), pas `_params.settings.smtp` (seul l'e-mail de
test l'utilise) ; sans SMTP configuré, PPBase écrit le corps des e-mails dans
ses logs ; les modèles importés sont stockés mais le service mail PPBase
utilise ses textes intégrés ; `GET /api/settings` PPBase renvoie
`smtp.password` en clair (`api/settings.py:365-372`) alors que 0.22 masque les
secrets (`models/settings/settings.go:279-334`).

## 4. Continuité : ce qui marche et ce qui ne marche pas

- **Sessions** : un jeton émis par PocketBase 0.22.34 avant l'import (admin,
  `users`, `agent_users`) est accepté après l'import : route superuser, lecture
  de records protégés, refresh, probes BUMA `/isConnected` et
  `/master/user/health`. Mots de passe d'origine et mots de passe changés dans
  PocketBase acceptés. Durées de session reprises (PPBase avait 1 j / 5 j).
- **Jetons refusés comme PocketBase** : altéré, expiré, jeton `users` sur le
  refresh `agent_users` (403), jeton record sur route admin (401), anonyme.
- **Jetons à usage unique non repris** : les secrets sont importés, mais PPBase
  exige `type=verification|passwordReset|emailChange`
  (`record_auth_service.py:622,727,879`) et `for=file` (`file_tokens.py:122`) ;
  ceux de 0.22 ont `type=authRecord`/`admin`. Un lien de vérification (7 j par
  défaut), de reset (30 min) ou de changement d'e-mail (30 min) envoyé avant la
  bascule est refusé (400) ; le reset admin 0.22 n'a pas de route PPBase (405).
  L'utilisateur redemande l'e-mail. La source gelée n'appelle aucun de ces
  flux ; aucun champ fichier protégé dans les 114 définitions (jetons fichier
  sans usage BUMA).
- **Nouveaux jetons** : après login/refresh, PPBase émet `type=auth` avec
  `refreshable` (0.22 émettait `authRecord`) ; accepté par PPBase et par
  l'adaptateur BUMA (C3). Un client qui lirait le claim `type` verrait la
  différence ; comportement C3 préexistant, non modifié ici.

## 5. Divergences PPBase préexistantes mesurées (non causées par l'import)

| Cas | PocketBase 0.22.34 | PPBase | Changement PPBase proposé (non fait) |
| --- | --- | --- | --- |
| jeton admin sur `POST /api/collections/users/auth-refresh` | 401 (`apis/middlewares.go:97-100`) | 403 | `ppbase/api/record_auth.py:1122-1126` : répondre 401 « The request requires valid record authorization token to be set. » pour `type=admin` hors `_superusers` |
| jeton forgé dont `collectionId` désigne une collection **base** | anonyme : 404/401 (`daos/record.go:427-434`) | 500 (`column "token_key" does not exist`) | `ppbase/api/deps.py:112-127` : `if coll.type != "auth": return None` avant le SELECT ; idem `ppbase/services/file_tokens.py:162-172` et `:81-91` (current : `v0.40.6 core/record_query.go:511-513`) |
| message 403 de collection | « from users collection » (collection du jeton) | collection de la route | `record_auth.py:1117-1121` : nom de la collection du jeton |
| enveloppe d'erreur native | `{code, message, data}` | `{status, message, data}` | hors WS-C (relevé, déjà mesuré en C10 pour les routes BUMA) |

## 6. API d'intégration (vague 3)

```python
async with engine.begin() as conn:            # base jetable buma_c11_rehearsal
    count = await load_isolated(conn, staged, definitions)
    auth = await import_auth(conn, staged, definitions)   # même transaction
```

- `import_auth(connection, data, definitions, *, fail_after=None)` : relit le
  manifeste et l'empreinte de `data.db` comme `load_isolated`, lit le snapshot
  par `scripts.c11_import.snapshot`, verrouille `_superusers`/`_params`
  (ACCESS EXCLUSIVE) et les collections auth (FOR UPDATE) **avant** les
  contrôles, exige `_buma_c11_rowids` (records importés), refuse une cible non
  vide, relit tout ce qu'il écrit. Retour : rapport agrégé (`public_report`),
  sans secret : présent, importé, longueur.
- `diagnose_auth(db, definitions)` : diagnostic lecture seule (structure,
  validité, blocages) ; CLI `python -m scripts.c11_auth_import --data DIR
  --schema EXPORT --report NEW.json`.
- `scripts.c11_auth_runtime.PocketBaseServer` et `pocketbase_tokens(...)` :
  démarrer un PocketBase jetable et obtenir des jetons par HTTP (répétition
  complète).
- Codes : `superusers_target_not_empty`, `settings_target_not_empty`,
  `records_import_required`, `target_auth_collections_mismatch`,
  `target_auth_collection_id_mismatch`, `settings_not_plain_json`,
  `settings_missing`, `unknown_param_keys`, `external_auths_present`,
  `s3_storage_enabled_requires_decision`,
  `oauth2_provider_enabled_requires_decision`, `invalid_token_config`,
  `invalid_settings_value`, `unmapped_admin_columns`,
  `admin_value_not_representable`, `isolated_rehearsal_database_required`.

## 7. Preuves

- `tests/test_c11_auth_import.py` : correspondance, `zeroFallback`, refus,
  rapports sans valeur, et vérification par le vrai `get_optional_auth` PPBase
  de jetons au format 0.22 : refusés avec les secrets PPBase d'origine,
  acceptés après fusion des options importées, refusés altérés, expirés, de
  mauvaise collection ou signés avec le mauvais secret.
- `docs/c11-auth-runtime-report.json` : binaire officiel 0.22.34, données
  synthétiques, PostgreSQL 16 jetable ; claims et algorithme relevés sur les
  jetons réellement émis, formule de clé vérifiée pour les 9 types, trois
  rollbacks dans la transaction de `load_isolated`, refus sans records, second
  import bloqué par le verrou puis refusé, 26 sondes comparées PocketBase /
  PPBase, 10 connexions, 7 sondes BUMA, 4 jetons à usage unique.
- Backup réel (lecture seule, `-I`, structure seulement) : 1 admin aux 8
  colonnes 0.22, `_params` = `settings` en JSON clair, 8 configurations de
  jetons présentes et dans les limites PocketBase, 0 `_externalAuths`, aucun
  identifiant OAuth, S3 vide, cron de sauvegarde présent : **aucun blocage**,
  un avertissement `backup_schedule_not_imported`. Rapport privé dans
  `backups/c11-session-20261024/ws-c/`.

## 8. Limites

- Preuve synthétique : l'import auth du vrai backup reste à exécuter par la
  répétition réelle (WS-G), dans la même transaction que `load_isolated`.
- La porte de version 0.22.34 appartient à WS-D : le banc l'écarte
  explicitement (`version_gate.waived_blockers`), sans autre blocage toléré.
- Le manifeste lu est celui de `stage_snapshot` actuel (`diagnostic`,
  `data_db_sha256`) ; à réaligner si WS-B change son format.
- Non testé ici : SDK JS/Dart réels, desktop, realtime avec un jeton 0.22
  (même vérification `get_optional_auth`, `ppbase/api/realtime.py:72,144`).
