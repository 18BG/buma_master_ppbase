# Invariants transactionnels à préserver

Ces invariants sont les critères métier des lots financiers. Ils doivent être
convertis en tests avant de marquer les lots concernés comme terminés.

## Caisse

- Une opération multi-écritures modifie la caisse, la session, le client, le
  wallet, le stock et/ou les transactions dans une transaction atomique.
- Une exception ne doit laisser aucun sous-effet persistant.
- `previous_*_balance` et `after_*_balance` doivent correspondre aux valeurs
  réellement observées dans la transaction.
- Les états de caisse/session (`OPEN`, `CLOSE`, `REQUEST_OPEN`,
  `REQUEST_CLOSE`, `PENDING`) doivent rester compatibles.
- Les annulations ne doivent pas créer de double débit ou de double crédit.

## Wallet

- Un transfert conserve le total des soldes source + destination.
- Les écritures liées et leurs numéros doivent être atomiques.
- Une clé idempotente ne doit jamais exécuter deux fois le même transfert.

## Stock, vente, achat et retour

- Les quantités de stock, vente, achat, retour et transactions restent
  cohérentes après succès, annulation et retry.
- Une même requête `system_id`/`mobile_message_id` ne doit pas doubler le débit,
  le crédit ou le mouvement de stock.
- Les relations historiques et les IDs source sont conservés.

## Concurrence

- Deux demandes concurrentes avec la même clé idempotente produisent une seule
  opération observable.
- Deux demandes concurrentes sur un solde ne perdent aucune mise à jour.
- Les compteurs `invoice_no` et `transaction_no` ne produisent pas de doublons.

## Validation

Chaque invariant doit avoir un test unitaire ou d'intégration identifié dans
`docs/test-matrix.md`, puis un scénario HTTP sur PostgreSQL avant staging.
