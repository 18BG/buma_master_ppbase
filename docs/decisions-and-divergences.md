# Décisions et divergences

## D-0001 — Référence source

La réécriture part de `fix/uv-paani-sup@530018c`.

## D-0002 — Séparation des dépôts

`buma_master_hooks` reste la source TypeScript. Le backend PPBase est développé
dans `buma-master-ppbase`.

## D-0003 — Schéma et données

Les migrations historiques servent à comprendre l'évolution. La baseline
runtime PPBase sera générée depuis l'état final du schéma validé, puis les
données seront importées séparément.

## D-0004 — Progression

Un checkpoint échoué bloque le suivant. Aucun domaine métier ne doit être porté
en masse avant validation du socle, du schéma, de l'auth et de l'idempotence.

## D-0005 — Baseline et vues

Le premier export JSON a servi uniquement à un contrôle intermédiaire et son
draft a été supprimé. Après correction des vues `customer_pdv`, le nouvel export
corrigé est devenu la référence. La baseline `base`/`auth` et la migration des
42 vues sont générées depuis lui ; les 42 requêtes passent la validation
PostgreSQL documentée dans `docs/view-verification-report.json`.

## D-0006 — Champ métier absent attendu par les vues

- Dans l'ancien export, `buma_customer_pdv_list_selector_view` et
  `buma_customer_pdv_list_view` utilisaient `bc.parent`, où `bc` est un alias
  de `buma_pdv`. Ces deux vues ont été corrigées dans PocketBase vers
  `buma_pdv.agent`, avec `par.id = bc.agent` ; le nouvel export expose donc
  `agent` et ne contient plus cette référence absente.
- `buma_purchase_list_view`, `buma_sale_list_view`,
  `buma_transaction_list_view`, `buma_stockage_no_manager` et
  `buma_users_list_view` utilisent `u.username`, où `u` est un alias de la
  collection d'authentification `users`. Le Dashboard confirme que `username`
  est bien un champ système PocketBase. Ces vues ne nécessitent donc pas de
  correction dans PocketBase ; PPBase 0.3.0 ne crée toutefois pas encore de
  colonne PostgreSQL physique `username` dans ses tables auth. Les migrations
  `1789236841` (`users`) et `1789236843` (`agent_users`) matérialisent cette
  compatibilité ; l'unicité et la sémantique de connexion restent C3.

- `buma_customer_pdv_list_view` renvoie actuellement HTTP 200 avec `data: []`
  dans le Dashboard. La requête observée utilise `fields=id`, ce qui ne valide
  pas à lui seul toutes les expressions de la vue ; il faut aussi tester les
  champs complets avec des données satisfaisant ses jointures et
  `sim.number != ''`.

Ces champs ne doivent pas être ajoutés, renommés ou supprimés dans la cible sans
un test runtime et une décision explicite de compatibilité PocketBase/PPBase.

Les autres erreurs du draft PostgreSQL (`COALESCE`, `GROUP BY`, `GREATEST`,
identifiants camelCase ou dépendances entre vues) sont des traductions SQL pour
PPBase ; elles ne constituent pas des champs absents à corriger dans PocketBase.

Les références `rowid` de `buma_article_with_no_stock_list_view`,
`buma_stock_transaction_list_view` et `buma_stockage_no_manager` sont des
pseudo-colonnes internes SQLite, pas des champs PocketBase manquants. Elles
seront traduites vers `id` dans PPBase et ne nécessitent pas l'ajout d'un champ
`rowid` dans PocketBase.

## Divergences à décider plus tard

- C3 : la migration persistée normalise `minPasswordLength` vers le champ
  système `password` (`users.min=5`, `agent_users.min=8`) et le runtime PPBase
  passe maintenant la création/mise à jour 5/4 et 8/7. L'adaptateur BUMA
  accepte désormais le claim standard `type=auth` ainsi que l'ancien
  `type=authRecord`; `authRecord` reste compris comme contexte PocketBase et
  non comme type JWT. **Rectification du 2026-10-24 (session serveur, WS-C) :**
  dans PocketBase 0.22.34, `authRecord` est bien la valeur du claim JWT `type`
  des jetons record : constante `TypeAuthRecord = "authRecord"`
  (v0.22.34 `tokens/tokens.go` l.6), signée par `NewRecordAuthToken`
  (v0.22.34 `tokens/record.go` l.21) et reconnue par le middleware
  (v0.22.34 `apis/middlewares.go` l.241) ; la version actuelle et PPBase signent
  `type=auth` (v0.40.6 `core/record_tokens.go` l.13 et l.62). L'adaptateur accepte les
  deux, ce qui rend valides après l'import les sessions émises par PocketBase
  0.22.34 (D-0027).

- comportement exact et protection des routes SQL ;
- compatibilité des hashes d'authentification ;
- filtres/pagination/expand du SDK ;
- comparaison runtime des valeurs de vues PocketBase et PPBase avec des données
  représentatives (la validation actuelle est structurelle/SQL, sans backup de
  production) ;
- jobs externes ou scheduler ;
- règles de concurrence et contraintes uniques finales.

## D-0007 — C7 et valeurs historiques de transaction

Le code source écrit `WALLET_TO_UV`, `UV_TO_WALLET` et
`CONVERSION_CANCEL_IN/OUT`, mais l'export corrigé ne les liste pas dans les
valeurs du select `buma_transaction.type`. Il ne s'agit pas d'une nouvelle
route ni d'une donnée de production : `app/stockage/service.py` les insère
uniquement dans les écritures historiques internes, tandis que les flux
normaux utilisent la validation PPBase. La baseline de schéma reste inchangée.

Le source affecte aussi `cancel_author` et `cancel_date` sur
`buma_conversion`, champs absents de l'export corrigé. C7 conserve l'état
annulé et l'audit auteur/date dans les transactions d'annulation et leurs
timestamps, sans ajouter silencieusement des colonnes.

## D-0008 — C8, ne pas corriger une divergence source/PocketBase

Le code source envoie certaines clés qui ne figurent pas dans le schéma
exporté. Le portage ne corrige ni la source ni PocketBase et n'ajoute aucune
colonne : il conserve les mêmes valeurs et le comportement des champs inconnus.
En particulier, `DEBT` n'est pas transformé en `PAYMENT`.

## D-0009 — C9 : différence d'atomicité corrigée

La différence introduite par le portage (rollback global ajouté aux fonctions
vente/achat non transactionnelles) est refusée par la clarification utilisateur
du 2026-09-23 : « on ne fait que réécrire même s'il y a des problèmes dans
l'ancien backend ». Elle n'est plus conservée comme limite de cutover.

`app/commerce/sequential.py` reproduit désormais les sauvegardes indépendantes
et leur ordre. Une erreur tardive laisse les effets déjà sauvegardés, notamment
vente/dette/stock avant une transaction d'article échouée, débit fournisseur
avant rejet wallet et achat reçu avant échec de lecture des articles.
Le script C9 contient les régressions HTTP/PostgreSQL correspondantes.
Les fonctions dont la source possède une transaction la conservent.

Les valeurs historiques `AUTRES`/`DEBT`, les champs source inconnus et le type
booléen exporté de `buma_buy.received_date` restent préservés sans migration.
Aucune amélioration du backend source n'est autorisée par la seule réécriture.

La comparaison de deux serveurs PocketBase/PPBase, files/realtime et les textes
de toutes les erreurs moteur ne sont pas prouvés par la gate locale.

## D-0010 — C10 SQL : reproduire les accès et les défauts source

Les quatre routes du sous-lot SQL gardent les gardes de la source : mot de
passe littéral pour `/sql`, aucune authentification ajoutée aux deux routes
article SQL. Le portage appelle les opérations de collection PPBase pour
modifier une vue ou supprimer sa collection, jamais un simple DROP laissant
des métadonnées orphelines.

Le handler `/api/sql2` est défectueux dans le code gelé. Sur PocketBase 0.22.27,
son callback async `(req,res)` produit HTTP 200 JSON `null` et n'exécute pas le
SQL fourni. Ce comportement est conservé et testé ; aucun service SQL
fonctionnel n'est substitué à cette route.

Les quatre routes sont vérifiées dans un environnement jetable, avec
restauration de la vue et suppression de la seule collection de test. Les
différences de dialecte SQL, textes moteur et inférences de schéma non
couvertes restent ouvertes ; ce sous-lot ne ferme pas C10.

## D-0011 — C10 stockage legacy : défauts conservés

Le sous-lot `/api2/stockage` ne réutilise pas automatiquement les services C7
transactionnels. Il conserve les sauvegardes séquentielles du source, le stock
négatif de transfert, l'absence de garde métier sur transfer/remove, le statut
false implicite des lignes add_article, l'erreur de paramètre `stock_info` et le
200 `okay` après exception de conversion. Les effets et leurs régressions sont
portés dans `app/legacy/stockage.py` et `scripts/c10_stockage_runtime.py`.

## D-0012 — C10 hooks natifs : ordre et gardes source

Les hooks natifs sont appliqués au chemin CRUD HTTP, pas aux écritures internes
des services, comme le distingue le POC C1. L'ordre source est conservé :
les défauts auteur/statut précèdent l'écriture, la dette employé débite
l'employé avant la dette, et les gardes de suppression/mise à jour lèvent leur
erreur avant le CRUD natif. Aucun garde supplémentaire n'est ajouté à un champ
ou à une route qui n'en possédait pas. La validation PocketBase de longueur
des IDs fournis par CRUD est reproduite avant les hooks métier ; les IDs
internes générés ne sont pas concernés.

Actualisation D-0012 : la preuve PocketBase 0.22.27 montre que le formulaire
CRUD natif englobe le hook dette dans sa transaction. Le portage utilise donc
`event.engine` pour ce débit : il est annulé si l'INSERT dette échoue. La
validation de longueur d'ID intervient avant les hooks métier. Cette correction
est couverte par les scénarios natifs ID invalide et trigger d'échec.

Complément D-0009 : la préparation SIM/PDV/client de `newReturn` est également
séquentielle, avant la transaction stock/retour. Un échec du PDV laisse le
client créé ; la régression C9 injecte cette faute et contrôle la persistance.

## D-0013 — C10 caisse legacy : ordre, permissions et sérialisation

Les dix handlers actifs de `caisse.ts` sont portés séparément de C8. Aucune
atomicité globale ni idempotence n'est ajoutée : une session peut être OPEN
alors que la sauvegarde caisse échoue ; un mouvement wallet peut exister alors
que l'insertion caisse échoue. `cancel2` conserve le statut actif de l'original
et peut créditer plusieurs fois, même pour un non-admin dont le flag isAdmin
est absent. La valeur REVERSAL reste une écriture DAO interne.

Les rapports session/plage restent publics, et le rapport par type de dépense
ne reçoit pas de permission read supplémentaire. La visibilité email et les
dates vides sont alignées sur `Record.PublicExport` PocketBase 0.22.27. Le
rapport SQL garde l'interpolation source et ses erreurs de syntaxe ; les autres
requêtes conservent les paramètres source. Aucun durcissement n'est présenté
comme une correction acceptée du backend.

Régressions : `tests/test_legacy_caisse_contract.py` et
`scripts/c10_caisse_runtime.py`. Limites explicites dans
`docs/contracts/legacy-caisse.md` : ce portage ne ferme ni la comparaison
exhaustive de ces handlers ni C10 global.

## D-0014 — C10 : écarts de portage corrigés par comparaison exécutée

Les 18 handlers restants et les dix handlers caisse sont comparés avec le
TypeScript gelé transpilé sur PocketBase 0.22.27 jetable. Le 401 middleware
exact contient code/message/data ; les anciens messages raccourcis ont été
remplacés dans les guards correspondants. Un DAO ErrNoRows non capturé est 404,
un GoError capturé est `{value:{}}`, un Error JavaScript capturé est `{}`. Les
régressions et les 94 comparaisons enregistrent ces différences de forme.

La traduction initiale de neuf vues avait perdu la casse de 27 alias. Ceci est
une erreur cible, pas un défaut source à conserver. `1789236847` renomme les
colonnes physiques sans supprimer les dépendances et conserve les requêtes
dans les métadonnées. Son rollback/replay a été exécuté. Le traducteur source
et le vérificateur sont corrigés ; C9 utilise maintenant `v."authorName"`.

Le SDK a révélé l'absence du rôle vivant de l'abonné dans les règles realtime.
L'adaptateur BUMA recharge cette identité pour chaque événement et l'isole de
celle de l'émetteur. Les fichiers du framework PPBase restent inchangés. La
preuve SDK utilise deux utilisateurs distincts et contrôle émission, refus
après révocation puis reprise après rétablissement du droit. Les événements
delete, toutes les options SSE et les fichiers protégés restent hors de cette
preuve ciblée ; ils ne sont pas déclarés vérifiés.

## D-0015 — C10 non clôturé : SQL SQLite et pseudo-colonnes

Un écart réel subsiste, mesuré sur les deux serveurs :
`SELECT strftime('%Y-%m-%d', '2026-09-24') AS day` avec fields `{day:""}` renvoie
200 `[{day:"2026-09-24"}]` dans `/sql` source, contre 500 UndefinedFunction dans
PPBase. Artefact : `docs/c10-sql-dialect-gap.json`. Il ne s'agit pas d'un défaut
source : un appel existant peut casser après changement d'URL. Aucun changement
client n'est accepté implicitement ; la gate C10 reste ouverte.

Le contrôle physique ajouté signale aussi `rowid` absent des deux projections
`buma_article_with_no_stock_list_view` et `buma_stockage_no_manager`. D-0006
avait prévu sa traduction vers id, mais la suppression de la projection en
double a retiré ce nom de champ. Le rapport de schéma expose maintenant ces
deux cas explicitement ; son succès structurel ne ferme pas cette parité.
Ne pas les masquer en ajoutant un fallback applicatif ni modifier le source.

