# Contrat C11 — comptes, paramètres et continuité des sessions (WS-C, WS-C2)

Chantiers WS-C puis WS-C2 de la session serveur du 2026-10-24. Source gelée
`buma_master_hooks@530018c` ; données produites par PocketBase **0.22.34** ;
cible PPBase `c11/ppbase-preview@22929a0` (`fix/d0019-relation-maxselect` +
`c11/ws-a5` + `c11/ws-a2`, corrections de sessions WS-A2 incluses ; c'est la
version mesurée par le banc, qui vérifie le paquet que le serveur charge).
Décision : D-0027 (comptes, paramètres et sessions), complétée après WS-C2.
Les citations `ppbase/...` renvoient à ce commit.

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
JSON en clair ou chiffré si `--encryptionEnv` (`daos/settings.go:12-56`). Les
jetons à usage (vérification, reset, changement d'e-mail, reset admin) ne sont
contrôlés que par leur signature avec le secret de leur usage
(`forms/record_verification_confirm.go:58-60`,
`record_password_reset_confirm.go:59-62`, `record_email_change_confirm.go:103-105`,
`admin_password_reset_confirm.go:56`).

PPBase vérifie la même formule, par collection, dans `ppbase/api/deps.py` :
`type=admin` `:89-110` (`_superusers.token_key + options.authToken.secret` de
`_superusers` `:94-99`, `jwt.decode` HS256 avec `exp` `:103`) ; `type` `auth` ou
`authRecord` `:112-161` (`collectionId` ; une collection absente ou **non auth**
rend la requête anonyme `:125-130`, comme `v0.22.34 daos/record.go:432-434` ;
`token_key` du record + `options.authToken.secret` de la collection `:145-147`,
`jwt.decode` `:149`). Les jetons à usage 0.22 (`type=authRecord`) sont acceptés
quand leur signature est vérifiée avec `token_key` + secret de l'usage
(`ppbase/services/auth_service.py:533-563`, appelé
`record_auth_service.py:658`, `:780`, `:936`) ; reset admin par
`ppbase/api/admins.py:278-357`. Les jetons de fichier 0.22 sans claim `for`
sont acceptés pour `type=admin|authRecord` (`ppbase/services/file_tokens.py:129-145`).
Le refresh accepte `authRecord` mais réémet `type=auth` avec `refreshable`
(`auth_service.py:463-496`) ; le refresh admin réémet `type=admin`.

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
| `avatar` | `avatar` | copié : PPBase le sérialise dans `/api/admins` comme 0.22 (`admin_service.py:44-55`) |
| `lastResetSentAt` | `last_reset_sent_at` | **importé** : même instant, `""` (la date zéro de 0.22) devient NULL ; texte source aussi archivé |
| rowid SQLite | — | archivé |

La migration officielle insère `id, verified=true, email, passwordHash→password,
tokenKey, created, updated` puis supprime `_admins` (`migrate.go:114-127`).
`verified` n'a pas de colonne dans `_superusers` PPBase
(`ppbase/db/system_tables.py:96-132`) : rien à porter. `avatar` est conservé
parce que PPBase expose encore l'API `/api/admins` de 0.22.

`lastResetSentAt` ne sert en 0.22 qu'au délai de 120 s de
`POST /api/admins/request-password-reset` (`forms/admin_password_reset_request.go:73-76` ;
la réponse reste 204, seul l'e-mail n'est pas envoyé), et il est remis à zéro
par tout nouveau mot de passe (`models/admin.go:45-60`). PPBase sert désormais
cette route avec le même délai sur `last_reset_sent_at`
(`ppbase/services/admin_service.py:27`, `:216-268`) et la même remise à zéro
(`:146-162`) : l'import l'alimente (WS-C2). Sans cela, au plus un e-mail de
reset admin de plus pouvait partir dans les 120 s suivant la bascule. La
migration officielle ne le reprend pas (0.23 n'a plus ce champ), d'où ce choix
propre à PPBase, sans effet sur les autres routes. Une valeur qui n'a pas la
forme PocketBase `AAAA-MM-JJ HH:MM:SS.mmmZ` **bloque**
(`admin_value_not_representable`, champ `lastResetSentAt`) au lieu d'être lue
comme la date zéro, ce que ferait le `cast` tolérant de 0.22. Backup réel
(lecture seule, agrégat) : 1 admin, `lastResetSentAt` vide, donc NULL ; aucun
nouveau blocage.

Archive : table technique `_buma_c11_auth_archive(source_table, record_id,
source_rowid, legacy)`, sans collection publique : `_admins` (rowid,
texte source de `lastResetSentAt`) et `_params` (rowid, clé). Elle n'est pas
mélangée à `_buma_c11_rowids`, dont les lignes désignent des collections de
l'export.

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

Document par défaut de PPBase (`ppbase/api/settings.py:42-107`) recouvert par :
`meta.appName`, `meta.appUrl` → `meta.appURL` (un seul `/` final retiré,
`migrate.go:193`), `meta.hideControls`, `meta.senderName`,
`meta.senderAddress`, `logs.maxDays`, `logs.minLevel`, `logs.logIp` →
`logs.logIP`, et `smtp.*` (`enabled, host, port, username, password,
authMethod, tls, localName`) (`migrate.go:192-209`). ID et dates de la ligne
source conservés. Les clés propres à PPBase (`batch`, `rateLimits`,
`rateLimiting`, `trustedProxy`, `logs.logAuthId`) gardent leurs valeurs PPBase.

`meta.appURL` importé est désormais **la seule base des liens envoyés par
e-mail** (vérification, reset, changement d'e-mail, reset admin :
`ppbase/services/app_url.py:32-51`, utilisé par `mail_service.py:83`, `:106`,
`:129`, `:184`), jamais les en-têtes de la requête, comme
`v0.22.34 mails/admin.go:22-26`. S'il manque ou n'est pas une URL absolue, les
liens partent vers `http://localhost:8090` (`app_url.py:29`) : vérifier sa
valeur après l'import (le banc la relit, `readback.settings_meta_logs_smtp_mapped`).

## 3. Non importé, refusé, et impact client

| Élément | Décision | Impact client / exploitation |
| --- | --- | --- |
| `s3`, `backups.s3` | non importés ; `s3.enabled=true` **bloque** | PPBase applique `_params.settings.s3` à son stockage au démarrage (`ppbase/app.py:179-187`) : l'importer ferait lire/écrire le bucket de production. Backup BUMA : S3 désactivé, champs vides (D-0021) : aucun impact. |
| `backups.cron`, `cronMaxKeep` | non importés (avertissement) | Le cron PPBase lance ses propres sauvegardes natives (`ppbase/backup/automation.py:144-172`), format différent du ZIP `pb_data`. Le backup BUMA a un cron : la politique de sauvegarde PostgreSQL/PPBase est à définir au runbook (WS-F). |
| fournisseurs OAuth2 (25), `_externalAuths` | non importés ; un fournisseur activé ou une ligne `_externalAuths` **bloque** | Backup BUMA : aucun identifiant client, 0 ligne : aucun impact. |
| `emailAuth` | ignoré | Réglage obsolète, ignoré par PocketBase 0.22 lui-même. |
| `logs.db` | jamais importé | Journaux de requêtes 0.22 non migrés (PocketBase 0.23 supprime aussi `logs.db`, `migrate.go:62-65`). |
| Paramètres chiffrés | **bloquent** (`settings_not_plain_json`) | Aucune clé de chiffrement n'est acceptée. Backup BUMA : JSON en clair. |
| Clé `_params` inconnue, colonne `_admins` inconnue, valeur non représentable (dont un `lastResetSentAt` hors de la forme PocketBase), collection auth cible différente | **bloquent** | Jamais de réparation ni de troncature. |
| Chaîne écrite contenant NUL ou un surrogate isolé (champs admin, `lastResetSentAt`, ID de la ligne, réglages repris, secrets, modèles) | **bloque** (`nul_in_postgresql_text`, `text_not_utf8_encodable`) | PostgreSQL `text`/`jsonb` refuse NUL ; code explicite au lieu d'une erreur brute du pilote. Backup BUMA : aucune. Une chaîne non écrite (S3, OAuth…) n'est pas contrôlée. |

### Secrets importés : jamais servis (mesuré)

Les 8 secrets de jetons importés et le mot de passe SMTP ne sont servis à
personne, superusers compris, comme en 0.22.34 (`GET` et `PATCH /api/settings`
renvoient `RedactClone`, secrets non vides remplacés par `******`,
`apis/settings.go:29-30` et `:75-80`, `models/settings/settings.go:281-334` ;
les collections 0.22 ne portent aucun secret) et comme la version actuelle
(`v0.40.6 core/collection_model.go:564-584`). PPBase : collections sans les
cinq secrets de jetons ni `oauth2.providers[].clientSecret`
(`ppbase/services/secret_redaction.py:105`, appliqué
`ppbase/models/collection.py:235`), `GET` et réponse de `PATCH /api/settings`
masquées (`secret_redaction.py:223`, `ppbase/api/settings.py:381` et `:479`) ;
le dashboard retire le masque avant d'enregistrer (relecture WS-A2, test node). Mesuré par le banc
(`comparison.secret_exposure_to_superusers`) : 0 exposition sur les 5 chemins
`GET` (`/api/settings`, `/api/collections?perPage=500`,
`/api/collections/{users,agent_users,_superusers}`) des deux côtés ; les 2
réponses `PATCH /api/settings` ne contiennent ni le secret envoyé ni un secret
importé, mot de passe SMTP et configurations de jetons masqués (PocketBase : 8
sur 8 ; PPBase : celles que la réponse contient, 1 puis 2, toutes masquées) ;
les 6 réponses `PATCH /api/collections/*` ne renvoient pas le secret envoyé.
Les secrets restent dans la base, ses sauvegardes et, potentiellement, dans
les traces d'erreur du pilote (voir plus bas) ; ils restent valables pour le
PocketBase du retour arrière.

Écarts délibérés de PPBase sur les allers-retours (D-0027, test PPBase
`tests/integration/test_secret_redaction.py`) : sur une collection, un secret
absent, vide, `null` ou masqué garde le secret stocké
(`secret_redaction.py:164`), là où `v0.23+` répond 400 ; dans `PATCH
/api/settings`, le masque `******` renvoyé et un `null` à toute profondeur
gardent la valeur stockée (`secret_redaction.py:245`, appelé
`settings.py:404-409`), là où 0.22.34 enregistrerait le masque (son dashboard
le retire lui-même, `ui/src/utils/CommonHelper.js:332-349`) ; `""` efface une
valeur des paramètres. Aucun appelant BUMA connu de ces routes.

### Révocation des sessions reprises (mesurée)

Après l'import, les secrets de session vivent dans
`_collections.options.authToken.secret` de `users`, `agent_users` et
`_superusers`, relus à chaque requête (`ppbase/api/deps.py:94-99`,
`:145-147` ; aucun cache, confirmé au banc). Mêmes appels sur PocketBase
0.22.34 (redémarré sur sa source) et sur PPBase après l'import
(`comparison.session_revocation*` du rapport) :

| Voie | PocketBase 0.22.34 | PPBase après l'import |
| --- | --- | --- |
| Par compte : un admin change le mot de passe (`PATCH /api/collections/users/records/{id}`, `PATCH /api/admins/{id}`) | 200 ; ancien jeton 401 ; nouveau mot de passe 200 (`forms/record_upsert.go:682-685` → `models/record.go:942-962`, sans `oldPassword` pour un admin `:544-548` ; `models/admin.go:45-60`) | **identique** |
| Globale, celle de 0.22 : `PATCH /api/settings {"recordAuthToken" ou "adminAuthToken": {"secret": …}}`, c'est-à-dire ce qu'envoie « Invalidate all previously issued tokens » du dashboard (`ui/src/components/settings/TokenField.svelte:16-29`, `PageTokenOptions.svelte:50-66`) | 200 ; refresh `users` et `agent_users` 401, record protégé 404, admin 401 (`apis/settings.go:48-85`, `forms/settings_upsert.go:56-73`, secret relu par `apis/middlewares.go:233-248`) | **200 sans effet** : la clé est fusionnée dans `_params.settings` (`ppbase/api/settings.py:455-458`), que rien ne lit (aucune occurrence de `recordAuthToken` ni `adminAuthToken` dans le code de vérification) ; les 4 sondes restent à 200 et le POST suivant d'un flux ouvert avec le jeton « révoqué » reçoit 204 (PocketBase 403) |
| Par collection : `PATCH /api/collections/{nom}` avec `authToken.secret`, à plat (forme actuelle, `v0.40.6 core/collection_model_auth_options.go:139`) ou dans `options` (modèle PPBase, `ppbase/models/collection.py:103-117`) | sans objet : pas d'options de jeton par collection en 0.22 | **400 pour les trois**, sans écho du secret envoyé : `users`, `agent_users` « Identity field 'username' must have a unique index. » (`collection_service.py:120` : `username` est non unique par les migrations 1789236841 et 1789236843 et sert d'identité par 1789236844) ; `_superusers` « Cannot modify a system collection. » (`collection_service.py:553`, alors que `v0.40.6 core/collection_validate.go:96-156` n'interdit que le drapeau, le nom, les champs et les règles système) ; sessions inchangées. Le dashboard PPBase n'a pas d'écran d'options de jetons. |
| Urgence : rotation SQL du §7 | — | effet immédiat sans redémarrage : jetons 0.22 et jetons émis par PPBase refusés (refresh et `/api/admins` 401, `/isConnected` 400 `{"message":"false"}`) ; mots de passe d'origine et nouvelles sessions acceptés |
| Flux realtime déjà ouvert pendant la révocation | non coupé : l'événement protégé arrive encore ; le POST suivant du SDK avec le jeton révoqué reçoit 403 (`apis/realtime.go:187-191`) | identique après la rotation SQL |

**Impact client et exploitation** : un admin qui applique la procédure 0.22
(dashboard ou API) reçoit 200 et croit les sessions révoquées ; elles restent
valables jusqu'à leur `exp` (durée importée de `recordAuthToken` et
`adminAuthToken`, 14 j par défaut en 0.22). Aucune voie API ni dashboard PPBase
ne fait tourner un secret de session : **la rotation SQL du §7 reste la seule
révocation globale** et doit figurer au runbook (WS-F). La révocation par
compte fonctionne comme en 0.22. La source gelée n'appelle ni `/api/settings`
ni `/api/admins` (grep de `src/`, hors déclarations de types) ; l'impact
concerne l'exploitation, pas les clients BUMA. Changements PPBase proposés,
non faits : dans `update_settings` (`ppbase/api/settings.py:385-479`), avant
`_deep_merge` (`:457`), retirer du corps les 8 clés de jetons 0.22, les valider
comme `models/settings/settings.go:379-384` (secret 30 à 300 caractères, durée
5 à 63 072 000 s) et les écrire dans la même transaction dans
`_collections.options` avec la correspondance de l'import (`record*` vers
chaque collection auth sauf `_superusers`, `v0.23.0 migrate.go:338-357` ;
`admin*` vers `_superusers`, `:86-109`), sans les stocker dans
`_params.settings` ; à défaut, les refuser en 400 au lieu d'un 200 silencieux.
Indépendamment : laisser modifier les options d'une collection système auth
comme la version actuelle (`collection_service.py:553`).

