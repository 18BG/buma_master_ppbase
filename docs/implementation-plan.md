# Plan d'implémentation contrôlée

## Règle principale

Ne jamais porter tout le backend en une seule passe. Chaque checkpoint couvre
un périmètre limité, contient ses tests et reste ouvert jusqu'à validation.

Source gelée : branche `fix/uv-paani-sup`, commit `530018c`.

## Règle de parité PocketBase obligatoire

La cible doit être interchangeable avec l'ancien backend pour les applications
qui utilisent les SDK PocketBase ou les appels HTTP directs. La validation se
fait donc sur le comportement observable exact, pas uniquement sur le résultat
métier ou la présence de données.

Chaque checkpoint doit comparer et tester, selon son périmètre : méthode et
route legacy, payload et valeurs par défaut, auth/middleware/permissions,
codes HTTP, content type, enveloppe JSON, messages et noms de champs, ordre et
effets des hooks, relations persistées, rollback, verrouillage, idempotence,
retry et opérations CRUD/SDK PocketBase consommées par les clients.

Un HTTP 200, un lint/build passant ou une écriture PostgreSQL ne prouve pas la
parité. Toute différence doit être enregistrée avant clôture dans
`docs/decisions-and-divergences.md`, avec impact client et test de régression.
Il est interdit de renommer, normaliser ou « améliorer » silencieusement un
contrat legacy.

## Checkpoints

### C0 — Provenance et inventaire

Inventorier les deux bootstraps (`master.pb.ts` et `index.pb.ts`), routes
actives/commentées, hooks, collections, vues, règles, permissions, SQL,
intégrations et invariants transactionnels.

Livrables : manifeste source, contrats routes/collections, matrice de parité,
invariants transactionnels et matrice de tests.

### C1 — Foundation PPBase

Créer configuration, PostgreSQL isolé, registre de hooks, health check, POC de
route, migrations natives vides et tests de structure.

Gate : `pytest`, Ruff, format check et démarrage du POC sans domaine métier.

### C2 — Schéma PostgreSQL

Réconcilier l'export Dashboard avec les migrations historiques, puis générer
une baseline PPBase déterministe. Traduire les vues SQLite séparément.

Gate : application reproductible sur deux bases vides, relations/indexes/vues
contrôlés, aucun import de données.

État courant : baseline des 70 collections `base` et 2 collections `auth`, puis
migration des 42 vues, validées sur une base PostgreSQL isolée. Les 114
définitions du schéma corrigé sont maintenant installables sans migration en
attente ; l'authentification métier et les données restent hors périmètre.

### C3 — Authentification et permissions

Porter `users.role -> roles.permission`, `buma_apps_roles.info.actions`,
superuser, middleware et contexte par requête.

Gate : login, 401, 403 et matrice des permissions.

### C4 — Transactions et idempotence

Porter repositories transactionnels, rollback, verrous PostgreSQL et
`master.idempotency.ts` avant les flux vente/retour/stockage.

Gate : doublon, retry, échec forcé avec rollback et concurrence.

### C5 à C9 — Domaines métier

Un checkpoint par famille :

1. settings et référentiels ;
2. clients, fournisseurs, employés, PDV, SIM et agents ;
3. article, stock et stockage ;
4. wallet et caisse ;
5. vente, recharge, achat, paiement, retour et rapports.

Pour chaque famille : lire le TypeScript, écrire les tests de contrat,
implémenter service puis route/hook, tester DB/réponse/auth/rollback et mettre à
jour la matrice. La gate compare aussi les enveloppes/réponses et les effets de
hooks avec la source, ainsi que les appels SDK PocketBase utilisés par les apps.

État C9 au 2026-09-23 : les 29 routes sont portées et leur gate locale est
passante (`docs/c9-runtime-report.json`). D-0009 a ensuite été corrigé : les sauvegardes partielles des anciens flux
non transactionnels sont conservées et testées. Cette preuve locale ne
constitue pas une autorisation de cutover.

### C10 — Compatibilité legacy

État au 2026-09-23 : les sous-lots SQL, `/api2/stockage` et hooks CRUD natifs sont portés et validés ; les autres routes `/api2/*`, master legacy et hooks restants restent ouverts.

Porter séparément les routes `/api2/*`, `/isConnected`, les hooks de
`index.pb.ts` et les routes `/master/*`. Les routes SQL doivent faire l'objet
d'une décision de compatibilité et de protection dédiée.

### C11 — Données et cutover

Après validation du schéma et de l'API : diagnostic d'import, import sur cible
fraîche, conservation des IDs/relations/fichiers, checksums, staging, rollback
et bascule progressive.

## Format obligatoire d'un checkpoint

- périmètre et fichiers touchés ;
- contrats couverts ;
- tests exécutés et résultats ;
- contrats exacts comparés : requête, réponse, codes, hooks et compatibilité
  SDK/CRUD ;
- écarts et risques ;
- décision de passage au checkpoint suivant ;
- commit du nouveau dépôt uniquement.

Un checkpoint échoué bloque le suivant. Aucun fallback silencieux ni donnée
de démonstration ne doit masquer un contrat manquant.
