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
| record (session) | `exp, id, type=authRecord, collectionId` | `record.tokenKey + recordAuthToken.secret` | `tokens/record.go:13-27` |
| record (vérification) | idem `+ email` | `tokenKey + recordVerificationToken.secret` | `tokens/record.go:30-45` |
| record (reset) | idem `+ email` | `tokenKey + recordPasswordResetToken.secret` | `tokens/record.go:48-63` |
| record (changement d'email) | idem `+ email, newEmail` | `tokenKey + recordEmailChangeToken.secret` | `tokens/record.go:66-78` |
| record (fichier) | `exp, id, type=authRecord, collectionId` | `tokenKey + recordFileToken.secret` | `tokens/record.go:81-95` |

`type=authRecord` est donc bien le **claim JWT** des sessions record en 0.22
(`tokens/tokens.go:4-7`), pas seulement un contexte : le banc le relève sur les
jetons réellement émis (`tokens_issued_by_pocketbase`). Références `v0.22.34` :
`tools/security/jwt.go:45-58` (`NewJWT` : `exp` puis claims, HS256), `:27-43`
(`ParseJWT` n'accepte que HS256), `:10-25` (`exp/iat/nbf` contrôlés) ;
`apis/middlewares.go:229-249` (`LoadAuthContext` ne charge que `type=admin`
ou `type=authRecord`, avec le secret du type) ; `daos/admin.go:52-78` et
`daos/record.go:414-444` (compte trouvé par `id`/`collectionId`, collection
auth exigée `:432`, puis vérification avec `tokenKey + secret`). Les secrets
sont **globaux** dans `_params.settings` (`models/settings/settings.go:24-70`),
JSON en clair ou chiffré si `--encryptionEnv` (`daos/settings.go:12-56`).

PPBase vérifie la même formule, par collection, dans `ppbase/api/deps.py` :
`type=admin` `:80-101` (`_superusers.token_key + options.authToken.secret` de
`_superusers` `:90`, `jwt.decode` HS256 avec `exp` `:94`) ; `type` `auth` ou
`authRecord` `:104-150` (`collectionId`, `token_key` du record +
`options.authToken.secret` de la collection `:136`, `jwt.decode` `:138`). Le
refresh accepte aussi `authRecord` (`ppbase/services/record_auth_service.py:533-537`)
mais réémet `type=auth` avec `refreshable` et `iat`
(`ppbase/services/auth_service.py:410`, `:476-484`) ; le refresh admin réémet
`type=admin` (`admin_service.py:182-198`, `auth_service.py:426-453`).

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

La migration officielle insère `id, verified=true, email, passwordHash→password,
tokenKey, created, updated` puis supprime `_admins` (`migrate.go:114-127`).
`verified` n'a pas de colonne dans `_superusers` PPBase
(`ppbase/db/system_tables.py:96-132`) : rien à porter. `avatar` est conservé
parce que PPBase expose encore l'API `/api/admins` de 0.22. `lastResetSentAt`
ne sert en 0.22 qu'au délai de 120 s de `/api/admins/request-password-reset`
(`forms/admin_password_reset_request.go:73-77`), route absente de PPBase ;
l'archive garde la valeur pour l'activer si cette route est ajoutée (§4).

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
| `meta.verificationTemplate`, `resetPasswordTemplate`, `confirmEmailChangeTemplate` | mêmes options de `users`/`agent_users` : `subject` et `body`, `{ACTION_URL}` remplacé par `actionUrl` (`migrate.go:443-462`) |

Secret vide ou durée nulle : la valeur cible est gardée (`zeroFallback`,
`migrate.go:904-912`). Le flag `hidden` des modèles 0.22 n'est pas porté, comme
dans la migration officielle (seuls `subject` et `body`) ; il vaut `false`
pour les trois modèles du backup réel. Les autres options PPBase
(`passwordAuth`, `oauth2`, `mfa`, `otp`, `authAlert`…) ne sont pas touchées.
Les ID des collections auth doivent être ceux de l'export (le claim
`collectionId` les désigne).

### `_params.settings` → `_params.settings` PPBase

Document par défaut de PPBase (`ppbase/api/settings.py:37-102`) recouvert par :
`meta.appName`, `meta.appUrl` → `meta.appURL` (un seul `/` final retiré,
`migrate.go:193`), `meta.hideControls`, `meta.senderName`,
`meta.senderAddress`, `logs.maxDays`, `logs.minLevel`, `logs.logIp` →
`logs.logIP`, et `smtp.*` (`enabled, host, port, username, password,
authMethod, tls, localName`) (`migrate.go:192-209`). ID et dates de la ligne
source conservés. Les clés propres à PPBase (`batch`, `rateLimits`,
`rateLimiting`, `trustedProxy`, `logs.logAuthId`) gardent leurs valeurs PPBase.

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
| Chaîne écrite contenant NUL ou un surrogate isolé (champs admin, `lastResetSentAt`, ID de la ligne, réglages repris, secrets, modèles) | **bloque** (`nul_in_postgresql_text`, `text_not_utf8_encodable`) | PostgreSQL `text`/`jsonb` refuse NUL ; code explicite au lieu d'une erreur brute du pilote. Backup BUMA : aucune. Une chaîne non écrite (S3, OAuth…) n'est pas contrôlée. |

**Exposition des secrets importés aux superusers (constat PPBase,
indépendant de l'import mais aggravé par lui)** : PPBase renvoie en clair à
tout superuser les 8 secrets de jetons importés dans
`GET /api/collections`, `/api/collections/{users,agent_users,_superusers}`
(`options.*Token.secret`, `ppbase/models/collection.py:207-233` copie
`options` sans masquage), `smtp.password` dans `GET /api/settings`
(`ppbase/api/settings.py:364-372`) et dans la réponse de `PATCH /api/settings`,
qui renvoie le document fusionné (`settings.py:463`, `return merged`) avec, en
plus, toute clé de jeton 0.22 que l'admin vient d'envoyer. PocketBase 0.22.34
ne les sert jamais : `GET` et `PATCH /api/settings` renvoient `RedactClone`,
secrets non vides remplacés par `******` (`apis/settings.go:29-30` et
`:75-80`, `models/settings/settings.go:281-334`), et ses collections ne portent
aucun secret. La version actuelle les vide aussi
(`v0.40.6 core/collection_model.go:549-584`, `core/settings_model.go:331-345`).
Mesuré par le banc (`comparison.secret_exposure_to_superusers`) : 0 exposition
sur PocketBase ; sur PPBase, 5 `GET` sur 5 exposent, et les 2 réponses `PATCH`
renvoient le secret envoyé et le mot de passe SMTP en clair (PocketBase : 8
configurations sur 8 et le mot de passe SMTP masqués). Ce n'est pas un gain de
privilège (un admin 0.22 peut télécharger un backup), mais la surface s'élargit
(réponses API, dashboard, exports, journaux de proxy) et ces secrets restent
valables pour le PocketBase du retour arrière. Changement PPBase proposé, non
fait : vider `secret` des cinq configurations de jetons et
`oauth2.providers[].clientSecret` dans `CollectionResponse.from_record`
(`collection.py:221`), et servir dans `GET` **et** `PATCH /api/settings` une
copie où `smtp.password`, `s3.secret`, `backups.s3.secret` et les secrets de
jetons non vides valent `******` (forme 0.22.34, visible des clients BUMA) ;
vérifier que le dashboard PPBase ne renvoie pas la valeur masquée lors d'un
enregistrement.

### Révocation des sessions reprises (mesurée)

Après l'import, les secrets de session vivent dans
`_collections.options.authToken.secret` de `users`, `agent_users` et
`_superusers`, relus à chaque requête (`ppbase/api/deps.py:85-90`,
`:130-136` ; aucun cache, confirmé au banc). Mêmes appels sur PocketBase
0.22.34 (redémarré sur sa source) et sur PPBase après l'import
(`comparison.session_revocation*` du rapport) :

| Voie | PocketBase 0.22.34 | PPBase après l'import |
| --- | --- | --- |
| Par compte : un admin change le mot de passe (`PATCH /api/collections/users/records/{id}`, `PATCH /api/admins/{id}`) | 200 ; ancien jeton 401 ; nouveau mot de passe 200 (`forms/record_upsert.go:682-685` → `models/record.go:942-962`, sans `oldPassword` pour un admin `:544-548` ; `models/admin.go:45-61`) | **identique** (`record_service.py:1039-1044`, `admin_service.py:129-135`) |
| Globale, celle de 0.22 : `PATCH /api/settings {"recordAuthToken" ou "adminAuthToken": {"secret": …}}`, c'est-à-dire ce qu'envoie « Invalidate all previously issued tokens » du dashboard (`ui/src/components/settings/TokenField.svelte:16-29`, `PageTokenOptions.svelte:50-66`) | 200 ; refresh `users` et `agent_users` 401, record protégé 404, admin 401 (`apis/settings.go:48-85`, `forms/settings_upsert.go:56-73`, secret relu par `apis/middlewares.go:233-248`) | **200 sans effet** : la clé est fusionnée dans `_params.settings` (`ppbase/api/settings.py:442-443`), que rien ne lit (aucune occurrence de `recordAuthToken` ni `adminAuthToken` dans `ppbase/`) ; les 4 sondes restent à 200 |
| Par collection : `PATCH /api/collections/{nom}` avec `authToken.secret`, à plat (forme actuelle, `v0.40.6 core/collection_model_auth_options.go:139`) ou dans `options` (modèle PPBase, `ppbase/models/collection.py:101-117`) | sans objet : pas d'options de jeton par collection en 0.22 | **400 pour les trois** : `users`, `agent_users` « Identity field 'username' must have a unique index. » (`collection_service.py:90-119`, appelée `:650-655`, refuse toute modification de ces collections : `username` est non unique par les migrations 1789236841 et 1789236843 et sert d'identité par 1789236844) ; `_superusers` « Cannot modify a system collection. » (`collection_service.py:550-551`, alors que `v0.40.6 core/collection_validate.go:96-156` n'interdit que le drapeau, le nom, les champs et les règles système) ; sessions inchangées. Le dashboard PPBase n'a pas d'écran d'options de jetons (aucune occurrence de `authToken` dans `admin-ui/src`). |
| Urgence : rotation SQL du §7 | — | effet immédiat sans redémarrage : jetons 0.22 et jetons émis par PPBase refusés (refresh et `/api/admins` 401, `/isConnected` 400 `{"message":"false"}`) ; mots de passe d'origine et nouvelles sessions acceptés |
| Flux realtime déjà ouvert pendant la révocation | non coupé : l'événement protégé arrive encore ; le POST suivant du SDK avec le jeton révoqué reçoit 403 (`apis/realtime.go:187-191`) | identique après la rotation SQL |

**Impact client et exploitation** : un admin qui applique la procédure 0.22
(dashboard ou API) reçoit 200 et croit les sessions révoquées ; elles restent
valables jusqu'à leur `exp` (durée importée de `recordAuthToken` et
`adminAuthToken`, 14 j par défaut en 0.22). Aucune voie API ni dashboard PPBase
ne fait tourner un secret de session : la rotation SQL du §7 est la seule
révocation globale tant que PPBase n'est pas corrigé, et elle doit figurer au
runbook (WS-F). La révocation par compte fonctionne comme en 0.22. La source
gelée n'appelle ni `/api/settings` ni `/api/admins` (grep de `src/`, hors
déclarations de types) ; l'impact concerne l'exploitation, pas les clients
BUMA. Changements PPBase proposés, non faits : dans `update_settings`
(`ppbase/api/settings.py:375-463`), avant `_deep_merge` (`:442`), retirer du
corps les 8 clés de jetons 0.22, les valider comme
`models/settings/settings.go:379-384` (secret 30 à 300 caractères, durée 5 à
63 072 000 s) et les écrire dans la même transaction dans
`_collections.options` avec la correspondance de l'import (`record*` vers
chaque collection auth sauf `_superusers`, `v0.23.0 migrate.go:338-357` ;
`admin*` vers `_superusers`, `:86-109`), sans les stocker dans
`_params.settings`, puis répondre la forme `RedactClone` ; à défaut, les
refuser en 400 au lieu d'un 200 silencieux (il ne resterait alors que la voie
SQL). Indépendamment : laisser modifier les options d'une collection système
auth comme la version actuelle (`collection_service.py:550-551`).