### Autres effets runtime PPBase (hors import)

- E-mails : envoyés seulement par `PPBASE_SMTP_HOST/PORT/USERNAME/PASSWORD/FROM`
  (`ppbase/config.py:88-92`, `services/mail_service.py:21-70`), jamais par
  `_params.settings.smtp` importé (seul l'e-mail de test l'utilise). Sans
  `PPBASE_SMTP_HOST`, rien n'est envoyé et seul un avertissement sans contenu
  est journalisé ; le corps, le lien et le jeton ne sont journalisés qu'en
  `PPBASE_DEV` (`mail_service.py:56-66`, `:92-93`), à ne jamais activer en
  production ; le reset admin ne journalise jamais son lien
  (`mail_service.py:165-200`). Les modèles importés sont stockés, mais le
  service mail PPBase utilise ses textes intégrés (`mail_service.py:83-90`).
- `logs.*` est stocké mais le journal de requêtes PPBase ne l'applique pas.
- Le moteur SQLAlchemy de PPBase a `hide_parameters=True`
  (`ppbase/db/engine.py:49`) : les valeurs liées ne sont plus recopiées dans
  ses erreurs. asyncpg ou PostgreSQL peuvent encore citer une valeur
  (`DETAIL`, erreur de conversion) : **les journaux PPBase restent
  confidentiels**.

