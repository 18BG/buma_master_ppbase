# Wallet et caisse — C8, portage partiel suspendu

## Portage commencé avant C7

Routes déjà portées :

- `/master/wallets/deposit` ;
- `/master/wallets/withdraw` ;
- `/master/wallets/transfer` ;
- `/master/caisse/deposit` ;
- `/master/caisse/withdraw` ;
- `/master/caisse/transfer`.

Les opérations utilisent le coordinateur transactionnel PPBase, verrouillent
les soldes PostgreSQL, créent les transactions liées et refusent les permissions
manquantes.

## Preuves exécutées

- wallet deposit/withdraw/transfer : HTTP 200 ;
- caisse deposit/withdraw/transfer : HTTP 200 ;
- conservation des soldes source/destination vérifiée ;
- permission refusée : HTTP 403 ;
- fixtures et serveur nettoyés.

## Ordre de reprise

C7 article/stock/stockage doit être terminé avant toute nouvelle modification
wallet/caisse. Les éléments ci-dessous restent ensuite à porter avant C8 :

- ouverture/fermeture et demandes de session caisse ;
- annulation wallet/caisse et écritures de compensation ;
- vérification de concurrence et rollback sur ces opérations.