Autres effets runtime PPBase (hors import) : `logs.*` est stocké mais le
journal de requêtes PPBase ne l'applique pas ; l'envoi des e-mails utilise
`PPBASE_SMTP_HOST/PORT/USERNAME/PASSWORD/FROM` (`ppbase/config.py:87-91`,
`services/mail_service.py:15-30`), pas `_params.settings.smtp` (seul l'e-mail de
test l'utilise) ; sans SMTP configuré, PPBase écrit le corps des e-mails dans
ses logs ; les modèles importés sont stockés mais le service mail PPBase
utilise ses textes intégrés. Le moteur SQLAlchemy de PPBase n'a pas
`hide_parameters` (`ppbase/db/engine.py:39-46`) : ses erreurs SQL journalisent
les valeurs liées (observé sur le banc : un ID de record).

## 4. Continuité : ce qui marche et ce qui ne marche pas

- **Sessions** : un jeton émis par PocketBase 0.22.34 avant l'import (admin,
  `users`, `agent_users`) est accepté après l'import : route superuser, lecture
  de records protégés, refresh, probes BUMA `/isConnected` et
  `/master/user/health`, avec les mêmes clés de réponse et d'entité. Mots de
  passe d'origine et mots de passe changés dans PocketBase acceptés. Durées de
  session reprises (PPBase avait 1 j / 5 j).