Précision après demande utilisateur : strftime était une **sonde synthétique**
envoyée à `/sql` par l'agent, pas une requête extraite d'une route métier. La
recherche dans src/, migrationsDir/ et l'export corrigé Master n'en trouve aucun
usage. Elle démontre une différence de dialecte du SQL fourni librement par un
appelant, sans établir qu'un client Master utilise cette fonction. Pour le cas
testé, PostgreSQL utilise `to_char(DATE '2026-09-24', 'YYYY-MM-DD')`.

Troisième usage source de rowid identifié dans l'export :
`buma_stock_transaction_list_view` sélectionne `a.rowid AS article_id` ; le
portage sélectionne `a.id AS article_id`. Le nom est présent mais le sens de la
valeur a changé. Il doit être examiné avec les deux projections absentes.

## D-0016 — rowid vérifié et corrigé, remplace la traduction de D-0006

Les trois usages ont été relus dans la source et l'export. La documentation
SQLite, le code PocketBase 0.22.27 et le DDL/les réponses d'un serveur jetable
confirment un entier de stockage distinct de l'ID texte. Il est rendu comme
nombre JSON dans les vues. Voir `docs/contracts/rowid.md` pour les références
primaires et les limites, notamment la stabilité après opérations de maintenance.

`1789236848` restaure rowid/`article_id` avec un bigint interne et l'allocation
max+1 transactionnelle observée. Le statut public JSON des champs de vue reste
inchangé, et les collections de base n'exposent pas le champ. Les données
existantes ne sont pas numérotées arbitrairement : migration sur tables vides,
import C11 explicitement responsable de conserver les rowid du snapshot source.
Le fallback `rowid -> id` de D-0006 est abandonné car il changeait le contrat.

Preuves : 12 comparaisons des deux serveurs, 20 insertions concurrentes,
maximum signé, rollback/réutilisation, filtres natifs, absence du champ dans
le CRUD de base, rollback/replay de migration et vérificateur de projection strict.

## D-0017 — périmètre SQL libre et clôture du code C10

**RETIRÉE comme décision de clôture.** Cette section conserve le raisonnement
historique, mais n'est pas une différence acceptée par l'utilisateur. L'agent
a clos C10 à tort en présentant l'exécution directe PostgreSQL comme une
réserve suffisante. C10 est rouvert pour finaliser le contrat SQL entrant.

La source index.pb.ts:37 exécute le texte reçu dans query sur son moteur,
sans interpréteur ni traducteur SQL. Le portage conserve ce contrat d'exécution
directe avec le moteur cible PostgreSQL, les mêmes chemins/champs/guards et la
projection DynamicModel vérifiée. executeSql modifie la vue sur ce même moteur ;
SQL2 garde son défaut source. Aucune permission supplémentaire n'est ajoutée.

La compatibilité universelle des deux dialectes SQL n'est **pas** revendiquée :
une requête libre utilisant une fonction SQLite peut nécessiter une traduction
PostgreSQL. L'exemple strftime reste une limite mesurée, conservée dans
c10-sql-dialect-gap.json, sans usage trouvé dans le code ou les vues BUMA gelés.
Il ne devient pas une nouvelle fonction métier inventée pour ce chantier.
Les requêtes client ad hoc et les erreurs propres au moteur restent à contrôler
avant cutover, conformément à cette limite explicitement documentée.

C10 est clôturé pour le portage du code gelé, du schéma fourni et des contrats
exercés par la gate locale. Cette clôture ne signifie ni émulation complète de
SQLite, ni validation de données/clients de production, ni autorisation de
cutover. Les 114 définitions, 42 vues et API/SDK testées restent conformes au
périmètre de preuve ; l'import des rowid historiques relève explicitement de C11.

## D-0018 — console PostgreSQL acceptée et étude de /sql reportée

Décision explicite de l'utilisateur le 2026-09-26, après présentation de l'appel
desktop retrouvé : l'administrateur écrira des requêtes PostgreSQL dans la
console ; faute d'appel `/sql` identifié, cette route est mise de côté pour
le moment. Cette autorisation nouvelle remplace le cadrage SQL ouvert de
D-0015/D-0017 ; elle ne valide pas rétroactivement la clôture retirée D-0017.

- `POST /master/article/executeSql` garde son chemin, le corps `{query}`, sa
  réponse collection et la modification de la vue `buma_sql_table`. Le SQL
  accepté pour cette console est celui de PostgreSQL ; aucune traduction
  générale SQLite vers PostgreSQL n'est requise dans ce périmètre.
- La route définit une vue : la requête doit fournir une sélection exploitable
  comme vue, avec une colonne `id`. Cette décision ne transforme pas la console
  en exécuteur de toutes les commandes SQL, notamment INSERT/UPDATE/DDL.
- Impact accepté : les requêtes saisies par l'administrateur doivent utiliser
  le dialecte PostgreSQL ; les anciennes expressions spécifiques SQLite ne
  sont pas promises compatibles. Aucun changement du transport QML, des noms
  de champs, des permissions ou des règles métier n'est autorisé par extension.
- `POST /sql` reste implémenté et enregistré. Son audit d'usage et sa parité
  de dialecte sont **reportés**, sans suppression ni désactivation, et ne
  bloquent plus la gate C10 du périmètre retenu. L'absence de résultat dans
  le desktop fourni ne prouve pas l'absence d'autres consommateurs.

Preuve ayant motivé la décision : `docs/c10-desktop-sql-usage-audit.json`.
La route utilise déjà PostgreSQL (`app/hooks/legacy_sql.py`) ; aucune modification
applicative n'est nécessaire pour enregistrer ce choix. Les preuves SQL ciblées
existantes restent dans `docs/c10-sql-runtime-report.json` et les régressions
`tests/test_legacy_sql_contract.py` ; elles ne couvrent pas encore tout le
parcours desktop découvert. La régression runtime attendue avant clôture C10
est : `{query: "SELECT * FROM users"}`, mutation de la vue, lecture native
`getList(1, 1000)`, champs/types/enveloppe exploitables, erreur sans mutation
résiduelle et restauration des fixtures. Ajouter un cas PostgreSQL spécifique
pour vérifier le dialecte accepté. Le helper de comparaison SQLite interrompu
et son rapport absent ne sont pas transformés en preuves passantes ; la partie
de cette comparaison consacrée à `/sql` est reportée avec la route.

C10 reste ouvert pour cette validation ciblée de la console et sa gate finale.
Cette décision ne clôture pas C10 et n'autorise ni C11, ni import, ni cutover.

## D-0019 — préparation C11 : relations multiples bloquées, aucune perte acceptée

Constat du 2026-09-26 pendant la préparation C11 explicitement autorisée dans
une session séparée : `buma_customer.pdv`, `buma_pilotage_ticket.tag` et
`buma_wallet.managers` ont `maxSelect: null` dans l'export corrigé. Une base
créée par PocketBase 0.22.27 stocke ces relations sous forme de tableaux JSON,
y compris `[]`. PPBase traite actuellement cette option comme 1 ; les trois
colonnes PostgreSQL observées après les neuf migrations sont
`character varying`, pas des tableaux.

Impact potentiel : aplatir une liste, choisir un seul ID ou copier sa chaîne
JSON dans une colonne scalaire changerait le contrat de relation et pourrait
perdre les références, filtres, expands ou permissions. **Aucune de ces
transformations n'est acceptée ni implémentée.** Le diagnostic C11 refuse
l'import de records dans ces trois collections ; s'il n'y en a aucun, il
conserve l'avertissement structurel.

Preuves : `scripts/c11_fixture.py` crée une référence PB sans hooks métier,
`scripts/c11_rehearsal.py` mesure les colonnes cibles et vérifie le rejet
d'une source avec deux IDs. `tests/test_c11_import.py` couvre les valeurs
`[]` et multi-IDs pour `maxSelect` null/0. Le rapport
`docs/c11-rehearsal-report.json` sépare ce scénario négatif du scénario
positif sur les collections représentables.

Décision : conserver le blocage et les données originales, attendre le
diagnostic du backup pour mesurer les lignes affectées. Aucun changement de
schéma/framework/routes et aucune reprise/clôture C10 dans cette session.
C11 reste ouvert. Ce constat ne concerne pas D-0018 ni le report de `/sql`.

Les primitives d'import C11 archivent également les rowids de toutes les
tables et les trois dates internes auth legacy ; le SQLite complet est
conservé octet pour octet. Elles ne restaurent pas les paramètres/secrets,
admins, OAuth ni anciens tokens dans le runtime. Cette limite est ouverte,
pas une autorisation de perdre ou de modifier ces comportements au cutover.

## D-0020 — clôture C10 décidée par l'utilisateur le 2026-09-28

L'utilisateur confirme « ppbase a été correctement corrigée » et demande
explicitement que C10 soit considéré comme totalement fini et clôturé.
**C10 est terminé.** Cette décision nouvelle remplace son statut ouvert/en
attente et les conditions supplémentaires de validation qui bloquaient la
clôture. Elle ne réhabilite pas la clôture unilatérale D-0017 retirée.

Le périmètre D-0018 demeure : SQL PostgreSQL pour la console administrateur,
étude de `/sql` reportée, route conservée. Les 106 comparaisons ciblées,
corrections alias/rowid, preuves SDK, 131 tests et vérifications 9 migrations /
114 définitions / 42 vues restent les preuves enregistrées de leurs sessions.
La correction PPBase est confirmée par l'utilisateur ; elle n'est pas
réauditée ni retestée dans ce tour. Les modifications locales du framework
issues de cette correction sont laissées intactes.

Cette clôture de suivi ne prétend pas produire un nouveau résultat de test
Qt/desktop ou du scénario SQL interrompu. L'absence du rapport
`c10-stored-sql-runtime-report.json`, les limites des scénarios antérieurs et
les anciens diagnostics des scripts restent identifiables ; ils ne sont pas
convertis en tests passants. Aucune nouvelle différence de comportement ou
modification de code n'est introduite par D-0020.

La suite est C11, déjà préparé et toujours ouvert. Son blocage D-0019 sur les
relations multiples reste à résoudre/vérifier ; les arrays ne doivent jamais
être aplatis. La fermeture C10 n'autorise aucun import de production ni cutover.

## D-0021 — C11 : historique réel incompatible avec l'import préparé

Le 2026-09-28, après explication des étapes C11, l'utilisateur autorise
l'examen de `master-backup.zip` et le diagnostic d'une copie hors ligne. Il
fournit la version **PocketBase 0.22.34**. Aucun import PostgreSQL, réparation
de données, changement de clé ou bascule n'est autorisé par ce tour.

L'archive contient 1 188 247 records et le même schéma corrigé de 114
définitions. Son diagnostic révèle trois catégories distinctes :

- **Limites cible/importeur** : D-0019 concerne 5 841 records dans les trois
  collections à relations multiples. L'index unique mobile C4 rejette aussi
  **877 groupes** `(type, author, mobile_message_id)` RETURN, soit **2 391
  transactions**. Tous comportent des différences métier après exclusion des
  IDs/timestamps. Suppression, fusion ou changement de clés perdraient le legacy.
- **Anomalies source** : 195 795 références sans cible, dont 115 325 ventes vers
  `buma_customer_old` vide. 115 168 correspondent à `buma_customer`, sans que
  cela justifie une redirection automatique. Autres cas : sentinelles `0`/`1`,
  `PERSONNE` dans une relation, deux IDs joints dans un élément de liste managers.
- **Fichiers** : 2 303 fichiers référencés absents du ZIP, dont 2 302 ressources
  et une image wallet. S3 est désactivé dans les paramètres archivés. Aucun
  autre serveur/backup consulté, aucun remplacement créé. Les 601 `.attrs`,
  80 miniatures et cinq objets non directement référencés sont conservés.

La source gelée `master.idempotency.ts` et `master.retour.functions.ts` a été
relue : elle recherche un premier record correspondant ; elle ne définit pas
de suppression des transactions historiques. Cette session n'affaiblit pas
l'index C4 et ne réouvre pas son implémentation. Une stratégie conservant les
lignes existantes et le comportement futur doit être conçue/testée séparément.

Preuves : `docs/c11-master-diagnostic-report.json`, rapports détaillés et
`docs/c11-master-diagnostic.md`. L'audit `scripts/c11_snapshot_audit.py` vérifie
tous les champs même après un premier défaut ; trois tests couvrent tableaux,
comptage des références et fichiers. Les compteurs initiaux partiels ne sont
pas présentés comme exhaustifs. Les données auth sont présentes mais aucune
connexion réelle n'est testée ; 0.22.34 reste la version déclarée, sans binaire.

Décision : diagnostic terminé, **import bloqué et C11 ouvert**. Aucune perte
ou divergence acceptée, aucun correctif de données/migration/schéma applicatif.
Conserver les anomalies source jusqu'à une décision explicite ; ne jamais
inventer leurs cibles, IDs, rowids ou fichiers.

## D-0022 — C4/C11 : index d'idempotence réaligné sur la source gelée

Décision explicite de l'utilisateur le 2026-10-23 (« si tu recommandes le a, on
part sur cela »), après le constat que les deux index uniques de `1789236845`
n'existaient ni dans la source gelée ni dans le schéma de production. Elle
corrige un écart C4 jamais documenté, sans rouvrir les autres livrables C4.

Faits établis (`docs/c11-idempotency-source-check.json`, lecture
`ro&immutable` de la copie privée, empreinte inchangée) :

- Source `530018c` : `migrationsDir/1785599959_updated_buma_transaction.js`
  ajoute le seul index unique `idx_transaction_idempotency`, sur `system_id`,
  limité aux valeurs `LIKE 'idem:%'`, sans type ni auteur ; `idx_bEWTmoj` sur
  `mobile_message_id` reste non unique.
