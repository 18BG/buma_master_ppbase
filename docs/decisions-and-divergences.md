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
  des jetons record (`tokens/tokens.go` l.4-7 : `TypeAuthRecord = "authRecord"`) ;
  la version actuelle émet `type=auth` (v0.40.6 `core/record_tokens.go` l.13).
  L'adaptateur accepte les deux : c'est ce qui rend valides après l'import les
  sessions émises par PocketBase 0.22.34 (D-0027).

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
