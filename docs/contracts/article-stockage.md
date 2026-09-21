# Contrat C7 — article, stock et stockage

Source gelée : `buma_master_hooks@530018c`, principalement
`src/entries/master.article.ts`, `master.article.functions.ts`,
`master.stockage.ts` et `master.stockage.functions.ts`.

Routes portées dans `app/hooks/stockage.py` :

- `POST /master/article/creates` — permission `article.create` ou admin ;
  chaque article crée aussi une ligne `buma_article_stock` à quantité zéro
  dans `STOCK_PRINCIPAL`.
- `POST /master/stockage/movement` — permission `stockage.input_output` ou
  admin ; entrée/sortie atomique et refus d'une sortie supérieure au stock.
- `POST /master/stockage/batch-movement` — même permission ; tous les
  mouvements et leurs `buma_transaction` sont dans une transaction unique.
- `POST /master/stockage/transfer` et `cancel_transfer` — permissions
  `stockage.transfer` et `stockage.cancel_transfer` ; les deux stocks, le
  transfert et les transactions liées sont verrouillés et atomiques.
- `POST /master/stockage/convert` et `cancel_conversion` — permissions
  `stockage.conversion` et `stockage.cancel_conversion` ; idempotence par
  `system_id`/`mobile_message_id`, verrouillage des deux stocks et rollback.
- `POST /master/stockage/wallet_uv` et `uv_wallet` — permissions
  `wallet.wallet_uv` et `wallet.uv_wallet` ; le solde wallet et la quantité
  UV sont modifiés dans la même transaction.

La preuve runtime est `scripts/c7_stockage_runtime.py`. Elle couvre création,
entrée/sortie, rollback batch, transfert, conversion et retry idempotent,
annulations, pont wallet/UV, 401/403, concurrence et nettoyage des fixtures
sur PostgreSQL isolé.

## Divergences documentées

L'export corrigé ne contient pas dans les valeurs du champ select
`buma_transaction.type` les valeurs historiques `WALLET_TO_UV`, `UV_TO_WALLET`
et `CONVERSION_CANCEL_IN/OUT`, alors que le TypeScript les écrit. Il n'existe
pas de contrainte PostgreSQL empêchant ces valeurs ; PPBase les conserve par une
insertion SQL interne limitée à ces écritures historiques. Les opérations
normales continuent d'utiliser `create_record` et la validation du schéma.

Le schéma corrigé ne possède pas non plus `buma_conversion.cancel_author` ni
`cancel_date`, malgré les affectations du TypeScript. PPBase marque donc la
conversion inactive et conserve l'auteur/date dans les transactions d'audit et
leurs timestamps, sans ajouter de colonnes à la baseline officielle.