- **Invalidations faites par PocketBase conservées** : un reset de mot de passe
  ou un changement d'e-mail renouvelle `tokenKey` (`models/record.go:942-962`,
  `forms/record_email_change_confirm.go:129-133`, `models/admin.go:45-67`).
  Les jetons émis avant ces renouvellements sont refusés par les deux serveurs
  avec le même statut (401 refresh et `/api/admins`, 404 record protégé) ; les
  jetons émis après sont acceptés.
- **Jetons refusés comme PocketBase** : altéré, expiré, jeton `users` sur le
  refresh `agent_users` (403), jeton record sur route admin (401), anonyme.
- **Realtime** : un client abonné avec un jeton 0.22 reçoit les événements
  protégés (l'anonyme non), comme 0.22.34. Le SDK JS 0.21 ne POSTe
  `/api/realtime` que pour le premier écouteur d'un nouveau topic
  (`js-sdk v0.21.5 src/services/RealtimeService.ts:85-94`) et, à
  `unsubscribe()`, seulement s'il reste d'autres topics (`:138-143`) ; chaque
  POST porte tous les topics restants et le jeton courant (`:258-283`) ; la
  désinscription du dernier topic ferme seulement le flux SSE, sans requête ni
  erreur (`:138-140`). Le banc envoie exactement ces requêtes (topic record
  plus `buma_supplier/*`, celui de `subscribe("*")`, `RecordService.ts:130-134`).
  **Divergence causée par le mélange de jetons** : après un refresh sur PPBase
  (jeton `type=auth`), sur la même connexion SSE, s'abonner à un nouveau topic
  et se désinscrire d'un topic quand un autre reste reçoivent
  `403 « The current and the previous request authorization don't match. »`
  (PocketBase 0.22.34 : 204), et le nouveau topic ne reçoit aucun événement.
  Mesuré aussi : le topic déjà actif continue à recevoir les événements après
  ce 403, sur les deux serveurs. Cause : `ppbase/api/realtime.py:45-52` met le
  `type` dans la clé d'identité, comparée `:167-175` ; PocketBase ne compare
  que l'ID (`v0.22.34 apis/realtime.go:187-191`, `extractAuthIdFromGetter`
  `:539-551`), ID et collection en version actuelle (`v0.40.6
  apis/realtime.go:218-221`, `isSameAuth` `:843-853`). Impact : pour tout
  client qui rafraîchit son jeton 0.22 pendant une connexion realtime,
  `subscribe()` d'un nouveau topic et `unsubscribe()` d'un topic parmi
  plusieurs rejettent leur promesse (403) jusqu'à la reconnexion SSE (nouveau
  `clientId`) ; côté serveur, le topic refusé n'est pas ajouté (mesuré) et un
  topic dont la désinscription est refusée reste abonné (lecture de code : le
  403 précède `replace_subscriptions`, `realtime.py:168-172` et `:235-238`).
  Préexistant, sans lien avec l'import : un client
  d'abord anonyme qui s'authentifie puis s'abonne à un nouveau topic sur la
  même connexion reçoit 403 sur PPBase, 204 sur PocketBase. Contrôles : jeton
  émis par PPBase 204 puis 204 ; jeton admin 0.22 rafraîchi (`type=admin`) 204
  puis 204 ; passage authentifié → anonyme refusé (403) des deux côtés.
  Changement PPBase proposé, non fait : clé `collectionId:id` sans `type`
  (`realtime.py:45-52`) et refus seulement si une identité était déjà fixée
  (`session.auth_key is not None and session.auth_key != current`), identité
  mise à jour à chaque souscription acceptée (`:167-175`), comme
  `v0.40.6 apis/realtime.go:218-231`. Repli côté BUMA si l'orchestrateur
  refuse le changement PPBase : remplacer `_build_realtime_auth_key` depuis
  `app/auth/realtime.py` (qui patche déjà `ppbase.services.realtime_service`) ;
  cela corrige le mélange `authRecord`/`auth`, pas le passage anonyme →
  authentifié.
