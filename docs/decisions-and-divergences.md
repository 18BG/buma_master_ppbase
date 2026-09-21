# Décisions et divergences

## D-0001 — Référence source

La réécriture part de `fix/uv-paani-sup@530018c`.

## D-0002 — Séparation des dépôts

`buma_master_hooks` reste la source TypeScript. Le backend PPBase est développé
dans `buma-master-ppbase`.

## D-0003 — Schéma et données

Les migrations historiques servent à comprendre l'évolution. La baseline
runtime PPBase sera générée depuis l'état final du schéma validé, puis les
données seront importées séparément.

## D-0004 — Progression

Un checkpoint échoué bloque le suivant. Aucun domaine métier ne doit être porté
en masse avant validation du socle, du schéma, de l'auth et de l'idempotence.

## D-0005 — Baseline et vues

Le premier export JSON a servi uniquement à un contrôle intermédiaire et son
draft a été supprimé. Après correction des vues `customer_pdv`, le nouvel export
corrigé est devenu la référence. La baseline `base`/`auth` et la migration des
42 vues sont générées depuis lui ; les 42 requêtes passent la validation
PostgreSQL documentée dans `docs/view-verification-report.json`.

## D-0006 — Champ métier absent attendu par les vues

- Dans l'ancien export, `buma_customer_pdv_list_selector_view` et
  `buma_customer_pdv_list_view` utilisaient `bc.parent`, où `bc` est un alias
  de `buma_pdv`. Ces deux vues ont été corrigées dans PocketBase vers
  `buma_pdv.agent`, avec `par.id = bc.agent` ; le nouvel export expose donc
  `agent` et ne contient plus cette référence absente.
- `buma_purchase_list_view`, `buma_sale_list_view`,
  `buma_transaction_list_view`, `buma_stockage_no_manager` et
  `buma_users_list_view` utilisent `u.username`, où `u` est un alias de la
  collection d'authentification `users`. Le Dashboard confirme que `username`
  est bien un champ système PocketBase. Ces vues ne nécessitent donc pas de
  correction dans PocketBase ; PPBase 0.3.0 ne crée toutefois pas encore de
  colonne PostgreSQL physique `username` dans ses tables auth. Les migrations
  `1789236841` (`users`) et `1789236843` (`agent_users`) matérialisent cette
  compatibilité ; l'unicité et la sémantique de connexion restent C3.

- `buma_customer_pdv_list_view` renvoie actuellement HTTP 200 avec `data: []`
  dans le Dashboard. La requête observée utilise `fields=id`, ce qui ne valide
  pas à lui seul toutes les expressions de la vue ; il faut aussi tester les
  champs complets avec des données satisfaisant ses jointures et
  `sim.number != ''`.

Ces champs ne doivent pas être ajoutés, renommés ou supprimés dans la cible sans
un test runtime et une décision explicite de compatibilité PocketBase/PPBase.

Les autres erreurs du draft PostgreSQL (`COALESCE`, `GROUP BY`, `GREATEST`,
identifiants camelCase ou dépendances entre vues) sont des traductions SQL pour
PPBase ; elles ne constituent pas des champs absents à corriger dans PocketBase.

Les références `rowid` de `buma_article_with_no_stock_list_view`,
`buma_stock_transaction_list_view` et `buma_stockage_no_manager` sont des
pseudo-colonnes internes SQLite, pas des champs PocketBase manquants. Elles
seront traduites vers `id` dans PPBase et ne nécessitent pas l'ajout d'un champ
`rowid` dans PocketBase.

## Divergences à décider plus tard

- C3 : la migration persistée normalise `minPasswordLength` vers le champ
  système `password` (`users.min=5`, `agent_users.min=8`) et le runtime PPBase
  passe maintenant la création/mise à jour 5/4 et 8/7. L'adaptateur BUMA
  accepte désormais le claim standard `type=auth` ainsi que l'ancien
  `type=authRecord`; `authRecord` reste compris comme contexte PocketBase et
  non comme type JWT.

- comportement exact et protection des routes SQL ;
- compatibilité des hashes d'authentification ;
- filtres/pagination/expand du SDK ;
- comparaison runtime des valeurs de vues PocketBase et PPBase avec des données
  représentatives (la validation actuelle est structurelle/SQL, sans backup de
  production) ;
- jobs externes ou scheduler ;
- règles de concurrence et contraintes uniques finales.

## D-0007 — C7 et valeurs historiques de transaction

Le code source écrit `WALLET_TO_UV`, `UV_TO_WALLET` et
`CONVERSION_CANCEL_IN/OUT`, mais l'export corrigé ne les liste pas dans les
valeurs du select `buma_transaction.type`. Il ne s'agit pas d'une nouvelle
route ni d'une donnée de production : `app/stockage/service.py` les insère
uniquement dans les écritures historiques internes, tandis que les flux
normaux utilisent la validation PPBase. La baseline de schéma reste inchangée.

Le source affecte aussi `cancel_author` et `cancel_date` sur
`buma_conversion`, champs absents de l'export corrigé. C7 conserve l'état
annulé et l'audit auteur/date dans les transactions d'annulation et leurs
timestamps, sans ajouter silencieusement des colonnes.
