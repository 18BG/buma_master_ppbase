# Employés, PDV, SIM et agents — C5

## Portage

- employés : `/master/employee/create` et `/api2/employee/create`, création
  atomique `buma_person_entity` → `buma_employee` ;
- PDV/SIM : `/master/pdv/newSim`, création atomique d’une SIM et d’un PDV liés ;
- agents : aucun endpoint métier agent dédié n’existe dans la source gelée ; la
  collection `buma_agent` reste accessible via les routes CRUD natives PPBase,
  avec ses règles de collection et relations existantes.

## Preuves runtime

- employé autorisé : HTTP 200 et relation personne/employé persistée ;
- type employé invalide : HTTP 400 sans écriture partielle ;
- PDV/SIM autorisé : HTTP 200, SIM liée au PDV ;
- permission PDV refusée : HTTP 403 ;
- création agent native authentifiée : HTTP 200 ;
- fixtures et serveur nettoyés après test.