## 4. Continuité : ce qui marche et ce qui ne marche pas

- **Sessions** : un jeton émis par PocketBase 0.22.34 avant l'import (admin,
  `users`, `agent_users`) est accepté après l'import : route superuser, lecture
  de records protégés, refresh, probes BUMA `/isConnected` et
  `/master/user/health`, avec les mêmes clés de réponse et d'entité. Mots de
  passe d'origine et mots de passe changés dans PocketBase acceptés. Durées de
  session reprises (PPBase avait 1 j / 5 j).
- **Invalidations faites par PocketBase conservées** : un reset de mot de passe
  ou un changement d'e-mail renouvelle `tokenKey` (`models/record.go:942-962`,
  `forms/record_email_change_confirm.go:129-133`, `models/admin.go:45-60`).
  Les jetons émis avant ces renouvellements sont refusés par les deux serveurs
  avec le même statut (401 refresh et `/api/admins`, 404 record protégé) ; les
  jetons émis après sont acceptés.
- **Refus identiques à PocketBase, statuts et libellés** : 33 sondes sur 33,
  dont 22 refus (altéré, expiré, anonyme, jeton `users` sur le refresh
  `agent_users` 403 nommant la collection **du jeton**, jeton record sur route
  admin 401, jeton admin sur un refresh record 401, jeton forgé vers une
  collection **base** traité comme anonyme : 404 sur le record protégé, 401 au
  refresh), libellés de `apis/middlewares.go:94-125`
  (`ppbase/api/record_auth.py:1049-1062` pour le refresh).
  `comparison.message_wording_differences` est vide.
