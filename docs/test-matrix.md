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
