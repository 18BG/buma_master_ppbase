# Client — sous-checkpoint C5

## Source couverte

- `src/entries/master.customer.ts` : `POST /master/customer/creates` ;
- `src/entries/master.customer.functions.ts` : validation du prénom, auteur,
  statut actif et attribution séquentielle de `customer_no` ;
- règles de `buma_customer` : permission `customer.create` ou administrateur.

## Portage

- route PPBase `/master/customer/creates` ;
- service `app/customers/service.py` ;
- hook `buma_customer_no` pour les créations natives ;
- transaction atomique pour un batch complet.

## Preuves runtime

- rôle autorisé : HTTP 200, client persisté avec auteur et `customer_no` ;
- prénom vide : HTTP 400, aucune écriture partielle ;
- rôle refusé : HTTP 403 ;
- fixtures et serveur nettoyés après le test.
