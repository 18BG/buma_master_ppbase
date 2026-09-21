# Fournisseur — sous-checkpoint C5

## Source couverte

- `src/entries/supplier.ts` : `POST /api2/supplier/creates` ;
- création liée `buma_person_entity` puis `buma_supplier` ;
- permission `supplier.create` ou administrateur.

## Portage

- route PPBase `/api2/supplier/creates` ;
- service `app/suppliers/service.py` ;
- création de la personne et du fournisseur dans une transaction atomique ;
- réponse legacy `{"message":"ok"}`.

## Preuves runtime

- rôle autorisé : HTTP 200, relation `supplier.person_entity` persistée ;
- valeur `type` invalide : HTTP 400, aucune écriture partielle ;
- rôle refusé : HTTP 403 ;
- fixtures et serveur nettoyés après le test.
