# Matrice de tests

Statut courant au 2026-09-28 : **C10 terminé sur décision explicite de
l'utilisateur (D-0020)** après confirmation de la correction PPBase.
Les sections datées ci-dessous conservent les preuves et limites historiques.
Aucun nouveau test applicatif/desktop n'est déclaré par cette clôture.

| Niveau | Foundation | Métier | Gate |
|---|---|---|---|
| Structure | registre, routes, manifest | route→handler | obligatoire |
| Unitaire | configuration, helpers | services de domaine | par checkpoint |
| Contrat | codes et JSON | payloads/enveloppes source | par route |
| Intégration | PPBase + PostgreSQL | écritures et relations | domaines critiques |
| Concurrence | POC rollback | caisse/wallet/stock/idempotence | avant production |
| Migration | aucune donnée en C1 | counts/relations/fichiers | avant cutover |

Pour le checkpoint schéma, `scripts/verify_schema_baseline.py` reste la preuve
ciblée des collections `base`/`auth`, `scripts/verify_view_queries.py` exécute
les 42 requêtes traduites, et `scripts/verify_complete_schema.py` vérifie les
114 définitions (72 collections `base`/`auth` + 42 vues) dans la même base.
Les erreurs de vues étaient bloquantes pour leur migration ; la migration
officielle est maintenant installée et les 42/42 requêtes passent.

Les scripts `test_*.py` du dépôt source servent de références comportementales.
Ils ne doivent pas être considérés comme validation PPBase tant qu'ils ne sont
pas paramétrables et exécutés contre une cible dédiée.

## Preuves C7

`tests/test_c7_stockage_contract.py` vérifie l'enregistrement des routes et la
présence des primitives transactionnelles/verrous. `scripts/c7_stockage_runtime.py`
exécute le scénario PostgreSQL complet : article et stock initial, mouvement,
rollback batch, transfert/annulation, conversion/retry idempotent,
wallet/UV, 401/403, concurrence et nettoyage.

## Preuves C8

`tests/test_finance_contract.py` vérifie les routes wallet/caisse autonomes et
leur enregistrement. `scripts/c8_finance_runtime.py` vérifie les enveloppes
HTTP, sessions, soldes liés, annulations, corrections, versements, dépenses,
banque, dettes, accès, vérification, concurrence et nettoyage sur PostgreSQL.
Les routes caisse dépendantes de vente/retour/rapports restent dans C9.

## Preuves C9

`tests/test_commerce_contract.py` compare les 29 méthodes/chemins à l'inventaire
gelé et exécute les handlers : 401/403/400 avec messages legacy, rapports publics,
`updatePDV` vide, lecture retour sans permission ajoutée, garde wallet sur achat
payé et préfixe d'erreur d'annulation recharge.

`scripts/c9_commerce_runtime.py` vérifie HTTP + PostgreSQL : 29 routes,
soldes/quantités/snapshots, antidatage, annulations, PAANI/supplément agent,
idempotence et concurrence recharge/retour, paiement concurrent sans
dépassement, réception NAFAMA, identité inconnue prévue par la source,
versements groupés et rollback, rapports non vides, CRUD natif avec filtre et
expand et échec forcé après une première écriture. Les fixtures et le trigger
de faute isolé sont supprimés en `finally`. Artefact :
`docs/c9-runtime-report.json`.

Il s'agit d'une comparaison statique au TypeScript et d'une exécution PPBase,
pas d'une exécution simultanée des deux backends. La différence d'atomicité des
anciens flux vente/achat non transactionnels est documentée en D-0009 ; files,
realtime et parité exhaustive des erreurs moteur ne sont pas prouvés.


Actualisation D-0009 (2026-09-23) : l'atomicité ajoutée aux fonctions vente/achat
legacy non transactionnelles a été retirée. Les tests exigent maintenant la
persistance des sauvegardes antérieures à l'échec, dans l'ordre TypeScript.
Les mentions précédentes d'un rollback global conservé comme divergence sont
supplantées par cette correction. Les vraies transactions source restent
atomiques et leur preuve reste applicable.

## Preuves C10 partielles — 2026-09-23

Trois sous-lots passants : SQL (quatre routes, référence PocketBase 0.22.27
jetable et PPBase), stockage legacy (cinq routes, effets persistés/échecs
partiels/permissions) et hooks natifs (défauts création, dette/payDebt, SIM et
neuf gardes suppression). Scripts `c10_legacy_sql_runtime.py`,
`c10_stockage_runtime.py`, `c10_native_hooks_runtime.py` et rapports JSON
associés. Suite locale finale : 104 tests ; C10 global reste ouvert.

D-0009 est corrigé : le script C9 exige maintenant les sauvegardes partielles
exactement là où la source ne possède pas de transaction englobante. Une
réussite du rollback ajouté ne compte plus comme preuve de parité legacy.

## Preuve C10 caisse locale — 2026-09-24

`tests/test_legacy_caisse_contract.py` vérifie les permissions absentes/false,
rapports publics, ordre des écritures, corps vide, répétition de reversal et
sérialisation publique. `scripts/c10_caisse_runtime.py` exerce les dix routes
HTTP avec PostgreSQL, données non vides, expand, solde fractionnaire, compteur
wallet défectueux, annulation, répétition et fautes tardives isolées. Rapport :
`docs/c10-caisse-runtime-report.json`, nettoyage vérifié. Suite : 115 tests.
Pas de replay de ces dix handlers sur PocketBase pendant cette session ; les
branches non exercées restent ouvertes dans le contrat caisse. C10 non clôturé.

