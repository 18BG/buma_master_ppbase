# Tasklist de migration

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

## Règle de suivi

Une case ne peut être cochée qu'avec une preuve de test et un artefact associé.
Les lots métier restent bloqués jusqu'à validation du schéma, de l'auth et de
la couche transactionnelle.

Tout agent doit mettre à jour cette tasklist et `docs/progress/ledger.md` avant
de terminer sa session, y compris en cas de blocage.
