# Contrat C3 — authentification et permissions

Source gelée : `buma_master_hooks/fix/uv-paani-sup@530018c`.

## Flux observés

- `src/entries/index.pb.ts` expose `GET /isConnected`. Il lit uniquement
  `requestInfo(c).authRecord` : un token admin n'est pas considéré comme une
  connexion utilisateur. Réponse : `200 {"message":"true"}` ou
  `400 {"message":"false"}`.
- `src/entries/master.user.ts` expose `GET /master/user/health`. La route est
  publique et renvoie le texte `true` ou `false` avec HTTP 200.
- Les routes métier utilisent `$apis.requireAdminOrRecordAuth()` ; l'absence
  de token est `401`, un modèle d'authentification non autorisé est `403`.
- `src/entries/master.utility.ts` charge `users.role`, puis `roles.permission`.
  `isAdmin` donne les permissions CRUD/manage par application et les actions
  de `buma_apps_roles.info.actions`; un rôle non admin utilise uniquement la
  section de l'application demandée.

## Collections auth

| Collection | Identités | Minimum source | Règle notable |
| --- | --- | ---: | --- |
| `users` | `username` | 5 | `allowEmailAuth=false`; règles liées à `role.permission` |
| `agent_users` | `email`, `username` | 8 | `viewRule=@request.auth.id = id` |

La migration `1789236844_auth_compat.py` matérialise ces options dans PPBase.
Le middleware de login essaie les identités autorisées et conserve le format
PocketBase `{token, record}` ainsi que les hooks d'authentification PPBase.

## Implémentation PPBase

- `app/hooks/auth.py` enregistre les deux routes legacy et les middlewares de
  contexte/login.
- `app/auth/login.py` adapte la recherche username/email avant d'appeler le
  service JWT PPBase.
- `app/auth/context.py` recharge le record auth et son rôle dans PostgreSQL,
  puis enrichit le contexte `@request.auth.role.permission.*` utilisé par les
  règles natives PPBase.
- `app/auth/middleware.py` reproduit `requireAdminOrRecordAuth()` pour les
  prochaines routes métier.
- `app/auth/permissions.py` reproduit la résolution `Access.get()` et les
  actions admin de `buma_apps_roles`.

## Preuves C3 exécutées

- `18` tests ciblés/suite locale : `pytest -p no:cacheprovider -q`.
- `ruff check app tests scripts` et `ruff format --check app tests scripts`.
- Base PostgreSQL isolée : migration `1789236844_auth_compat.py` appliquée.
- Runtime avec fixtures synthétiques supprimées après test : login users par
  username, agent_users par username/email, JWT, refresh, probes legacy,
  owner `200`/autre token `404`, permission autorisée `200`, permission
  refusée `400` avec transaction annulée, refresh sans token `401`.

## État de la parité mot de passe

La base PostgreSQL persistée contient `users.password.min=5` et
`agent_users.password.min=8`. Après correction PPBase, la preuve HTTP est
passante : `users` accepte 5 et rejette 4 en création et mise à jour ;
`agent_users` accepte 8 et rejette 7 ; login username et refresh répondent
HTTP 200 ; les minima retournent les messages attendus.

L'adaptateur BUMA accepte maintenant les deux représentations : le claim JWT
standard `type=auth` et l'ancien `type=authRecord`. Rectification du
2026-10-24 : dans PocketBase 0.22.34, `authRecord` est la valeur du claim JWT
`type` des jetons record (`tokens/tokens.go` l.4-7) ; la version actuelle émet
`type=auth` (v0.40.6 `core/record_tokens.go` l.13). Accepter les deux garde
valides les sessions émises par PocketBase 0.22.34 avant l'import (D-0027).

Preuve HTTP après correction : login username HTTP 200, `/isConnected` HTTP 200
`{"message":"true"}`, `/master/user/health` HTTP 200 `true`, refresh HTTP 200,
refresh anonyme HTTP 401. La matrice runtime des permissions avec le claim
standard `type=auth` est également validée : autorisation HTTP 200, refus HTTP
400 avec rollback, et accès owner `agent_users` 200 contre autre record 404.

C3 est fermé. La suite est le checkpoint C4 transactions/idempotence.