- Production : `_migrations` du backup contient 325 des 326 migrations source,
  toutes sauf celle-ci. `buma_transaction` n'a que deux index non uniques et
  aucune valeur `system_id` préfixée `idem:` (0 sur 93 220 non vides). Les 877
  groupes `(type, author, mobile_message_id)`, soit 2 391 lignes RETURN, datent
  du 2024-11-18 au 2025-01-10.
- C4 imposait `UNIQUE (type, author, system_id) WHERE system_id <> ''` et
  `UNIQUE (type, author, mobile_message_id) WHERE mobile_message_id <> ''`.
  Aucun n'existe dans la source ; le second rejetait l'historique accepté par
  PocketBase.

Correction, `pb_migrations/1789236849_c11_source_transaction_idempotency.py` :

- suppression des deux index C4, puis ajout de l'index source aux métadonnées
  de `buma_transaction` par `update_collection`, comme `saveCollection` dans la
  source : métadonnées et index physique changent dans la même transaction ;
- prédicat `translate(substr("system_id", 1, 5), 'IDEM', 'idem') = 'idem:'` :
  le `LIKE` SQLite ne replie que la casse ASCII, alors que `LIKE` PostgreSQL
  est sensible à la casse et qu'`ILIKE` dépend de la locale. Un test compare
  ce prédicat à SQLite sur 14 cas limites ;
- `down` restaure les deux index C4 et leurs métadonnées ; il échoue et
  s'annule entièrement si l'historique contient des doublons mobile.

Effets observables, désormais conformes à la source : deux transactions avec le
même `mobile_message_id`, ou la même clé non `idem:`, sont acceptées par la
base ; une clé `idem:` (toute casse ASCII) est unique dans toute la table, quels
que soient type et auteur, y compris via le CRUD natif. Routes, payloads et
réponses ne changent pas. Recharge, retour et conversion gardent le lookup
source et le verrou advisory par clé : la source s'appuyait sur ce lookup dans
une transaction d'écriture sérialisée par SQLite, PPBase s'appuie sur le verrou.

Impact client : aucun appel à modifier. Une écriture CRUD native refusée par
l'index mobile C4 est acceptée comme dans PocketBase. Les 2 391 transactions
historiques deviennent importables telles quelles et ne doivent jamais être
dédupliquées. Le vérificateur de schéma attend l'export plus cet index, lu dans
la migration. Le diagnostic C11 ne bloque plus que sur les doublons que l'index
source rejetterait (0 dans le backup).

Régressions : `tests/test_idempotency.py` (index et 14 cas de prédicat),
`tests/test_c11_import.py`, `tests/test_schema_baseline.py`,
`tests/test_view_verifier.py`. Runtime `scripts/c11_idempotency_runtime.py` sur
PostgreSQL 16 jetable : 10 migrations, matrice source/C4 comparée,
rollback/replay, rollback refusé atomiquement avec historique dupliqué, huit
tentatives concurrentes sans index mobile pour une seule ligne, puis C4, C7 et
C9 (29 routes) passants. Rapport : `docs/c11-idempotency-runtime-report.json`.

Reste ouvert, sans décision : l'ordre du lookup sur l'historique dupliqué. La
source renvoie la première ligne dans l'ordre SQLite (rowid), PPBase trie par
`created`. Sur les 877 groupes : 843 identiques, 30 égalités sur la première
date et 4 où le plus petit rowid n'est pas le plus ancien. Seule la relance
d'un ancien message renverrait alors un autre identifiant, sans nouvelle
écriture. D-0019, les orphelins et les fichiers absents de D-0021 sont inchangés.
Cet ordre est tranché par D-0023.

## D-0023 — C11 : ordre du lookup sur l'historique dupliqué, écart accepté

Décision explicite de l'utilisateur le 2026-10-24 (« je suis d'accord pour le 2
si l'impact est quasi nul »), après présentation des deux options : reproduire
l'ordre SQLite ou documenter l'écart.

Écart : la source (`master.idempotency.ts`, `findRecordsByExpr` sans tri)
renvoie la première ligne lue par SQLite, soit le plus petit rowid.
`app/transactions/idempotency.py` renvoie `ORDER BY "created" ASC LIMIT 1`, et
PostgreSQL ne garantit aucun ordre entre lignes de même `created`. Sur les
877 groupes historiques importés tels quels (D-0022) : 843 donnent la même
ligne, 30 ont plusieurs lignes à la même première date et 4 ont un plus petit
rowid qui n'est pas le plus ancien (`docs/c11-idempotency-source-check.json`).

Impact client : seulement si un client relance une clé de ces 34 groupes, tous
de type RETURN et créés entre le 2024-11-18 et le 2025-01-10. Le lookup détecte
le doublon dans les deux systèmes et n'écrit rien ; seul l'identifiant de
transaction renvoyé peut différer. Les flux métier ne créent pas de nouveau
doublon (verrou et lookup) ; l'ordre `created` s'appliquerait aussi aux
doublons qu'un CRUD natif créerait. Option écartée : conserver le rowid source
des transactions pour trier dessus, plus lourde et susceptible de ralentir les
écritures de transactions.

Régression : `tests/test_idempotency.py::test_d0023_lookup_keeps_created_order_and_source_precedence`
fige ce tri et la précédence source `system_id`, ancienne clé, puis
`mobile_message_id`.

## D-0024 — C11 : PocketBase 0.22.34 accepté comme version source du backup

Constat du 2026-10-24 (session serveur, chantier WS-D), sur le dépôt officiel
PocketBase (tags `v0.22.27` = `b83f924c`, `v0.22.34` = `ea732074`) et sur les
binaires officiels linux amd64, vérifiés contre le `checksums.txt` de chaque
release (binaires `ca223268…` et `f164a633…`). Preuves :
`scripts/c11_version_reference.py` et `docs/c11-pb-0.22.34-reference-report.json`
(rapport public agrégé, rejouable par la commande qu'il contient).

- Code : `git diff --stat v0.22.27 v0.22.34` touche 52 fichiers, dont 41 sous
  `ui/`. Hors interface, seuls changent `daos/view.go` (extraction des champs
  d'une vue : les commentaires SQL sont retirés plus tôt, l.465-467 ; les
  42 vues BUMA, dont 4 commentées, donnent les champs de l'export avec les
  deux binaires), `plugins/jsvm` (chemin de `require()`), `tools/logger`
  (`logs.db`, jamais importé), `tools/auth/oidc.go` (OAuth2, `_externalAuths`
  vide), les dépendances (`go.mod`) et le workflow de release. Les 28 chemins
  qui décident du contenu de `data.db` et de `storage/` (models, models/schema,
  models/settings, tools/types, tools/security, tools/filesystem, forms,
  migrations, core, apis, tokens et les daos record, table et settings) ont le
  même objet Git dans les deux tags. Les deux binaires embarquent SQLite 3.46.0.
- Binaires : construites par chacun des deux binaires, les fixtures `default` et
  `real_shape` ont les mêmes `sqlite_master`, colonnes, valeurs, classes SQLite,
  rowids et objets `storage/` (chemins, tailles, SHA-256). Seules diffèrent les
  valeurs aléatoires par construction, au même format : bcrypt `$2a$12$`,
  tokenKey et secrets alphanumériques de 50 caractères, IDs générés, suffixe de
  10 caractères des noms de fichiers, horodatages, IDs des champs de vues.
  L'importeur de `fb381ac` lit les deux identiquement.
- Backup réel (copie privée lue en `mode=ro&immutable=1`, agrégats seulement,
  empreinte inchangée) : en-tête écrit par SQLite 3.46.0, aucune différence de
  déclaration de colonne, formats identiques à la fixture 0.22.34, paramètres en
  JSON clair (264 chemins de même structure), 14 migrations système identiques.

Décision : `0.22.34` est ajoutée au registre des versions validées de
`scripts/c11_import.py`, avec ce rapport comme preuve relue à l'exécution
(`status` passed et `conclusion.importer_can_accept_0_22_34`), sans changement
du lecteur ; `0.22.27` reste validée pour la fixture historique. La version
reste fournie avec le backup, jamais devinée.

Formes réelles relevées par la même lecture, conservées telles quelles comme
l'historique de D-0021 (aucune normalisation) : 26 IDs de records hors du format
généré (18 de 3 à 9 minuscules dans `buma_apps_roles`, 8 de 15 caractères avec
majuscules et `_`), 436 relations vers ces IDs, 51 145 valeurs de select hors
des valeurs déclarées sur 4 champs (dont 49 794 pour
`buma_customer_solder_history.type` et 1 342 pour `buma_sale.kind`), 10 objets
`storage/` de records supprimés, 8 objets sous un ID personnalisé, 4 JSON texte
`null` à côté de 100 `NULL` SQL, 23 comptes auth sans email. Les tableaux des
relations multiples réelles n'ont que 0 ou 1 élément (5 505 et 336).

Impact client : aucun (aucune route, réponse ni donnée modifiée). Limites : seuls
les binaires officiels linux amd64 sont comparés ; le build exact de la
production n'est pas connu (l'en-tête SQLite est cohérent avec 0.22.34 sans
distinguer 0.22.27) ; les comportements HTTP des deux versions ne sont pas
comparés. Régressions : `tests/test_c11_fixture.py` (34 tests, dont 30 exigent
les binaires désignés par `BUMA_C11_POCKETBASE_0_22_27` et `_0_22_34`).

## D-0028 — C11 : extraction vérifiée du ZIP complet, plus stricte que PocketBase

Le ZIP réel (1 555 849 147 octets, 1 290 membres, SHA-256 `541ca900…1295`)
n'est pas sur le serveur ; l'extraction faite sur le Mac le 2026-09-28 était
ponctuelle et non versionnée. Pour la répétition avec fichiers prévue devant
l'utilisateur et pour la bascule, `scripts/c11_extract.py` (chantier WS-B2) est
l'unique chemin d'extraction du mode complet :

```sh
.venv/bin/python -m scripts.c11_extract --archive X.zip --destination NOUVEAU \
  --manifest-out NOUVEAU [--expect-manifest manifeste.json]
```