- **Realtime : identique sur les 19 résultats comparés** (statuts des POST et
  livraisons d'événements), avec les requêtes exactes du SDK JS 0.21 : le SDK ne
  POSTe `/api/realtime` que pour le premier écouteur d'un nouveau topic
  (`js-sdk v0.21.5 src/services/RealtimeService.ts:85-94`) et, à
  `unsubscribe()`, seulement s'il reste d'autres topics (`:138-143`) ; chaque
  POST porte tous les topics restants et le jeton courant (`:258-283`) ; la
  désinscription du dernier topic ferme seulement le flux SSE (`:138-140`). Le
  banc envoie exactement ces requêtes (topic record plus `buma_supplier/*`,
  celui de `subscribe("*")`, `RecordService.ts:130-134`). Un client abonné avec
  un jeton 0.22 reçoit les événements protégés (l'anonyme non) ; après un
  refresh PPBase sur la même connexion SSE, s'abonner à un nouveau topic et se
  désinscrire d'un topic parmi deux répondent 204 et le nouveau topic reçoit
  les événements ; un client anonyme qui s'authentifie puis s'abonne répond 204 ;
  le passage authentifié → anonyme est refusé (403) des deux côtés. PPBase
  compare l'identité `collectionId:id` sans le type du jeton et la met à jour à
  chaque abonnement accepté (`ppbase/api/realtime.py:45-72`, refus `:186-193`,
  mise à jour `:255-258`). **Écart délibéré** (D-0027) : 0.22.34 ne compare que
  l'ID (`apis/realtime.go:187-191`, `extractAuthIdFromGetter` `:539-551`),
  PPBase l'ID et la collection comme la version actuelle (`v0.40.6
  apis/realtime.go:218-231`, `isSameAuth` `:843-853`) : passer, sur un même
  client SSE, à un compte d'une autre collection auth (ou d'un admin à un
  record) qui a le même ID répond 403 au lieu de 204. Impact BUMA nul : aucun
  ID partagé entre `users`, `agent_users` et `_admins` dans le backup réel
  (agrégat de la relecture WS-A2) ; test PPBase
  `tests/test_realtime_auth_identity.py`.
- **Type des nouveaux jetons** : après login/refresh, PPBase émet `type=auth`
  avec `refreshable` (0.22 : `authRecord`). Les clients qui lisent le claim
  voient la différence : `pb.authStore.isAuthRecord` du SDK JS 0.21 vaut
  `false` après le premier refresh PPBase (`js-sdk v0.21.5
  src/stores/BaseAuthStore.ts:51-53`) ; `isAdmin` reste vrai pour les admins
  (`:44-46`, PPBase garde `type=admin`) ; `isValid` (claim `exp`) et le SDK Dart
  (`dart-sdk v0.18.1 lib/src/auth_store.dart:34-49`, `exp` seulement) ne
  changent pas. Sources des SDK lues sur leurs tags GitHub officiels ; quelle
  version utilise chaque client BUMA n'est pas établi ici (D-0027 : à vérifier
  sur ces clients avant la bascule). Preuve : `tokens/tokens.go:4-7`,
  `tokens_issued_by_pocketbase.users_auth.type` et
  `comparison.token_type_after_login_or_refresh` du rapport.
- **Jetons à usage 0.22 repris (mesuré, WS-C2)** : vérification, reset,
  changement d'e-mail et reset admin émis par PocketBase avant l'import, sur
  des comptes dédiés (deux `users`, un admin), sont consommés en dernier par
  PPBase après l'import, puis, dans le même ordre, par PocketBase redémarré sur
  sa source, qui sert de référence (`comparison.purpose_tokens*`) :
  vérification 204 ; reset 204 puis changement d'e-mail du même compte 400
  (`tokenKey` renouvelé) ; sur l'autre compte, changement d'e-mail avec un
  mauvais mot de passe 400, puis 204, puis reset 400 ; reset admin 204. Statuts
  identiques, connexions qui suivent identiques (nouveau mot de passe 200,
  ancien 400), effets identiques (vérifié, `tokenKey` renouvelé, mot de passe
  ou e-mail changé, `last_reset_sent_at` remis à NULL comme `lastResetSentAt`
  à vide). Trois corps de refus diffèrent encore (§5). Impact : un lien envoyé
  avant la bascule reste valable jusqu'à son `exp` (vérification 7 j, reset et
  changement d'e-mail 30 min par défaut) **par l'API** ; mais les pages que ces
  liens ouvrent (`/_/#/auth/confirm-verification/…`,
  `/_/#/auth/confirm-password-reset/…`, `/_/#/auth/confirm-email-change/…`,
  admin `/_/#/confirm-password-reset/…`) n'existent pas dans le dashboard
  PPBase (point ouvert de WS-A2) : l'utilisateur arrive sur `/_/` puis sur la
  connexion. Les liens que PPBase envoie après la bascule (sur `meta.appURL`,
  mêmes chemins) ont la même limite. Seul un client qui poste le jeton à
  `/api/collections/{nom}/confirm-*` ou `/api/admins/confirm-password-reset`
  les utilise. La source gelée n'appelle aucun de ces flux.
- **Jetons de fichier 0.22** acceptés (`file_tokens.py:129-145`) ; aucun champ
  fichier protégé dans les 114 définitions BUMA : preuve côté PPBase seulement
  (tests WS-A2 avec le binaire 0.22.34).

## 5. Divergences PPBase restantes (mesurées ou relevées dans le code)

Corrigées par WS-A2 et vérifiées par le banc (n'apparaissent plus) : 403 au
lieu de 401 pour un jeton admin sur un refresh record ; 500 pour un jeton forgé
vers une collection base ; 12 libellés 401/403 ; realtime anonyme puis
authentifié et mélange `authRecord`/`auth` ; secrets servis ; jetons à usage
0.22 refusés ; route de reset admin absente (405).

| Cas | PocketBase 0.22.34 | PPBase | Impact / suite |
| --- | --- | --- | --- |
| enveloppe d'erreur native | `{code, message, data}` (`apis/api_error.go:12-15`) | par défaut `{status, message, data}` (forme actuelle, `v0.40.6 tools/router/error.go:36-42`) ; **BUMA active le mode d'erreur PocketBase 0.22** (`pb.set_error_compat("0.22")` dans `app/hooks/register.py`) : `{code, message, data}` et messages de 0.22.34 | résolu pour BUMA par D-0034 ; le banc exige les mêmes clés d'enveloppe des deux côtés ; écarts restants du mode (statuts non réécrits, détails de `data`) : D-0034 |
| corps de refus des jetons à usage (statuts identiques ; épinglés par `PURPOSE_KNOWN_DIVERGENCES` du banc, rapport `comparison.purpose_token_refusal_body_known_divergences`) | changement d'e-mail, jeton invalidé : `data` = `token` **et** `password` (`validation_invalid_password`) ; changement d'e-mail, mauvais mot de passe : `password.code = validation_invalid_password` (`forms/record_email_change_confirm.go:41-55`, `:75-87`) | `data` = `token` seul (`ppbase/services/record_auth_service.py`) ; `validation_invalid_credentials` ; le message du reset avec un jeton invalidé est, sous le mode 0.22 activé par BUMA, celui de 0.22.34 (mesuré : résolu) | un client qui lit `data.password` ou ces codes voit autre chose ; aucun appelant BUMA connu ; correction PPBase proposée, non faite |
| `POST /api/files/token` sans auth | 400 « Failed to generate file token. » (`apis/file.go:60-75`) | 401 ; sous le mode 0.22 activé par BUMA, message de garde « The request requires admin or record authorization token to be set. » | mesuré par la relecture WS-A2 et par WS-A8, pas par ce banc ; aucun champ fichier protégé dans BUMA |
| forme de `GET /api/settings` (hors secrets) | `meta.appUrl`, `logs.logIp`, les 8 configurations de jetons masquées, 25 fournisseurs OAuth2, `emailAuth` (`models/settings/settings.go:24-70`, `apis/settings.go:29-45`) | forme actuelle : `meta.appURL`, `logs.logIP`, aucune configuration de jeton (elles vivent dans les collections), ni fournisseurs ni `emailAuth`, plus `batch`, `rateLimits`, `trustedProxy` (`ppbase/api/settings.py:42-107`) ; mesuré : 0 configuration contre 8 | un `PATCH` au format 0.22 est stocké dans `_params` sans effet sur les collections (§3) ; aucun appelant BUMA connu |
| modification d'une collection système auth (`_superusers`) | sans objet en 0.22 ; la version actuelle accepte ses options (`v0.40.6 core/collection_validate.go:96-156`) | 400 « Cannot modify a system collection. » (`collection_service.py:553`) | n'interdire que le drapeau, le nom, les champs et les règles système, comme la version actuelle |
| identité realtime, allers-retours des secrets masqués | §4 et §3 | §4 et §3 | écarts délibérés, D-0027 |
| liens e-mail | pages du dashboard 0.22 | pages absentes du dashboard PPBase | §4 ; à traiter dans un chantier PPBase ou par une page cliente |

Autre écart vu en écrivant le banc (un `PATCH` répété avec la même valeur n'a
livré aucun événement), non comparé à PocketBase : PPBase ignore les valeurs
inchangées puis n'écrit rien et ne diffuse rien quand il ne reste que
`updated`, alors que PocketBase 0.22 enregistre toujours le record
(`forms/record_upsert.go:809`, `daos/base.go:266-275` ; lecture de code). Le
`PATCH /api/collections/{users,agent_users}` refusé en 400 tient au schéma
BUMA (`username` non unique, décision C3), pas à PPBase, qui valide comme la
version actuelle (`v0.40.6 core/collection_model_auth_options.go:227`).

## 6. API d'intégration

`scripts.c11_import.import_snapshot` est le **point d'entrée unique** de
l'import (D-0026) : records, puis comptes et paramètres, dans une seule
transaction. Le banc l'utilise pour l'import publié et pour les trois
échecs injectés.

```python
engine = create_async_engine(url, connect_args={"ssl": "disable"}, hide_parameters=True)
evidence = {}
rows = await import_snapshot(
    engine, data, destination, diagnostic, definitions, report=evidence
)  # base jetable buma_c11_rehearsal ; destination : chemin neuf
```

- Avant toute écriture : moteur `hide_parameters=True`
  (`engine_hide_parameters_required`), diagnostic prêt (`diagnostic_blockers`),
  puis `check_ready` de `scripts.c11_auth_import` sur sa propre connexion,
  lecture seule (base de répétition, comptes et paramètres de la source sans
  blocage, `_superusers` vide, pas de paramètre `settings`, pas d'archive,
  mêmes ID de collections auth) : un refus des comptes n'arrive jamais après le
  chargement complet.
- Puis `stage_snapshot`, et une transaction : `load_isolated` (records),
  `recount_relation_arrays`, `import_auth` (admins, secrets, paramètres, qui
  refait chaque contrôle sous ses verrous). Tout échec, `fail_after` (records)
  et `auth_fail_after` (`admins`, `options`, `settings`) compris, annule tout
  et supprime le répertoire de staging créé par l'appel (prouvé par le banc :
  `rollback_in_import_snapshot_transaction`).
- `evidence` reçoit `auth` (rapport sans valeur d'`import_auth`),
  `single_transaction` (`records`, `relation_arrays_recounted`,
  `accounts_and_settings`, `committed`), `relation_arrays` et les durées. La
  source 0.22.34 est une version validée (D-0024) : aucun blocage n'est écarté.
- **`hide_parameters=True` est exigé** : sans lui, SQLAlchemy recopie toutes les
  valeurs liées dans le message d'une `DBAPIError` (`[parameters: …]`) : hash,
  `tokenKey` et secrets ici, hash et `tokenKey` de chaque record auth dans
  `load_isolated`. Toute erreur SQLAlchemy pendant l'import des comptes devient
  `ImportBlocked("auth_import_database_error")`, levée hors du gestionnaire
  (aucun `__context__` : ni SQL, ni `DETAIL` PostgreSQL « Failing row contains
  … », ni valeur), avec pour seul attribut le `sqlstate` (5 caractères).
- Primitives, pour les tests et les répétitions seulement :
  `import_auth(connection, data, definitions, *, fail_after=None)` relit le
  manifeste et l'empreinte de `data.db` comme `load_isolated`, lit le snapshot
  par `scripts.c11_import.snapshot`, verrouille `_superusers`/`_params`
  (ACCESS EXCLUSIVE), `_buma_c11_rowids` (SHARE) et les collections auth
  (FOR UPDATE) **avant** les contrôles, exige que `_buma_c11_rowids` existe et
  provienne **du même snapshot** (par collection : nombre de lignes et
  empreinte SHA-256 des lignes `rowid:id\n` dans l'ordre des rowids, calculées
  par PostgreSQL et comparées à `diagnostic.collections[].identity_sha256` du
  manifeste), refuse une cible non vide, relit tout ce qu'il écrit
  (`last_reset_sent_at` compris). Retour : rapport agrégé (`public_report` +
  `records_snapshot`), sans secret : présent, importé, longueur ; pour les
  admins, `imported_as_last_reset_sent_at`, `archived_source_text` et le
  nombre `with_last_reset_sent_at`. Le banc exerce ces refus sous le point
  d'entrée (`refusals_in_load_isolated_transaction`, `auth_without_records`,
  `second_import`).
- `diagnose_auth(db, definitions)` : diagnostic lecture seule (structure,
  validité, blocages) ; CLI `python -m scripts.c11_auth_import --data DIR
  --schema EXPORT --report NEW.json`.
- `scripts.c11_auth_runtime.PocketBaseServer`, `pocketbase_tokens(...)`,
  `RealtimeClient` et `ppbase_server_environment` : démarrer un PocketBase
  jetable, obtenir des jetons par HTTP, s'abonner au realtime, lancer le PPBase
  testé (répétition complète) ; `EMERGENCY_SESSION_REVOCATION_SQL` : la
  rotation d'urgence du §7, telle que le banc l'exécute.
- Codes de refus, chacun testé (unitaires sans aucune écriture, avec arrêt
  d'`import_auth` avant tout verrou pour ceux de la source ; banc PostgreSQL
  avec retour exact à l'état initial pour ceux côté base) :
  - préconditions : `engine_hide_parameters_required`,
    `diagnostic_blockers`, `staged_snapshot_changed`,
    `isolated_rehearsal_database_required`, `records_snapshot_unverifiable` ;
  - source (`check_source`) : `missing_internal_table`,
    `source_auth_collections_missing`, `unmapped_admin_columns`,
    `admin_value_not_representable` (dont `lastResetSentAt` hors de la forme
    PocketBase), `duplicate_admin_email`, `duplicate_admin_token_key`,
    `unmapped_param_columns`, `unknown_param_keys`, `external_auths_present`,
    `settings_missing`, `settings_not_plain_json`,
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
  hash, secrets et `lastResetSentAt` des admins. Tout changement fait dans
  PocketBase entre le backup et la bascule est annulé sur PPBase : mot de passe
  changé ou réinitialisé, e-mail changé, admin supprimé ou ajouté, compte créé.
  L'ancien mot de passe redevient valable et les jetons que PocketBase avait
  invalidés redeviennent acceptés jusqu'à leur `exp` (14 j par défaut pour les
  sessions) ; un lien de reset ou de changement d'e-mail déjà consommé dans
  PocketBase redevient utilisable par l'API jusqu'à son `exp` (conséquence
  du snapshot, non mesurée). Pour WS-F : **le
  gel des écritures doit commencer avant le backup frais** et couvrir, en plus
  du gel général des données, tout ce qui change l'état d'authentification :
  `confirm-*` (vérification, reset, changement d'e-mail, reset admin),
  `request-*` (qui écrivent `lastResetSentAt` ou `lastVerificationSentAt`),
  création, `PATCH` et suppression des records auth, gestion des admins
  (`/api/admins*`) et `/api/settings`. Les connexions (`auth-with-password`)
  et les refresh ne changent aucun état en 0.22.34, et la source gelée n'a
  aucun hook d'authentification (seulement les déclarations de
  `src/types/pocketbase.d.ts`) : un jeton émis par PocketBase pendant la
  fenêtre est signé avec le `tokenKey` et le secret du snapshot, et PPBase
  l'accepte après l'import (`tokens/record.go:13-27`, `daos/record.go:414-444`,
  `daos/admin.go:52-78` : vérification sans état serveur). Geler aussi
  connexions et refresh est un choix d'exploitation (fenêtre de maintenance),
  pas une condition de continuité des sessions.
- **Révocation d'urgence après la bascule** (prouvée par le banc,
  `comparison.session_revocation_emergency_sql`) : PPBase n'offre toujours
  aucune voie API ni dashboard pour faire tourner un secret de session (§3) ;
  **cette rotation SQL est la seule révocation globale**. Sur la base PPBase,
  en une transaction :

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
  `_superusers`. Tout changement de compte fait sur PPBase après la bascule
  (mot de passe, e-mail, vérification, jeton à usage consommé) est absent de
  PocketBase : un jeton à usage 0.22 consommé sur PPBase reste utilisable sur
  PocketBase jusqu'à son `exp` (mesuré : le banc consomme les mêmes jetons sur
  les deux serveurs). Non mesuré : le sort, sur 0.22.34, des jetons à usage
  émis par PPBase après la bascule.

