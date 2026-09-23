# Tasklist de migration

## Règle de parité pour toutes les cases

Une case métier n'est terminée que si la route ou le hook garde le contrat
PocketBase observable : chemin/méthode, payload, auth et permissions, code HTTP,
content type, enveloppe/message/champs de réponse, effets persistés, hooks,
rollback, concurrence et idempotence applicables. Les opérations SDK/CRUD
utilisées par les apps doivent également être vérifiées. Un HTTP 200 seul ne
valide jamais une case ; tout écart doit être documenté et testé.

## Foundation

- [x] Choisir le dossier `buma-master-ppbase`.
- [x] Figer la source sur `fix/uv-paani-sup@530018c`.
- [x] Créer le registre PPBase minimal.
- [x] Ajouter health check et POC de route.
- [x] Ajouter PostgreSQL Docker isolé.
- [x] Ajouter tests de structure foundation.
- [x] Ajouter le POC de hook create, route et transaction technique.
- [x] Retirer la migration technique `foundation_poc` du répertoire officiel avant le replay final sur base vide.
- [x] Simplifier l'exécution locale avec `.env`, `make migrate-up` et `make verify-all` sans options répétées.
- [x] Exécuter les tests statiques du foundation avec PPBase 0.3.0 local.
- [x] Exécuter le POC HTTP contre PostgreSQL avec l'environnement PPBase local validé.
- [x] Vérifier une transaction à deux écritures et un rollback forcé (3 records avant / 3 après).
- [x] Recevoir et analyser l'export JSON corrigé PocketBase (114 collections : 70 base, 2 auth, 42 vues).
- [x] Ajouter l'analyse de réconciliation entre schema final et `migrationsDir` (`docs/contracts/schema-migration-reconciliation.json`).
- [x] Supprimer l'ancien draft de baseline ; seul l'export corrigé est utilisé.
- [x] Générer et valider la baseline native PPBase officielle `base/auth` depuis l'export corrigé (`pb_migrations/1789236840_buma_master_schema_baseline.py`).
- [x] Matérialiser les champs système d'authentification `users.username` et `agent_users.username` pour la compatibilité PPBase (`pb_migrations/1789236841_users_username_compat.py`, `pb_migrations/1789236843_agent_users_username_compat.py`).
- [x] Traduire et valider les 42 vues PostgreSQL ; migration officielle `pb_migrations/1789236842_buma_master_views.py`, rapport `docs/view-verification-report.json` : 42 OK, 0 échec.
- [x] Vérifier le schéma PPBase complet : 114 définitions attendues (70 `base`, 2 `auth`, 42 `view`) sur une base PostgreSQL jetable (`docs/complete-schema-verification-report.json`).
- [ ] Ajouter le snapshot Dashboard du schéma source.
- [ ] Ajouter le backup source et ses checksums.

## C3 — Authentification et permissions

- [x] Analyser les flux source `users`, `agent_users`, `roles`,
  `buma_apps_roles`, `/isConnected`, `/master/user/health` et les guards
  `requireAdminOrRecordAuth`.
- [x] Écrire les tests de contrat des routes, JWT, 401/403 et permissions.
- [x] Porter les routes legacy et l'identité username/email des deux
  collections auth.
- [x] Porter le contexte live `users.role -> roles.permission` et les actions
  admin `buma_apps_roles.info.actions` dans les règles PPBase.
- [x] Valider login, JWT, refresh, owner rule `agent_users`, 401/403 et
  rollback de permission sur PostgreSQL isolé avec fixtures synthétiques.
- [x] Corriger la longueur minimale effective de création/mise à jour du mot de
  passe `users` dans le dépôt PPBase editable : le schéma et le runtime
  utilisent maintenant `password.min=5` (`agent_users=8`).
- [x] Vérifier la correction avec le dépôt PPBase editable réellement chargé par
  BUMA : création/mise à jour runtime 5/4 et 8/7 conformes.
- [x] Vérifier statiquement la correction PPBase editable : conversion legacy
  `minPasswordLength` vers `password.min` présente, tests PPBase ciblés et
  suite BUMA locale passants ; une régression couvre aussi une collection dont
  des champs métier précèdent le champ système `password`.