Lancé depuis la racine du dépôt, jamais depuis le dossier extrait. Inventaire
préalable (`inspect_archive` et règles plus strictes : NUL, composants vides,
caractères de contrôle, liens et fichiers spéciaux, chiffrement, méthodes autres
que Stored et Deflate, conflits fichier/dossier, plus de 32 composants) ; un
refus ne crée rien. Destination et manifeste neufs, espace libre suffisant,
limite de descripteurs vérifiée. Extraction en flux (environ 4 Mio de mémoire
Python), répertoires 0700 et fichiers 0600 quels que soient l'umask et les modes
archivés, `O_EXCL` et `O_NOFOLLOW`, CRC-32 vérifié par lecture complète, taille
et SHA-256 de chaque membre, relecture complète de l'arbre, archive inchangée du
début à la fin. Manifeste au format exact du manifeste privé (relu et réécrit à
l'identique octet pour octet), publié atomiquement, comparé exactement à
`--expect-manifest`. Tout échec ou signal supprime seulement la destination créée
par l'invocation ; codes de sortie documentés par `--help`.

Écarts volontaires avec PocketBase (outil hors ligne, aucun effet client) :
la restauration de PocketBase (v0.22.34 `tools/archive/extract.go`, identique en
v0.40.6) ignore en silence liens et fichiers spéciaux, écrase avec `O_TRUNC` et
le mode archivé, crée les dossiers en `os.ModePerm` ; l'outil BUMA refuse,
n'écrase jamais, impose 0700/0600 et vérifie CRC, SHA-256, relecture et archive.
Le format accepté est celui que produit PocketBase (v0.22.34
`tools/archive/create.go`), contrôlé sur une vraie sauvegarde 0.22.34 de données
synthétiques.

Durées constatées sur archives synthétiques seulement : 1 290 membres et
8,85 Go en environ 103 s (RSS 27 Mo). Le ZIP réel n'a jamais été traité sur le
serveur. Points ouverts mineurs, sans blocage : un signal reçu pendant le
nettoyage d'un échec ordinaire rend le code 3 sans `cleanup_error` ; `nohup`
n'est pas respecté (lancer dans tmux ou screen) ; quelques protections sans test
dédié (comparaison SHA-256 avant/après, `O_EXCL`, `O_NOFOLLOW`) ; l'aide omet le
code 2. Régressions : `tests/test_c11_extract.py` (113 tests ; 20 mutants
supprimant chacun une protection, tous détectés).

## D-0025 — C11 : D-0019 résolu, relations maxSelect null multiples (PPBase et BUMA)

Session serveur du 2026-10-24, chantiers WS-A (PPBase) et WS-E (BUMA), relus
indépendamment, sur l'autorisation du plan (§2) de corriger PPBase en prenant
PocketBase pour référence. Résout le blocage D-0019 sans aplatir aucune liste.

Sémantique de référence, citée et vérifiée sur le code officiel :

- PocketBase v0.22.34 `models/schema/schema_field.go` : l.693 `MaxSelect *int`
  (« If nil no limits are applied »), l.709 la valeur 0 est refusée, l.715-716
  `IsMultiple() = MaxSelect == nil || *MaxSelect > 1`, l.150-165 colonne
  `JSON DEFAULT '[]'` (multiple) ou `TEXT DEFAULT ''` (simple), l.375-386
  `PrepareValue` (`tools/list/list.go` l.110-140) ; validation de tout le
  formulaire avant les hooks (`forms/validators/record_data.go` l.52-97 et
  l.361-398, `forms/record_upsert.go` l.472-563) ; suppression
  `daos/record.go` l.619-776 ; filtres `resolvers/record_field_resolve_runner.go`
  l.356-477 et `tools/search/filter.go`.
- Version actuelle v0.40.6 `core/field_relation.go` l.98 et l.151-162
  (`IsMultiple() = MaxSelect > 1`) ; montée v0.23.0
  `migrations/1717233556_v0.23_migrate.go` l.844-848 (`maxSelect <= 0`, donc
  null, devient 2147483647).

PPBase, branche `fix/d0019-relation-maxselect` (fusion `526397a` de `c11/ws-a`,
poussée sur `ssakone/ppbase`) :

1. Règle unique `ppbase.models.field_multiplicity` : relation avec `maxSelect`
   présent et null → multiple sans limite ; entier n → multiple si n > 1 ; clé
   absente → simple ; select ou file à null → simple. Le schéma stocké n'est pas
   réécrit : l'API renvoie toujours `maxSelect: null`. Écarts voulus, sans effet
   sur BUMA (les 191 relations de l'export portent la clé) : clé absente au
   format 0.22 lue simple, `maxSelect: null` au format plat lu multiple.
2. Colonnes créées désormais : relation multiple `TEXT[] NOT NULL DEFAULT '{}'`
   avec index GIN, relation simple `TEXT NOT NULL DEFAULT ''` avec btree partiel,
   sans limite de longueur comme PocketBase. Les colonnes `VARCHAR(15)` existantes
   de même forme restent compatibles (une valeur de plus de 15 caractères y est
   refusée, jamais tronquée).
3. Écritures selon `PrepareValue` (une chaîne envoyée à une relation multiple
   devient une liste d'un élément, une liste envoyée à une relation simple garde
   le dernier ID, décodage JSON partiel à la Go). L'API record refuse une cible
   inexistante (`validation_missing_rel_records`) avant les hooks de requête et
   après les 403/404, en listant aussi les autres erreurs du formulaire ; les
   valeurs posées par un hook et les écritures internes ne sont pas vérifiées,
   comme `dao.SaveRecord`.
4. Suppression : portage de `Dao.DeleteRecord` ; l'ID est retiré des relations
   qui le référencent (mise à jour de `updated` et événement realtime), cascade
   seulement quand la liste devient vide, refus (400, message PocketBase) si une
   relation requise serait vidée.
5. Filtres : un champ multiple, direct ou en fin de chemin, est comparé par son
   texte JSON ; `:length` et `:each` suivent PocketBase ; une liste `@request`
   est liée en texte JSON ; un identifiant invalide donne 400.
6. Base créée avant la règle : PPBase refuse de démarrer
   (`MultiValueShapeMismatchError`) tant qu'une colonne contredit la règle ;
   réparation explicite et transactionnelle par
   `app.reconcile_multi_value_columns(...)` dans une migration.

BUMA (`c11/ws-e`, 7 commits) :

- Migration `1789236850_c11_relation_multiple_columns` : sur une base migrée
  avant la correction, `buma_customer.pdv`, `buma_pilotage_ticket.tag` et
  `buma_wallet.managers` passent de `varchar(15)` à `text[] NOT NULL DEFAULT '{}'`
  avec index GIN (`''` et `'[]'` → `{}`, toute autre valeur sans `[` →
  `{valeur}`) ; refus atomique d'une valeur contenant `[` autre que `[]`, d'une
  forme inattendue, d'une vue dépendante ou d'un PPBase non corrigé ; verrou
  `ACCESS EXCLUSIVE` ; aucun effet sur une base neuve ; `_collections` jamais
  écrit. `down` seulement si chaque tableau a au plus un élément non vide d'au
  plus 15 caractères. À appliquer serveur arrêté (un serveur resté démarré garde
  des requêtes préparées de l'ancien type et échoue une fois).
- Graine C5 `1789236846` : `CUSTOMER_DIVERS` n'envoie plus `pdv`, comme
  `master.setup.ts` l.52-60 (PocketBase stocke alors `[]`). Le contenu d'une
  migration déjà appliquée change parce que l'ancien `''` explicite échoue sur
  une colonne `TEXT[]` ; là où elle est enregistrée, rien n'est rejoué et
  `1789236850` convertit la valeur `''`.
- `app/legacy/records.py` `prepared()` et `app/legacy/views.py` utilisent la
  règle PPBase au lieu de `(maxSelect or 1) > 1`.
- Vérificateurs : forme physique des 191 colonnes relationnelles contrôlée, mode
  strict `fresh` par défaut (multiple `text[]` + GIN, simple `text` + btree),
  `--relation-columns migrated` explicite pour une base créée avant la correction
  (les 188 relations simples y restent `varchar(15)` : les élargir imposerait de
  recréer les vues).
- Bancs : `scripts/c9_commerce_runtime.py` attend `[id]` pour `customer.pdv`
  (la source réécrit toujours `pdv`, `master.retour.functions.ts` l.96-99, et
  PocketBase stocke une liste) ; `scripts/c11_rehearsal.py` attend des tableaux ;
  `scripts/c11_idempotency_runtime.py` propage le `PYTHONPATH` de l'appelant au
  serveur isolé et vérifie le PPBase réellement chargé.

Impact client : `pdv`, `tag` et `managers` sont servis en listes (API, expand
ordonné, realtime), comme en production ; une écriture API vers une cible
inexistante reçoit 400, comme en production, y compris la mise à jour d'un
record qui porte un orphelin historique ; les suppressions nettoient désormais
les références comme en production ; la règle de consultation de `buma_wallet`
renvoie 404 comme PocketBase au lieu de couper la connexion. BUMA exige
désormais ce PPBase corrigé (`ppbase.models.field_multiplicity`) ; le
`pyproject.toml` ne peut pas l'exprimer par la version (0.3.2 dans les deux
cas) : la révision PPBase requise est consignée dans la note de passation.

Écarts PPBase restants, antérieurs à ce changement et documentés dans
`docs/relation-multiplicity.md` de PPBase (xfail stricts) : multi-match des
traversées et de `@collection` (faille des règles `users`, traitée par D-0030),
feuille NULL d'une relation absente dans un chemin, enveloppe d'erreur
`status`/`code` et messages, code de dépassement `maxSelect`, `minSelect` non
appliqué, `+champ` appliqué, expand `_via_` absent, `expand: {}` présent, tri sur
un champ multiple, aucun hook de modèle pendant le nettoyage des références.

Preuves : suite PPBase 927 passés, 71 sautés, 6 xfail stricts et exactement les
10 échecs antérieurs ; sonde appariée `tests/pocketbase_parity/relation_multiplicity_probe.py` :
132 valeurs comparées au binaire PocketBase 0.22.34, 119 identiques, les 13
autres étant les écarts ci-dessus (0.22.27 donne les mêmes valeurs) ; BUMA :
`tests/test_c11_relation_columns.py` (41 tests, dont sondes 0.22.27 et 0.22.34),
`scripts/c11_relation_columns_runtime.py` (base partielle, rejeu, down/up des
5 dernières migrations, serveur démarré), banc d'idempotence C4/C7/C9 et
répétition passants sur le PPBase corrigé.

## D-0026 — C11 : contrat de l'importeur, anomalies source conservées et vérification exacte

Chantier WS-B de la session serveur du 2026-10-24 (`scripts/c11_import.py`,
`scripts/c11_snapshot_audit.py`, `scripts/c11_volume_benchmark.py`), relu
indépendamment sur trois tours. Applique le choix par défaut du plan (§2,
conforme à D-0009 et D-0021) : importer telles quelles les références orphelines
et les références de fichiers absents.

1. **Anomalies conservées, non bloquantes.** Une relation sans cible et une
   référence de fichier absent sont importées telles quelles : aucune cible ni
   fichier inventé, aucune référence supprimée, scindée ou redirigée
   (PocketBase 0.22.34 stocke une relation en texte sans clé étrangère, PPBase
   n'en crée pas). Publication en agrégats (`preserved_source_anomalies`, par
   collection et champ : références, records, cibles ou fichiers distincts) ;
   détail par record seulement dans `--private-report` (fichier neuf 0600, sous
   `backups/`). Après chargement, PostgreSQL recompte ces anomalies ; le résultat
   doit égaler le diagnostic (`anomaly_conservation_mismatch`, sinon annulation).
   Sur le backup réel : les 195 795 références orphelines (5 champs) et les
   2 303 fichiers absents (2 champs) de D-0021, à l'identique.
2. **Types cibles.** Contrôle préalable de chaque champ de l'export contre sa
   colonne PostgreSQL (introspection une fois par table), puis de chaque valeur
   avant écriture : longueur `varchar(n)` (éléments compris), tableau ou scalaire,
   NOT NULL, NUL et UTF-8 invalide, entiers, double exact, booléen, timestamptz
   au format écrit par PocketBase, JSON. Codes explicites, **aucune troncature ni
   conversion** (PostgreSQL 16 tronque en silence les espaces excédentaires d'un
   `varchar(n)` : ce cas est refusé). Sur le backup réel, aucune valeur hors
   D-0019 ne dépasse sa colonne.
3. **Version source.** Registre `VALIDATED_SOURCE_VERSIONS`, preuve publique relue
   à l'exécution (statut `passed` et version déclarée) ; 0.22.27 → rapport de
   répétition historique ; 0.22.34 → D-0024 (ajout par WS-B3).
4. **Chargement et vérification exacte**, dans une seule transaction de
   l'appelant, sur une base nommée `buma_c11_rehearsal` : définitions identiques
   au diagnostic ; seuls les deux déclencheurs rowid de `1789236848` admis, avec
   leur définition exacte, et aucun déclencheur d'événement (contrôlés trois
   fois) ; COPY binaire par lots de 5 000 ; relecture typée de chaque lot, puis
   `SET CONSTRAINTS ALL IMMEDIATE`, relecture complète de chaque table et de
   l'archive rowid (compte, empreinte multiensemble, somme des hash PostgreSQL),
   total égal au diagnostic, recomptage des anomalies. Les erreurs PostgreSQL et
   de décodage ne sont jamais chaînées (le DETAIL cite la ligne) : seuls
   collection, SQLSTATE et contrainte sont rapportés. Réglages limités à la
   transaction : `plan_cache_mode = force_custom_plan`,
   `max_parallel_workers_per_gather = 0`.
5. **Robustesse.** Échec injecté sans aucun effet (72 tables vides, pas
   d'archive, staging supprimé, aucun verrou ni session restants) ; reprise propre
   sur la même cible ; deux imports simultanés : un seul réussit, l'autre reçoit
   `target_not_empty` ; SIGTERM et SIGHUP transformés en annulation avec
   nettoyage (`cancelled_by_termination`). Après SIGKILL ou OOM, seul le staging
   privé reste, à supprimer à la main.
6. **Mode manifeste** (paquet du serveur, sans `storage/`) : `--manifest` remplace
   `storage/` ; `data.db` doit égaler son entrée, les WAL et journaux doivent être
   vides, une entrée de 0 octet porte les empreintes du contenu vide ; aucun octet
   copié (`file_bytes_copied: false`) et le rapport le dit. Sur le backup réel,
   l'empreinte de l'inventaire tirée du manifeste égale celle du vrai `storage/`
   diagnostiqué sur le Mac le 2026-09-28.
7. **Mesures** (jeu synthétique de 1 182 406 records, PostgreSQL limité à 2 Go et
   2 CPU, hôte partagé) : import 243 à 321 s selon la charge, environ
   1 400 instructions au lieu d'environ 3,55 millions, RSS de l'importeur
   114 Mio ; rollback d'un import à moitié 91 à 128 s ; diagnostic réel 110 à
   128 s (RSS 146 à 169 Mio) ; audit 30 à 38 s.

Impact client : aucun par lui-même (outil hors ligne) ; il fixe ce que la base
PPBase contiendra à la bascule : les historiques, y compris leurs anomalies, sont
ceux de PocketBase. Régressions : `tests/test_c11_import.py`,
`tests/test_c11_snapshot_audit.py`, `tests/test_c11_volume_benchmark.py`,
`negative_diagnostics()` de `scripts/c11_rehearsal.py` (20 cas),
`python -m scripts.c11_volume_benchmark --negative-only` (13 refus vérifiés sans
effet) et `docs/c11-volume-benchmark-report.json`.

## D-0029 — C11 : représentation jsonb et lectures normalisées par PocketBase (écarts documentés, acceptation demandée)

Constatés par WS-B en comparant les deux binaires PocketBase et PPBase ; aucun
n'est créé par l'import, qui garde les valeurs stockées.

- **Ordre des clés JSON.** PocketBase 0.22.34 sert les octets JSON stockés
  (`tools/types/json_raw.go` l.26-32) ; PPBase stocke en `jsonb`, qui trie les
  clés. Même valeur après décodage, ordre d'itération différent. Backup réel :
  7 objets (`roles.permission` 6, `buma_config.posConfig` 1). Avertissement
  `jsonb_key_order_normalized`.
- **SQL NULL d'un champ json.** PocketBase sert `null` et son filtre `= null` est
  vrai pour SQL NULL comme pour JSON null ; la colonne PPBase est
  `JSONB NOT NULL DEFAULT 'null'` et son filtre `= null` est compilé en
  `IS NULL`. Même valeur servie, filtres `= null` et `!= null` différents (écart
  général de PPBase). Backup réel : 100 valeurs (`users.more` 12,
  `buma_article.specification` 44, `buma_article.details` 44). Avertissement
  `sql_null_json_stored_as_jsonb_null`.
- **Notation des nombres JSON.** `jsonb` réécrit `1e2` en `100` ; un décodeur
  typé (Dart) lit l'un comme double, l'autre comme entier. Backup réel : 0.
  Avertissement `jsonb_number_notation_normalized`.
- **Listes servies normalisées par PocketBase.** À chaque lecture, PocketBase
  0.22.34 passe select, file et relation par `list.ToUniqueStringSlice` puis
  `NonzeroUniques` (`models/schema/schema_field.go` l.351-386) : une liste
  multiple perd ses `""` et ses doublons, une valeur simple stockée comme texte
  de tableau JSON est servie par son dernier élément. PPBase sert la valeur
  stockée. L'import garde la valeur (ni dédoublonnage ni aplatissement, interdits
  par le plan). Backup réel : 0 cas sur 8 650 valeurs multiples et environ
  12,5 millions de valeurs simples. Avertissements
  `list_served_deduplicated_by_pocketbase`, `single_value_served_as_last_element`.
- Restent **bloqués** : clés JSON dupliquées (`jsonb_duplicate_keys_collapsed`,
  jsonb ne garde que la dernière) et textes que PocketBase servirait comme chaîne
  JSON (`json_text_served_as_string_by_pocketbase`) : 0 cas réel.

Statut : écarts documentés et signalés par avertissement, sans effet mesuré sur
le backup réel hors les 7 ordres de clés et les 100 NULL. **Acceptation de
l'utilisateur demandée** pour les deux premiers ; la correction possible côté
PPBase (filtre `= null` compilé en `(col IS NULL OR col = 'null'::jsonb)`,
colonne JSON nullable, nombres servis avec leur texte, normalisation des listes à
la lecture) reste ouverte. Régressions : tests jsonb et listes de
`tests/test_c11_import.py`, cas correspondants de `negative_diagnostics()`.

## D-0027 — C11 : comptes, paramètres et continuité des sessions

Chantiers relus indépendamment : WS-C (BUMA, `scripts/c11_auth_import.py`, contrat
`docs/contracts/c11-auth-import.md`, banc `scripts/c11_auth_runtime.py`, preuves
d'origine mesurées avec PPBase `c6821aa`), WS-A2 (PPBase, branche `c11/ws-a2`,
fusionnée dans `fix/d0019-relation-maxselect` en `237b403`) et WS-C2 (BUMA, banc et
contrat alignés sur le PPBase corrigé, fusionnés dans `c11/integration` en
`0bb0b93` ; rapport `docs/c11-auth-runtime-report.json` régénéré avec l'aperçu PPBase
`22929a0` = fix + WS-A5 + WS-A2). Banc rejoué par l'orchestrateur sur
`c11/integration` avec PPBase `237b403`, puis sur l'état intégré final (PPBase
`fix/d0019-relation-maxselect` et raccordement BUMA du mode d'erreur 0.22, D-0034) :
passant, rapport régénéré sur cet état (révisions relevées dans le rapport et au
ledger). Applique le choix par défaut du
plan (§2) : reprendre les secrets de jetons pour que les sessions continuent. La
correspondance suit la mise à niveau officielle de PocketBase
(`v0.23.0 migrations/1717233556_v0.23_migrate.go`) partout où PPBase sait
représenter la valeur.

**Import** (`import_auth`, appelé par `scripts.c11_import.import_snapshot` dans la
même transaction que les records, D-0026 ; moteur `hide_parameters=True`
obligatoire ; cible non vide refusée ; records du même snapshot, vérifiés par
l'empreinte `rowid:id` de chaque collection) :

- `_admins` → `_superusers` : id, email, hash bcrypt, tokenKey, avatar, created et
  updated copiés à l'identique, aucun mot de passe régénéré. `lastResetSentAt` est
  importé dans `last_reset_sent_at` (même instant, `""` devient NULL) et son texte
  source archivé avec le rowid dans `_buma_c11_auth_archive` ; une valeur qui n'a pas
  la forme PocketBase bloque (`admin_value_not_representable`). Motif : PPBase
  applique au reset admin le délai de 120 s de v0.22.34
  (`forms/admin_password_reset_request.go` l.73-76) ; sans cet import, un e-mail de
  reset de plus pouvait partir dans les 120 s suivant la bascule. Écart à l'upgrade
  officiel 0.23, qui ne reprend pas ce champ, sans autre effet client. Backup réel :
  1 admin, valeur vide, donc NULL.
- Secrets et durées : les 5 configurations `record*` vers les options de `users`
  et `agent_users`, `adminAuthToken`, `adminPasswordResetToken` et
  `adminFileToken` vers `_superusers` ; une valeur vide garde la valeur cible ;
  une valeur hors des limites de PocketBase est importée telle quelle avec un
  avertissement (PocketBase 0.22 signe avec elle).
- Paramètres : `meta` (appName, appUrl devenu appURL, hideControls, expéditeur),
  `logs` (maxDays, minLevel, logIP), `smtp` complet, modèles d'e-mail (sujet et
  corps, `{ACTION_URL}` remplacé).
- **Non importés**, chacun avec un code : `s3` (`s3.enabled` bloque),
  `backups.s3` (activé : avertissement `backup_s3_target_not_imported`),
  `backups.cron` (avertissement), fournisseurs OAuth2 (un fournisseur activé
  bloque), `emailAuth` (obsolète), `_externalAuths` (une ligne bloque ; 0 dans le
  backup), `logs.db` (jamais importé), paramètres chiffrés (bloquent). Backup réel
  (structure seulement) : 1 administrateur, 8 configurations de jetons valides sur 8,
  aucun OAuth ni S3, un cron de sauvegarde : prêt, un avertissement.

**Continuité prouvée** sur binaire PocketBase 0.22.34 et données synthétiques :

- WS-C (PPBase `c6821aa`) : les jetons admin, users et agent_users émis par
  PocketBase **avant** l'import sont acceptés par PPBase après (route superuser,
  records protégés, refresh, `/isConnected`, `/master/user/health`), jusqu'à leur
  expiration ; les mots de passe d'origine fonctionnent ; les invalidations
  PocketBase sont conservées (jeton d'avant un reset ou un changement d'e-mail
  refusé).