## 8. Preuves

- `tests/test_c11_auth_import.py` (68 tests) : correspondance,
  `zeroFallback`, chaque refus de la source (dont collection auth absente,
  colonne `_params` inconnue, ID de la ligne `settings` trop long ou avec NUL,
  `created`/`updated` vide, sans fuseau ou invalide, `lastResetSentAt` sans
  `Z` ou date impossible : arrêt d'`import_auth` après
  `SELECT current_database()` seulement), chaque avertissement (valeur stockée
  gardée, rapport sans valeur), NUL et surrogate signalés par emplacement sans
  valeur, rapports sans valeur ; `lastResetSentAt` importé (instant exact, `""`
  en NULL, texte source archivé) ; base scriptée en mémoire qui répond à chaque
  instruction d'`import_auth` : chemin complet (ordre des écritures, valeurs
  écrites), chaque garde cible ou précondition refusée sans aucune écriture,
  chaque relecture altérée arrêtée avant l'étape suivante, erreur pilote
  convertie sans valeur ni contexte ; vérification par le vrai
  `get_optional_auth` PPBase de jetons au format 0.22 : refusés avec les
  secrets PPBase d'origine, acceptés après fusion des options importées,
  refusés altérés, expirés, de mauvaise collection, signés avec le mauvais
  secret ou après renouvellement du `token_key` stocké. Avec l'importeur
  d'avant WS-C2, ce fichier donne 8 échecs (cas `lastResetSentAt` et base
  scriptée, qui enregistre désormais la valeur liée).
