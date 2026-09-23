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

## Gate C4 — fondation transactionnelle

Le helper source `master.idempotency.ts` est porté dans
`app/transactions/idempotency.py` : normalisation de clé
`idem:<operation>:<key>`, recherche par `system_id`, ancienne clé puis
`mobile_message_id`, toujours bornée par `type` et `author`.

La migration `pb_migrations/1789236845_c4_transaction_idempotency.py` ajoute
deux index uniques partiels PostgreSQL sur `buma_transaction` afin de ne pas
interdire les écritures legacy sans clé :

- `(type, author, system_id)` lorsque `system_id <> ''` ;
- `(type, author, mobile_message_id)` lorsque `mobile_message_id <> ''`.

Preuve runtime `scripts/c4_idempotency_runtime.py` via le coordinateur de
transaction PPBase : rollback forcé sans ligne persistée, deux insertions
concurrentes avec une seule ligne gagnante, lookup du gagnant par la clé
idempotente, fallback par ancienne clé et `mobile_message_id`, puis nettoyage
des fixtures. Le verrou advisory transactionnel `pg_advisory_xact_lock`
sérialise les tentatives d’une même clé.

Cette gate ne porte pas encore les flux métier vente, retour, conversion ou
wallet ; ils restent les checkpoints métier suivants.

## Gate C7 — stock et stockage

Le service `app/stockage/service.py` porte les mouvements, transferts,
conversions et ponts wallet/UV dans `run_record_storage_transaction`. Les
stocks sont lus sous `SELECT ... FOR UPDATE`, les paires stockage/article
utilisent un verrou advisory pour éviter la création concurrente de doublons,
et les conversions réutilisent le verrou idempotent C4.

La preuve `scripts/c7_stockage_runtime.py` démontre : batch rejeté sans
écriture partielle, transfert et annulation avec quantités restaurées,
conversion et retry sans double débit, sortie concurrente avec une seule
réussite, pont wallet/UV cohérent et nettoyage complet.

## Gate C9 — commerce

La preuve runtime C9 vérifie un seul effet stock/dette pour les retries et
requêtes concurrentes recharge/retour, et un seul paiement accepté quand deux
requêtes concurrentes tentent de solder la même vente. Un retour groupé qui
contient un retour déjà payé ne persiste aucun sous-effet.

La faute de stockage injectée après un premier article vérifie le rollback
de l'agrégat vente, stock et historique. Cette atomicité est une différence
explicite vis-à-vis des anciennes fonctions vente/achat sans transaction
englobante, voir D-0009. Les contrôles ne normalisent pas les effets legacy :
annuler une vente laisse ses transactions caisse actives ; annuler un retour
ne diminue pas `due` et ne désactive pas sa transaction initiale.


Actualisation D-0009 (2026-09-23) : l'atomicité ajoutée aux fonctions vente/achat
legacy non transactionnelles a été retirée. Les tests exigent maintenant la
persistance des sauvegardes antérieures à l'échec, dans l'ordre TypeScript.
Les mentions précédentes d'un rollback global conservé comme divergence sont
supplantées par cette correction. Les vraies transactions source restent
atomiques et leur preuve reste applicable.