- **Type des nouveaux jetons** : après login/refresh, PPBase émet `type=auth`
  avec `refreshable` et `iat` (0.22 : `authRecord`). Les clients qui lisent le
  claim voient la différence : `pb.authStore.isAuthRecord` du SDK JS 0.21 vaut
  `false` après le premier refresh PPBase (`js-sdk v0.21.5
  src/stores/BaseAuthStore.ts:51-53`) ; `isAdmin` reste vrai pour les admins
  (`:44-46`, PPBase garde `type=admin`) ; `isValid` (claim `exp`) et le SDK Dart
  (`dart-sdk v0.18.1 lib/src/auth_store.dart:34-49`, `exp` seulement) ne
  changent pas. Sources des SDK lues sur leurs tags GitHub officiels ; quelle
  version utilise chaque client BUMA n'est pas établi ici.
  Comportement C3 préexistant ; la même affirmation inexacte (« `authRecord`
  est le contexte PocketBase, pas le type du claim JWT ») est à corriger par
  l'orchestrateur à quatre endroits : `docs/decisions-and-divergences.md:71-74`,
  `docs/contracts/authentication.md:63-66`, `docs/progress/ledger.md:385-390`
  et la docstring `app/auth/context.py:25-27`. Preuve : `tokens/tokens.go:4-7`,
  `tokens_issued_by_pocketbase.users_auth.type` et
  `comparison.token_type_after_login_or_refresh` du rapport.