- `docs/c11-auth-runtime-report.json` (WS-C2) : binaire officiel 0.22.34,
  PPBase `c11/ppbase-preview@22929a0` dont le paquet chargé par le serveur est
  vérifié (`server_ppbase` : même paquet que le banc, arbre propre, `/proc`),
  données synthétiques, PostgreSQL 16 jetable ; deux passages identiques hors
  durées et horodatage. Contenu : claims et algorithme relevés sur les jetons
  réellement émis, formule de clé vérifiée pour les 11 jetons et pour les
  `tokenKey` renouvelés ; import par `import_snapshot` (8 records, comptes et
  paramètres dans la même transaction), trois échecs injectés et dix refus
  côté base (dont relectures altérées par trigger, erreur base sans valeur,
  moteur sans `hide_parameters`), chacun revenu exactement à l'état initial ;
  refus sans records ; second import bloqué par le verrou puis refusé ;
  relecture indépendante (comptes, dates et `last_reset_sent_at` des admins,
  ligne `settings`, tous les champs `meta`, `logs` et `smtp` repris) ; 33
  sondes HTTP identiques (statut, enveloppe, libellé), 10 connexions, 7 sondes
  BUMA, scénario realtime aux requêtes du SDK JS 0.21 (19 résultats comparés,
  identiques), exposition des secrets (`GET` et `PATCH`, 0 des deux côtés),
  révocation (14 résultats comparés, dont 5 écarts de la voie
  `PATCH /api/settings`, 6 `PATCH` de collection, rotation SQL d'urgence),
  7 consommations de jetons à usage et 5 connexions comparées avec leurs
  effets, retour arrière.
- Backup réel (lecture seule, `-I`, structure seulement) : 1 admin aux 8
  colonnes 0.22, `lastResetSentAt` vide, `_params` = `settings` en JSON clair,
  8 configurations de jetons présentes et dans les limites PocketBase, 0
  `_externalAuths`, aucun identifiant OAuth, S3 vide, cron de sauvegarde
  présent : **aucun blocage**, un avertissement `backup_schedule_not_imported`.

## 9. Limites

- Preuve synthétique : l'import du vrai backup reste à exécuter par la
  répétition réelle (WS-G), par `import_snapshot`.
- **Consignes pour WS-G** : après l'import, le PPBase jetable porte les 8
  secrets de jetons et le mot de passe SMTP de production (non servis par
  l'API, mais présents dans la base et cités potentiellement par une erreur
  asyncpg ou PostgreSQL) et accepte les vraies sessions 0.22 encore valides,
  ainsi que les vrais liens de vérification, de reset et de changement
  d'e-mail encore valables. Donc : port loopback seulement ; créer le
  superuser synthétique **après** l'import (avant, il refuse :
  `superusers_target_not_empty`), dans la seule base jetable ; ne jamais
  enregistrer le corps des réponses de `/api/collections*` ni de
  `/api/settings` (`GET` ou `PATCH`), seulement statuts et noms de clés ; ne
  jamais appeler `/api/settings/test/email` (il utiliserait les identifiants
  SMTP de production importés : action sur un serveur de production), ni les
  routes `request-*` et `confirm-*` ; lancer PPBase **sans** `PPBASE_SMTP_*`
  (rien n'est envoyé) et **sans** `PPBASE_DEV` (sinon le corps des e-mails et
  leurs jetons iraient dans les journaux), avec
  `BUMA_MASTER_ENABLE_INTERNAL_JOBS=false`, comme le banc ; journaux privés
  (0600), traités comme confidentiels ; détruire la base et les journaux à la
  fin. Vérifier `meta.appURL` importé.