- WS-C2 (PPBase `22929a0`, deux passages identiques hors durées, PPBase chargé par le
  serveur vérifié dans `/proc`) : 33 sondes HTTP sur 33 identiques en statut et
  libellé, 19 résultats realtime sur 19 identiques (et 3 types de jeton
  informatifs), 0 secret servi (5 GET, 2 PATCH de settings, 6 PATCH de collections).
  Jetons d'usage 0.22 émis avant l'import sur des comptes dédiés, consommés en
  dernier par PPBase puis par PocketBase redémarré sur sa source, qui sert de
  référence : mêmes statuts, effets et connexions (vérification 204 ; reset 204 puis
  changement d'e-mail du même compte 400 ; changement d'e-mail avec mauvais mot de
  passe 400, puis 204, puis reset du même compte 400 ; reset admin 204 ; verified,
  tokenKey renouvelé, `last_reset_sent_at` remis à NULL). Les liens de vérification,
  de reset et de changement d'e-mail émis avant la bascule restent valables par
  l'API jusqu'à leur expiration.
- Claims, algorithme et clé vérifiés dans les deux codes : HS256, `tokenKey` +
  secret du type (v0.22.34 `tokens/*.go`, `apis/middlewares.go` l.229-249 ; PPBase
  `ppbase/api/deps.py`, `get_optional_auth`, à `237b403`).

**Corrections PPBase** (chantier WS-A2, intégrées à `237b403`), mesurées contre le
binaire 0.22.34 :

- realtime : identité `collectionId:id`, jamais le type de jeton ; un client qui
  rafraîchit son jeton sur la même connexion SSE n'est plus refusé (403) ;
- secrets jamais servis : sans les secrets de jetons ni `clientSecret` dans les
  collections (un secret absent, `null`, vide ou masqué soumis garde le secret
  stocké, `merge_auth_options`) ; `/api/settings` sous la forme masquée de 0.22.34
  (le masque et `null` gardent le secret stocké, une valeur vide l'efface) ;
- `hide_parameters=True` sur tous les moteurs (les valeurs liées ne sont plus
  ajoutées aux erreurs par SQLAlchemy ; asyncpg ou PostgreSQL peuvent encore
  citer une valeur : journaux PPBase à traiter comme confidentiels) ;
- jetons à usage 0.22 (`authRecord`) et jetons de fichier sans claim `for`
  acceptés ; routes 0.22 de réinitialisation du mot de passe admin, avec le délai
  de 120 s ;
- liens e-mail construits sur le seul `meta.appURL` stocké, jamais sur des
  en-têtes du client ; sans `PPBASE_SMTP_*`, aucun e-mail n'est envoyé (un
  avertissement sans destinataire, lien ni code est journalisé ; hors `PPBASE_DEV`) ;
- statuts et libellés des middlewares 0.22.34 ; jeton forgé vers une collection
  non auth traité comme anonyme.

**Écarts restants**, documentés (impact client) :

- après un login ou un refresh PPBase, le jeton porte `type=auth` (PPBase
  `ppbase/services/auth_service.py` l.19 et l.487, comme v0.40.6
  `core/record_tokens.go` l.13 et l.62) au lieu de `authRecord` (v0.22.34
  `tokens/record.go` l.21) : avec le SDK JS 0.21, `isAuthRecord` devient faux
  (`BaseAuthStore.ts` l.51-53 de js-sdk v0.21.5) ; `isValid` et le SDK Dart
  (v0.18.1, qui ne lit que `exp`) sont inchangés. Aucun usage trouvé dans les
  projets présents sur le serveur ; les clients BUMA Master (desktop, web, mobile)
  ne sont pas sur le serveur : **à vérifier sur ces clients avant la bascule** ;
- révocation globale : `PATCH /api/settings` (procédure « Invalidate all
  previously issued tokens » du dashboard 0.22) répond 200 sans effet dans PPBase,
  alors que PocketBase révoque. La seule révocation globale **mesurée** est la
  rotation SQL de `options.authToken.secret` (contrat §7, prouvée sans
  redémarrage) ; à la lecture du code, `PUT /api/collections/import` avec un
  secret explicite non masqué le remplacerait aussi (non mesuré, à ne pas
  utiliser). Après une rotation, un retour arrière vers PocketBase fait revivre
  les sessions révoquées ;
- enveloppe d'erreur : PocketBase 0.22.34 répond `{code, message, data}`
  (`apis/api_error.go` l.12-15), la version actuelle et PPBase par défaut
  `{status, message, data}` (v0.40.6 `tools/router/error.go` l.36-42 ; la recopie
  de `status` dans `code` de `237b403` est retirée du PPBase intégré). BUMA active
  le mode d'erreur PocketBase 0.22 de PPBase (`pb.set_error_compat("0.22")`,
  D-0034) : `{code, message, data}` et messages de 0.22.34 ; mesuré sur les 22 refus
  du banc (mêmes statuts, mêmes libellés, mêmes clés d'enveloppe des deux côtés,
  désormais exigées par le banc). Écarts restants du mode : D-0034 ;
- corps de refus des jetons d'usage, statuts identiques (WS-C2) : changement
  d'e-mail avec un jeton invalidé, PPBase ne signale que `token` (0.22.34 aussi
  `password`) ; mauvais mot de passe : `validation_invalid_credentials` au lieu de
  `validation_invalid_password` (le message du reset avec un jeton invalidé est,
  lui, celui de 0.22.34 sous le mode 0.22). Impact : un client qui lit
  `data.password` ou ces codes ; aucun appelant BUMA connu. Test :
  `PURPOSE_KNOWN_DIVERGENCES` du banc, épinglé des deux côtés ;
- les pages `/_/#/auth/confirm-*` et `/_/#/confirm-password-reset` ouvertes par les
  liens e-mail (0.22 comme PPBase) n'existent pas dans le dashboard PPBase : ces
  liens ne valent que si un client poste le jeton à l'API ; sinon l'utilisateur
  arrive sur la connexion ;
- `POST /api/files/token` sans auth répond 401 (sous le mode 0.22, avec le message
  de garde « The request requires admin or record authorization token to be set. »)
  au lieu de 400 « Failed to generate file token. » (mesuré par la relecture WS-A2) ;
  aucun champ fichier protégé dans BUMA ;
- forme de `GET /api/settings` (clés propres à PPBase) ; un PATCH sans changement
  n'écrit rien et ne diffuse rien ;
- identité realtime `collectionId:id` (version actuelle) contre l'id seul en
  0.22.34 : 0 id partagé entre chaque paire de `users`, `agent_users` et `_admins`
  du backup réel (agrégat mesuré en lecture seule par la relecture WS-ORCH du
  2026-10-24), donc sans effet ;
- e-mails : ils passent par `PPBASE_SMTP_*` seulement, **sauf** l'e-mail de test
  `POST /api/settings/test/email`, envoyé avec le `_params.settings.smtp` importé,
  donc avec les identifiants SMTP de production (`ppbase/api/settings.py` l.482-534,
  `_smtp_config_from_settings` l.151-180). Risque : un envoi par le SMTP de
  production depuis un PPBase de répétition ou de staging. Consigne : ne jamais
  appeler cette route tant que `smtp` vient de la production (contrat §3 et §9,
  runbook).

**Exploitation** : le gel des écritures doit précéder le backup frais (l'import
recopie l'état d'authentification du snapshot) ; aucune reconnexion forcée à la
bascule ; au retour arrière vers PocketBase intact, les jetons 0.22 restent
valides, les jetons record émis par PPBase sont refusés (reconnexion), les jetons
admin émis par PPBase restent acceptés sauf après une rotation, et un jeton d'usage
0.22 consommé sur PPBase reste utilisable sur PocketBase jusqu'à son expiration
(mesuré). Avant d'ouvrir : vérifier `meta.appURL` importé (seule base des liens,
sinon `http://localhost:8090`), ne configurer `PPBASE_SMTP_*` que si des e-mails
doivent partir, ne jamais activer `PPBASE_DEV` en production ; traiter les journaux
PPBase comme confidentiels ; pendant les répétitions sur données réelles, ne jamais
appeler les routes `request-*`, `confirm-*` ni l'e-mail de test.

Non mesuré : le sort, au retour arrière, des jetons d'usage émis par PPBase après
la bascule ; un lien déjà consommé dans PocketBase après le backup, qui redevient
valable sur PPBase (conséquence du snapshot) ; les vrais SDK JS et Dart et le
desktop (le realtime est sondé au niveau HTTP/SSE avec les requêtes du SDK JS
0.21) ; le délai de 120 s côté PPBase, prouvé par la relecture en base et les tests
WS-A2, pas par HTTP (le banc ne donne pas de SMTP à PPBase).