- **Jetons à usage unique non repris** : les secrets sont importés, mais PPBase
  exige `type=verification|passwordReset|emailChange`
  (`record_auth_service.py:622` et `:636`, `:727` et `:756`, `:879` et `:892`)
  et `for=file` (`file_tokens.py:122`) ; ceux de 0.22 ont `type=authRecord`
  ou `admin`, et PocketBase 0.22.34 ne vérifie que la signature avec le secret
  du type (`forms/record_verification_confirm.go:58-60`,
  `record_password_reset_confirm.go:59-62`, `record_email_change_confirm.go:103-105`,
  `admin_password_reset_confirm.go:56`). Mesuré : PocketBase 204, PPBase 400
  pour vérification, reset et changement d'e-mail. Le reset admin 0.22
  (`POST /api/admins/request-password-reset` et `confirm-password-reset`,
  `apis/admin.go:21-22`) n'existe pas dans PPBase : la requête tombe sur
  `/api/admins/{admin_id}` (`ppbase/api/admins.py:252-299`) et reçoit 405,
  **divergence de route à décider**. Impact : un lien de vérification (7 j par
  défaut), de reset (30 min) ou de changement d'e-mail (30 min) envoyé avant la
  bascule est refusé ; l'utilisateur redemande l'e-mail ; un admin ne peut
  plus demander de reset par e-mail. La source gelée n'appelle aucun de ces
  flux ; aucun champ fichier protégé dans les 114 définitions. Changements
  PPBase proposés, non faits : accepter aussi `type=authRecord` dans les trois
  confirmations quand la signature est vérifiée avec `token_key` + secret du
  type (les deux contrôles de chaque ligne citée) ; ajouter les deux routes
  admin 0.22 dans `ppbase/api/admins.py` avant `/{admin_id}`, signées avec
  `_superusers.passwordResetToken.secret` (= `adminPasswordResetToken`
  importé), délai de 120 s sur `last_reset_sent_at`, que l'import activerait
  alors depuis l'archive ; facultatif : accepter les jetons fichier 0.22 sans
  claim `for` quand `type` vaut `admin` ou `authRecord`.

## 5. Divergences PPBase préexistantes mesurées (non causées par l'import)

| Cas | PocketBase 0.22.34 | PPBase | Changement PPBase proposé (non fait) |
| --- | --- | --- | --- |
| jeton admin sur `POST /api/collections/users/auth-refresh` | 401 (`apis/middlewares.go:97-100`) | 403 | `ppbase/api/record_auth.py:1122-1126` : 401 « The request requires valid record authorization token to be set. » pour `type=admin` hors `_superusers` |
| jeton forgé dont `collectionId` désigne une collection **base** | anonyme : 404/401 (`daos/record.go:432-434`) | 500 (`column "token_key" does not exist`) | `ppbase/api/deps.py:112-127` : `if coll.type != "auth": return None` avant le SELECT ; idem `ppbase/services/file_tokens.py:162-172` et `:81-91` (current : `v0.40.6 core/record_query.go:511-513`) |
| libellés 401/403 (12 sondes, statuts identiques) | « …requires valid record authorization token… » (`middlewares.go:99`) ; « …requires valid admin authorization token… » (`:119`) ; 403 avec la collection **du jeton** (`:104`) | « …requires valid auth token… » (`record_auth.py:1147,1157,1163`) ×5 ; « …requires admin authorization token… » (`deps.py:170,191`, `admins.py:196`) ×6 ; 403 avec la collection **de la route** (`record_auth.py:1117-1121`) ×1 | reprendre les trois libellés 0.22.34 ; liste exacte : `comparison.message_wording_differences` du rapport |
| enveloppe d'erreur native | `{code, message, data}` (`apis/api_error.go:12-15`) | `{status, message, data}` (forme actuelle, `v0.40.6 tools/router/error.go:36-42`) | **non consignée** dans les décisions ni le ledger (C10 a mesuré `{code,…}` sur les routes BUMA, où l'adaptateur la reproduit) : à consigner ; impact sur les clients qui lisent `response.code` d'une erreur native (`/api/admins`, `/api/collections/*`, `/api/realtime`) |
| realtime : anonyme puis authentifié, nouveau topic sur le même client | 204 (`apis/realtime.go:187-191`) | 403 | voir §4 (`realtime.py:167-175`) |
| secrets dans `/api/collections*` et dans les réponses `GET`/`PATCH /api/settings` | jamais servis (`RedactClone`) | servis aux superusers | voir §3 |
| forme de `GET /api/settings` (hors secrets) | `meta.appUrl`, `logs.logIp`, les 8 configurations de jetons masquées, 25 fournisseurs OAuth2, `emailAuth` (`models/settings/settings.go:24-70`, `apis/settings.go:29-45`) | forme actuelle : `meta.appURL`, `logs.logIP`, aucune configuration de jeton (elles vivent dans les collections), ni fournisseurs ni `emailAuth`, plus `batch`, `rateLimits`, `trustedProxy` (`ppbase/api/settings.py:37-102`, `:364-372`) ; mesuré : 0 configuration contre 8 | aucun, sauf si l'orchestrateur veut la forme 0.22 ; un `PATCH` au format 0.22 (par exemple `recordAuthToken.duration`) est stocké dans `_params` sans effet sur les collections (voir §3). Impact limité aux clients admin de `/api/settings` : aucun appelant BUMA connu (grep de la source gelée et des contrats) |
| modification d'une collection système auth (`_superusers`) | sans objet en 0.22 (les admins ne sont pas une collection) ; la version actuelle accepte ses options (`v0.40.6 core/collection_validate.go:96-156`) | 400 « Cannot modify a system collection. » pour tout `PATCH` (`collection_service.py:550-551`) | n'interdire que le drapeau, le nom, les champs et les règles système, comme la version actuelle |

