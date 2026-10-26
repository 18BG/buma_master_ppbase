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

**Terminé le 2026-09-28 sur décision explicite de l'utilisateur (D-0020)**,
après confirmation que PPBase a été corrigé. Les preuves antérieures sont
conservées ; aucun nouveau test desktop n'est déclaré exécuté dans ce tour.

- [x] Sous-lot SQL : lire et comparer les quatre handlers source sur PocketBase
  0.22.27 jetable, conserver notamment le défaut SQL2 (`200 null`).
- [x] Porter `/sql`, `/api/sql2`, `/master/article/executeSql` et `deleteSql`.
- [x] Vérifier projection, types, mot de passe, validation, mutation de vue,
  suppression de collection et restauration (`scripts/c10_legacy_sql_runtime.py`).
- [x] Acter la décision utilisateur du 2026-09-26 (D-0018) : SQL PostgreSQL
  saisi dans la console executeSql ; étude de `/sql` reportée pour le moment.
  Cette nouvelle autorisation ne rétablit pas l'ancienne clôture D-0017.
- [x] Reconstituer la session SQL interrompue du 2026-09-25 : audit de recherche
  produit, deux requêtes présentes dans l'export/migration identifiées, deux
  essais runtime interrompus par le helper de restauration PocketBase.
  Voir la note de reprise du 2026-09-25 dans le ledger.
- [ ] Reporté par D-0018 : compléter l'audit/parité SQLite de `/sql`, y compris
  sa partie dans `scripts/c10_stored_sql_runtime.py`, si ce périmètre est repris.
  Le script a échoué sur son helper de restauration et son rapport est absent ;
  aucune preuve passante n'est déduite du report. La route reste enregistrée.
- [x] Identifier un appel dans le frontend Master fourni le 2026-09-26 :
  desktop `buma-master-desktop-2.3.1`, console administrateur,
  `POST /master/article/executeSql` avec `{query: editor.text}`, puis lecture
  native de `buma_sql_table`. Preuve statique :
  `docs/c10-desktop-sql-usage-audit.json`. Aucun appel `/sql` identifié dans
  cette copie ; aucune utilisation en production n'est déduite de ce constat.
- [x] Enregistrer l'acceptation finale du périmètre console par l'utilisateur
  le 2026-09-28 (D-0020). Les contrôles supplémentaires précédemment demandés
  ne bloquent plus la clôture. Cette case atteste la décision, pas l'exécution
  d'un nouveau test POST executeSql → GET natif ni d'un test Qt dans ce tour.
- [x] Porter et vérifier les cinq routes `/api2/stockage`, y compris
  stock négatif, sauvegardes partielles et réponses aux erreurs legacy.
- [x] Porter et comparer les routes legacy `/api2/*` restantes : client/PDV,
  achat, vente et retour (`docs/c10-remaining-runtime-report.json`).
- [x] Porter les dix routes `/api2/caisse` et exercer leurs scénarios locaux
  HTTP/PostgreSQL (rapports publics, permissions, sauvegardes partielles,
  annulations et répétitions) : `docs/c10-caisse-runtime-report.json`.
- [x] Comparer les dix routes caisse sur PocketBase/PPBase : 34 comparaisons,
  quatre types d'annulation, rapports et erreurs (`docs/c10-caisse-reference-report.json`).
- [x] Porter et vérifier les hooks natifs de création/mise à jour/suppression
  dans le périmètre de la preuve native ; les cas non exercés restent ouverts.
- [x] Porter et vérifier `/master/employee/payDebt` avec la dette native.
- [x] Porter et comparer les cinq routes master restantes (PDV, pilotage, rupture).
- [x] Exécuter le SDK PocketBase réel sur les deux serveurs : login, refresh,
  CRUD, filtre, expand, upload/download PNG, realtime update et révocation/rétablissement
  du droit de l'abonné (`docs/c10-sdk-runtime-report.json`).
- [x] Corriger 27 alias de neuf vues dont la casse avait été perdue en PostgreSQL ;
  migration `1789236847`, rollback/replay testés et 42 vues vérifiées.
- [x] Rejouer les gates C10 SQL/stockage/hooks/caisse et C8/C9 après les corrections.
- [x] Rechercher rowid dans les sources primaires SQLite/PocketBase et vérifier
  le DDL réel, les trois vues/API, leurs types et règles d'allocation.
- [x] Restaurer les rowid des deux vues et article_id de la troisième :
  migration `1789236848`, 12 comparaisons et 20 insertions concurrentes.
