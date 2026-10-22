# C10 — client/PDV, commerce et master restants

Source gelée `530018cd20c2acbe88f38b3639e500b5271e2eb2`. Les 18 méthodes/chemins
restants sont maintenant enregistrés et ont une preuve comparée PocketBase
0.22.27 / PPBase : `docs/c10-remaining-runtime-report.json`, 60 comparaisons
de réponses et d'effets persistés, sans écart dans les scénarios exercés.

| Source | Méthodes / chemins |
| --- | --- |
| customer.ts | POST `/api2/customer/create`, `/api2/pvd/import`, `/api2/pvd/importSim` ; GET `/api2/customer/importPDVFromAgent/:agentID/:customerID` |
| purchase.ts | POST `/api2/purchase/add`, `/api2/purchase/cancel`, `/api2/purchase/infos`, `/api2/purchase/payment/add` |
| sale.ts | POST `/api2/sale/cancel`, `/api2/sale/infos`, `/api2/sale/payment/add`, `/api2/return`, `/api2/return/confirm` |
| master.pdv.ts | POST `/master/pdv/turnovers`, `/master/pdv/assign`, `/master/pdv/unassign` |
| master.pilotage.ts | GET `/master/pilotage/date-report/:startDate` |
| master.rupture.ts | POST `/save-rupture` |

Les modules `app/legacy/customer.py`, `commerce.py`, `master.py` et `records.py`
reproduisent les sauvegardes DAO indépendantes et la projection des champs
internes. Les hooks HTTP sont `legacy_people`, `legacy_commerce`, `legacy_master`.
Ils ne remplacent pas ces handlers par les versions master déjà portées.

Comportements source testés et conservés :

- Le manager customer reçoit canManage/read mais pas create, même si le JSON du
  rôle contient aussi create. Le message est `Your are not allowed` pour create
  et import-agent ; le préfixe `pvd` des imports reste inchangé.
- Un PDV sans identifiant SIM sauvegarde d'abord personne/PDV puis échoue en
  appelant set sur un objet ordinaire. Le PDV a son statut false implicite ; la
  nouvelle SIM créée par l'import PDV n'a ni auteur ni statut true ajouté.
- L'import depuis agent conserve sa requête sur `parent` absent, donc son 400.
- Achat sans permission create supplémentaire ; facture toujours numérotée 1
  après la requête source défectueuse. L'annulation répétée recompense plusieurs
  fois solde/stock/caisse. Une erreur d'article tardive laisse achat/dette et le
  premier mouvement stock persistés.
- Paiement vente enregistré avant lecture de session, sans relation ajoutée au
  mouvement caisse. L'annulation vente cherche `buy` dans sa boucle de
  compensation, comme la source ; le solde caisse n'est pas corrigé à sa place.
- Retour sans garde métier supplémentaire, état/auteur implicites conservés,
  montant fractionnaire persisté et getInt pour stock/caisse. Confirmation
  répétée débite de nouveau, sans compléter les relations source omises.
- Agrégation PDV : borne de fin lexicale excluant la journée finale, totalDays
  inclusif, déduplication PDV/SIM/date, objectif du premier record, filtres et
  pagination conservés. Assign/unassign lit l'agent du record authentifié,
  sans ajouter le middleware de rôle absent de ces deux routes.
- Pilotage : middleware userInfo absent ; rejet de la promesse avant lecture de
  configuration, HTTP 200 vide observé sur la référence, aucun appel externe.
- Rupture public : ignore Balance string/bool, met à jour le premier numéro
  existant, conserve les compteurs du payload et les sauvegardes avant erreur.

Enveloppes mesurées : 401 du guard `{code:401,message:"The request requires admin
or record authorization token to be set.",data:{}}`, 404 DAO non capturé,
500 `{message:{value:{}}}` pour erreur DAO capturée, 500 `{message:{}}` pour
erreur JavaScript capturée et 200 vide pour le catch silencieux du paiement
achat. Les réponses métier françaises et leur orthographe sont conservées.

`scripts/c10_prepare_reference.py` transpile la source dans un dossier temporaire
et importe l'export corrigé via des instances `Collection`, sans modifier le
source. Seuls les modules listés sont enregistrés. Le cron rupture est commenté
malgré son message de démarrage trompeur. Le helper de fixtures reste dans ce
dossier temporaire ; aucune route de fixture n'est ajoutée à l'application.

`return.ts` est dormant : aucun bootstrap ne l'appelle. Ses deux retours
dupliquent les registrations actives de sale.ts ; `/api2/test` reste absent.

C10 est clôturé par décision utilisateur le 2026-09-28 (D-0020), avec SQL
PostgreSQL accepté pour la console et étude de `/sql` reportée sous D-0018.
La sonde SQLite synthétique reste dans `docs/c10-sql-dialect-gap.json`.
Ces 60 scénarios ne couvrent pas toutes les combinaisons de payloads,
transactions concurrentes legacy et erreurs moteur ; cette limite demeure.