Régressions : `tests/test_c11_auth_import.py` (68 tests, chaque refus et
avertissement), banc `scripts/c11_auth_runtime.py` (sondes appariées
PocketBase/PPBase, jetons d'usage, révocation, exposition des secrets, retour
arrière, trois rollbacks injectés aux étapes des comptes et l'import concurrent ;
l'échec côté records est prouvé par `scripts/c11_rehearsal.py`), tests PPBase de
WS-A2.

## D-0030 — C11 SÉCURITÉ : multi-match et LEFT JOIN de PocketBase 0.22.34 dans les règles (faille des règles users)

Constat de la session serveur du 2026-10-24 (chantier PPBase WS-A3, sonde appariée
avec le binaire officiel 0.22.34) : `users.createRule` et `users.updateRule`
commencent par `@collection.users.id = @request.auth.id`. PocketBase 0.22.34 ajoute
à toute comparaison sans `?` qui passe par `@collection`, une relation multiple ou
une relation inverse sans index unique une sous-requête « aucune ligne ne doit
échouer » (`tools/search/filter.go` l.183-215 et l.569-706 ;
`resolvers/record_field_resolve_runner.go` l.144-182) : avec deux comptes ou plus,
la clause est fausse, et toujours fausse à la création (le compte créé est déjà dans
la table, `apis/record_crud.go` l.215-260). PPBase (jusqu'à `526397a`) utilisait un
EXISTS « au moins une ligne ». Mesuré : un utilisateur ordinaire créait des comptes
(200 au lieu de 400), modifiait le sien et ceux des autres (200 au lieu de 404) et
pouvait s'attribuer le rôle administrateur. Données réelles (agrégats) : 20 comptes,
dont 4 à rôle isAdmin et aucun rôle `user.create` ni `user.update` : les 16 autres
pouvaient s'élever administrateurs.

Correction PPBase (branche `c11/ws-a3`, incluse dans `c11/ws-a5`) : chemins relation,
relation inverse et `@collection` en LEFT JOIN partagés par préfixe (une cible absente
ou orpheline donne une feuille NULL), `=` et `!=` selon `resolveEqualExpr`,
multi-match pour les opérateurs sans `?` avec la clause NULL de 0.22.34 ; liste par
un invité ou un record dont `filter` ou `sort` contient `@collection.` ou `@request.` :
403 « Only admins can filter by … » (`apis/record_helpers.go` l.319-345). Les
comportements de la version actuelle ne sont pas repris (clause NULL et dry submit
retirés en v0.24.0) : les clients BUMA sont sur 0.22.34.

Impact client, identique à la production : un utilisateur ordinaire ne peut plus créer
ni modifier de compte ; `buma_pdv.deleteRule` (`client.id = '' && agent.id = ''`)
autorise désormais, comme PocketBase, la suppression des 1 930 PDV réels sans client
ni agent ; `rels.x = v` exige que toutes les lignes liées correspondent. Écart restant :
une création refusée par la règle répond `data.rule` au lieu de `data: {}`.
Régressions : `tests/integration/test_multi_match_rules.py` (53 filtres et matrice des
règles BUMA), `tests/integration/test_admin_only_filter_fields.py`, six anciens xfail
stricts devenus tests, sonde `relation_multiplicity_probe.py --groups multimatch,buma_rules`.

## D-0031 — C11 SÉCURITÉ : colonnes auth cachées non filtrables, valeurs @request absentes sans erreur

Chantier PPBase WS-A5 (branche `c11/ws-a5`), sondes appariées avec le binaire 0.22.34.

1. **Colonnes cachées.** PPBase résolvait tout identifiant vers sa colonne physique :
   avec `users.listRule = ""`, un anonyme pouvait tester `password_hash~"$2a$12$A"`,
   `token_key!=""` ou un e-mail masqué (200 avec correspondances : oracle
   d'extraction). PocketBase 0.22.34 ne résout que les champs du modèle, du schéma et,
   pour une collection auth, username, verified, emailVisibility et email
   (`resolvers/record_field_resolve_runner.go` l.718-732) ; tout autre nom donne un 400
   opaque, et email est restreint à `emailVisibility = TRUE` pour un non-admin
   (l.356-375). Correction identique dans PPBase, sur les feuilles directes, de
   relation, de relation inverse et de `@collection`, et sur le tri ; en realtime, la
   règle d'accès (champs cachés permis) est séparée du filtre client (refusés) et le
   contrôle `@collection.`/`@request.` s'applique au filtre et au tri d'abonnement.
2. **Valeurs @request.* absentes.** La répétition réelle a montré des erreurs 500
   (connexion fermée) pour un anonyme sur `buma_article_stockage`,
   `buma_caisse_open_close`, `buma_customer`, `buma_employee_list_view`, `buma_pdv`,
   `buma_pdv_list_view` : PPBase liait une valeur absente en `''` et générait
   `$n IS NOT NULL` ou `'' = TRUE` (IndeterminateDatatypeError, DataError). PocketBase
   la résout comme NULL (`resolvers/record_field_resolver.go` l.162-223) et n'échoue
   jamais. Correction : égalités PostgreSQL équivalentes à PocketBase ; une clé de
   permission absente du contexte BUMA vaut faux, comme NULL dans PocketBase.
3. **Formes de liste.** `totalPages` d'une liste vide vaut 0 (`tools/search/provider.go`
   l.254) ; un record sans relation développée n'a pas de clé `expand`
   (`models/record.go` l.545-559).

La première relecture a trouvé deux contournements, fermés avant approbation (tête
`da2d25b`) : la forme qualifiée (`users.password_hash~…`, `users.token_key`,
`users.email`) et les modificateurs d'un champ système (`email:lower`, `:length`,
`:isset`, `:each`), désormais des champs inconnus (400 opaque, comme 0.22.34). Le
payload realtime ne diffuse plus les e-mails masqués ; `emailVisibility` et
`verified` envoyés en multipart (`"false"`, `"0"`) sont lus comme `cast.ToBool` ; une
valeur `@request.*` présente mais non booléenne comparée à `true` ne produit plus de 500.

Impact client : aucune règle ni aucun filtre BUMA (114 définitions, `app/`, source
gelée) ne référence un champ désormais refusé ; les listes anonymes et les rôles
partiels reçoivent 200 et les records autorisés au lieu d'une erreur 500. Écarts
restants : une règle référençant un champ caché n'est pas refusée à l'enregistrement
de la collection (aucune règle BUMA) ; username n'est filtrable que sur les
collections auth qui ont la colonne physique (cas de BUMA) ; `?=` et `?!=` comparés à
une valeur `@request.*` absente ne correspondent plus à rien, alors que PocketBase la
traite comme vide (`resolveEqualExpr`) : la seule règle BUMA concernée,
`buma_wallet.viewRule` (`@request.auth.id ?= managers`), est précédée de
`@request.auth.id != ''`, donc sans effet ; PPBase accepte encore les modificateurs
`:lower` et `:changed` sur les champs du schéma, que 0.22.34 refuse (extension
antérieure, aucun usage BUMA). Régressions :
`tests/integration/test_hidden_auth_filter_fields.py`,
`tests/integration/test_request_static_rules.py`, `tests/test_filter_parser_request_static.py`,
sondes `hidden_fields_probe.py` et `request_static_rules_probe.py`.

## D-0032 — C11 : dates vides servies, filtrées et triées comme PocketBase

PocketBase stocke une date vide en `''` et la sert `""` (`tools/types/datetime.go`
l.40-58) ; PPBase la stocke en NULL (colonne `TIMESTAMPTZ NULL`, inchangée) et la
servait `null`. Le backup réel contient 335 972 dates vides dans 10 des 18 champs date.
Correction PPBase (chantier WS-A4, intégré à `c11/ws-a5`) : une date vide est servie
`""` dans toutes les réponses de records ; filtres (`= ""`, `!= ""`, comparaisons) et
tri (ASC et DESC) donnent les résultats de PocketBase ; aucune migration de données.
Écarts restants documentés (`docs/empty-dates.md` de PPBase) : une colonne de vue
NULL par LEFT JOIN (sans ligne correspondante) ne se distingue pas d'une date vide :
pour `<`, `<=`, `!~` et le tri ASC, PPBase la traite comme `""` là où PocketBase voit
NULL (la relecture a montré que l'écart couvre aussi `<=`) ; une chaîne de date
illisible est stockée `''` par PocketBase, refusée (400) par PPBase. Impact non mesuré :
les filtres des clients BUMA (desktop, web, mobile) ne sont pas sur le serveur.
Régressions : `tests/integration/test_empty_dates.py`, `tests/test_empty_dates.py`,
sonde `empty_date_probe.py`.

## D-0033 — C11 SÉCURITÉ : l'expand applique la viewRule de la collection liée, e-mails des auth records comme EnrichRecords

Constat de WS-A5, vérifié par l'orchestrateur : `ppbase/services/expand_service.py`
ne contrôlait aucune règle, alors que PocketBase 0.22.34 filtre chaque record
développé par la viewRule de sa collection avec le contexte de la requête
(`apis/record_helpers.go` : `RecordAuthResponse` l.105-116, `EnrichRecords` l.153-174,
`expandFetch` l.177-210 ; `daos/record_expand.go` l.37-302 ; même structure en
v0.40.6). Dans le schéma BUMA, 49 relations partent d'une collection lisible par un
anonyme vers une cible dont la viewRule n'est pas publique (dont `buma_customer` 8,
`buma_sim` 7, `buma_pdv` 7, `buma_agent` 6, `buma_supplier` 4,
`buma_caisse_transactions` 1) : un anonyme y lisait par expand des records protégés
(par exemple `buma_sale.customer`, `buma_customer_pdv.pdv`,
`buma_payment.caisse_transaction`, `users.employee`, `buma_buy.supplier`). Constat
complémentaire de la relecture : les réponses de création et de mise à jour d'un
auth record renvoyaient toujours l'e-mail ; avec `users.updateRule` de BUMA, tout
utilisateur connecté lisait l'e-mail caché d'un autre par un PATCH ; à l'inverse, la
liste et la vue ignoraient la manageRule, si bien qu'un rôle admin BUMA ne voyait pas
les e-mails que la production lui montre (`autoIgnoreAuthRecordsEmailVisibility`,
l.216-279).

Correction PPBase (chantier WS-A6, relu deux fois, fusionné par WS-A8, relu par deux relecteurs, intégré dans
`fix/d0019-relation-maxselect` en `67bbc4f`) :
chaque niveau développé est lu par une requête par chemin et par niveau, filtrée par
la viewRule de sa collection avec le même évaluateur et le même contexte `@request.*`
que la liste ; un admin voit tout ; une viewRule null fait échouer le chemin ; un
niveau imbriqué refusé supprime le chemin entier ; les cibles refusées ou absentes
sont omises dans l'ordre du champ ; back-relations `_via_` ; e-mails cachés montrés
seulement à l'admin, au propriétaire et à qui satisfait la manageRule (liste, vue,
création, mise à jour, batch, `enrich_records`, realtime) ; les réponses d'auth
développent comme le record authentifié, avec le contexte de rôle fourni par l'hôte.

Raccordement BUMA (`d0a6dc8`, fusionné dans `c11/integration`) : `pb.set_auth_record_rule_context(load_auth_record_context)`
(`app/hooks/auth.py`) et `request_context=auth_response_rule_context(...)` dans la
connexion par username (`app/auth/login.py`), qui développait sans aucune règle ni
masquage (par exemple `agent_users.agent.parent` → `buma_customer`).

Impact client, identique à la production : un anonyme ou un rôle sans le droit ne
reçoit plus par expand les records protégés ; un rôle admin BUMA voit de nouveau les
e-mails des users dans les listes ; un appelant non autorisé ne reçoit plus d'e-mail
caché dans une réponse d'écriture ; la réponse d'inscription d'un invité masque
l'e-mail sauf `emailVisibility` ; rien ne change pour un appelant autorisé. Écarts
restants : ordre des back-relations par `ctid` (aucun client BUMA n'utilise `_via_`) ;
quand la manageRule échoue à l'évaluation, 0.22.34 saute aussi l'expand des records
principaux ; `@request.context` vaut `default`/`realtime` pendant l'expand (comme
0.22.34, `expand` en v0.40.6) ; préexistants : inscription d'invité qui fixe
`emailVisibility` refusée (403) au lieu d'acceptée, `fields=id,expand` retire
l'expand. Realtime : l'e-mail caché du record principal n'est plus diffusé (correction
WS-A5 gardée à la fusion ; 9 événements identiques au binaire) ; les événements
`delete` restent envoyés `{id}` seul, aux seuls admins et abonnements à règle publique
sans filtre, alors que 0.22.34 envoie le record complet développé à chaque abonné dont
la règle et le filtre correspondaient avant la suppression (`apis/realtime.go`
l.310-336, l.424-437) : écart ouvert, du côté restrictif (impact : un client BUMA
abonné qui lit les champs d'un événement `delete` n'en reçoit que l'id, ou ne le reçoit
pas s'il n'est pas admin et que la règle n'est pas publique), figé par
`test_realtime_delete_events_carry_only_the_id` et mesuré par la sonde. Régressions : `tests/integration/test_expand_view_rules.py`,
`tests/test_expand_service.py`, sonde `tests/pocketbase_parity/expand_rules_probe.py`
(105 contrôles d'expand, 18 écritures, 8 réponses d'auth, 4 messages realtime) ; BUMA :
`tests/test_auth_contract.py`.

## D-0034 — C11 : erreurs au format PocketBase 0.22 pour BUMA (mode de compatibilité PPBase activé par BUMA)

PocketBase 0.22.34 répond `{code, message, data}` (`apis/api_error.go` l.11-15) avec
ses propres messages (« Only admins can perform this action. »,
`apis/record_crud.go` l.57) ; la version actuelle et PPBase par défaut répondent
`{status, message, data}` (v0.40.6 `tools/router/error.go` l.36-42) avec d'autres
textes (« Only superusers … »). La répétition réelle sur le PPBase de l'aperçu :
247 contrôles HTTP sur 256 identiques à PocketBase, les 9 autres étant cette
enveloppe et ces libellés. Le corps seul ne distingue pas certains cas (création
d'un admin et d'un utilisateur, validation et persistance, deux gardes).

Correction PPBase (WS-A6, fusionné par WS-A8) : mode explicite, désactivé par défaut
(`pb.set_error_compat("0.22")`, `PPBASE_ERROR_COMPAT=0.22`) ; un seul post-traitement
central réécrit l'enveloppe de toute erreur JSON de PPBase en `{code, message, data}`
avec le message de 0.22.34 (table citée, et étiquettes posées par les handlers dans un
en-tête privé `x-ppbase-error-kind`, toujours retiré) ; les statuts ne sont jamais
modifiés ; un corps propre à l'hôte n'est jamais réécrit ; les corps ont les octets
d'echo (clés de `data` triées, `<`, `>`, `&` et U+2028 échappés, saut de ligne final :
mesurés sur le binaire). À la fusion (WS-A8), la recopie de `status` dans `code`
ajoutée par WS-A5 est retirée : le mode par défaut de PPBase redevient l'enveloppe
exacte de la version actuelle, et toutes les erreurs ajoutées par WS-A5 et WS-A2
(champs cachés, « Only admins can filter by … », formulaires et reset admin, gardes,
corps illisibles, settings, backups) passent par le mode 0.22.

