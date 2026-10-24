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
  non comme type JWT.

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