- [x] Clôturer C10 le 2026-09-28 sur décision explicite de l'utilisateur
  (D-0020), dans le périmètre D-0018. Les 131 tests, 106 comparaisons ciblées
  et validations SDK/schéma restent historiques ; `/sql` est reporté.
- [x] Sous-lot hooks natifs : auteur/statut, caisse CLOSE, dette employé,
  protection SIM et neuf gardes suppression (`scripts/c10_native_hooks_runtime.py`).

Point au 2026-09-28 : **C10 terminé ; aucune route active inventoriée ne reste
à porter**. Les 18 dernières routes ont 60 comparaisons PocketBase/PPBase ; la
caisse en ajoute 34 et rowid 12. `return.ts` reste dormant dans les deux bootstraps
et `/api2/test` n'est pas ajouté. L'ancienne clôture conditionnelle D-0017 reste
annulée ; la décision utilisateur D-0020 clôture maintenant le checkpoint.
PostgreSQL pour la saisie administrateur et le report de `/sql` restent D-0018.
Rowid est corrigé. Prochaine étape : poursuivre C11 déjà préparé (section
suivante), avec son blocage D-0019 conservé, sans import de production autorisé.
Les anciens noms C10.1/C10.2/C10.3 sont des sous-lots historiques, pas des étapes
supplémentaires du plan officiel.

## Règle de suivi

**Actualisation du 2026-09-28 :** l'utilisateur confirme PPBase corrigé et
demande la clôture totale de C10. C10 est fermé sous D-0020 ; les notes datées
du 26 septembre ci-dessous sont historiques et ne rétablissent pas son attente.

Rectification dashboard du 2026-09-26 : le formulaire « New users record »
affiche réellement id/tokenKey comme entrées requises. Défaut du dashboard
PPBase confirmé dans record-editor : les métadonnées système de l'API ne sont
pas filtrées avant l'édition. Ces valeurs sont générées côté serveur ; correction
UI encore à faire, sans modification du schéma BUMA. Aucun compte créé par l'agent.
Origine confirmée : édition agent PPBase du 19 septembre, commit `8aec061` du
21 septembre sur `pocketbase-auth-parity-v0404`. L'ajout des métadonnées auth
dans CollectionResponse déclenche le défaut avec un formulaire inchangé.
Comparaison avant/après des trois collections auth :
`docs/c10-auth-dashboard-regression-audit.json`. Correction non appliquée.

Constat accès du 2026-09-26 : serveur BUMA 8091 accessible, un superuser
technique existe déjà (donc pas de lien premier admin), mais users/roles/config
sont vides. L'accès dashboard personnel et l'initialisation explicite des
comptes/configuration métier restent nécessaires avant les tests desktop.

Actualisation du 2026-09-26 : build desktop reçu selon l'utilisateur ; commandes
de démarrage local communiquées (port BUMA proposé 8091, 8090 occupé par Mali
Foot). Aucun démarrage/test effectué par l'agent à ce stade ; C10 reste ouvert
en attente des preuves sur le build, sous responsabilité de la session d'origine.

Coordination du 2026-09-26 : C10 est en attente du build desktop et des tests
réels ; seule la session d'origine assure sa validation/clôture. L'utilisateur
autorise une nouvelle session à préparer C11 malgré C10 ouvert, sans reprendre
C10 et sans import de production ni bascule. La préparation C11 et sa première
répétition synthétique sont documentées ci-dessous ; préserver IDs, relations,
fichiers et rowid. Le backup Master est en téléchargement chez l'utilisateur.

## C11 — préparation autorisée, checkpoint ouvert

Passation/publication du 2026-10-22 : l'utilisateur autorise explicitement le
commit et le push du code, des migrations, tests et rapports locaux sur
`origin/features/auth`. Point d'entrée : `docs/next-agent-handoff.md`. Les
données réelles, `.env`, `pb_data/` et le dépôt PPBase sibling ne font pas
partie de cette livraison. Cette publication n'autorise pas l'import réel
ni la bascule et ne modifie pas les statuts des checkpoints.

- [x] Confirmer source gelée/export corrigé et inventorier les artefacts
  disponibles : `docs/c11-artifact-diagnostic.json`. Aucune autre sauvegarde
  trouvée dans Downloads n'est assimilée à Master ni ouverte.
- [x] Préparer l'inventaire ZIP en lecture seule, sans extraction, et le
  diagnostic du snapshot fermé : empreintes, intégrité, schéma, comptes,
  IDs/rowids, relations, fichiers, valeurs non représentables et doublons C4.