Les autres libellés et statuts des sondes sont identiques (rapport). Autre
écart vu en écrivant le banc (un `PATCH` répété avec la même valeur n'a
livré aucun événement), non comparé à PocketBase : PPBase ignore les valeurs
inchangées puis n'écrit rien et ne diffuse rien quand il ne reste que
`updated` (`ppbase/services/record_service.py:1229-1231`, `:1287-1290`), alors
que PocketBase 0.22 enregistre toujours le record (`forms/record_upsert.go:809`,
`daos/base.go:266-275` ; lecture de code). Le
`PATCH /api/collections/{users,agent_users}` refusé en 400 tient au schéma
BUMA (`username` non unique, décision C3), pas à PPBase, qui valide comme la
version actuelle (`v0.40.6 core/collection_model_auth_options.go:227`).

## 6. API d'intégration (vague 3)

```python
engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
async with engine.begin() as conn:            # base jetable buma_c11_rehearsal
    count = await load_isolated(conn, staged, definitions)
    auth = await import_auth(conn, staged, definitions)   # même transaction
```

- **`hide_parameters=True` est exigé** (WS-B, WS-G) : sans lui, SQLAlchemy
  recopie toutes les valeurs liées dans le message d'une `DBAPIError`
  (`[parameters: …]`) : hash, `tokenKey` et secrets ici, hash et `tokenKey` de
  chaque record auth dans `load_isolated`. `import_auth` refuse une connexion
  dont le moteur ne cache pas les paramètres (`engine_hide_parameters_required`).
  De plus, toute erreur SQLAlchemy pendant l'import devient
  `ImportBlocked("auth_import_database_error")`, levée hors du gestionnaire
  (aucun `__context__` : ni SQL, ni `DETAIL` PostgreSQL « Failing row contains
  … », ni valeur), avec pour seul attribut le `sqlstate` (5 caractères).
- `import_auth(connection, data, definitions, *, fail_after=None)` : relit le
  manifeste et l'empreinte de `data.db` comme `load_isolated`, lit le snapshot
  par `scripts.c11_import.snapshot`, verrouille `_superusers`/`_params`
  (ACCESS EXCLUSIVE), `_buma_c11_rowids` (SHARE) et les collections auth
  (FOR UPDATE) **avant** les contrôles, exige que `_buma_c11_rowids` existe et
  provienne **du même snapshot** (par collection : nombre de lignes et
  empreinte SHA-256 des lignes `rowid:id\n` dans l'ordre des rowids, calculées
  par PostgreSQL et comparées à `diagnostic.collections[].identity_sha256` du
  manifeste), refuse une cible non vide, relit tout ce qu'il écrit. Retour :
  rapport agrégé (`public_report` + `records_snapshot`), sans secret :
  présent, importé, longueur.
- `diagnose_auth(db, definitions)` : diagnostic lecture seule (structure,
  validité, blocages) ; CLI `python -m scripts.c11_auth_import --data DIR
  --schema EXPORT --report NEW.json`.
- `scripts.c11_auth_runtime.PocketBaseServer`, `pocketbase_tokens(...)` et
  `RealtimeClient` : démarrer un PocketBase jetable, obtenir des jetons par
  HTTP, s'abonner au realtime (répétition complète) ;
  `EMERGENCY_SESSION_REVOCATION_SQL` : la rotation d'urgence du §7, telle que
  le banc l'exécute.
