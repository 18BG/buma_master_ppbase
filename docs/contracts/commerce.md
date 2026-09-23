# C9 — Vente, recharge, achat, paiement, retour et rapports

Source : `buma_master_hooks@530018cd20c2acbe88f38b3639e500b5271e2eb2`,
`master.sale.ts`, `master.sale.functions.ts`, `master.purchase.ts`,
`master.purchase.functions.ts`, `master.retour.ts`, `master.retour.functions.ts`,
`master.caisse.ts`, `master.caisse.functions.ts`, `master.caisse.functions2.ts`,
`master.utility.ts`, `master.constants.ts` et les types associés.

Les 29 méthodes/chemins sont comparés à `source-inventory.json` dans
`tests/test_commerce_contract.py`. `routes.json` contient leurs enveloppes et
permissions. `scripts/c9_commerce_runtime.py` exécute les scénarios HTTP et
contrôle directement les effets PostgreSQL ; `docs/c9-runtime-report.json`
porte la date, les routes réellement exercées et les limites de cette preuve.

## Contrats préservés

- Vente ordinaire : `customer`, `items`, `article_stock`, `stockage`, `payed`,
  `debt`, `caisse`, `method`, `created`. Réponse `{"message":"Operation effectuee"}`
  sans ID ajouté. Valeur `kind=AUTRES`, état par défaut, stock et dette, paiements
  initiaux et antidatage autorisé sont conservés.
- Recharge : `articleStock`, `sim`, `amount`, `transaction_date`, `system_id`,
  `mobile_message_id`, `sent`, `notes`. `{id}` désigne la transaction ;
  `cancel_recharge.sale_id` désigne la vente. Débit stock/supplément agent,
  crédit client/bonus PAANI et leurs snapshots sont distincts. Annulation :
  `Enregistrement reussi !`, statut désactivé sans état `CANCELED` ajouté.
- Achat : cinq routes, `buyId`, `purchaseId`, `walletId` et `id` gardent leurs
  noms distincts. `received`, NAFAMA, solde fournisseur, snapshot wallet après
  le débit initial et avant un paiement additionnel restent fidèles au source.
- Retour : `/new` renvoie l'ID transaction, mais `/cancel/:id` et `/info/:id`
  attendent l'ID `buma_return`. Création « Client Inconnu » uniquement lorsque
  le code source le prévoit ; relations SIM/PDV/client et clé idempotente
  préservées. `updatePDV` a une réponse HTTP 200 vide.
- Mise à jour retour : conserve notamment le stock initial capturé lorsque
  les IDs ancien/nouveau sont identiques. L'annulation utilise le stock de la
  transaction initiale, désactive le retour, conserve la transaction et ne
  diminue pas `customer.due`. Elle exige l'admin pour un retour payé.
- Caisse return : `{message:"Ok"}` ; la sortie caisse ne diminue pas `due`.
  Versement UV simple/groupé : sorties RETURN et entrée VERSEMENT se compensent
  en caisse et créditent le client cible. `grouped:true` reste un groupe même
  après déduplication à un seul retour. `fromUnknown` contrôle session/solde
  sans débiter ce solde, comme la source.
- Paiement additionnel vente : `{success,message,caisse_transaction,payment,
  sale:{id,newPayed,newRemaining}}`, état de vente inchangé. Les transactions
  caisse ne sont pas annulées par `/sale/cancel`, conformément au code gelé.

## Rapports et accès

Les huit routes vente de rapports/vérification et `reportByCaisseType` sont
publiques dans la source : aucun garde auth n'est ajouté. Les statistiques
recharge/retour exigent `read`. `retour/info` exige l'auth sans permission de
lecture supplémentaire. `reportByExpenseType` exige `caisse.read/manage`.
`retour/updatePDV` et `caisse/return` refusent un flag explicitement `false` ;
l'absence d'un flag reste distincte.

Les rapports conservent les clés camelCase, filtres alphanumériques, pagination
(page 1, perPage 30, totalPages minimal 1), GROUP BY, dates de fin à 23:59:59
pour les dates seules, calcul revenu crédité des recharges et coût basé sur la
quantité de stock. Le résumé clients calcule `totalMissing` par ligne UNION,
tandis que le rapport clients-produits calcule le solde net par client : cette
différence source n'est pas corrigée.

Les statistiques conservent aussi l'absence de contrainte type/status lorsque
ni date ni réseau n'est fourni, ainsi que les paramètres absents interpolés
`undefined`. Une date seule des statistiques n'est pas élargie à fin de journée.

## Preuves et limites

La preuve couvre succès, erreurs métier et permissions, valeurs persistées,
annulations, snapshots, retries/concurrence recharge et retour, paiement
concurrent sans dépassement, retour groupé avec rollback, création d'identités,
NAFAMA, antidatage et nettoyage systématique. Les accès natifs exercés sont
login, CRUD retour, liste filtrée et `expand=customer,article_stock` ; aucune
mutation native retour ne déclenche implicitement l'agrégat métier custom.

Les méthodes et messages sont comparés au TypeScript gelé ; aucun second
serveur PocketBase n'est exécuté en parallèle. La différence d'atomicité de
certains anciens flux non transactionnels est explicite en D-0009, avec son
impact client et sa régression. Files/realtime, `/api2/*`, toutes les combinaisons
de règles natives et la parité des textes d'erreur du moteur restent hors gate.


Actualisation D-0009 (2026-09-23) : l'atomicité ajoutée aux fonctions vente/achat
legacy non transactionnelles a été retirée. Les tests exigent maintenant la
persistance des sauvegardes antérieures à l'échec, dans l'ordre TypeScript.
Les mentions précédentes d'un rollback global conservé comme divergence sont
supplantées par cette correction. Les vraies transactions source restent
atomiques et leur preuve reste applicable.

La préparation SIM/PDV/client des retours garde elle aussi ses sauvegardes
indépendantes : le test de faute sur la création PDV confirme que le client
créé avant l'échec reste présent. Seule la phase stock/retour explicitement
transactionnelle de la source est englobée.