- [x] Préparer la copie privée vérifiée et les primitives d'import : IDs,
  relations, fichiers, auth, dates et rowids d'origine ; aucun hook rejoué,
  aucune donnée normalisée et aucune cible existante vidée par l'importeur.
- [x] Exécuter la répétition synthétique PB 0.22.27 → PostgreSQL jetable :
  neuf records, trois fichiers, rollback avec restauration des 24 seeds,
  concurrence/refus de réimport, contrôles HTTP auth/permissions/list/expand/
  fichiers. Preuves : `docs/c11-rehearsal-report.json`.
- [x] Signaler et bloquer les trois relations `maxSelect: null` vers colonnes
  cibles scalaires : D-0019, régression négative et tests. Aucun correctif de
  domaine/C10 introduit pour contourner ce blocage.
- [x] Recevoir le chemin annoncé du backup Master :
  `/Users/omarsisss/Documents/wproject/protech/master-backup.zip`, communiqué
  par l'utilisateur le 2026-09-28, puis diagnostic expressément autorisé.
- [x] Vérifier les 1 290 CRC, extraire une copie privée et diagnostiquer
  `data.db` en lecture seule : intégrité OK, 114 définitions conformes,
  1 188 247 records. Version **0.22.34 fournie par l'utilisateur**,
  binaire/runtime non vérifiés. ZIP et fichiers extraits inchangés.
  Voir `docs/c11-master-diagnostic.md` et ses rapports.
- [x] Mesurer les anomalies sans les réparer : 5 841 records avec relations
  multiples (D-0019), 877 groupes de clés mobile couvrant 2 391 transactions
  distinctes, 195 795 références sans cible et 2 303 fichiers référencés absents.
  Audit des 191 champs relations et 20 champs fichiers, distinct du premier
  diagnostic limité au premier défaut par record.
- [x] Préparer la passation Git du travail local : note de reprise, chemins
  des dépendances locales, rapports agrégés sans backup/secret, 157 tests
  rejoués et Ruff/format globaux passants le 2026-10-22. Les cinq diagnostics
  statiques C10 historiques sont corrigés sans rejouer leur runtime SQL.
- [x] D-0022 (décision utilisateur du 2026-10-23) : réaligner l'idempotence C4
  sur la source gelée. Migration `1789236849`, diagnostic et vérificateur
  réalignés, 17 tests ajoutés, preuve PostgreSQL jetable (rollback/replay,
  concurrence, C4/C7/C9 rejoués) : `docs/c11-idempotency-runtime-report.json`.
  Les 2 391 transactions historiques deviennent importables sans
  déduplication.
- [x] D-0023 (décision utilisateur du 2026-10-24) : accepter l'ordre `created`
  du lookup sur cet historique (34 groupes sur 877 diffèrent de l'ordre rowid
  SQLite, sans écriture) ; régression dans `tests/test_idempotency.py`.
- [x] Préparer la session serveur : PPBase local commité et poussé (`c6821aa`),
  export configurable (`BUMA_SCHEMA_EXPORT`), plan d'exécution
  `docs/server-session-plan.md` avec les autorisations du 2026-10-24.
- [x] Session serveur du 2026-10-24/25 (`docs/server-session-plan.md`) : environnement,
  source gelée, empreintes, binaires PocketBase 0.22.27 et 0.22.34, paquet extrait et
  contrôlé ; orchestration multi-agents avec relecture indépendante de chaque chantier
  (entrées du ledger du 2026-10-24 et du 2026-10-25).
- [x] Résoudre les incompatibilités d'import mesurées sans altérer l'historique :
  D-0019 résolu sans aplatir aucune liste (D-0025, migration `1789236850`, PPBase
  corrigé) ; version source 0.22.34 validée sur preuve (D-0024) ; importeur avec
  anomalies conservées et recomptées, types cibles contrôlés, COPY vérifié, mode
  manifeste (D-0026, D-0029) ; extraction vérifiée du ZIP complet (D-0028).