- Codes de refus, chacun testé (unitaires sans aucune écriture, avec arrêt
  d'`import_auth` avant tout verrou pour ceux de la source ; banc PostgreSQL
  avec retour exact à l'état initial pour ceux côté base) :
  - préconditions : `engine_hide_parameters_required`,
    `diagnostic_blockers`, `staged_snapshot_changed`,
    `isolated_rehearsal_database_required`, `records_snapshot_unverifiable` ;
  - source (`check_source`) : `missing_internal_table`,
    `source_auth_collections_missing`, `unmapped_admin_columns`,
    `admin_value_not_representable`, `duplicate_admin_email`,
    `duplicate_admin_token_key`, `unmapped_param_columns`, `unknown_param_keys`,
    `external_auths_present`, `settings_missing`, `settings_not_plain_json`,
    `param_value_not_representable`, `invalid_token_config`,
    `invalid_settings_value`, `nul_in_postgresql_text`,
    `text_not_utf8_encodable`, `s3_storage_enabled_requires_decision`,
    `oauth2_provider_enabled_requires_decision` ;
  - cible : `superusers_target_not_empty`, `settings_target_not_empty`,
    `auth_archive_exists`, `records_import_required`,
    `records_snapshot_mismatch`, `target_auth_collections_mismatch`,
    `target_auth_collection_id_mismatch` ;
  - relecture : `superusers_readback_mismatch`,
    `auth_options_readback_mismatch`, `settings_readback_mismatch` ;
  - base : `auth_import_database_error` ; banc : `injected_auth_import_failure`.
  Avertissements, chacun testé (ils ne bloquent pas et sont rapportés sans
  valeur) : `token_config_absent_target_default_kept`,
  `token_config_outside_pocketbase_limits` (la valeur stockée est importée
  telle quelle : PocketBase 0.22 signe et vérifie avec elle, il ne la contrôle
  qu'à l'enregistrement), `backup_schedule_not_imported`,
  `backup_s3_target_not_imported`, `unknown_settings_keys_ignored` (clés
  comptées, jamais nommées).

## 7. Gel des écritures, backup frais et retour arrière

- L'import recopie l'état d'authentification **du snapshot** : `tokenKey`,
  hash et secrets. Tout changement fait dans PocketBase entre le backup et la
  bascule est annulé sur PPBase : mot de passe changé ou réinitialisé,
  e-mail changé, admin supprimé ou ajouté, compte créé. L'ancien mot de
  passe redevient valable et les jetons que PocketBase avait invalidés
  redeviennent acceptés jusqu'à leur `exp` (14 j par défaut pour les
  sessions). Pour WS-F : **le gel des écritures doit commencer avant le
  backup frais** et couvrir, en plus du gel général des données, tout ce qui
  change l'état d'authentification : `confirm-*` (vérification, reset,
  changement d'e-mail), `request-*` (qui écrivent `lastResetSentAt` ou
  `lastVerificationSentAt`), création, `PATCH` et suppression des records
  auth, gestion des admins (`/api/admins*`) et `/api/settings`. Les connexions
  (`auth-with-password`) et les refresh ne changent aucun état en 0.22.34, et
  la source gelée n'a aucun hook d'authentification (seulement les
  déclarations de `src/types/pocketbase.d.ts`) : un jeton émis par PocketBase
  pendant la fenêtre est signé avec le `tokenKey` et le secret du snapshot, et
  PPBase l'accepte après l'import (`tokens/record.go:13-27`,
  `daos/record.go:414-444`, `daos/admin.go:52-78` : vérification sans état
  serveur). Geler aussi connexions et refresh est un choix d'exploitation
  (fenêtre de maintenance), pas une condition de continuité des sessions.
- **Révocation d'urgence après la bascule** (prouvée par le banc,
  `comparison.session_revocation_emergency_sql`) : PPBase n'offre aucune voie
  API ni dashboard pour faire tourner un secret de session (§3). Sur la base
  PPBase, en une transaction :

  ```sql
  BEGIN;
  UPDATE "_collections"
     SET "options" = jsonb_set("options", '{authToken,secret}',
           to_jsonb(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))),
         "updated" = now()
   WHERE "type" = 'auth' AND "name" IN ('users', 'agent_users', '_superusers')
  RETURNING "name", length("options" -> 'authToken' ->> 'secret') AS secret_length;
  -- attendu : une ligne par nom, secret_length = 64 ; sinon ROLLBACK
  COMMIT;
  ```

  Réduire la liste pour ne révoquer que les sessions record (`'users',
  'agent_users'`) ou admin (`'_superusers'`). Un secret aléatoire distinct par
  collection, tiré par `gen_random_uuid()` (`pg_strong_random`, PostgreSQL 13
  ou plus), qui ne quitte jamais la base. Effet immédiat, sans redémarrer
  PPBase : jetons 0.22 et jetons émis par PPBase refusés (401, `/isConnected`
  400) ; mots de passe inchangés, nouvelles connexions acceptées ; les flux
  realtime déjà ouverts reçoivent encore les événements jusqu'à leur
  reconnexion, leur prochain POST est refusé (403), comme après une révocation
  0.22. Pour un seul compte, la voie API suffit : un admin change son mot de
  passe (`PATCH` du record ou de `/api/admins/{id}`), comme en 0.22. Après une
  rotation, PocketBase garde ses anciens secrets : en cas de retour arrière,
  les sessions révoquées sur PPBase redeviennent valables sur PocketBase
  jusqu'à leur `exp`, sauf à révoquer aussi côté PocketBase (Settings → Token
  options).
- Retour arrière vers PocketBase intact (mesuré, `pocketbase_restarted_on_source`) :
  les jetons 0.22 d'avant la bascule restent valides (200) ; les jetons
  record émis par PPBase (`type=auth`, login ou refresh d'un jeton 0.22) sont
  ignorés par 0.22.34 (`apis/middlewares.go:229-249`) : 401 au refresh, record
  protégé 404 ; **les utilisateurs doivent se reconnecter**. Les jetons admin
  émis par PPBase (`type=admin`, même `tokenKey` et même secret importé) sont
  acceptés par PocketBase (200), sauf après une rotation d'urgence de
  `_superusers`. Tout changement de compte fait sur PPBase après la bascule est
  absent de PocketBase.

## 8. Preuves

- `tests/test_c11_auth_import.py` (57 tests) : correspondance,
  `zeroFallback`, chaque refus de la source (dont collection auth absente,
  colonne `_params` inconnue, ID de la ligne `settings` trop long ou avec NUL,
  `created`/`updated` vide, sans fuseau ou invalide : arrêt d'`import_auth`
  après `SELECT current_database()` seulement), chaque avertissement (valeur
  stockée gardée, rapport sans valeur), NUL et surrogate signalés par
  emplacement sans valeur, rapports sans valeur ; base scriptée en mémoire qui
  répond à chaque instruction d'`import_auth` : chemin complet (ordre des
  écritures, valeurs écrites), chaque garde cible ou précondition refusée sans
  aucune écriture, chaque relecture altérée arrêtée avant l'étape suivante,
  erreur pilote convertie sans valeur ni contexte ; vérification par le vrai
  `get_optional_auth` PPBase de jetons au format 0.22 : refusés avec les
  secrets PPBase d'origine, acceptés après fusion des options importées,
  refusés altérés, expirés, de mauvaise collection, signés avec le mauvais
  secret ou après renouvellement du `token_key` stocké.
