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

## D-0008 — C8, ne pas corriger une divergence source/PocketBase

Le code source envoie certaines clés qui ne figurent pas dans le schéma
exporté. Le portage ne corrige ni la source ni PocketBase et n'ajoute aucune
colonne : il conserve les mêmes valeurs et le comportement des champs inconnus.
En particulier, `DEBT` n'est pas transformé en `PAYMENT`.

## D-0009 — C9 : différence d'atomicité corrigée

La différence introduite par le portage (rollback global ajouté aux fonctions
vente/achat non transactionnelles) est refusée par la clarification utilisateur
du 2026-09-23 : « on ne fait que réécrire même s'il y a des problèmes dans
l'ancien backend ». Elle n'est plus conservée comme limite de cutover.

`app/commerce/sequential.py` reproduit désormais les sauvegardes indépendantes
et leur ordre. Une erreur tardive laisse les effets déjà sauvegardés, notamment
vente/dette/stock avant une transaction d'article échouée, débit fournisseur
avant rejet wallet et achat reçu avant échec de lecture des articles.
Le script C9 contient les régressions HTTP/PostgreSQL correspondantes.
Les fonctions dont la source possède une transaction la conservent.

Les valeurs historiques `AUTRES`/`DEBT`, les champs source inconnus et le type
booléen exporté de `buma_buy.received_date` restent préservés sans migration.
Aucune amélioration du backend source n'est autorisée par la seule réécriture.

La comparaison de deux serveurs PocketBase/PPBase, files/realtime et les textes
de toutes les erreurs moteur ne sont pas prouvés par la gate locale.

## D-0010 — C10 SQL : reproduire les accès et les défauts source

Les quatre routes du sous-lot SQL gardent les gardes de la source : mot de
passe littéral pour `/sql`, aucune authentification ajoutée aux deux routes
article SQL. Le portage appelle les opérations de collection PPBase pour
modifier une vue ou supprimer sa collection, jamais un simple DROP laissant
des métadonnées orphelines.

Le handler `/api/sql2` est défectueux dans le code gelé. Sur PocketBase 0.22.27,
son callback async `(req,res)` produit HTTP 200 JSON `null` et n'exécute pas le
SQL fourni. Ce comportement est conservé et testé ; aucun service SQL
fonctionnel n'est substitué à cette route.

Les quatre routes sont vérifiées dans un environnement jetable, avec
restauration de la vue et suppression de la seule collection de test. Les
différences de dialecte SQL, textes moteur et inférences de schéma non
couvertes restent ouvertes ; ce sous-lot ne ferme pas C10.

## D-0011 — C10 stockage legacy : défauts conservés

Le sous-lot `/api2/stockage` ne réutilise pas automatiquement les services C7
transactionnels. Il conserve les sauvegardes séquentielles du source, le stock
négatif de transfert, l'absence de garde métier sur transfer/remove, le statut
false implicite des lignes add_article, l'erreur de paramètre `stock_info` et le
200 `okay` après exception de conversion. Les effets et leurs régressions sont
portés dans `app/legacy/stockage.py` et `scripts/c10_stockage_runtime.py`.

## D-0012 — C10 hooks natifs : ordre et gardes source

Les hooks natifs sont appliqués au chemin CRUD HTTP, pas aux écritures internes
des services, comme le distingue le POC C1. L'ordre source est conservé :
les défauts auteur/statut précèdent l'écriture, la dette employé débite
l'employé avant la dette, et les gardes de suppression/mise à jour lèvent leur
erreur avant le CRUD natif. Aucun garde supplémentaire n'est ajouté à un champ
ou à une route qui n'en possédait pas. La validation PocketBase de longueur
des IDs fournis par CRUD est reproduite avant les hooks métier ; les IDs
internes générés ne sont pas concernés.

Actualisation D-0012 : la preuve PocketBase 0.22.27 montre que le formulaire
CRUD natif englobe le hook dette dans sa transaction. Le portage utilise donc
`event.engine` pour ce débit : il est annulé si l'INSERT dette échoue. La
validation de longueur d'ID intervient avant les hooks métier. Cette correction
est couverte par les scénarios natifs ID invalide et trigger d'échec.

Complément D-0009 : la préparation SIM/PDV/client de `newReturn` est également
séquentielle, avant la transaction stock/retour. Un échec du PDV laisse le
client créé ; la régression C9 injecte cette faute et contrôle la persistance.
