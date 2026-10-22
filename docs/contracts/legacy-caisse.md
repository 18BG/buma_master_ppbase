# C10 — caisse legacy

Source : `src/entries/caisse.ts` au commit gelé `530018c`. Portage séparé dans
`app/legacy/caisse.py` et `app/hooks/legacy_caisse.py`, sans alias vers C8.

| Méthode / chemin | Contrat conservé |
| --- | --- |
| POST `/api2/caisse/openCaisse` | `data.caisse`, canManage, session sauvegardée avant caisse, 200 `{message:"ok"}` |
| POST `/api2/caisse/reject` | annulation des mouvements sans filtre status, suppressions successives, erreur aval vide 200 |
| POST `/api2/caisse/reject_closing` | REQUEST_CLOSE vers OPEN, succès vide 200 et exception aval vide 200 |
| GET `/api2/caisse/openAgain/:id` | session/caisse CLOSE requises, caisse sauvegardée avant session, reopen=true |
| GET `/api2/caisse/report/:id` | public, `{caisse,report,cashier,transactions}`, dates de session, transactions actives |
| GET `/api2/caisse/rangeReport/:id/:start/:end` | public, `{caisse,transactions}`, dates comparées comme chaînes SQLite |
| GET `/api2/caisse/reportByExpenseType/:id/:start/:end` | authentification sans garde read, `{datas:[{amount,count,label}]}` |
| GET `/api2/caisse/transaction/cancel/:id` | canCancelTransaction ou canManage, annulation et compensations séquentielles |
| GET `/api2/caisse/transaction/cancel2/:id` | isAdmin absent pour non-admin, REVERSAL, status original conservé, répétition non idempotente |
| POST `/api2/caisse/withdrawal` | canManage, 403 `{error:...}`, montant persisté fractionnaire mais soldes tronqués, erreur aval vide 200 |

Le rôle legacy est calculé avant le handler. Les deux rapports publics passent
également par ce calcul si une identité est présente. Une erreur de middleware
n'est pas absorbée par le catch interne du handler.

Les DAO saves restent indépendants. Le compteur wallet vaut toujours 1 à cause
de la requête source `WHERE ORDER BY` invalide. Withdrawal n'ajoute ni state ni
caisseSession au mouvement wallet. Son annulation laisse ce mouvement actif.
Les valeurs historiques REVERSAL et les champs inconnus suivent la projection
du schéma sans nouvelle migration. Chaque suppression et ses relations possède
sa propre transaction PPBase ; aucune transaction ne regroupe le handler.

Les rapports reproduisent `Record.PublicExport` PocketBase 0.22.27
(`models/record.go:518-568`) : email absent si emailVisibility=false, dates
absentes sérialisées en chaîne vide, expand auteur/type de dépense sans garde
CRUD ajouté. Un SUM fractionnaire ne devient pas arbitrairement un entier : le
scan du DynamicModel entier échoue et le handler renvoie 400
`{message:"Error",error:{value:{}}}` (enveloppe du GoError observée).

Preuve locale : `scripts/c10_caisse_runtime.py`, résultat dans
`docs/c10-caisse-runtime-report.json`. Dix routes exercées, permissions,
rapports non vides, effets persistés, répétition cancel/cancel2, rejet d'une
transaction inactive et fautes PostgreSQL injectées sur la caisse et la seconde
transaction de retrait. Fixtures et triggers supprimés en finally.

Complément du 2026-09-24 : `scripts/c10_caisse_reference_runtime.py` a rejoué
les dix handlers sur PocketBase 0.22.27 et PPBase, avec **34 comparaisons égales**
de réponses et d'effets, dont les quatre types d'annulation, rapports avec
expand, dates vides, emailVisibility et erreurs 401/404/SQL. Artefact :
`docs/c10-caisse-reference-report.json`. Les IDs générés et heures d'exécution
sont neutralisés dans la comparaison ; états et relations sont contrôlés.

Le rapport par dépense conserve l'interpolation source (une apostrophe dans la
date produit 400), sans durcissement silencieux. Les formes malformées non
exercées, erreurs moteur exhaustives et concurrence de ces flux séquentiels
restent des limites de preuve. Le SDK a une preuve séparée fichiers/realtime
dans `docs/c10-sdk-runtime-report.json` ; SQL arbitraire reste ouvert.