- [x] Couvrir tous les records et tables, comptes, paramètres et continuité des
  sessions : import en une transaction de records, comptes et paramètres (D-0026,
  D-0027) ; jetons PocketBase d'avant l'import acceptés après (banc des sessions).
  Sort des autres éléments de l'ancien point : S3 hors périmètre (un S3 activé bloque
  l'import, D-0027) ; aucun champ fichier protégé dans BUMA (D-0037) ; arrêt brutal et
  manque de mémoire traités au runbook (transaction unique, rien d'importé) ; disque
  plein : point ouvert ci-dessous.
- [x] Corriger les failles de sécurité PPBase préexistantes trouvées en chemin : règles
  `users` (D-0030), champs auth cachés et `@request` absents (D-0031), expand et viewRule
  (D-0033), noms de fichiers d'une écriture (D-0038) ; et les parités de réponses : dates
  vides (D-0032), mode d'erreur 0.22 activé par BUMA (D-0034), `totalItems` (D-0035),
  valeurs zéro des NULL (D-0036), fichiers (D-0037), vues (D-0039, migration
  `1789236851`), booléens écrits (D-0042).
- [x] Répétition synthétique complète sur le code final (D-0040) : 84 réponses sur 84
  identiques à PocketBase 0.22.34 en statut et en corps (parmi les éléments comparés (statut, `Content-Type`, corps, forme du jeton ; octets et `Content-Disposition` des fichiers), 75 ne diffèrent que par le `Content-Type` ; en-têtes de sécurité, `Server` et `ETag` non comparés (D-0034, runbook 10.4)) ;
  `docs/c11-synthetic-rehearsal-report.json`.
- [x] Répétition complète sur le vrai backup, sans octets de fichiers, dans un PostgreSQL
  jetable (D-0041) : deux passages passés et identiques ;
  `docs/c11-real-rehearsal-report.json`.
- [x] Runbook de bascule et de retour arrière chronométré par cette répétition :
  `docs/c11-cutover-runbook.md`.
- [ ] Acceptations de l'utilisateur demandées : D-0029, D-0036, D-0039, D-0042.
- [ ] Écarts ouverts de la section 10.4 du runbook corrigés ou décidés avant G0.
- [ ] Import interrompu par un disque plein : non testé.
- [ ] Répétition réelle rejouée au commit de release (C-45 : le rapport publié couvre
  `b7427fe`, et `e1e2cca` puis les corrections du 2026-10-26 changent `scripts/` et `tests/`).
- [ ] Outil d'import de production et autres outils de la section 10.2 du runbook :
  à écrire, relire, tester et faire autoriser (aucun n'existe ; G0 NO-GO d'ici là).
- [ ] Répétition avec le ZIP complet (octets des fichiers), devant l'utilisateur.
- [ ] Tests des clients réels (desktop, web, mobile) sur un staging, dont `isAuthRecord`
  après un refresh PPBase (D-0027) et les valeurs des vues (D-0039).
- [ ] Autorisation distincte d'import de production et de bascule, après les
  gates requises ; **non demandée, non obtenue, non exécutée**.

Guide et commandes : `docs/c11-preparation.md`, `docs/c11-cutover-runbook.md`. État au
2026-10-26 : C11 ouvert, prêt pour la bascule sous conditions : acceptations, écarts de la
section 10.4 corrigés ou décidés, outil d'import de production et autres outils de la
section 10.2 écrits, relus, testés et autorisés, répétition réelle rejouée au commit de
release, répétition avec le ZIP complet, clients réels sur un staging, autorisation
distincte. Les seuls imports du vrai backup ont eu lieu dans des PostgreSQL jetables
détruits ; aucun import de production ni bascule effectué. Prochaine action : soumettre à
l'utilisateur les acceptations D-0029, D-0036, D-0039 et D-0042 et les écarts de la
section 10.4.

Reprise du 2026-10-23 (nouvel agent, analyse seule) : handoff relu, aucun code
modifié, aucune case cochée. Suite locale : 150 tests passants et 7 échecs dus
uniquement au refus macOS de lire l'export dans `~/Downloads`. Constat à
arbitrer avant toute stratégie d'import des doublons D-0021 : les index uniques
C4 (`1789236845`) n'existent ni dans la source gelée, qui n'a que
`UNIQUE system_id LIKE 'idem:%'`, ni dans le schéma de production. Détails et
prochaine action exacte : entrée du 2026-10-23 dans le ledger. Arbitrage du
même jour : l'utilisateur retient l'option (a), réalisée sous D-0022.

Une case ne peut être cochée qu'avec une preuve de test et un artefact associé.
Les lots métier restent bloqués jusqu'à validation du schéma, de l'auth et de
la couche transactionnelle.

Tout agent doit mettre à jour cette tasklist et `docs/progress/ledger.md` avant
de terminer sa session, y compris en cas de blocage.