- [x] Fermer C3 après la dernière revalidation runtime de la matrice des
  permissions avec un token `type=auth`. C4 peut commencer ; les domaines
  métier restent soumis à la validation transactionnelle C4.

## C4 — Transactions et idempotence

- [x] Inventorier `master.idempotency.ts`, les clés `system_id`/
  `mobile_message_id` et les usages transactionnels source.
- [x] Porter la normalisation et la recherche idempotente dans
  `app/transactions/idempotency.py`.
- [x] Ajouter les index uniques partiels PostgreSQL bornés par `type` et
  `author` (`pb_migrations/1789236845_c4_transaction_idempotency.py`).
- [x] Ajouter le verrou advisory transactionnel par clé idempotente.
- [x] Vérifier rollback, persistance unique, concurrence et lookup sur
  PostgreSQL avec fixtures synthétiques (`scripts/c4_idempotency_runtime.py`).
- [x] Fermer la fondation C4 ; les flux métier vente/retour/stock/wallet restent
  les checkpoints suivants.

## C5 — Settings et référentiels

- [x] Porter la lecture stricte de `buma_config.CONFIGURATION_1`.
- [x] Porter les defaults explicites de `master.setup.ts` : stockage principal,
  moyens de paiement, client/fournisseur divers et 18 rôles applicatifs.
- [x] Rendre le seed idempotent sans supprimer les données existantes.
- [x] Vérifier persistance, second passage, erreur settings absent et intégrité
  du schéma PostgreSQL.
- [x] Fermer le checkpoint C5 settings/référentiels.

## C6 — Clients, fournisseurs, employés, PDV, SIM et agents

### C6.1 — Clients

- [x] Porter `POST /master/customer/creates` et la validation du prénom.
- [x] Porter l’auteur, le statut actif et l’attribution de `customer_no`.
- [x] Ajouter transaction batch, permission `customer.create` et hook de
  numérotation native.
- [x] Vérifier HTTP 200/400/403, persistance et rollback du batch.
- [x] Porter les clients.

### C6.2 — Fournisseurs

- [x] Porter `POST /api2/supplier/creates` et la réponse legacy `{"message":"ok"}`.
- [x] Porter la création liée `buma_person_entity` → `buma_supplier`.
- [x] Ajouter permission `supplier.create`/admin et transaction atomique.
- [x] Vérifier HTTP 200/400/403, relation persistée et rollback batch.
- [x] Porter les fournisseurs.

### C6.3 — Employés, PDV, SIM et agents

- [x] Porter `/master/employee/create` et `/api2/employee/create` avec relation
  `buma_person_entity` → `buma_employee`.
- [x] Porter `/master/pdv/newSim` avec création SIM/PDV atomique et validation
  de l’unicité SIM.
- [x] Vérifier la création native authentifiée de `buma_agent` ; aucun endpoint
  métier agent supplémentaire n’existe dans la source gelée.
- [x] Vérifier HTTP 200/400/403, relations, rollback et nettoyage PostgreSQL.
- [x] Fermer le checkpoint C6 complet.

## C7 — Article, stock et stockage

- [x] Porter les routes article, stock et stockage source : article creation,
  mouvements unitaires/en lot, transferts, conversions, annulations et ponts
  wallet/UV.
- [x] Vérifier quantités, permissions, relations, rollback, idempotence et
  concurrence sur PostgreSQL isolé (`scripts/c7_stockage_runtime.py`).
- [x] Fermer C7 avant de reprendre l'ouverture/fermeture de caisse et les
  compensations C8.

## C8 — Wallet et caisse

- [x] Porter provisoirement wallet deposit, withdraw et transfer.
- [x] Porter provisoirement caisse deposit, withdraw et transfer.
- [x] Ajouter provisoirement verrouillage des soldes et transaction atomique des écritures
  liées.
- [x] Vérification provisoire HTTP 200/403 et conservation des soldes.
- [x] Porter ouverture/fermeture de caisse, demandes de session et annulations.
- [x] Reprendre et fermer C8 après C7, avec rollback/concurrence des sessions et
  compensations.