## C10 — comparaison des serveurs et SDK — 2026-09-24

- `c10_remaining_runtime.py` : **18 routes, 60 comparaisons** de réponses et
  effets sur le TypeScript gelé exécuté par PocketBase 0.22.27 puis PPBase.
- `c10_caisse_reference_runtime.py` : **10 routes, 34 comparaisons** ; complète
  les fautes PostgreSQL injectées par `c10_caisse_runtime.py`.
- `c10_sdk_runtime.py` / `c10_sdk_probe.mjs` : SDK JS réel, login/refresh,
  création/liste filtrée/expand/update/delete, upload/download PNG et realtime
  update. Un utilisateur abonné distinct perd puis retrouve son droit de lecture.
- `test_realtime_role_context.py` : deux abonnés concurrents, contextes séparés
  de l'émetteur. `test_legacy_remaining_contract.py` couvre les enveloppes
  GoError/Error et l'absence de toute route active inventoriée manquante.
- C10 SQL/stockage/natif/caisse et C8/C9 rejoués ; 126 tests unitaires passants,
  Ruff/format passants. Migration alias : rollback/replay ; 114 définitions,
  42 vues, 8 migrations appliquées.

Le rapport `docs/c10-runtime-report.json` reste **incomplete** : comparaison
SQL `strftime` en échec et deux projections rowid signalées explicitement.
Les cases de portage passantes ne remplacent pas cette gate globale.

## C10 — rowid sourcé, corrigé et gate locale clôturée — 2026-09-24

`docs/contracts/rowid.md` relie documentation SQLite, code PocketBase 0.22.27,
DDL effectif et usages BUMA exacts. `c10_rowid_runtime.py` ajoute 12 comparaisons
des trois vues sur PocketBase/SQLite 3.46.0 et PPBase, dont réponses complètes,
types JSON, filters/fields, update, suppression, réutilisation après rollback,
limite du bigint et non-exposition du champ sur le CRUD de base. Le scénario
PostgreSQL vérifie 20 insertions concurrentes puis le nettoyage.

Migration 1789236848 appliquée, rollback/replay sur tables vides vérifiés ;
les tests rejettent toute tentative de deviner des rowid sur données existantes.
Le vérificateur ne possède plus d'exception pour les projections absentes.
Résultat final : 131 tests, 106 comparaisons ciblées au total, SDK passant,
régressions C8/C9 et toutes les gates C10 rejouées, 9 migrations, 114 définitions,
42 vues. La clôture source/locale est bornée par D-0017 (SQL natif PostgreSQL,
pas d'émulation universelle SQLite) et ne valide ni import ni cutover.

Rectification ultérieure : la clôture conditionnelle sous D-0017 est retirée.
Les résultats ci-dessus restent acquis ; C10 reste ouvert pour la compatibilité
des requêtes entrantes de /sql et executeSql. Une différence documentée n'est
pas automatiquement autorisée ni résolue.

## C10 — périmètre SQL décidé par l'utilisateur — 2026-09-26

D-0018 accepte les requêtes PostgreSQL saisies dans la console desktop et
reporte les recherches/parités supplémentaires de `/sql`. La route est
conservée ; les anciennes preuves ne sont ni effacées ni étendues.

Gate encore ouverte : POST executeSql avec la requête desktop initiale
`SELECT * FROM users`, puis GET natif `buma_sql_table` page 1/perPage 1000 ;
vérifier les champs/types et l'enveloppe `items` utilisés par le tableau,
un cas PostgreSQL spécifique, les erreurs sans modification résiduelle de la
vue, puis la restauration/nettoyage. Aucun résultat nouveau n'est déclaré
par la décision documentaire. La parité générale SQLite et les essais `/sql`
du script interrompu sont reportés, pas marqués passants.

## C11 — D-0022, index d'idempotence source — 2026-10-23

- `tests/test_idempotency.py` : migration `1789236849` (index source exact,
  index C4 supprimés, écart attendu par le vérificateur) et 14 cas comparant le
  prédicat PostgreSQL au `LIKE 'idem:%'` de SQLite.
- `tests/test_c11_import.py` : le diagnostic ne bloque que sur les doublons de
  clé `idem:` (globaux, casse ASCII repliée), pas sur l'historique mobile.
- `tests/test_schema_baseline.py`, `tests/test_view_verifier.py` : le
  vérificateur attend l'export plus l'index source ; les tests qui lisent
  l'export restent bloqués par macOS dans l'environnement du 2026-10-23.
- `scripts/c11_idempotency_runtime.py` : PostgreSQL 16 jetable, 10 migrations,
  matrice comparée source/C4, rollback/replay, refus atomique du rollback avec
  historique dupliqué, concurrence sans index mobile, puis C4, C7 et C9
  (29 routes). Rapport `docs/c11-idempotency-runtime-report.json`.
- `scripts/c11_idempotency_source_check.py` : agrégats read-only du backup
  (migrations non appliquées, index, clés `idem:`, dates et ordre des
  doublons). Rapport `docs/c11-idempotency-source-check.json`.

Suite locale : 167 tests passants, 7 échecs dus à l'export illisible.

Actualisation du 2026-10-24 : D-0023 est figée par
`tests/test_idempotency.py::test_d0023_lookup_keeps_created_order_and_source_precedence`
(tri `created` et précédence des clés). L'export se lit désormais depuis
`BUMA_SCHEMA_EXPORT` (défaut inchangé). Suite locale : 168 passants, mêmes
7 échecs liés à l'export illisible sur le Mac.