Raccordement BUMA (`app/hooks/register.py`, première instruction de `register(pb)`) :
`pb.set_error_compat("0.22")`.

Impact client : les SDK 0.22 lisent de nouveau `code` et les messages de production.
Écarts restants, épinglés par test : 7 statuts non réécrits (auth-refresh avec un
jeton admin 403/401 ; route d'auth sur une collection de base 404/400 ; jeton de
fichier sans auth 401/400 ; méthode inconnue 405/404 ; trois gardes de PPBase non
utilisées par BUMA) ; détails de `data` (textes des contraintes de champ, doublon
d'e-mail `validation_not_unique` au lieu de `validation_invalid_email`, violation
d'index unique `data: {}`, jeton de vérification illisible, confirm-password-reset
sans champs, identité admin vide, pas de limite de 2048 caractères de la query
string) ; une exception non gérée reste un 500 en texte ; `POST /api/files/token` sans
auth répond 401 avec le message de garde 0.22 au lieu de 400 « Failed to generate file
token. » ; l'en-tête `Content-Type` des réponses JSON est `application/json` au lieu de
`application/json; charset=UTF-8` (classe `json_content_type_charset` de la répétition
synthétique, en-tête seulement ; propre à 0.22.34, qui l'hérite d'echo v5 ; v0.40.6
envoie `application/json`, `tools/router/event.go` l.206 ; sans effet sur les SDK : le
SDK Dart v0.18.1 décode le corps par `response.stream.bytesToString()`, en UTF-8 par
défaut quel que soit l'en-tête, le SDK JS par `response.json()`). Mesures avec le raccordement BUMA : répétition
synthétique complète 0.22.34 passante, 84 sondes dont 82 identiques en statut et corps,
0 différence inattendue, enveloppes d'erreur identiques à l'octet ; banc des sessions :
33 sur 33.
Régressions : `tests/test_error_compat_unit.py`, `tests/integration/test_error_compat.py`,
`tests/integration/test_error_compat_guards.py`, groupes `errors` et `guards` de la
sonde ; BUMA : `tests/test_auth_contract.py`, répétitions synthétique et réelle.

## D-0035 — C11 : totalItems compté comme PocketBase (COUNT(DISTINCT id))

PocketBase compte les records d'une liste par `COUNT(DISTINCT id)` dans les deux
versions (v0.22.34 `tools/search/provider.go` l.238-256, v0.40.6 l.300-320) ; PPBase
comptait `COUNT(*)`. Les deux coïncident pour une collection de base (id unique),
pas pour une vue à ids répétés : sur le vrai backup, la vue publique
`buma_article_stockage_list_view` a 989 lignes pour 42 ids distincts ; PocketBase sert
`totalItems` 42 (et `totalPages` calculé dessus), PPBase servait 989 (constat de la
relecture WS-G). Correction PPBase (WS-A8, `8c23510`, intégré en `67bbc4f`) : `COUNT(DISTINCT id)` pour
les listes de vues (le `COUNT(*)` équivalent est gardé pour les tables, à ids uniques) ;
`totalPages` et `skipTotal` suivent ; écart restant : sous un filtre ou une règle avec
jointure, PocketBase fusionne les lignes identiques d'une vue (`SELECT DISTINCT`), PPBase
les sert toutes (aucune vue BUMA n'a de lignes identiques, mesuré sur le vrai backup). Impact
client : pagination identique à la production (`totalItems` et `totalPages`) ; les
items servis ne changent pas. Régression : `tests/integration/test_list_total_items.py`,
cas ajoutés à `tests/pocketbase_parity/empty_date_probe.py`, contrôle des
totaux de la répétition réelle (attendu `COUNT(DISTINCT id)` de la source).

## D-0036 — C11 : NULL servi comme la valeur zéro du type ; filtres et tris des NULL de vues (écart ouvert, acceptation demandée)

Constat de la répétition réelle (WS-G), qui compare les valeurs servies à un modèle tiré
des valeurs SQLite brutes : sur 11 vues BUMA, 2 813 valeurs NULL issues de LEFT JOIN
sans correspondance étaient servies `null` par PPBase, alors que PocketBase 0.22.34
sert la valeur zéro du type (par exemple `buma_article_list_view.categorie_*`,
`buma_caisse_open_close_listview.currentCashier`, `buma_pdv_list_view.network`,
`number` et `sim`, `buma_sim_list_view.*`, `buma_transaction_list_view.returnPayed` et
`return_note`, `buma_users_list_view.person` et `photos`). PocketBase lit un NULL SQL
comme nil et passe chaque champ par `PrepareValue` (v0.22.34 `models/record.go`
l.48-91 et l.328-359, `models/schema/schema_field.go` l.304-390 ; mêmes valeurs en
v0.40.6, `core/record_model.go` l.484-518 et `core/field_*.go`, sauf geoPoint, absent de
0.22.34) : `""` pour text, email, url, editor, date et select, file ou relation simples ;
`0` pour number ; `false` pour bool ; `[]` pour select, file ou relation multiples
(multiplicité de D-0019/D-0025) ; `null` pour json.

Correction PPBase (WS-A9, `ppbase/models/record.py`, `null_field_value` dans
`build_record_response`, donc sur tous les chemins : liste, consultation, écritures,
batch, expand vers et depuis une vue, réponses d'auth, realtime) ; valeurs non NULL,
filtres, tris et totaux inchangés ; un json NULL reste `null`. Les tables de base et
auth créées par PPBase sont NOT NULL hors dates : BUMA n'est concerné que par ses vues.
**Écart ouvert, acceptation de l'utilisateur demandée (ou reprise)** : filtres et tris
sur une colonne NULL (en pratique une colonne de vue issue d'un LEFT JOIN). PocketBase
compte un NULL de colonne chaîne dans `= ""` (et un json NULL dans `= null`) et le trie
en premier en ordre croissant, en dernier en ordre décroissant (SQLite) ; PPBase garde
la sémantique NULL de PostgreSQL (NULL exclu de `= ""`, trié en dernier en ordre
croissant). Impact : un filtre comme `network = ""` sur `buma_pdv_list_view` peut rendre
moins de lignes qu'en production, et un tri sur une telle colonne place ces lignes
autrement ; aucune règle d'API BUMA n'emprunte ces formes. Le chantier WS-A11 (branche
PPBase `c11/ws-a11`, `61bc951`, 17 commits) corrige le placement des NULL sans erreur 500
nouvelle, mais n'a pas été approuvé après deux relectures (régressions hors NULL : json
comparé à une colonne, date comparée à un json dans les chemins, deux tests instables) :
il n'est pas intégré et reste disponible pour reprise.

Impact client : les vues BUMA servent les valeurs de production ; un client typé (Dart,
TypeScript) ne reçoit plus `null` pour un champ text, number, bool, select, relation ou
file d'une ligne LEFT JOIN sans correspondance ; aucun appel ne change. Régressions :
`tests/test_null_zero_values.py`, `tests/integration/test_null_zero_values.py` (3 sur 5
échouent sans le correctif), sonde `tests/pocketbase_parity/null_zero_values_probe.py`
(42 écarts de valeurs servies avant, 0 après) ; répétition réelle (contrôle
`null_served_for_pocketbase_zero_value`).

## D-0037 — C11 : fichiers servis comme PocketBase (v0.22.34 sous le mode 0.22, v0.40.6 par défaut)

Écarts relevés par la répétition synthétique complète (WS-D2) : `?thumb=100x100` servait
l'original, aucun `Content-Disposition`, type de contenu déduit de l'extension au lieu
des métadonnées `.attrs` écrites par PocketBase. Correction PPBase (chantier WS-A7,
relu trois fois) sur `GET`/`HEAD /api/files/…`, règles communes aux deux versions dans
les deux modes : type lu dans le side-car `<fichier>.attrs` d'un stockage local
(indécodable : 404) ; `Content-Disposition` `inline` pour les types sûrs, `attachment`
sinon ou avec `?download` ; en-têtes de sécurité et `Cache-Control` de PocketBase ;
`HEAD` comme `GET` ; miniatures `100x100` et `thumbs` du champ (stockée : servie octet
pour octet ; sinon générée et publiée avec son side-car, une génération par chemin ;
échec : original) ; 404 pour un record absent, un nom non référencé, un fichier absent
et toute clé `.attrs`, même référencée (v0.22.34 `apis/file.go` l.40-219,
`tools/filesystem/filesystem.go` l.367-526 ; v0.40.6 `apis/file.go` l.22-242,
`tools/filesystem/filesystem.go` l.521-721). Le mode 0.22 (même interrupteur que
D-0034, donc actif pour BUMA) suit v0.22.34 là où les versions diffèrent : nom brut du
`Content-Disposition` (v0.40.6 le quote depuis v0.40.0), surcharges d'extension,
miniature webp, décodage json v1 des `.attrs`, jeton de fichier trimé comme
`strings.TrimSpace` (segments décodés comme golang-jwt dans les deux modes).

Impact client : images, miniatures et pièces jointes servies comme en production (le
schéma BUMA compte 34 champs fichier, aucun `protected`, aucun avec `thumbs` : seule la
miniature `100x100` s'applique ; aucune des 2 899 références réelles ne finit par
`.attrs`). Écarts restants (`docs/file-serving.md` de PPBase) : un fichier téléversé par
PPBase n'a pas de side-car `.attrs` et une suppression ne retire pas le side-car, donc
PocketBase servirait un tel fichier en `application/octet-stream` `attachment` après un
retour arrière (à vérifier dans le runbook) ; S3 : type de l'extension, sans miniature ;
pas de `304`/`412`/multi-plages comme `http.ServeContent`, `ETag` en plus ; octets des
miniatures générées (Pillow contre imaging) ; side-car de plus de 64 Kio refusé ;
fichier protégé refusé en 404 au lieu de 403 (0.22.34) et sans évaluation de la
viewRule en invité (aucun champ protégé dans BUMA) ; téléversement d'un fichier nommé
`*.attrs` accepté par PPBase, refusé par PocketBase. Régressions :
`tests/test_file_serving_rules.py`, `tests/test_file_thumbs.py`,
`tests/integration/test_files_pocketbase_storage.py`, sonde
`tests/pocketbase_parity/files_probe.py` (109 requêtes, 0 différence inattendue dans
les deux modes) ; répétition synthétique (classes de fichiers disparues).

## D-0038 — C11 SÉCURITÉ : noms de fichiers d'une écriture vérifiés comme PocketBase

Faille préexistante (relecture WS-A7, chantier WS-A10, relu deux fois) : jusqu'à PPBase
`3817c89`, un champ fichier acceptait tout nom bien formé (mise à jour JSON, valeurs
texte et `@jsonPayload` d'un multipart, création, batch, `createData` d'OAuth2,
`pb.records()`), et le service des fichiers sert tout nom référencé : un client autorisé
par l'updateRule pouvait référencer puis télécharger un side-car `.attrs`, un objet non
référencé du dossier du record (686 dans le `storage/` réel) ou le fichier d'un autre
record. Mesuré : objet non référencé 404, puis `PATCH {"doc": "<objet>"}` 200, puis
`GET` 200 avec le contenu ; 39 cas sur 49 différents de 0.22.34. Les deux versions de
PocketBase refusent (v0.22.34 `forms/record_upsert.go` l.428-439 ; v0.40.6
`core/field_file.go` l.250-269). Correction (`ppbase/services/record_file_fields.py`) :
la valeur finale de chaque champ fichier est calculée à partir de la valeur stockée, de
la requête et des téléversements, sur tous ces chemins ; mode par défaut comme v0.40.6
(400 `validation_invalid_file`), mode 0.22 comme 0.22.34 (400 « Failed to load the
submitted data due to invalid formatting. », `validation_unknown_filenames`, et les
modificateurs, suppressions héritées et téléversements multiples de 0.22.34).

Impact client, identique à la production : un client ne peut plus référencer un fichier
qu'il n'a pas téléversé dans ce record ; garder, retirer, réordonner ou dédoublonner
les noms existants reste permis. Données réelles (agrégats) : 2 899 noms stockés dans
20 champs, aucun refusé ; aucun fournisseur OAuth2 activé ; aucun hook ni service BUMA
n'écrit de champ fichier. Écart assumé : en mode 0.22, un nom posé par un hook de
requête dans `e.data` est refusé, alors que 0.22.34 enregistre ce qu'un hook pose
(les hooks PPBase reçoivent les données de la requête ; v0.40.6 valide après les hooks).
Régressions : `tests/integration/test_record_file_names.py`,
`tests/test_record_file_fields.py`, sonde `tests/pocketbase_parity/file_names_probe.py`
(56 cas, 0 différence).

## D-0039 — C11 : treize vues servies avec les valeurs des vues SQLite de PocketBase 0.22.34 ; nombres et booléens des vues (acceptation demandée)

Constat de la répétition réelle (WS-G, modèle des valeurs servies tiré des valeurs SQLite
brutes) : la traduction PostgreSQL de plusieurs vues (`1789236842`, corrigée par
`1789236847` et `1789236848`) servait d'autres valeurs que PocketBase 0.22.34 :
`payedPercent` de `buma_sale_info` en division réelle au lieu de la division entière de
SQLite ; `ROW_NUMBER() OVER ()` renuméroté à chaque requête par PostgreSQL (vue des
dépenses `buma_caisse_transaction_by_categorie_list_view` : 2 476 lignes sur 15 797 servies
deux fois et autant jamais en pages de 500) ; éléments concaténés (`pdv_sim`, `stockages`,
`stockage`) dans un autre ordre ; nombres servis en chaîne et entiers 1/0 servis
`true`/`false`. Une première correction par domaines PostgreSQL et 48 opérateurs a été
écartée à la relecture (`?=` silencieusement différent, tris en 500).

Correction BUMA, migration `1789236851_c11_view_sqlite_semantics.py` (chantier WS-H, relu
trois fois) : treize vues recréées sans CASCADE, schémas et règles inchangés, une seule
fonction `buma_source_rowids` qui lit l'archive `_buma_c11_rowids` de l'import :
`payedPercent` comme SQLite (division entière tronquée si les deux sommes sont entières,
NULL pour un diviseur nul au lieu d'une erreur 500 ; les 115 093 opérandes réels sont
entiers) ; les sept vues `ROW_NUMBER()` numérotées dans l'ordre de SQLite (ordre rowid,
ordre du GROUP BY, ou « 1 » pour une vue d'une ligne ; repli stable pour une ligne créée
après l'import : après les lignes archivées, par created puis id) avec un id texte
`CAST(… AS TEXT) COLLATE "C"` comme le `cast(id as text)` de PocketBase ; ordre des
concaténations comme SQLite (rowids archivés, ou valeur pour les deux vues stock) ;
colonnes nues (`isNafama`) et `user` cité (la traduction lisait `CURRENT_USER`). Seuls
les 7 id (bigint → text) et `uvUser` (name → text) changent de type sur les 579 colonnes
des 42 vues ; `down` restaure à l'identique. Mesuré sur le vrai backup : numérotation
identique à SQLite pour 15 797 lignes sur 15 797 (avant : 4 778), 2 611 sur 2 611 pour
`buma_caisse_session_info` (avant : 1), pagination sans perte ni doublon.

**Écarts restants, acceptation de l'utilisateur demandée** :

- (`number_served_as_text`) `agent_sim` et `pdv_sim` des quatre vues clients et retours
  et `number` de `buma_agent_list_view` : PocketBase sert la valeur SQLite comme un champ
  json (un nombre JSON, ou `""` pour la valeur de repli), PPBase une chaîne ; impact : un
  client typé qui lit ces champs comme un nombre (Dart `as int`, comparaison stricte en
  JS) reçoit une chaîne ;
- (`sqlite_integer_boolean_served_as_boolean`) `isNafama` de
  `buma_customer_mobile_list_view`, `payed` et `salePayed` de `buma_transaction_list_view` :
  PocketBase sert `1`/`0`, PPBase `true`/`false` ; impact : un client qui teste `== 1`
  reçoit faux ;
- les filtres, `?=` et tris sur ces colonnes répondent comme avant la migration ;
- un nombre comparé à l'id d'une vue numérotée (`id = 3`, `<`, `>`) ou l'id comparé à une
  colonne nombre répond 500 au lieu de 200 (PocketBase compare `'3'` à `'3.0'` et ne trouve
  rien) ; c'est déjà la réponse de PPBase pour l'id texte de toute collection ; `id = "3"`,
  `?=`, `~` et getOne répondent comme PocketBase ;
- une liste sans `sort` des vues numérotées à plusieurs lignes suit l'ordre texte de l'id
  (`"1"`, `"10"`, `"100"`…) car PPBase trie par défaut la première colonne, alors que
  PocketBase n'ajoute pas d'ORDER BY : le contenu est complet, l'ordre des pages diffère ;
- la lecture par id des deux vues stock concatène dans l'ordre de la valeur (comme une
  liste de PocketBase), alors que PocketBase lit alors dans l'ordre rowid ;
- sommation compensée de SQLite pour les SUM des vues, nombres en float8, notation `7e2`
  servie `700` (catégorie de D-0029) ;
- coût : la vue des dépenses est 2,5 à 3,5 fois plus lente (count 111 → 383 ms, première
  page 122 → 301 ms, getFullList 4,3 → 11,2 s ; `buma_return_list_view` inchangée) ;
- exploitation : `_buma_c11_rowids` porte désormais la numérotation et l'ordre des
  concaténations : il doit être conservé par les sauvegardes et restaurations après la
  bascule (la sauvegarde native de PPBase refuse cette table et la fonction, comme pour
  `1789236848` : sauvegarde par `pg_dump`, runbook).

Régressions : `tests/test_c11_view_sqlite_semantics.py`, `tests/test_view_verifier.py`,
banc `scripts/c11_view_semantics_runtime.py` (binaire 0.22.34, matrice de 6 433 requêtes
avant et après la migration, 190 requêtes appariées, pagination), vérificateurs du
schéma (12 migrations, 114 définitions, 42 vues), répétition réelle (contrôles
`served_values`).

## D-0040 — C11 : répétition synthétique complète sur le code final (PocketBase 0.22.34, profil real_shape)

`scripts/c11_rehearsal.py` (chantiers WS-D et WS-D2, adapté par WS-C2 et à l'intégration)
est la répétition synthétique complète et la porte avant fusion : une fixture
`real_shape` construite par le binaire officiel PocketBase 0.22.34 (relations multiples,
références orphelines, fichiers absents, doublons RETURN, IDs personnalisés, select non
déclarés, JSON null, dates vides, comptes, superuser, paramètres, jetons), des jetons
émis par PocketBase **avant** l'import, l'import par `import_snapshot`, puis un serveur
PPBase isolé comparé réponse par réponse au binaire. Elle prouve, en deux passages
identiques : la fidélité de l'import (records, rowids, tableaux dans l'ordre stocké,
orphelins, dates, JSON null, comptes, superuser, 16 chemins de paramètres, secrets de
jetons), l'acceptation des jetons 0.22.34 d'avant l'import (admin, users, agent_users,
refresh, realtime), les mots de passe d'origine, les fichiers présents servis octet pour
octet et les absents en 404, trois échecs injectés qui laissent l'état migré vide, un seul
gagnant sur deux imports concurrents, le schéma (114 définitions, 42 vues) avant et
après. Sur l'état final (BUMA `b7427fe`, PPBase `069e0bb`, mode d'erreur 0.22 activé),
les **84 réponses comparées sont identiques à PocketBase 0.22.34 en statut et en corps**,
sans différence inattendue ; seule reste ouverte la classe `json_content_type_charset`
(en-tête `Content-Type` sans `charset`, D-0034). Toute autre différence fait échouer la
répétition ; les classes d'enveloppe d'erreur, de libellés 401/403 et de détail d'échec
de connexion ont été retirées : leur retour serait une régression. Rapport public :
`docs/c11-synthetic-rehearsal-report.json` (régénéré sur l'état final ; l'ancien rapport
`docs/c11-rehearsal-report.json` reste l'historique de C11).

## D-0041 — C11 : répétition complète sur le vrai backup, sans octets de fichiers ; ce qu'elle prouve et ne prouve pas

`scripts/c11_real_rehearsal.py` (chantier WS-G, relu trois fois, et ses correctifs
d'intégration) importe le vrai backup PocketBase 0.22.34 (lecture seule, `mode=ro&immutable=1`,
Python `-I`) dans un PostgreSQL jetable possédé par le script (refus de toute autre
cible), en mode manifeste sans octets de fichiers, puis contrôle données et réponses
HTTP. Publication : `docs/c11-real-rehearsal-report.json`, deux passages sur le code
commité et propre (BUMA `b7427fe`, PPBase `fix/d0019-relation-maxselect` `069e0bb`),
le 2026-10-25 de 12:46 à 13:08 UTC.

**Prouvé** (deux passages passés et identiques : empreintes des tables et hachages des
lignes stockées identiques) : les 1 188 247 records importés en une transaction, comptes
par table égaux au diagnostic ; empreintes des lignes converties égales aux lignes
relues après commit ; rowids archivés pour toutes les tables et rowid natif de `users` et
`buma_article` ; les trois relations multiples en tableaux égaux aux listes SQLite ; les
195 795 références orphelines et les 2 303 références de fichiers absents conservées et
recomptées (596 fichiers présents d'après le manifeste, aucun octet copié) ; comptes,
admin, secrets de jetons et paramètres égaux à la source ; schéma inchangé hors archives
de l'import ; les 42 vues interrogeables, lignes et ids distincts égaux à SQLite ; 414
contrôles HTTP (lectures publiques et protégées, listes, totaux, filtres dont relations
multiples et orphelines, expand, tri, refus, fichiers, valeurs servies comparées à un
modèle de la sérialisation 0.22.34 tiré des valeurs SQLite brutes, indépendant de
l'importeur) : 405 sans écart, 0 échec, les autres couverts par des divergences
documentées dont l'acceptation est demandée (D-0029 : 3, D-0039 : 7) ; copie privée
inchangée (empreinte et listage) ; confidentialité : 0 violation sur 3 442 001 valeurs.
Durées par passage : diagnostic 144-146 s, import 248-249 s, contrôles après commit 77-78 s,
lectures HTTP 105-107 s ; base 784,5 Mo, staging 325,2 Mo (runbook).

**Non prouvé** : les octets des fichiers (prouvés sur la fixture, à refaire avec le ZIP
complet, D-0028) ; la continuité des jetons sur les comptes réels (fixture, D-0027) ;
l'ordre des tableaux à plusieurs éléments (aucun dans les vraies données ; fixture) ; les
valeurs des records non lus par HTTP (couvertes par l'empreinte des lignes converties) ;
les clients desktop, web et mobile ; les écritures, hooks et realtime sur les données
importées ; la collation d'un PostgreSQL glibc ou ICU (la cible musl trie les octets
comme SQLite ; un tri `en_US` glibc changerait l'ordre de 2 905 clients sur 5 826) ; la
production (fraîcheur du backup, fenêtre, configuration du PostgreSQL de production),
**hors périmètre : import de production et bascule ni demandés, ni autorisés, ni
exécutés**.

## D-0042 — C11 : booléens écrits convertis comme PocketBase (cast.ToBool) ; validation partielle des mises à jour (écart ouvert)

Constat (relecture de WS-A11, vérifié par WS-A12, relu deux fois) : `_validate_bool` de
PPBase faisait `bool(value)` ; le SDK JS envoie les booléens en chaînes dans un FormData
dès qu'un fichier est joint, si bien qu'un `"false"` ou un `"0"` multipart (ou une chaîne
JSON `"false"`) était enregistré `true`, et un bool required acceptait `"false"` ; la
sonde appariée trouvait 84 cas sur 145 différents de 0.22.34. PocketBase convertit par
`cast.ToBool` (v0.22.34 `forms/record_upsert.go` l.366-417, `models/schema/schema_field.go`
l.346-347 ; identique en v0.40.6, `core/field_bool.go` l.103-106).

Correction PPBase (WS-A12, intégrée dans `fix/d0019-relation-maxselect` en `069e0bb`),
pour la création, la mise à jour et le batch, en JSON, multipart et `@jsonPayload` :
booléen gardé, null faux, nombre vrai s'il est non nul, chaîne vraie seulement pour
`"1"`, `"t"`, `"T"`, `"TRUE"`, `"true"`, `"True"` (sans trim), toute autre valeur fausse ;
en mode 0.22, première valeur d'une clé multipart répétée, comme 0.22.34 ; clés
modificatrices (`flag+`, `flag-`, `+flag`) sans effet sur un bool, comme dans les deux
versions ; booléens JSON natifs et règle required inchangés. Sonde appariée : 254 cas,
0 différence en mode 0.22. Écart du mode par défaut seulement (sans effet sur BUMA) : un
nombre multipart autre que 0 et 1 vaut faux, comme 0.22.34, alors que v0.40.6 le lit vrai
(`inferValue`).

Impact client : un booléen envoyé en FormData, en chaîne JSON, en clé répétée ou avec une
clé modificatrice est enregistré comme en production.

**Écart ouvert, préexistant, relevé par WS-A12** : à la mise à jour, PPBase ne valide que
les champs que la requête change, alors que PocketBase valide tous les champs du record
(v0.22.34 `forms/validators/record_data.go` l.74-90 ; v0.40.6 `core/record_model.go`
l.1427-1441). Mesuré : sur un record dont un bool required vaut déjà `false`,
`PATCH {"label": "b"}` répond 400 dans 0.22.34 et 200 dans PPBase. Sur le vrai backup
(agrégat en lecture seule), 872 des 989 records de `buma_article_stock` ont un `status`
required à `false` : en production, une mise à jour par l'API de ces records qui ne passe
pas `status` à `true` est refusée, PPBase l'accepte. Impact : PPBase est plus permissif
que la production sur ces mises à jour ; correction PPBase à décider (valider tous les
champs comme PocketBase), acceptation de l'utilisateur demandée d'ici là.

Régressions : `tests/test_bool_field_cast.py`, `tests/integration/test_bool_field_writes.py`,
sonde `tests/pocketbase_parity/bool_values_probe.py`.