- [x] Porter dans C8 les autres routes caisse internes : correction, versement,
  dépense, versement bancaire/annulation, désallocation, modification
  montant/client/note, changement de caissier, dette employé, vérification de
  session, type de dépense et accès.
- [x] Conserver les payloads source sans corriger le schéma PocketBase : champs
  inconnus inchangés et valeur `DEBT` non normalisée.
- [x] Laisser explicitement en C9 les routes caisse transverses
  `/return`, `/versement_uv`, `/add_payment_to_sale` et les deux rapports,
  car elles appartiennent aux familles retour/paiement/vente/rapports.

## C9 — Vente, recharge, achat, paiement, retour et rapports

Portage et gate locale terminés le 2026-09-23 pour les 29 routes et les preuves
listées dans `docs/c9-runtime-report.json`. D-0009 a été corrigé après clarification utilisateur : les sauvegardes
partielles des flux vente/achat sont maintenant reproduites et testées. Cette
clôture ne vaut pas une preuve de parité absolue ni autorisation de cutover.

- [x] Retrouver le point d'arrêt et inventorier les 29 routes attendues.
- [x] Valider vente/recharge et annulations, les cinq routes achat et la
  création/annulation retour avec réponses et effets PostgreSQL.
- [x] Porter les quatre routes retour : update, updatePDV, info, return_stats.
- [x] Porter les neuf routes restantes vente/recharge/statistiques/rapports.
- [x] Porter les cinq routes caisse transverses réservées depuis C8.
- [x] Vérifier PAANI/supplément agent, NAFAMA, snapshots, antidatage,
  création d'identités prévue par la source et statuts legacy.
- [x] Exécuter les contrats HTTP (69 tests locaux au total), la gate C9 avec
  erreurs partielles source, rollback des vraies transactions,
  idempotence/concurrence, permissions et CRUD natif applicable.
- [x] Vérifier le nettoyage des fixtures, l'absence de régression C8 et le
  maintien des 7 migrations, 114 définitions et 42 vues.
- [x] Documenter les limites et fermer le périmètre C9 testé ; prochaine
  famille d'implémentation : C10.
- [x] Corriger D-0009 : conserver l’ordre et les effets partiels des sauvegardes
  legacy ; régression vente tardive, wallet manquant et réception sans article.
- [ ] Compléter avant cutover les preuves PocketBase/PPBase comparées, SDK
  files/realtime et règles natives non testées.

## C10 — Compatibilité legacy

- [x] Sous-lot SQL : lire et comparer les quatre handlers source sur PocketBase
  0.22.27 jetable, conserver notamment le défaut SQL2 (`200 null`).
- [x] Porter `/sql`, `/api/sql2`, `/master/article/executeSql` et `deleteSql`.
- [x] Vérifier projection, types, mot de passe, validation, mutation de vue,
  suppression de collection et restauration (`scripts/c10_legacy_sql_runtime.py`).
- [ ] Compléter la parité des dialectes SQL/erreurs/types non couverts.
- [x] Porter et vérifier les cinq routes `/api2/stockage` (C10.2), y compris
  stock négatif, sauvegardes partielles et réponses aux erreurs legacy.
- [ ] Porter et vérifier les routes legacy `/api2/*` encore absentes.
- [x] Porter et vérifier les hooks natifs de création/mise à jour/suppression
  dans le périmètre de la preuve C10.3 ; les cas non exercés restent ouverts.
- [x] Porter et vérifier `/master/employee/payDebt` avec la dette native C10.3.
- [ ] Porter les routes master restantes (PDV, pilotage, rupture).
- [ ] Exécuter la gate C10 globale avant clôture.
- [x] Sous-lot hooks natifs : auteur/statut, caisse CLOSE, dette employé,
  protection SIM et neuf gardes suppression (`scripts/c10_native_hooks_runtime.py`).

## Règle de suivi

Une case ne peut être cochée qu'avec une preuve de test et un artefact associé.
Les lots métier restent bloqués jusqu'à validation du schéma, de l'auth et de
la couche transactionnelle.

Tout agent doit mettre à jour cette tasklist et `docs/progress/ledger.md` avant
de terminer sa session, y compris en cas de blocage.