- Version source : 0.22.34 est validée (D-0024) ; le banc n'écarte plus aucun
  blocage (`version_gate.waiver_applied=false`).
- Le manifeste lu est celui de `stage_snapshot` actuel (`diagnostic`,
  `data_db_sha256`, `collections[].identity_sha256`) et `_buma_c11_rowids`
  (`collection_id`, `record_id`, `source_rowid`) ; à réaligner si WS-B change
  ces formats.
- Non testé ici : SDK JS/Dart réels, desktop. Le realtime est sondé au niveau
  HTTP/SSE avec les requêtes du SDK JS 0.21 (lues dans son code), pas avec le
  SDK lui-même. Les e-mails envoyés par PPBase et ses pages de confirmation ne
  sont pas sondés (le banc ne lui donne pas de SMTP).
- Révocation : seule la rotation de `authToken` est prouvée ; le même
  `jsonb_set` vaudrait pour les autres types (`passwordResetToken`,
  `verificationToken`, `emailChangeToken`, `fileToken`), non mesuré. La
  coupure des flux realtime ouverts n'existe sur aucun des deux serveurs.
- Ouverts côté PPBase, sans correction ici : pages de confirmation absentes du
  dashboard, deux corps de refus des jetons à usage (codes de `data`), statut de
  `POST /api/files/token` sans auth, révocation globale par `PATCH /api/settings`
  sans effet, type `auth` des jetons après un refresh PPBase, journaux à traiter
  comme confidentiels. L'enveloppe d'erreur est résolue pour BUMA par le mode
  d'erreur PocketBase 0.22 (D-0034).
