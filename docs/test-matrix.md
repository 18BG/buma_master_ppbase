# Matrice de tests

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
