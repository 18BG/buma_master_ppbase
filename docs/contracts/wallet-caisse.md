# Contrat C8 — wallet et caisse

Source gelée : `master.wallet.ts`, `master.wallet.functions.ts`,
`master.caisse.ts`, `master.caisse.functions.ts` et
`master.caisse.functions2.ts`.

## Périmètre C8 porté

Wallet :

- `/master/wallets/deposit` ;
- `/master/wallets/withdraw` ;
- `/master/wallets/transfer` ;
- `/master/wallets/cancel_transaction` ;
- `/master/wallets/updateNote`.

Caisse et sessions :

- `/master/caisse/requestOpen`, `openCaisse`, `requestClose`, `closeCaisse` ;
- `/master/caisse/deposit`, `withdraw`, `transfer`, `cancel` ;
- `/master/caisse/new_correction`, `new_versement`, `expense` ;
- `/master/caisse/bank_versement`, `cancel_bank_versement` ;
- `/master/caisse/unallocate`, `updateAmount`,
  `changeTransactionCustomer`, `updateNote`, `changeCashier` ;
- `/master/caisse/expense_type/archive`, `verifySession`,
  `employee_debt`, `employee_debt_repayment`, `accessible`.

Les opérations de solde et de session utilisent une transaction PPBase unique,
`FOR UPDATE` sur les caisses/wallets/sessions, des verrous advisory pour les
numéros de transaction et des contrôles de doublons sur les fenêtres définies
par la source. Les annulations rétablissent les soldes et marquent les lignes
liées comme annulées.

## Routes volontairement réservées à C9

Les routes caisse qui dépendent des domaines vente/retour/rapports restent dans
C9 : `/master/caisse/return`, `/master/caisse/versement_uv`,
`/master/caisse/add_payment_to_sale`, `reportByExpenseType` et
`reportByCaisseType`. Elles ne sont pas comptées comme C8 terminé.

## Preuve runtime

`scripts/c8_finance_runtime.py` vérifie sur PostgreSQL isolé : login/401,
wallet CRUD, transfert et annulation, notes, ouverture/fermeture de sessions,
versement/dépense/versement bancaire, annulation, correction, changement de
client, dettes employé, accès, vérification de session, concurrence et
nettoyage complet des fixtures.

## Comportement source conservé sans correction

Le TypeScript envoie aussi des clés non déclarées par le schéma exporté. Le
portage conserve le même payload logique, sans modifier PocketBase, le dépôt
TypeScript ou la baseline. Les champs réellement déclarés sont persistés ; les
clés absentes gardent le comportement d'un champ inconnu. La valeur historique
`DEBT` est écrite telle quelle par le flux source, sans la convertir en
`PAYMENT`.