- `docs/c11-auth-runtime-report.json` : binaire officiel 0.22.34, données
  synthétiques, PostgreSQL 16 jetable ; claims et algorithme relevés sur les
  jetons réellement émis, formule de clé vérifiée pour les 9 types et pour les
  `tokenKey` renouvelés ; trois rollbacks injectés et dix refus côté base
  (dont relectures altérées par trigger, erreur base sans valeur, moteur sans
  `hide_parameters`), chacun revenu exactement à l'état initial ; refus sans
  records ; second import bloqué par le verrou puis refusé ; relecture
  indépendante (comptes, dates des admins et de la ligne `settings`, tous les
  champs `meta`, `logs` et `smtp` repris) ; 33 sondes HTTP comparées, 10
  connexions, 7 sondes BUMA, 4 jetons à usage unique, scénario realtime aux
  requêtes du SDK JS 0.21 (22 résultats), exposition des secrets (`GET` et
  `PATCH`), révocation (14 résultats comparés et 2 réponses
  `PATCH /api/settings` sur les deux serveurs, 6 `PATCH` de collection, rotation
  SQL d'urgence), retour arrière.
- Backup réel (lecture seule, `-I`, structure seulement) : 1 admin aux 8
  colonnes 0.22, `_params` = `settings` en JSON clair, 8 configurations de
  jetons présentes et dans les limites PocketBase, 0 `_externalAuths`, aucun
  identifiant OAuth, S3 vide, cron de sauvegarde présent : **aucun blocage**,
  un avertissement `backup_schedule_not_imported`. Rapport privé dans
  `backups/c11-session-20261024/ws-c/`.

## 9. Limites

- Preuve synthétique : l'import auth du vrai backup reste à exécuter par la
  répétition réelle (WS-G), dans la même transaction que `load_isolated`, avec
  un moteur `hide_parameters=True`.
- **Consignes pour WS-G** : après `import_auth`, le PPBase jetable porte les 8
  secrets de jetons et le mot de passe SMTP de production, servis en clair à
  tout superuser (§3), et accepte les vraies sessions 0.22 encore valides.
  Donc : port loopback seulement ; créer le superuser synthétique **après**
  `import_auth` (avant, l'import refuse : `superusers_target_not_empty`), dans
  la seule base jetable ; ne jamais enregistrer le corps des réponses de
  `/api/collections*` ni de `/api/settings` (`GET` ou `PATCH`), seulement
  statuts et noms de clés ; ne jamais appeler `/api/settings/test/email` (il
  utiliserait les identifiants SMTP de production importés, `settings.py:146-179`
  et `:517-548` : action sur un serveur de production) ni les routes
  `request-*` (sans `PPBASE_SMTP_*`, PPBase écrit le corps de l'e-mail et son
  jeton dans ses journaux, `mail_service.py:25-30` ; avec, il écrirait à de
  vraies adresses) ; lancer PPBase sans `PPBASE_SMTP_*` et avec
  `BUMA_MASTER_ENABLE_INTERNAL_JOBS=false`, comme le banc ; journaux privés
  (0600) ; détruire la base et les journaux à la fin.
- La porte de version 0.22.34 appartient à WS-D : le banc l'écarte
  explicitement (`version_gate.waiver_applied`), sans autre blocage toléré ;
  l'écart disparaît seul quand la porte s'ouvre et ne doit pas être repris par
  la répétition réelle.
- Le manifeste lu est celui de `stage_snapshot` actuel (`diagnostic`,
  `data_db_sha256`, `collections[].identity_sha256`) et `_buma_c11_rowids`
  (`collection_id`, `record_id`, `source_rowid`) ; à réaligner si WS-B change
  ces formats.
- Non testé ici : SDK JS/Dart réels, desktop. Le realtime est sondé au niveau
  HTTP/SSE avec les requêtes du SDK JS 0.21 (lues dans son code), pas avec le
  SDK lui-même.
- Révocation : seule la rotation de `authToken` est prouvée ; le même
  `jsonb_set` vaudrait pour les autres types (`passwordResetToken`,
  `verificationToken`, `emailChangeToken`, `fileToken`), non mesuré. La
  coupure des flux realtime ouverts n'existe sur aucun des deux serveurs.
