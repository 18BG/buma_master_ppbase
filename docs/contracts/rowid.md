# rowid : provenance, preuve PocketBase et traduction PostgreSQL

Vérification du 2026-09-24, source BUMA gelée
`530018cd20c2acbe88f38b3639e500b5271e2eb2`, export corrigé SHA-256
`9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`.

## Où BUMA l'utilise

Dans **le dépôt TypeScript**, la migration
`../buma_master_hooks/migrationsDir/1774706510_updated_buma_stock_transaction_list_view.js:7`
définit la vue `h4rh3uyxsbaqqji` / `buma_stock_transaction_list_view` :

```sql
SELECT t.id, a.rowid AS article_id, t.article, ...
FROM buma_transaction AS t
INNER JOIN buma_article AS a ON a.id = t.article
```

La même expression figure dans des migrations antérieures. Dans `src/entries`,
les seules occurrences littérales rowid sont les types optionnels de
`master.tables.types.ts:1013` et `:1450`. Aucun handler métier gelé ne lit
explicitement ce champ.

Dans **l'export de schéma fourni**, deux autres vues projettent directement
`a.rowid, a.id` :

| Vue | Table de `a` | Champ public |
| --- | --- | --- |
| buma_article_with_no_stock_list_view | buma_article | rowid |
| buma_stockage_no_manager | users | rowid |
| buma_stock_transaction_list_view | buma_article | article_id |

Ces vues sont exposées par l'API native `/api/collections/<vue>/records`.
Les deux premières définitions proviennent de l'export, pas d'une fonction
TypeScript retrouvée dans ce dépôt. Cette distinction est conservée dans le suivi.

## Ce que SQLite et PocketBase établissent

Sources primaires consultées :

- SQLite, [Rowid Tables](https://www.sqlite.org/rowidtable.html) : entier signé
  64 bits, unique/non nul, pour une table ordinaire sans WITHOUT ROWID. Un champ
  explicitement nommé rowid peut masquer ce nom ; INTEGER PRIMARY KEY peut en
  être un alias. Ces exceptions doivent être vérifiées avant toute conclusion.
- SQLite, [Autoincrement](https://www.sqlite.org/autoinc.html) : sans
  AUTOINCREMENT, allocation habituelle max(rowid)+1, départ à 1, réutilisation
  possible après suppression du maximum ; limite 9223372036854775807 puis
  recherche d'un entier positif libre. Une transaction annulée ne réserve pas
  son identifiant.
- PocketBase **v0.22.27**,
  [record_table_sync.go:26](https://github.com/pocketbase/pocketbase/blob/v0.22.27/daos/record_table_sync.go#L26) :
  `id TEXT PRIMARY KEY ...`, sans WITHOUT ROWID et sans INTEGER PRIMARY KEY.
- PocketBase,
  [base.go](https://github.com/pocketbase/pocketbase/blob/v0.22.27/daos/base.go) :
  les sauvegardes utilisent INSERT/UPDATE ; le Record n'ajoute pas un champ rowid
  dans sa ColumnValueMap.
- PocketBase,
  [schema_field.go:312](https://github.com/pocketbase/pocketbase/blob/v0.22.27/models/schema/schema_field.go#L312)
  et [record.go:518](https://github.com/pocketbase/pocketbase/blob/v0.22.27/models/record.go#L518) :
  PrepareValue/PublicExport déterminent la sérialisation. Le type de champ
  **JSON dans la vue n'implique pas une valeur objet** : rowid est rendu comme
  un nombre JSON. Les dates système absentes d'une vue ne sont pas exportées.

Le DDL réel a été lu dans une base **créée par PocketBase**, avec l'export
exact. `users` et `buma_article` n'ont aucun champ déclaré nommé rowid et ne
sont pas WITHOUT ROWID. Le moteur de cette référence indique **SQLite 3.46.0**
via sqlite_version() ; la version du module sqlite3 utilisé pour lire le fichier
n'est pas utilisée comme version du serveur.

PocketBase utilise aussi strftime dans ses defaults internes created/updated.
Cela appartient au stockage du framework ; aucune requête BUMA gelée ni vue
exportée n'appelle strftime. PPBase possède déjà ses timestamps PostgreSQL.

## Valeurs mesurées

Artefacts : `docs/c10-rowid-reference-research.json` puis
`docs/c10-rowid-runtime-report.json`.

- Deux créations article : rowid 1 puis 2 ; leurs `id` restent des chaînes de
  15 caractères différentes de ces nombres.
- UPDATE du second article : rowid reste 2.
- Suppression du premier puis création : 3, le trou 1 n'est pas comblé.
- Suppression du maximum 3 puis création : 3 est réutilisé.
- INSERT avec rowid 4 annulé : la création suivante reçoit 4.
- Les APIs des vues retournent `rowid:2`, `rowid:1` (utilisateur) et
  `article_id:2` (transaction liée à l'article).
- La réponse native de buma_article ne contient pas rowid. Un champ rowid
  fourni au CRUD natif est ignoré, comme un champ hors schéma.
- Après affectation SQL explicite du maximum signé, une insertion reçoit un
  entier positif libre ; sa valeur aléatoire exacte n'est pas comparée.

La documentation SQLite précise que des opérations de stockage comme VACUUM
peuvent changer des rowids sans INTEGER PRIMARY KEY. Ce n'est donc pas une
garantie d'identité métier persistante. Aucun VACUUM de production n'a été lancé.

## Correction du portage

La migration `1789236848_c10_sqlite_rowid.py` ajoute un entier interne `rowid`
à users/buma_article et restaure les **expressions source a.rowid**, sans les
remplacer par id, ctid ou un rang recalculé. Un trigger utilise les lignes
committées, avec verrou transactionnel, afin de reproduire réutilisation et
rollback ; une séquence PostgreSQL ne donnerait pas ces propriétés.

Le champ n'est pas ajouté au schéma public des collections et est exclu du
contexte auth. L'inférence JSON de PocketBase reste dans les métadonnées des
vues ; le résultat SQL est un bigint, permettant les comparaisons numériques
natives. `app/legacy/views.py` reproduit PublicExport pour ces trois vues,
y compris dates absentes et valeurs typées après LEFT JOIN.

La migration refuse de deviner les rowids de lignes existantes. Les tables
locales étaient vides avant application. En C11, l'import devra copier
explicitement `SELECT rowid, id, ...` depuis le snapshot SQLite. Le rollback
refuse également de supprimer des rowids lorsqu'il reste des données.

Validation : 12 comparaisons PocketBase/PPBase, réponses complètes des trois
vues, filters/fields, contrôle du champ caché, max/rollback/update/delete,
20 insertions PostgreSQL concurrentes sans collision, rollback/replay de la
migration et nettoyage. Le vérificateur de schéma ne tolère plus les deux
projections manquantes sous forme d'exception.

Reproduction : préparer une référence avec
`scripts/c10_prepare_reference.py --rowid-only`, lancer PocketBase 0.22.27 sur
8099 avec les trois dossiers produits, puis exécuter
`scripts/c10_rowid_runtime.py --reference-data <dossier>/data/data.db` face au
PPBase local 8090. Ces tests exigent users/article vides sur la cible isolée.
La version exacte actuellement déployée en production reste à identifier en C11.
