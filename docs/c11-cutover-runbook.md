# Runbook C11 — bascule et retour arrière

**Version finale WS-F2 du 2026-10-24, non exécutée.** Ce document décrit la
bascule des clients BUMA Master de PocketBase 0.22.34 (SQLite) vers PPBase et
PostgreSQL, puis le retour arrière. Il n'autorise rien : l'import de
production et la bascule exigent une autorisation distincte de l'utilisateur,
un backup frais et une fenêtre de maintenance (`docs/server-session-plan.md`,
sections 1 et 8). `AGENTS.md` prime sur ce runbook. Aucune de ses étapes n'a
été exécutée sur la production ; seules les répétitions citées en section 5
ont tourné, sur le serveur de session, dans des PostgreSQL jetables.

État à la rédaction :

- les commandes citées sont celles des outils intégrés à `c11/integration`
  (`0bb0b93`) et, pour `scripts/c11_real_rehearsal.py`, celles de la branche
  `c11/ws-g` (`d792a50`, en relecture : ses options peuvent encore changer) ;
  options et codes de sortie relevés par `--help` et dans le code ;
- les durées et les ressources mesurées viennent d'un passage d'**aperçu** de
  la répétition réelle et ne figurent qu'aux tableaux 5.1 et 5.2 ; le rapport
  final `docs/c11-real-rehearsal-report.json` n'existe pas encore :
  l'orchestrateur y reportera ses valeurs ;
- **aucune commande d'import de production n'existe** : le point d'entrée
  `import_snapshot` refuse toute base autre que la base jetable
  `buma_c11_rehearsal` (6.6). Tant que l'outil décrit en 10.2 n'est pas écrit,
  relu, testé et autorisé, la porte G0 est un NO-GO ;
- PPBase exigé : branche `fix/d0019-relation-maxselect` (`237b403` à la
  rédaction), complétée par WS-A6 et WS-A7 (10.1), à la révision que relève le
  rapport final (clé `code.ppbase.commit`), contrôlée par C-45.

## Conventions

- **L1 à L19** : lignes du tableau des durées (5.1) ; **R1 à R11** : lignes
  du tableau des ressources (5.2). Ces deux tableaux sont les seuls endroits
  du runbook où figurent des durées et des ressources mesurées ; ailleurs, le
  texte renvoie à leurs lignes.
- **[aperçu]** : valeur du passage d'aperçu de la répétition réelle (5.1), à
  remplacer par celle du rapport final.
- **[durée non mesurée]** : étape propre à la production, ou qui manipule les
  octets des fichiers, jamais chronométrée ; à mesurer au diagnostic à blanc
  (6.1), lors d'un essai autorisé par l'utilisateur ou le jour J.
- **[outil non attribué]** : contrôle ou action nécessaire pour lequel aucun
  outil n'existe ; l'intention est décrite ; l'outil est à écrire, relire et
  tester avant G0 (10.2).
- **[à confirmer à l'intégration]** : comportement attendu d'un chantier qui
  n'est pas encore intégré (10.1), à ne pas tenir pour acquis.
- `<…>` : valeur fournie le jour J. `I1` à `I25` renvoient aux informations
  de la section 3, `G0` à `G6` aux portes de la section 7, `C-01` à `C-46` aux
  contrôles de la section 6 ; C-44 à C-46, ajoutés à la version finale,
  figurent là où ils s'appliquent.
- `<privé>` : dossier privé de la bascule (dossiers 0700, fichiers 0600), hors
  de tout dépôt Git ou dans `backups/` (ignoré par Git). Les rapports publics
  ne contiennent que des agrégats et des empreintes de fichiers : jamais d'ID,
  de nom, d'email, de mot de passe ou de hash de mot de passe, de jeton, de
  secret, de montant, ni d'URL contenant des identifiants. Une capture ou un
  backup de production contient les `tokenKey` et les secrets des jetons et
  du SMTP : qui les lit peut signer des jetons acceptés par la production
  (clé `tokenKey` + secret, `tokens/record.go:13-27`). La base PPBase importée
  contient les mêmes secrets (D-0027). Ils se protègent comme la production.
- **Journaux confidentiels** : les journaux du processus PPBase, sa table
  `_requests`, les journaux du proxy, les rapports privés et le journal de
  bascule contiennent des données personnelles et parfois des valeurs citées
  par asyncpg ou PostgreSQL ; ils se protègent comme la production (6.11).
- Les commandes du dépôt se lancent depuis la racine du checkout BUMA de
  release, avec son venv (`.venv/bin/python`) et le fichier d'environnement
  de 6.1 chargé, jamais depuis le dossier de la capture ; les chemins de
  données sont passés en arguments. Le serveur PPBase se lance depuis un
  dossier vide (6.1, étape 4).
- Heures en UTC partout (journal, `T_gel`, `T_ouverture`) : un changement
  d'heure locale peut tomber dans la fenêtre.

## 1. Principes

1. **Un seul backend accepte des écritures à tout instant.** PocketBase est
   gelé avant la capture et reste arrêté ; PPBase n'est ouvert qu'après la
   porte G4 et il est arrêté avant tout redémarrage de PocketBase. Pas de
   bascule partielle des clients, pas de double écriture.
2. **PocketBase reste intact** : `pb_data`, binaire, hooks, migrations et
   ligne de commande ne sont ni modifiés, ni restaurés, ni nettoyés, ni mis à
   jour (`pocketbase update`). Toute analyse porte sur une copie. Un
   démarrage de PocketBase écrit dans `data.db` (6.3) : il n'a lieu que
   selon la règle de redémarrage de 6.3 ou en retour arrière.
3. **Copie à l'identique** : aucun doublon supprimé, aucune liste aplatie,
   aucune cible, aucun ID, rowid ou fichier inventé, aucune référence
   supprimée ou redirigée (D-0019, D-0021 à D-0023, D-0025, D-0026). Choix par
   défaut de l'utilisateur : références orphelines et références de fichiers
   absents importées telles quelles ; secrets de jetons repris pour que les
   sessions continuent (D-0027).
4. **Tout écart arrête la procédure** à la porte courante. On ne corrige ni
   code ni donnée pendant la fenêtre : on applique le retour arrière
   (section 8) ou on reporte.
5. **Chaque porte est relue par un vérificateur** qui n'a pas produit ses
   preuves, puis décidée par le décideur ; tout est consigné (section 9).
6. Les agents ne touchent pas la production : ce runbook est exécuté par
   l'équipe nommée par l'utilisateur (section 4).
7. **Aucun redémarrage non décidé pendant la fenêtre** : ni mise à jour
   automatique, ni redémarrage de service par `needrestart` sur les hôtes
   PocketBase, PPBase et PostgreSQL (C-46) ; un redémarrage de PocketBase sous
   gel suit la règle de 6.3.

Hors périmètre : stockage S3 (désactivé dans les paramètres du backup de
septembre), OAuth2 (`_externalAuths` vide), import de `logs.db` (conservé
dans la capture, jamais importé) et compatibilité SQLite de `POST /sql`
(reportée par D-0018 ; la console desktop envoie du SQL PostgreSQL).

## 2. Autorisations et prérequis (porte G0)

### 2.1 Autorisations distinctes de l'utilisateur

Écrites, datées et recopiées dans le journal :

- l'import de production et la bascule, nommément, pour une fenêtre donnée ;
- la fenêtre de maintenance (début, fin au plus tard, fuseau) et son annonce ;
- l'outil d'import de production (10.2) et la décision D-XXXX qui l'autorise
  à écrire dans la base de production, levée de la garde
  `isolated_rehearsal_database_required` comprise (6.6) ;
- le gel du schéma de production depuis l'export corrigé et la règle
  `buma_sql_table` (2.4), ou à défaut la procédure de ré-export et de
  revalidation ;
- le diagnostic à blanc d'un backup récent (6.1) : copie, machine,
  conservation et destruction ;
- toute répétition sur données réelles hors du serveur de session (6.1) : le
  plan de la session serveur n'autorise l'import de données réelles que dans
  un PostgreSQL jetable de ce serveur (`docs/server-session-plan.md`,
  section 2) ;
- le staging (6.9) : machine, accès, données réelles chargées, conservation,
  destruction et règles d'isolement ;
- les méthodes de gel (6.2), de capture (6.3) et de bascule d'URL (6.10) ;
- le plan de retour arrière : durée de la période de retour arrière et sort
  des écritures reçues par PPBase en cas de retour (8.3) ;
- les seuils d'arrêt de la section 7 et les marges de durée de 5.1 ;
- le sort de chaque écart et de chaque constat de la section 10 : correction,
  ou divergence acceptée avec son impact client et son test de régression
  (`AGENTS.md`) ; D-0029 attend explicitement l'acceptation de l'utilisateur ;
- la politique des machines pendant la fenêtre (C-46), proposée par
  l'infrastructure et approuvée par le décideur ;
- les personnes de chaque rôle (section 4).

### 2.2 Prérequis techniques acquis avant le jour J

- **Code de release figé.** BUMA : un commit de `features/auth` qui contient
  C11 intégré, dont la migration
  `pb_migrations/1789236850_c11_relation_multiple_columns.py` (D-0025),
  `scripts/c11_real_rehearsal.py` (WS-G) et l'activation du mode d'erreur
  PocketBase 0.22 par l'enregistrement des hooks BUMA (`app/hooks/register.py`)
  [à confirmer à l'intégration : WS-A6]. PPBase : branche
  `fix/d0019-relation-maxselect` (`237b403` à la rédaction : correction
  D-0019, WS-A5 et WS-A2), complétée par WS-A6 et WS-A7 ; la révision exigée
  est celle que relève le rapport final, `code.ppbase.commit` de
  `docs/c11-real-rehearsal-report.json` (C-45). La version du paquet PPBase
  ne peut pas l'exprimer (0.3.2 avant et après la correction, D-0025).
- **Répétition réelle finale passante** : `docs/c11-real-rehearsal-report.json`
  avec `status` `passed`, deux passages aux agrégats identiques
  (`repeatability.identical`), `accepted_divergences` limité à des écarts
  décidés, commits égaux aux commits de release (`code.buma.commit`,
  `code.ppbase.commit`) et arbres propres (`code.buma.tracked_files_clean`,
  `code.ppbase.tracked_files_clean`). Commande : annexe A. Ses valeurs
  remplacent celles de l'aperçu dans les tableaux 5.1 et 5.2.
- **Porte de release** (annexe A) passante sur ces commits, analyse des
  échecs PPBase comprise.
- **Outils de 10.2** nécessaires aux portes G1 à G4, dont l'import de
  production : écrits, relus indépendamment, testés sur données synthétiques
  puis en répétition, autorisés (2.1).
- Version de la production (I1, I2) : PocketBase **0.22.34**, la version que
  D-0024 valide comme source et que l'outillage reçoit avec le backup
  (`--source-version 0.22.34`), jamais devinée. La preuve vient du binaire en
  service : `pocketbase --version` et SHA-256 comparés au binaire officiel
  (annexe B), contrôle C-02 fait une première fois avant le jour J. Le ZIP de
  septembre ne désigne pas la version. Il en donne deux indices compatibles
  avec 0.22.34, qui ne sont pas des preuves : son `types.d.ts`, que le binaire
  réécrit au démarrage s'il diffère du sien (`plugins/jsvm/jsvm.go:150-157`,
  `:475-504`), est identique à celui des tags `v0.22.23` à `v0.22.57` ;
  `_migrations` contient les 14 migrations système du tag `v0.22.34`. Une
  autre version ou un build personnalisé arrête la préparation : la parité
  n'est établie que pour cette référence, et D-0024 n'a comparé que les
  binaires officiels linux amd64.
- Révision des hooks déployés (I2) : `530018c`. `_migrations` ne la prouve
  pas : il ne contient que des noms de migrations, et `1785599959`, dernière
  migration de la source (datée du 2026-08-01), n'y figurait pas en
  septembre (D-0022). Soit le dossier de migrations déployé ne la contient
  pas, soit PocketBase n'a pas redémarré depuis son dépôt ; dans le second
  cas, le prochain démarrage l'appliquerait (`apis/serve.go:66-69`,
  `plugins/jsvm/jsvm.go:175-213`). Preuve attendue, sur une copie en lecture
  des dossiers `--hooksDir` et `--migrationsDir` déployés
  [outil non attribué] :
  - hooks comparés fichier par fichier (taille, SHA-256) à un build de
    `530018c` fait sur la machine de préparation avec la commande de la
    source (`tsup src/entries/**/*.ts -d ./dist`, `package.json`) et ses
    lockfiles : 0 fichier manquant, en trop ou différent ;
  - migrations déployées comparées à `migrationsDir/` de la source et à
    `_migrations` : 0 migration en attente, sinon décision avant G0.

  Toute différence, y compris d'outil de build, est examinée par le
  vérificateur et décidée ; une autre révision arrête la préparation.
- `_migrations`, référence du contrôle C-08 : 1 336 lignes en septembre
  (`docs/c11-master-initial-diagnostic.json:720`), soit
  - 325 des 326 migrations source, toutes sauf `1785599959`
    (`docs/c11-idempotency-source-check.json`) ;
  - les 14 migrations système (`.go`) du tag `v0.22.34` ;
  - 252 lignes `*_updated_buma_sql_table.js` laissées par la console
    (2.4), du 2025-01-14 au 2025-11-21 ;
  - 745 automigrations du tableau de bord absentes de la source (91
    `created_`, 651 `updated_`, 3 `deleted_`), du 2024-02-25 au 2026-09-12.

  Agrégats WS-F en lecture seule sur la copie privée de septembre, empreinte
  de `data.db` inchangée, sans nom de migration publié.
- Gel du schéma respecté depuis l'export (2.4) et diagnostic à blanc
  conforme (6.1).
- Tests desktop, mobile et administration passants sur staging (6.9), sous
  l'autorisation et les règles d'isolement prévues.
- Écarts et constats de la section 10 corrigés ou décidés.
- PostgreSQL de production en version 16, celle des répétitions (R10) ; une
  autre version majeure impose une répétition supplémentaire. PPBase se
  connecte à PostgreSQL sans TLS (`ppbase/db/engine.py:50`) : PostgreSQL sur
  le même hôte ou sur un réseau privé de confiance (I6).
- Horloges synchronisées (NTP) sur l'hôte PocketBase, l'hôte PPBase et le
  serveur PostgreSQL (contrôles C-10 et C-28).
- **Aucun redémarrage automatique possible pendant la fenêtre (C-46).** Le
  2026-10-24, sur le serveur de session, `apt-daily-upgrade.timer` (06:26:29)
  a lancé `unattended-upgrade`, qui a installé un correctif `zlib1g`
  (06:27:11 à 06:27:15) ; le hook `DPkg::Post-Invoke` de `needrestart` 3.6
  (`-m u`) a alors redémarré les services liés à l'ancienne bibliothèque,
  dont l'hôte de la session (`docs/progress/ledger.md`, section « Interruption
  de 06:27 à 10:37 »). Pendant une bascule, le même mécanisme pourrait
  redémarrer PocketBase sous gel (capture annulée, 6.3), PostgreSQL pendant
  l'import (NO-GO) ou PPBase pendant les contrôles. L'infrastructure vérifie
  donc chaque hôte avant la fenêtre (6.1, étape 2) et décide ; ce runbook ne
  prescrit aucune modification système.
- Espace disque suffisant (2.3).

### 2.3 Dimensionnement connu

Mesures du backup de septembre (`docs/c11-master-diagnostic.md`), à
réévaluer sur le backup frais :

| Élément | Septembre 2026 |
| --- | ---: |
| ZIP de backup | 1 555 849 147 octets |
| Contenu décompressé (1 290 membres) | 8 526 465 264 octets |
| `data.db` | 324 898 816 octets |
| `logs.db` (non importé) | 3 310 325 760 octets |
| `storage/` | 1 282 objets, 4 890 458 144 octets |
| Records | 1 188 247 dans 72 collections, plus 42 vues |

Les ressources mesurées par la répétition (base, mémoire, disque) sont au
tableau 5.2. Elles ne couvrent que le mode sans octets de fichiers.

- Hôte PocketBase, capture A : PocketBase recommande un espace libre d'au
  moins deux fois la taille de `pb_data` (`core/base_backup.go:38-39`,
  v0.22.34), soit au moins 17,1 Go pour un `pb_data` de la taille du
  contenu de septembre.
- Machine de bascule, mode complet (ZIP et `storage/`), postes connus à la
  taille de septembre :

| Poste | Octets | Source |
| --- | ---: | --- |
| capture ZIP | 1 555 849 147 | septembre (tableau ci-dessus) |
| extraction vérifiée | 8 526 465 264 | septembre |
| réserve exigée par l'extraction | 1 073 741 824 | `scripts/c11_extract.py:68`, `:697-704` |
| staging de l'import en mode complet (`data.db` et `storage/`) | 5 215 356 960 | septembre ; `stage_snapshot`, `scripts/c11_import.py:2066-2077` |
| `storage/` de PPBase (6.8) | 4 890 458 144 | septembre |
| PostgreSQL (données, index, WAL) | 1 865 055 973 | R3 |
| sauvegarde de PPBase en retour arrière (8.3) | non mesurée | — |
| total, hors sauvegarde de 8.3 | 23 126 927 312 | somme |

Marge de sécurité proposée : ×2 sur ce total (croissance depuis septembre,
WAL et fichiers temporaires d'une autre configuration PostgreSQL, journaux),
soit au moins 47 Go libres au début de la fenêtre pour un backup de la taille
de septembre, recalculés sur les tailles du backup frais (C-05) et du
diagnostic à blanc. Si PostgreSQL est sur un autre hôte (I6), son poste y
est compté. Si le staging de l'import sert de source à `storage/` de PPBase
(6.8), l'un des deux postes de `storage/` peut tomber, sur décision.

### 2.4 Gel du schéma de production et règle `buma_sql_table`

L'outillage compare les 114 définitions de la capture à l'export corrigé,
dont l'empreinte est figée (`scripts/c11_import.py:42`, `:338-341`) : une
collection en plus (`unexpected_collection`) ou en moins
(`missing_collection`), ou toute différence de règles, d'index, d'options ou
de champs (`schema_drift`), bloque (`scripts/c11_import.py:1745-1750`,
`:1776-1808`, C-07). Les migrations de référence de PPBase sont générées
depuis cet export (`pb_migrations/1789236840_buma_master_schema_baseline.py`,
`pb_migrations/1789236842_buma_master_views.py`). Or la production modifie
ses collections hors de la source :

- tableau de bord : l'automigration, active par défaut dans le binaire
  officiel (`examples/base/main.go:57-63`), ajoute une ligne à `_migrations`
  et écrit un fichier de migration à chaque changement de collection
  (`plugins/migratecmd/migratecmd.go:84-100`,
  `plugins/migratecmd/automigrate.go:65-87`) ; 745 lignes de ce type en
  septembre, la dernière le 2026-09-12 ;
- console `POST /master/article/executeSql` : réécrit la requête et les
  champs de la vue `buma_sql_table`, puis tente de supprimer les fichiers
  `*_updated_buma_sql_table.js` ; la ligne de `_migrations` reste
  (`master.article.ts:30-63`, D-0018) ; 252 lignes en septembre, la dernière
  le 2025-11-21 ;
- `POST /master/article/deleteSql`, route publique, supprime la collection
  nommée (`master.article.ts:65-75`, `:91-93` ;
  `docs/contracts/legacy-sql.md`).

Sans automigration, ces changements ne laissent aucune ligne dans
`_migrations` : C-07 reste le contrôle décisif, C-08 un indice.

Règles proposées, à valider par l'utilisateur (2.1, I22) :

1. **Gel du schéma depuis l'export** : aucune modification de collection
   par le tableau de bord, aucun appel à `deleteSql`. Une modification
   impose un nouvel export, de nouvelles migrations de référence relues dans
   le dépôt, la porte de release (annexe A) et une nouvelle répétition réelle
   avant une nouvelle fenêtre ; rien de cela ne se fait pendant la fenêtre.
2. **Console gelée par défaut** : aucun `executeSql` en production depuis
   l'export. Ce gel est praticable : en septembre, la dernière ligne console
   datait du 2025-11-21 et `buma_sql_table` était égale à l'export. C-07
   attend alors 0 dérive, `buma_sql_table` comprise.
3. **Si la console a servi malgré tout**, la dérive de `buma_sql_table`
   (`options` et `schema`) est attendue, mais l'outillage actuel la bloque.
   Deux issues, à trancher avant G0 :
   - NO-GO et report ;
   - ou décision D-XXXX acceptant une dérive limitée à cette vue, la cible
     gardant la requête de l'export, avec prise en charge par l'importeur
     et test de régression [outil non attribué]. Impact client : jusqu'au
     premier `executeSql` après la bascule, une lecture native de
     `buma_sql_table` renvoie le résultat de la requête de l'export, et non
     celui de la dernière requête console de production, écrite en SQL
     SQLite (D-0018). Le parcours desktop, qui appelle `executeSql` avant de
     lire la vue, n'est pas touché.
4. Le diagnostic à blanc (6.1) vérifie ces règles sur un backup récent,
   avant la fenêtre.

## 3. Informations à fournir par l'utilisateur

Infrastructure de production :

- **I1** — Hôte PocketBase : système, architecture, gestionnaire de service
  et politique de redémarrage automatique, chemin de `pb_data`, horloge
  synchronisée (NTP) ; mises à jour automatiques (I25).
- **I2** — Binaire officiel ou build personnalisé, et ligne de commande
  exacte de PocketBase : `--dir`, `--encryptionEnv`, `--hooksDir`,
  `--hooksWatch`, `--migrationsDir`, `--automigrate`, `--publicDir`,
  `--indexFallback`, `--queryTimeout`, `serve` avec ou sans domaines,
  `--http`, `--https`, `--origins` (v0.22.34 : `pocketbase.go:187-207`,
  `examples/base/main.go:25-87`, `cmd/serve.go:20-75`). Copie en lecture des
  dossiers `--hooksDir` et `--migrationsDir` déployés, pour la preuve de
  révision (2.2). Présence d'un `pb_public` servi : par défaut, le binaire
  officiel sert le dossier `pb_public` voisin de l'exécutable
  (`examples/base/main.go:65-71`, `:129-137`).
- **I3** — Paramètres chiffrés (`--encryptionEnv`) ? L'import les refuse
  (`settings_not_plain_json`, contrat `docs/contracts/c11-auth-import.md`
  §3) : aucune clé de chiffrement n'est acceptée. Les paramètres du backup de
  septembre étaient lisibles en JSON.
- **I4** — TLS terminé par PocketBase (`serve <domaine>`, certificats dans
  `pb_data/.autocert_cache`, `apis/serve.go:137-140`) ou par un proxy ? Le ZIP
  de septembre ne contenait aucun membre `.autocert_cache/` : c'est un indice,
  pas une preuve.
- **I5** — Hôte PPBase et machine de bascule : système, Python 3.11 ou plus,
  CPU, mémoire, disque, réseau vers l'hôte PocketBase, utilisateur système et
  gestionnaire de service de PPBase, horloge (NTP) ; mises à jour
  automatiques (I25). Les durées de 5.1 ne valent que pour une machine au
  moins équivalente au serveur de session.
- **I6** — PostgreSQL de production : hébergement, version, base neuve
  dédiée, rôle propriétaire, accès réseau restreint ; PPBase s'y connecte
  sans TLS (`ppbase/db/engine.py:50`) et ouvre jusqu'à 10 + 20 connexions par
  processus (`ppbase/config.py:29-30`) ; configuration (`shared_buffers`,
  `max_wal_size`…) comparée à celle des répétitions (R10) ; mises à jour
  automatiques (I25).
- **I7** — Proxy, DNS et certificats : logiciel et configuration actuelle
  (taille maximale des corps, alors que `buma_ressources.file` accepte des
  fichiers jusqu'à 120 242 880 octets ; délais ; en-têtes ; SSE), TTL DNS,
  moyen de changer l'amont.

Clients :

- **I8** — URL de base utilisées par chaque client : desktop (dont la console
  SQL), application(s) mobile(s), tableau de bord d'administration, scripts ;
  configurables ou codées en dur ; versions déployées et SDK utilisés, dont
  l'usage éventuel de `isAuthRecord` du SDK JS 0.21 (D-0027).
- **I9** — Sondes de supervision (chemins appelés, par exemple `GET /` ou
  `/api/health`) et journaux du proxy disponibles pour établir le taux
  d'erreurs habituel de PocketBase.
- **I10** — Comportement hors ligne du mobile : file d'attente, rejeu, usage
  de `mobile_message_id`.

Gel et fenêtre :

- **I11** — Méthode de gel retenue (6.2) et accès de l'opérateur pendant le
  gel.
- **I12** — Fenêtre, annonce aux utilisateurs, consigne de synchronisation
  des mobiles avant le gel.
- **I13** — Sauvegardes automatiques PocketBase (`backups.cron`,
  `backups.cronMaxKeep`) ; stockage des sauvegardes : si `backups.s3` est
  actif, la sauvegarde de la méthode A part dans le bucket et non dans
  `pb_data/backups/` (`core/base.go:538-552`), alors qu'il était inactif en
  septembre. **Tâches externes** qui écrivent dans PocketBase : liste, URL
  cible, moyen de les suspendre au gel (6.2), de les rediriger vers PPBase à
  l'ouverture (6.10) et de les rétablir vers PocketBase en retour arrière
  (8.2).

Accès et comptes :

- **I14** — Accès nécessaires (SSH, administration, PostgreSQL) et gestion
  des secrets (coffre ou fichier d'environnement 0600) ; aucun secret par
  ticket ou messagerie.
- **I15** — Comptes de contrôle : un compte `users`, un compte `agent_users`
  et l'administrateur PocketBase ; mots de passe détenus par leurs
  titulaires ; accord pour conserver un jeton émis avant le gel, détruit
  après C-34.
- **I16** — SMTP. PocketBase 0.22.34 envoie ses emails par le SMTP de ses
  paramètres s'il est actif, sinon par `sendmail` (`core/base.go:495-509`).
  PPBase n'utilise que son environnement (`PPBASE_SMTP_*`,
  `ppbase/config.py:86-92`, `ppbase/services/mail_service.py:49-72`), jamais
  `_params.settings.smtp` importé (D-0027 ; contrat
  `docs/contracts/c11-auth-import.md` §3). Sans `PPBASE_SMTP_HOST`, rien
  n'est envoyé et seul un avertissement sans destinataire, lien ni jeton est
  journalisé, sauf en `PPBASE_DEV`, qui journalise corps, liens et jetons
  (`mail_service.py:56-66`) et ne doit jamais être activé en production. Les
  liens sont construits sur le seul `meta.appURL` importé
  (`ppbase/services/app_url.py:32-51`, comme `mails/admin.go:22-26` et
  `mails/record.go:175-184` de 0.22.34). À fournir : paramètres SMTP de
  PPBase en production (en pratique ceux du SMTP de PocketBase), ou décision
  de ne pas en avoir (aucun email de vérification, de réinitialisation ni de
  changement d'email), et politique d'accès aux journaux.

Sauvegardes, rétention et décisions :

- **I17** — Emplacement privé de la capture ; durée de conservation du
  `pb_data` de PocketBase et de la capture après la bascule (jamais supprimés
  sans décision).
- **I18** — Sauvegarde PostgreSQL et du dossier de données PPBase après la
  bascule : outil, fréquence, essai de restauration ; valeur de
  `backups.cron` dans les paramètres de PPBase (C-26) ; conservation de la
  table `_requests` (6.11).
- **I19** — Personnes et suppléants : décideur, opérateur, vérificateur,
  infrastructure, référents métier (desktop, mobile, caisse), communication ;
  canal d'astreinte.
- **I20** — Fichiers absents (2 303 en septembre) : recherche préalable sur
  un ancien serveur ou un autre backup, ou absence documentée (D-0021). Par
  défaut, références conservées telles quelles.
- **I21** — Durée de la surveillance renforcée et de la période de retour
  arrière ; validation des seuils de la section 7.

Préparation hors fenêtre :

- **I22** — Gel du schéma (2.4) : date de l'export corrigé, consigne donnée
  aux administrateurs (tableau de bord, console, `deleteSql`), personne qui
  la fait respecter, usages de la console depuis l'export.
- **I23** — Staging (6.9) : machine, réseau et exposition, accès, PostgreSQL
  jetable, conservation et destruction des données, SMTP et secrets de
  jetons retenus.
- **I24** — Backup récent du diagnostic à blanc (6.1) : méthode (dernière
  sauvegarde automatique, ou sauvegarde native à un moment creux), date,
  transfert et emplacement privé.
- **I25** — Mises à jour automatiques et redémarrages de services sur l'hôte
  PocketBase, la machine de bascule (PPBase) et le serveur PostgreSQL :
  minuteries `apt-daily` et `apt-daily-upgrade`, `unattended-upgrades`
  (redémarrage automatique compris), mode de `needrestart` et son hook APT,
  ou leurs équivalents sur un autre système ; décision de l'infrastructure
  pour la fenêtre (C-46).

## 4. Rôles et responsabilités

| Rôle | Responsabilités | Personne |
| --- | --- | --- |
| Décideur (utilisateur) | autorisations ; go/no-go à chaque porte ; déclenchement et fin du retour arrière ; sort des écritures reçues par PPBase ; révocation d'urgence (6.12) | I19 |
| Opérateur de bascule | exécute les étapes, tient le journal, n'improvise aucune correction | I19 |
| Vérificateur | relit les preuves de chaque porte, sans les avoir produites | I19 |
| Infrastructure | proxy, DNS, TLS, services, accès, sauvegardes, staging ; politique des mises à jour automatiques (C-46) | I19 |
| Référents métier | contrôles desktop, mobile et caisse ; premières opérations réelles | I19 |
| Communication | annonce, information pendant la fenêtre, ouverture ou retour | I19 |
| Agents Claude | préparation, outillage et relecture hors production ; aucune action sur la production | — |

## 5. Chronologie et durées

### 5.1 Durées mesurées et durées à planifier

Sources :

- **AP** : `/tmp/buma-c11-session/orch-real-preview-1.json`, rapport agrégé
  d'**aperçu** produit par `scripts/c11_real_rehearsal.py`, non versionné
  (conservé par l'orchestrateur). Un passage, le 2026-10-24 de 19:02 à 19:12
  heure du serveur (`timestamp_utc` : 17:01:48 UTC), BUMA `2c7a17e` et PPBase
  `22929a0`, deux aperçus (`code.buma.commit`, `code.ppbase.commit`), sur le
  backup réel de septembre en mode manifeste : 1 188 247 records
  (`passes[0].checks.diagnosis.rows`), aucun octet de fichier
  (`real_backup.file_bytes_available` faux). Serveur de session de 4 CPU et
  8 Gio (`environment.cpu_count`, `environment.memory_total_kib`), aucun autre
  conteneur au départ (`environment.other_running_containers`), charge de
  l'hôte jusqu'à 2,66 pendant l'import
  (`passes[0].measures.container_memory_by_phase.import.max_host_loadavg_1min`) ;
  PostgreSQL 16.15 jetable (`postgres:16-alpine`) limité à 2 CPU et 2 Gio
  (`passes[0].container`), réglages par défaut (R10). Statut `failed` à
  cause des seuls 9 écarts HTTP d'enveloppe d'erreur (`passes[0].failure`,
  `passes[0].checks.http.divergences`) : toutes les phases de données ont
  réussi, les durées sont valides comme mesures.
- **VB** : `docs/c11-volume-benchmark-report.json` (D-0026), banc synthétique
  du 2026-10-24 (`timestamp_utc` : 11:42:14 UTC), 1 182 406 records, BUMA
  `bf00a14`, PPBase `c6821aa` (`environment`), PostgreSQL limité à 2 CPU et
  2 Go (`container`), un autre conteneur actif au départ
  (`environment.concurrent_load_at_start`).
- **D-0028** : `docs/decisions-and-divergences.md`, D-0028, paragraphe
  « Durées constatées » (archives synthétiques seulement).

Avec le rapport final, l'orchestrateur remplace « AP » par
`docs/c11-real-rehearsal-report.json` (mêmes clés, `passes[i]` pour chaque
passage ; `runbook_durations_seconds.<phase>.max` donne le maximum des
passages réussis pour chaque clé de `phase_seconds`), puis recalcule la
dernière colonne.

Marges de sécurité :

- **M1 = ×2**, pour les phases mesurées sur le backup réel : un seul passage
  d'aperçu ; variabilité du même import sur l'hôte partagé (243 à 321 s,
  D-0026 §7) ; backup frais plus volumineux ; machine de bascule inconnue.
  Valable seulement si le backup frais compte au plus 1,5 fois les records de
  septembre (C-11), si la machine de bascule est au moins équivalente au
  serveur de session (I5) et si PostgreSQL dispose d'au moins 2 CPU et
  2 Gio ; sinon, nouvelle mesure avant de fixer la fenêtre (6.1).
- **M2 = ×3**, pour les mesures synthétiques : cache disque froid,
  compressibilité réelle, autre disque ; nouvelle mesure obligatoire au
  diagnostic à blanc sur le vrai ZIP récent (6.1).
- Les durées à planifier sont arrondies à la minute supérieure.

| Ligne | Étape (section) | Durée mesurée | Source exacte (rapport et clé JSON) | Conditions | Marge de sécurité | Durée à planifier |
| --- | --- | --- | --- | --- | --- | --- |
| L1 | Gel des écritures, C-01 (6.2) | non mesurée | — | propre à la production | — | [durée non mesurée] |
| L2 | Capture A ou B, arrêt de PocketBase, `H_stop` (6.3) | non mesurée | — | sauvegarde native : écritures bloquées pendant sa création (`core/base_backup.go:35-37`) ; taille du `pb_data` de production | — | [durée non mesurée] |
| L3 | Transfert de la capture, C-04 (6.4) | non mesurée | — | réseau entre l'hôte PocketBase et la machine de bascule (I5) | — | [durée non mesurée] |
| L4 | Inventaire `scripts.c11_import --archive`, C-05 (6.4) | non mesurée séparément | — | le même inventaire est refait au début de L5 | — | comprise dans L5 |
| L5 | Extraction vérifiée `scripts.c11_extract`, C-06 (6.4) | environ 103 s | D-0028 | archives synthétiques au format PocketBase, 1 290 membres, 8,85 Go décompressés, serveur de session ; le ZIP réel n'a jamais été extrait sur le serveur | M2 (×3) | 6 min, à remplacer par la mesure du diagnostic à blanc |
| L6 | Diagnostic `scripts.c11_import --data`, C-07 à C-12 (6.5) | 114,59 s [aperçu] | AP `passes[0].measures.diagnosis_step_seconds.diagnosis` | mode manifeste, aucun fichier haché ; avec les types cibles | M1 (×2) | 4 min, plus le hachage de `storage/` [durée non mesurée] |
| L7 | Audit `scripts.c11_snapshot_audit`, C-09, C-12, C-13 (6.5) | 31,019 s [aperçu] | AP `passes[0].measures.diagnosis_step_seconds.independent_audit` | même processus que L6 (phase `passes[0].measures.phase_seconds.diagnosis` : 149,18 s pour L6 et L7) | M1 (×2) | 2 min, plus le hachage de `storage/` [durée non mesurée] |
| L8 | Contrôle d'idempotence `scripts.c11_idempotency_source_check`, C-08 a (6.5) | non mesurée | aperçu lancé sans `--source-migrations` ; clé attendue du rapport final : `passes[i].measures.diagnosis_step_seconds.idempotency_source_check` | — | — | [durée non mesurée] |
| L9 | Diagnostic des comptes `scripts.c11_auth_import --data`, C-13 (6.5) | non mesurée séparément | `import_snapshot` refait ces contrôles : AP `passes[0].measures.importer_timings_seconds.readiness_check` (0,189 s) | — | — | [durée non mesurée] |
| L10 | Base neuve : migrations, schéma, colonnes relationnelles, 42 vues, graines C5 (6.6, étapes 2 à 4) | 13,832 s [aperçu] | AP `passes[0].measures.phase_seconds.migrations_schema_and_target_description` | base vide, 11 migrations, outillage du banc | M1 (×2) | 1 min |
| L11 | Import des records, des comptes et des paramètres en une transaction (6.6, étape 5) | 280,907 s [aperçu] | AP `passes[0].measures.phase_seconds.import` ; dont importeur `passes[0].measures.importer_timings_seconds.total` (273,857 s : `copy_and_readback` 184,99, `final_check` 69,852, `conservation` 17,191, `auth_import` 2,54) | COPY par lots de 5 000 ; mode manifeste : seul `data.db` est copié au staging | M1 (×2) | 10 min, plus la copie de `storage/` au staging en mode complet [durée non mesurée] |
| L12 | Échec de l'import et annulation complète (6.6, étape 7) | 128,015 s pour un échec injecté après la moitié des records | VB `failure_injection.seconds_until_rollback_complete` (`failure_injection.fail_after_rows` : 591 203) | synthétique ; durée totale du processus, chargement de la moitié compris, pas de l'annulation seule ; D-0026 §7 relève 91 à 128 s | — | un échec peut survenir jusqu'à la fin de L11 : réserver L11 planifiée avant la décision de NO-GO |
| L13 | Contrôles de conservation après commit, C-17 à C-29 (6.7) | 89,304 s [aperçu] | AP `passes[0].measures.phase_seconds.post_commit_checks` (dont `passes[0].measures.post_commit_check_seconds.fingerprints` 62,022 et `anomalies` 14,836) | contrôles du banc, dans de nouvelles sessions | M1 (×2) | 3 min |
| L14 | Copie et contrôle de `storage/`, C-30 (6.8) | non mesurée | aucun octet de fichier sur le serveur (`real_backup.file_bytes_available`) | — | — | [durée non mesurée], à mesurer avec le ZIP complet |
| L15 | Démarrage de PPBase (6.9) | 4,959 s [aperçu] | AP `passes[0].measures.phase_seconds.server_start` (serveur prêt : `passes[0].server.startup_seconds`, 2,71 s) | serveur isolé, boucle locale, jobs désactivés | M1 (×2) | 1 min |
| L16 | Lectures HTTP automatiques, part de C-31 à C-38 (6.9) | 35,201 s [aperçu] | AP `passes[0].measures.phase_seconds.http_reads` (256 contrôles, `passes[0].checks.http.checks_run`) | lectures anonymes et superuser synthétique, sans fichiers | M1 (×2) | 2 min ; contrôles manuels, SDK et desktop [durée non mesurée] |
| L17 | Contrôle des écritures après les lectures, C-41 (6.9) | 4,687 s [aperçu] | AP `passes[0].measures.phase_seconds.after_http_checks` | — | M1 (×2) | 1 min |
| L18 | Bascule d'URL, C-42, ouverture, C-43 (6.10) | non mesurée | — | proxy et DNS (I7) | — | [durée non mesurée] |
| L19 | Retour arrière avant ouverture (8.2) | non mesurée | — | redémarrage de PocketBase et de son bootstrap, proxy | — | [durée non mesurée], réservée avant la fin de la fenêtre |

Somme des durées à planifier des lignes mesurées (L5, L6, L7, L10, L11, L13,
L15, L16, L17) : 30 min. Ce n'est **pas** la fenêtre : elle n'inclut ni les
lignes non mesurées (L1 à L3, L8, L9, L14, L18, L19), ni le hachage et la
copie des 4,9 Go de `storage/` en mode complet, ni les contrôles manuels,
SDK et desktop, ni les relectures du vérificateur aux portes G1 à G5. Aucune
durée de production n'est connue : gel, capture, transfert et octets de
fichiers n'ont jamais été mesurés.

### 5.2 Ressources mesurées

| Ligne | Ressource | Mesure | Source exacte (rapport et clé JSON) | Conditions |
| --- | --- | --- | --- | --- |
| R1 | Base PostgreSQL après les migrations | 15 752 215 octets [aperçu] | AP `passes[0].measures.database_bytes_after_migrations` | base vide |
| R2 | Base PostgreSQL après l'import (`pg_database_size`) | 784 366 615 octets [aperçu] | AP `passes[0].measures.database_bytes_after_import` | 1 188 247 records |
| R3 | Répertoire de données PostgreSQL après l'import (données, index, WAL) | 1 865 055 973 octets [aperçu] | AP `passes[0].measures.postgresql_data_directory_bytes_after_import` | `max_wal_size` 1GB (R10) |
| R4 | Disque consommé au plus pendant le passage | 2 352 029 696 octets [aperçu] | AP `passes[0].measures.disk_free_before.private_dir` moins `passes[0].measures.disk_free_minimum_during_pass.private_dir` | dossier privé, `/tmp` et Docker sur le même système de fichiers ; sans octets de fichiers |
| R5 | Staging de l'import (copie privée de `data.db`) | 325 212 482 octets [aperçu] | AP `passes[0].measures.staging_bytes` | mode manifeste, sans `storage/` |
| R6 | Pic RSS de l'importeur | 143 196 Kio [aperçu] | AP `passes[0].measures.import_child.peak_rss_kib` | — |
| R7 | Pic RSS du diagnostic et de l'audit | 233 028 Kio [aperçu] | AP `passes[0].measures.diagnosis_child.peak_rss_kib` | — |
| R8 | Pic RSS du serveur PPBase | 109 924 Kio [aperçu] | AP `passes[0].server.peak_rss_kib` | lectures de L16 |
| R9 | Mémoire PostgreSQL non récupérable (anon + shmem, maximum échantillonné) | 311 136 256 octets [aperçu] | AP `passes[0].measures.container_memory.sampled_max_anon_plus_shmem_bytes` | limite de 2 Gio atteinte par le cache de pages, 0 OOM (`passes[0].measures.container_memory.events`) |
| R10 | Configuration PostgreSQL de la répétition | 16.15 ; `shared_buffers` 128MB ; `work_mem` 4MB ; `maintenance_work_mem` 64MB ; `max_wal_size` 1GB ; `synchronous_commit` on | AP `passes[0].measures.database` | réglages par défaut de l'image |
| R11 | Pic RSS de l'extraction | 27 Mo | D-0028 | archives synthétiques |

### 5.3 Fenêtre de maintenance

| N° | Étape | Section | Durées |
| --- | --- | --- | --- |
| 1 | Gel des écritures et vérification | 6.2 | L1 |
| 2 | Capture du backup fermé, arrêt de PocketBase, référence d'intégrité | 6.3 | L2 |
| 3 | Transfert de la capture et empreinte | 6.4 | L3 |
| 4 | Inventaire et extraction vérifiée | 6.4 | L4, L5 |
| 5 | Diagnostic, audit, idempotence, comptes | 6.5 | L6 à L9 |
| 6 | Base neuve, migrations, schéma, graines | 6.6 | L10 |
| 7 | Import des records, des comptes et des paramètres | 6.6 | L11 (échec : L12) |
| 8 | Contrôles de conservation | 6.7 | L13 |
| 9 | Copie et contrôle de `storage/` | 6.8 | L14 |
| 10 | Démarrage de PPBase et contrôles avant ouverture | 6.9 | L15 à L17 |
| 11 | Bascule d'URL et contrôles d'ouverture | 6.10 | L18 |
| 12 | Retour arrière avant ouverture | 8.2 | L19 |

La fenêtre couvre au moins les étapes 1 à 11, plus une marge fixée par le
décideur, et laisse le temps de l'étape 12 avant sa fin. L'étape 9 peut se
dérouler en parallèle des étapes 5 à 8 si les disques de la machine de
bascule le permettent ; à décider après mesure. Un redémarrage de PocketBase
sous gel (6.3) refait les étapes 2 et suivantes. Le diagnostic à blanc
(6.1), hors fenêtre, mesure L5 à L9 en mode complet sur la machine de
bascule ; la fenêtre n'est fixée qu'après lui.

## 6. Procédure

### 6.1 Préparation, avant la fenêtre

1. Installer sur la machine de bascule les checkouts BUMA et PPBase aux
   commits de release, avec un seul venv comme sur le serveur de session
   (`docs/server-session-plan.md`, section 3) ; consigner les deux commits et
   `pip freeze`. **C-45** : depuis la racine du checkout BUMA,

   ```sh
   git rev-parse HEAD
   git status --porcelain
   git -C <checkout PPBase> rev-parse HEAD
   git -C <checkout PPBase> status --porcelain
   .venv/bin/python -c "import ppbase, ppbase.models.field_multiplicity; print(ppbase.__file__)"
   ```

   donnent les commits `code.buma.commit` et `code.ppbase.commit` du rapport
   final, deux états vides, et un chemin sous `<checkout PPBase>`. Sans
   `ppbase.models.field_multiplicity`, les hooks BUMA ne se chargent pas
   (D-0025). Y copier aussi l'export corrigé, dont le SHA-256 est contrôlé
   par l'outillage (`scripts/c11_import.py:42`, `:338-341`), et un checkout en
   lecture seule de la source gelée `530018c` (C-08, `--source-migrations`).
2. **C-46**, machines : sur l'hôte PocketBase, la machine de bascule et le
   serveur PostgreSQL, l'infrastructure relève, sans rien modifier (commandes
   Ubuntu et Debian ; équivalents sur un autre système) :

   ```sh
   systemctl list-timers apt-daily.timer apt-daily-upgrade.timer --no-pager
   cat /etc/apt/apt.conf.d/20auto-upgrades
   grep -n 'Automatic-Reboot' /etc/apt/apt.conf.d/50unattended-upgrades
   cat /etc/apt/apt.conf.d/99needrestart
   grep -rn 'nrconf{restart}' /etc/needrestart/
   dpkg-query -W needrestart unattended-upgrades
   ```

   Pour chaque hôte, elle consigne la prochaine exécution des deux minuteries,
   l'activation des mises à jour automatiques et du redémarrage automatique,
   le mode de `needrestart` et son hook APT, puis propose une décision au
   décideur (2.1), par exemple : fenêtre hors des prochaines exécutions,
   suspension des minuteries pendant la fenêtre, ou `needrestart` en simple
   liste. Ni le runbook ni l'opérateur ne modifient le système sans cette
   décision ; toute modification décidée est consignée avec son retour à
   l'état initial.
3. Préparer un fichier d'environnement de production 0600, hors dépôt :
   `PPBASE_DATABASE_URL`, `PPBASE_DATA_DIR`, `PPBASE_HOST`, `PPBASE_PORT`,
   `PPBASE_MIGRATIONS_DIR`, `PPBASE_AUTO_MIGRATE=false`,
   `PPBASE_STORAGE_BACKEND=local` et `BUMA_MASTER_ENABLE_INTERNAL_JOBS=false`
   (la source gelée n'a aucun cron actif : le seul `cronAdd`, dans
   `master.rupture.ts:222`, est commenté), comme les répétitions
   (`scripts/c11_auth_runtime.py:1555-1571`). `PPBASE_DEV` reste absent ou
   faux. Y ajouter :
   - `PPBASE_SMTP_HOST`, `PPBASE_SMTP_PORT`, `PPBASE_SMTP_USERNAME`,
     `PPBASE_SMTP_PASSWORD` et `PPBASE_SMTP_FROM`, selon I16 : c'est la seule
     configuration d'envoi que PPBase lit ;
   - `PPBASE_JWT_SECRET`, secret propre à PPBase. Sans lui, PPBase génère et
     conserve `<PPBASE_DATA_DIR>/.jwt_secret` (0600) et l'annonce au
     démarrage (`ppbase/config.py:125-171`, `ppbase/app.py:70-74`). Il ne
     signe et ne vérifie les jetons superuser qu'en l'absence de collection
     `_superusers` (`ppbase/api/deps.py:89-101`), ce qui n'arrive pas ici :
     la continuité des sessions repose sur `token_key` et les secrets
     `authToken` importés (contrat `docs/contracts/c11-auth-import.md` §1).
     C'est néanmoins un secret, géré selon I14 et compris dans la sauvegarde
     du dossier de données (I18) ;
   - l'équivalent PPBase de `--origins` (`origins`, `ppbase/config.py:95`)
     si PocketBase n'utilise pas la valeur par défaut `*`
     (`cmd/serve.go:55-60`) ;
   - si I2 montre un `pb_public` servi : une copie de ce dossier et
     `PPBASE_PUBLIC_DIR`, ou `--publicDir` (`ppbase/config.py:39`,
     `ppbase/__main__.py:667-674`, `ppbase/app.py:492-532`). Le repli sur
     `index.html` (`--indexFallback`, vrai par défaut dans 0.22.34,
     `examples/base/main.go:73-79`) et la cohabitation avec la route `GET /`
     de BUMA (10.4) sont à vérifier sur staging ; non vérifié par WS-F.

   Le mode d'erreur PocketBase 0.22 (enveloppe `{code, message, data}`) est
   activé par l'enregistrement des hooks BUMA, pas par ce fichier
   [à confirmer à l'intégration : WS-A6] ; C-44 le contrôle.
4. Dossier de lancement de PPBase : un dossier vide et privé, sans `.env` ni
   `pb_hooks/` : PPBase lit `./.env` (`ppbase/config.py:118-123`), charge les
   fichiers `*.py` de `./pb_hooks` (`ppbase/config.py:109`,
   `ppbase/services/hook_manager.py:315-326`) et les surveille
   (`ppbase/app.py:258-278`). `PYTHONPATH=<checkout BUMA>` donne le module
   `app` du checkout de release, comme les répétitions
   (`scripts/c11_rehearsal.py:2284-2320`). Ne pas utiliser les cibles `make`,
   qui visent par défaut la base locale du port `55434`. Ne jamais passer
   l'URL PostgreSQL par `--db` : le mot de passe serait visible dans la liste
   des processus ; les outils cités lisent `PPBASE_DATABASE_URL`, et les deux
   vérificateurs de schéma visent la base locale `55434` si elle manque
   (`scripts/verify_complete_schema.py:56-59`,
   `scripts/verify_view_queries.py:32-35`).
5. Créer la base PostgreSQL de production neuve et son rôle (I6) : aucune
   donnée, aucun serveur PPBase dessus.
6. Préparer sans l'activer la configuration du proxy vers PPBase et la page
   de maintenance ; reprendre à l'identique les limites, délais, en-têtes et
   réglages SSE de la configuration PocketBase (I7).
7. **Diagnostic à blanc**, quelques jours avant la fenêtre, avec
   l'autorisation de 2.1, sur un backup récent (I24) copié dans `<privé>` de
   la machine de bascule : 6.4 (inventaire et extraction), 6.5 (C-05, C-07 à
   C-13, dont les parties b et c de C-08), preuve de révision des hooks et
   des migrations déployés (2.2) et règles du gel du schéma (2.4). Lecture
   seule, aucun import. Il chronomètre L4 à L9 en mode complet (hachage de
   `storage/` compris) sur cette machine : ces mesures remplacent, pour la
   fenêtre, celles du tableau 5.1. Tout écart se traite avant la fenêtre,
   jamais pendant. Une sauvegarde native bloque les écritures pendant sa
   création (`core/base_backup.go:35-37`, `:70-84`) : la faire à un moment
   creux choisi par l'utilisateur [durée non mesurée]. La copie est conservée
   ou détruite selon I17, jamais publiée. Avec une autorisation propre (2.1),
   la répétition réelle de l'annexe A peut aussi tourner sur ce backup, dans
   un PostgreSQL jetable, pour mesurer L10 à L17 sur la machine de bascule.
8. Communication (I12) : annonce ; demander aux utilisateurs mobiles de
   synchroniser leurs opérations en attente et de ne rien saisir pendant la
   fenêtre ; prévenir les utilisateurs de la console desktop que, depuis
   D-0018, leurs requêtes `executeSql` s'écrivent en SQL PostgreSQL.
9. Juste avant le gel, chaque titulaire d'un compte de contrôle (I15)
   obtient un jeton PocketBase par une connexion normale ; l'opérateur le
   conserve dans `<privé>`, sous la forme d'un fichier d'en-tête 0600
   contenant la ligne `Authorization: <jeton>`, pour le contrôle C-34, jamais
   dans le journal, et le détruit après C-34 (6.9).

### 6.2 Gel des écritures côté PocketBase (méthode à confirmer, I11)

| Méthode | Principe | Limites |
| --- | --- | --- |
| G-A, maintenance au proxy (proposée) | le proxy répond « maintenance » à tous les clients ; seul l'opérateur atteint PocketBase, en local ou depuis une adresse autorisée | suppose qu'aucun client n'atteint PocketBase sans passer par le proxy ; bloque aussi les connexions |
| G-B, arrêt du service | arrêt propre de PocketBase avant la capture | impose la capture B (6.3) |
| Écartée : bloquer seulement POST, PUT, PATCH et DELETE | les lectures restent servies | une lecture peut écrire : miniature créée à la demande dans `storage/` (`apis/file.go:162-193`) |
| Écartée : règles d'API réservées aux admins | les collections deviennent inaccessibles | modifie `_collections` de PocketBase (principe 2) et ne couvre pas les routes personnalisées des hooks |

1. **C-46** revérifié sur les trois hôtes : aucune exécution des minuteries
   de mise à jour n'est planifiée dans la fenêtre, ou la décision de 6.1
   (étape 2) est appliquée.
2. Activer le gel ; noter `T_gel` en UTC, lu sur l'horloge de l'hôte
   PocketBase.
3. Suspendre les tâches externes qui écrivent (I13) et le redémarrage
   automatique du service PocketBase, sans toucher à `pb_data` ; consigner
   chaque suspension, pour les rétablir à l'ouverture (6.10) ou au retour
   arrière (8.2). Ne pas modifier `pb_hooks` : `--hooksWatch`, vrai par
   défaut, redémarre PocketBase à chaque changement
   (`examples/base/main.go:33-39`).
4. Vérifier qu'aucune sauvegarde automatique ne tourne ni n'est planifiée
   pendant la fenêtre (`backups.cron`, `core/base_backup.go:267-283`) : une
   seconde sauvegarde simultanée est refusée (`core/base_backup.go:49-51`).
5. **C-01** : dans les journaux du proxy, 0 requête transmise à PocketBase
   après `T_gel`, hors actions de l'opérateur. Durée de l'étape : L1.

### 6.3 Capture d'un backup fermé (méthode à confirmer)

**Règle de redémarrage.** Tout démarrage de PocketBase écrit dans
`data.db` : le bootstrap de la source supprime puis recrée les 18
`buma_apps_roles` (`master.setup.ts:5-8`, `:72-73`, `:309-324`) et le
démarrage applique toute migration en attente du dossier `--migrationsDir`
déployé (`apis/serve.go:66-69`, `:254-278` ;
`plugins/jsvm/jsvm.go:175-213`). Un redémarrage après `T_gel`, gel maintenu,
annule donc la capture en cours : le consigner, noter `T_gel'` (UTC, horloge
de l'hôte PocketBase) quand PocketBase répond de nouveau en local
(bootstrap terminé), puis reprendre la capture. C-01 reste compté depuis le
premier `T_gel`, C-10 depuis le dernier `T_gel'`. `buma_apps_roles` n'est
jamais exclue de C-10.

Méthode A — sauvegarde native de PocketBase, sous gel G-A :

1. L'administrateur crée une sauvegarde au nom explicite, conforme à
   `^[a-z0-9_-]+\.zip$` (`forms/backup_create.go:11`), depuis le tableau de
   bord de PocketBase ou par `POST /api/backups` (admin,
   `apis/backup.go:25-31`). Ne pas retenir une sauvegarde automatique
   `@auto_pb_backup_*`, que la rotation peut supprimer
   (`core/base_backup.go:272-330`).
2. Dans une transaction qui bloque les autres écritures, PocketBase lance
   `PRAGMA wal_checkpoint(TRUNCATE)`, dont les erreurs sont ignorées, puis
   archive `pb_data` sans `backups` ni `.pb_temp_to_delete`
   (`core/base_backup.go:61`, `:70-84`). Le ZIP contient donc tout le reste :
   `data.db`, `logs.db`, leurs fichiers `-wal` et `-shm`, `storage/` (en
   septembre, 8 fichiers à la racine et 1 282 objets). Un checkpoint en échec
   peut laisser un WAL non vide dans le ZIP, que l'inventaire refuse (C-05).
   Avec S3, les fichiers n'y seraient pas (`core/base_backup.go:44-45`).
3. Copier le ZIP vers `<privé>` en lecture : par le système de fichiers
   depuis `pb_data/backups/`, ou depuis le bucket si `backups.s3` est actif
   (I13). Le téléchargement HTTP exige un jeton de fichier admin et son
   contexte est limité à 10 minutes (`apis/backup.go:115-127`).
4. Arrêter PocketBase proprement (SIGTERM : `pocketbase.go:158-183` ;
   fermeture des bases : `core/base.go:381-404`) et vérifier la fin du
   processus.

Méthode B — copie à froid, sous gel G-A ou G-B :

1. Arrêter PocketBase proprement et vérifier la fin du processus.
2. `data.db-wal` et `data.db-journal` doivent être absents ou vides. Sinon,
   ne rien supprimer ni tronquer : redémarrer PocketBase sous gel, selon la
   règle de redémarrage, l'arrêter proprement et recontrôler ; ou passer à
   la méthode A, qui exige le même redémarrage.
3. Copier `data.db`, `storage/` et, pour archive seulement, `logs.db` vers
   `<privé>` ; manifeste SHA-256 et tailles de chaque fichier, calculé sur la
   source arrêtée et sur la copie, puis comparé : 0 écart
   [outil non attribué ; le format du manifeste privé,
   `{archive_sha256, files: [{path, bytes, sha256, crc32}]}`, que produit
   `scripts/c11_extract.py` à partir d'un ZIP, sert de modèle].

Dans les deux cas :

- **C-02** : `pocketbase --version` donne `pocketbase version 0.22.34` et le
  SHA-256 du binaire égale celui du binaire officiel de la même architecture
  (annexe B).
- **C-03** : référence d'intégrité `H_stop` de PocketBase arrêté : SHA-256 de
  `data.db` (`sha256sum` suffit) et manifeste SHA-256 et tailles de
  `storage/` du `pb_data` de production [outil non attribué, le même que pour
  le manifeste de la méthode B], calculés après l'arrêt sans rien écrire dans
  `pb_data`. Méthode A : le SHA-256 de ce `data.db` doit égaler celui du
  `data.db` du ZIP (manifeste de C-06). Un écart signale une écriture après
  la capture : le ZIP est écarté et, PocketBase étant déjà arrêté, la capture
  reprend par la méthode B sans redémarrage, si son étape 2 le permet ; C-01
  et C-10 disent ensuite si un client a écrit après `T_gel`. `H_stop` est
  revérifiée avant tout redémarrage de retour arrière (8.2).
- La méthode A ajoute le ZIP dans `pb_data/backups/`, dossier géré par
  PocketBase et exclu de ses propres sauvegardes ; `H_stop` ne porte que sur
  `data.db` et `storage/`.
- PocketBase reste arrêté, redémarrage automatique suspendu ; il ne
  redémarre qu'en cas de retour arrière ou selon la règle de redémarrage.
  Durée : L2.

### 6.4 Transfert, inventaire et extraction vérifiée

1. Copier la capture sur la machine de bascule dans `<privé>`. **C-04** :
   SHA-256 identique avant et après le transfert (`sha256sum`). Durée : L3.
2. Inventaire sans extraction ; il ne vérifie ni les CRC ni le contenu
   SQLite (`content_crc_verified` et `database_schema_and_records_checked`
   faux dans le rapport) :

   ```sh
   .venv/bin/python -m scripts.c11_import \
     --archive <privé>/<capture>.zip \
     --report <privé>/c11-cutover-archive-inventory.json
   ```

   Code de sortie 0 sans blocage (`ready_for_offline_extraction_review`),
   1 sinon ; le rapport est écrit dans les deux cas et doit être un chemin
   neuf. **C-05** : `blockers` vide, un seul `data.db` (`data_db_members`),
   WAL et journal vides (refusés sinon, `scripts/c11_import.py:297-298`),
   `archive_sha256` égal à C-04 ; `members` et
   `declared_uncompressed_bytes` consignés. Durée : L4.
3. Extraction vérifiée dans un dossier neuf de `<privé>` (D-0028), dans
   `tmux` ou `screen` (`nohup` n'est pas respecté) :

   ```sh
   .venv/bin/python -m scripts.c11_extract \
     --archive <privé>/<capture>.zip \
     --destination <privé>/extraction \
     --manifest-out <privé>/c11-cutover-extraction-manifest.json \
     > <privé>/c11-cutover-extraction-summary.json
   ```

   Inventaire repris avec des règles plus strictes (un refus ne crée rien),
   espace libre exigé égal au contenu déclaré plus 1 Gio
   (`scripts/c11_extract.py:68`, `:697-704`), extraction en flux, dossiers
   0700 et fichiers 0600, CRC-32 et SHA-256 de chaque membre, relecture
   complète, archive inchangée du début à la fin, manifeste privé publié
   atomiquement ; la sortie standard porte un seul résumé agrégé, sans nom
   de membre. `--expect-manifest <manifeste>` compare en plus à un manifeste
   existant de la même capture. Codes de sortie (`--help`) :
   - 0 : extrait et vérifié, manifeste écrit (`"ok": true`) ;
   - 1 : refus ou échec ; rien ne reste, sauf le manifeste quand seul le
     contenu vérifié diffère de `--expect-manifest` ;
   - 3 : la destination créée n'a pas pu être supprimée (`cleanup_error`) :
     ne jamais l'utiliser, l'inspecter, la supprimer à la main, relancer
     avec des chemins neufs ;
   - 128+N : interrompu par le signal N, destination supprimée.

   Après un arrêt brutal (SIGKILL, coupure), supprimer à la main la
   destination partielle et les fichiers cachés `.c11-extract-*` voisins du
   manifeste, puis relancer avec des chemins neufs. **C-06** : code 0,
   `"ok": true`, 100 % des membres vérifiés, `archive_sha256` égal à C-04,
   `members` égal à C-05. Durée : L5. Avec la méthode B, la copie à froid et
   son manifeste tiennent lieu d'extraction et de C-06.

### 6.5 Diagnostic de la capture (porte G2)

Commandes en lecture seule, sur l'extraction (mode complet : `storage/` est
lu et haché ; aucun octet n'est copié). Chaque rapport doit être un chemin
neuf.

```sh
.venv/bin/python -m scripts.c11_import \
  --data <privé>/extraction \
  --schema <export corrigé> \
  --source-version 0.22.34 \
  --target-columns <privé>/target-columns.json \
  --report <privé>/c11-cutover-diagnostic.json \
  --private-report <privé>/c11-cutover-diagnostic-private.json

.venv/bin/python -m scripts.c11_snapshot_audit \
  --data <privé>/extraction \
  --source-version 0.22.34 \
  --report <privé>/c11-cutover-audit.json \
  --private-report <privé>/c11-cutover-audit-private.json

.venv/bin/python -m scripts.c11_idempotency_source_check \
  --data <privé>/extraction \
  --source-migrations <source gelée>/migrationsDir \
  --report <privé>/c11-cutover-idempotency-source-check.json

.venv/bin/python -m scripts.c11_auth_import \
  --data <privé>/extraction \
  --schema <export corrigé> \
  --report <privé>/c11-cutover-auth-diagnostic.json
```

- `<source gelée>` est le checkout en lecture seule de `530018c` (6.1) ; son
  dossier `migrationsDir/` contient les 326 migrations source.
- `<privé>/target-columns.json` décrit les colonnes de la base cible
  (format `target_spec_json`) : celui que la répétition réelle aux commits de
  release écrit dans son dossier privé (`pass-<n>/target-columns.json`), ou
  celui de `scripts.c11_volume_benchmark --target-columns-out` ; même
  description pour toute base neuve migrée par les mêmes commits. Avec lui,
  chaque valeur est contrôlée contre sa colonne dès le diagnostic (D-0026,
  point 2) ; sans lui, ce contrôle n'a lieu qu'à l'import.
- Codes de sortie : `scripts.c11_import` rend 0 si
  `ready_for_isolated_rehearsal` est vrai, 1 sinon ; `scripts.c11_auth_import`
  rend 0 si `ready_for_auth_import` est vrai, 1 sinon ;
  `scripts.c11_snapshot_audit` et `scripts.c11_idempotency_source_check`
  rendent 0 dès que le rapport est écrit : leur résultat se lit dans le
  rapport. Le nom `ready_for_isolated_rehearsal` vaut aussi pour la
  bascule ; tous les rapports disent `production_authorized: false`.
- Durées : L6 à L9.

Contrôles :

- **C-07** : `PRAGMA integrity_check` donne `ok` (sinon
  `sqlite_integrity_failure`) ; aucun blocage `unexpected_collection`,
  `missing_collection` ni `schema_drift` sur les 114 définitions de l'export
  corrigé, hors dérive de `buma_sql_table` acceptée selon 2.4 (l'outillage
  actuel la bloque) ; `source_version` 0.22.34 avec
  `source_version_validated` vrai (D-0024), prouvée par C-02 ;
  `storage_mode` `directory` et `file_bytes_verified` vrai ; `blockers` vide
  et `ready_for_isolated_rehearsal` vrai. C-07 est le contrôle décisif du gel
  du schéma (2.4).
- **C-08** : `_migrations`, comparé à sa référence (2.2) en trois parties ;
  il ne prouve pas la révision des hooks.
  - a) Migrations source, mesurées par `scripts.c11_idempotency_source_check` :
    `source_migrations` vaut 326,
    `source_migrations_not_applied_in_snapshot` ne contient que
    `1785599959_updated_buma_transaction.js` et
    `source_index_migration_applied` vaut `false`
    (`scripts/c11_idempotency_source_check.py:47-64`). Si `1785599959` est
    désormais appliquée, l'index source existe aussi en production et C-07
    signale une dérive des index de `buma_transaction` : arrêt et décision,
    la cible créant déjà cet index (D-0022).
  - b) Migrations système : exactement les 14 fichiers `.go` de
    `migrations/` du tag `v0.22.34` [outil non attribué].
  - c) Autres lignes, comparées une à une à la référence privée la plus
    récente (copie de septembre pour le diagnostic à blanc, diagnostic à
    blanc pour la capture) [outil non attribué] : 0 ligne disparue ; 0
    nouvelle automigration du tableau de bord (2.4, règle 1) ; une nouvelle
    ligne console `*_updated_buma_sql_table.js` relève de la règle 3 de 2.4.
- **C-09** : `idempotency` de l'audit : 0 groupe sur `system_id` (clés
  `idem:`, index source de D-0022) ; groupes `mobile_message_id` mesurés et
  conservés (877 groupes, 2 391 lignes en septembre).
- **C-10** : aucun `created` ni `updated` postérieur à `T_gel`, ou au dernier
  `T_gel'` (6.3), dans les 72 tables ; activité la plus récente cohérente
  avec la dernière activité connue (référent métier) [outil non attribué].
- **C-11** : comptages par table de la capture comparés à ceux du diagnostic
  à blanc, et ceux du diagnostic à blanc à septembre ; toute diminution est
  expliquée et acceptée par le décideur avant de continuer. Au-delà de
  1,5 fois les records de septembre, les marges de 5.1 ne valent plus.
- **C-12** : références orphelines par champ, fichiers référencés absents et
  objets non référencés : `preserved_source_anomalies` du diagnostic et
  `relations_summary` et `files` de l'audit, égaux entre eux (septembre :
  195 795 références dans 5 champs, 2 303 fichiers, 686 objets). Ces valeurs
  deviennent les attendus de la porte G3.
- **C-13** : comptes et paramètres : `auth` de l'audit (nombres de `users` et
  `agent_users`, `empty_password_hash` à 0, `bcrypt_hashes` égal au nombre de
  records, longueurs de `tokenKey` non nulles ; septembre : 20 `users`,
  3 `agent_users`) ; `admin_accounts` (1 en septembre) ;
  `storage_s3_enabled_in_snapshot` et `backup_s3_enabled_in_snapshot` faux ;
  diagnostic des comptes : `ready_for_auth_import` vrai, `external_auths_rows`
  à 0, `params.rows` à 1 avec `settings_plain_json_object` vrai,
  avertissements consignés (`backup_schedule_not_imported` attendu si la
  production a un cron de sauvegarde). Toute autre situation n'est pas
  couverte par l'outillage : arrêt.

### 6.6 Import dans le PostgreSQL de production fraîchement migré (porte G3)

1. **C-14** : avec le fichier d'environnement chargé, la requête
   `SELECT current_database()` donne la base attendue, le schéma `public` ne
   contient aucune table et aucun serveur PPBase ne sert cette base.
2. Migrations ; pour `migrate`, `--dir` désigne le dossier des migrations,
   alors que pour `serve` il désigne le dossier de données :

   ```sh
   .venv/bin/python -m ppbase migrate up --dir pb_migrations
   .venv/bin/python -m ppbase migrate status --dir pb_migrations
   ```

   **C-15** : les onze migrations de la release appliquées, de `1789236840`
   à `1789236850`, `Pending: 0`, `Orphaned: 0`. La migration `1789236850`
   n'a aucun effet sur cette base neuve, dont PPBase corrigé crée déjà les
   trois colonnes en `TEXT[]` ; comme elle le demande, aucun serveur PPBase
   ne tourne pendant les migrations (un serveur resté démarré garderait des
   requêtes préparées de l'ancien type,
   `pb_migrations/1789236850_c11_relation_multiple_columns.py`, D-0025).
3. Schéma, en lecture (les vues de contrôle sont temporaires,
   `scripts/verify_view_queries.py:57-76`) :

   ```sh
   .venv/bin/python scripts/verify_complete_schema.py --schema <export corrigé>
   .venv/bin/python scripts/verify_view_queries.py --schema <export corrigé>
   ```

   Codes de sortie 0 si conforme, 1 sinon ; `--relation-columns fresh`, la
   valeur par défaut, convient à une base neuve. **C-16** : `"ok": true`
   (114 définitions ; métadonnées d'index égales à l'export plus l'index
   source de D-0022 ; 191 colonnes relationnelles, les trois multiples en
   `text[]` avec index GIN) et 42 vues sur 42 en `OK`. Durée des étapes 2 à
   4 : L10.
4. Graines C5 : la migration `1789236846` insère 24 lignes
   (`STOCK_PRINCIPAL`, trois moyens de paiement, `CUSTOMER_DIVERS`,
   `SUPPLIER_DIVERS` et 18 rôles applicatifs). Le backup contient leurs
   versions source, créées par le bootstrap de PocketBase
   (`master.setup.ts`), et l'importeur refuse une cible non vide. Les
   répétitions ne les retirent que dans la base qu'elles viennent de créer,
   par leur harnais (`scripts/c11_rehearsal.py:1949-1956` ; `prepare_target`
   de `scripts/c11_real_rehearsal.py`, `seed_rows_removed` à 24).
   [outil non attribué] : une commande qui refuse si la base n'est pas celle
   de C-14 juste migrée (exactement les 24 lignes de `1789236846`, aucun
   `_superusers`, aucun paramètre `settings`), les supprime en une
   transaction et consigne les comptages, dans cette seule base, avant
   l'import.
5. **Import des records, des comptes et des paramètres, en une seule
   transaction** (D-0026, D-0027 ; contrat `docs/contracts/c11-auth-import.md`
   §6) [outil non attribué]. Le seul point d'entrée est la fonction
   `scripts.c11_import.import_snapshot(engine, data, destination, diagnostic,
   definitions, report=…)` :
   - avant toute écriture : moteur `create_async_engine(…,
     hide_parameters=True)` exigé (`engine_hide_parameters_required`),
     diagnostic prêt, `check_ready` des comptes en lecture seule ;
   - copie privée vérifiée de la capture dans `destination`, chemin neuf ;
     en mode complet, `data.db` et tous les objets de `storage/`
     (`stage_snapshot`, `scripts/c11_import.py:2066-2077`) ;
   - une transaction : verrou des 72 tables, refus d'une cible non vide,
     COPY par lots de 5 000 avec relecture typée de chaque lot,
     `SET CONSTRAINTS ALL IMMEDIATE`, relecture complète et empreintes de
     chaque table, archive des rowids, recomptage des anomalies et des
     tableaux, puis `_admins` vers `_superusers`, secrets de jetons et
     paramètres (`import_auth`). Tout échec annule tout et supprime le
     staging.

   Ce point d'entrée refuse toute base autre que `buma_c11_rehearsal`
   (`isolated_rehearsal_database_required`, `scripts/c11_import.py:2815-2816`
   ; `scripts/c11_auth_import.py:44`), et aucun module n'offre de commande de
   production (`scripts/c11_import.py:1`, `scripts/c11_auth_import.py:3-4`).
   L'outil à écrire : une commande de production qui appelle `import_snapshot`
   sur la base neuve de C-14, sous `cancelled_by_termination()` (SIGTERM et
   SIGHUP deviennent une annulation avec nettoyage,
   `scripts/c11_import.py:3170-3188`), lit l'URL dans `PPBASE_DATABASE_URL`,
   écrit dans `<privé>` son rapport agrégé et la preuve `report`, sans
   aucune valeur, et lève la garde de base pour cette seule cible par un
   changement relu et une décision D-XXXX (2.1). Ne jamais contourner la
   garde en nommant la base de production `buma_c11_rehearsal`. Lancer
   l'outil dans `tmux` ou `screen` ; après SIGKILL ou OOM, rien ne reste dans
   PostgreSQL (le serveur annule la transaction) mais le staging privé
   reste, à supprimer à la main avant une reprise avec un chemin neuf
   (D-0026, point 5). Après succès, le staging garde une copie privée de
   `data.db`, et de `storage/` en mode complet : il sert de source à 6.8 ou
   est détruit selon I17. Durée : L11 ; échec : L12 ; disque : 2.3.
6. Comptes, paramètres et sessions : dans la même transaction (étape 5),
   `import_auth` copie `_admins` vers `_superusers` (hash bcrypt, `tokenKey`,
   avatar et dates à l'identique, `lastResetSentAt` importé), les huit
   configurations de jetons vers `users`, `agent_users` et `_superusers`,
   et les paramètres que PPBase sait représenter (`meta`, `logs`, `smtp`,
   modèles d'email) ; `s3`, `backups`, OAuth2 et `_externalAuths` ne sont pas
   importés (D-0027). Aucun secret n'est journalisé. À faire **avant** tout
   démarrage de PPBase sur cette base : sans superuser, PPBase crée un jeton
   d'installation dans `_params` et imprime une URL d'installation dans sa
   console (`ppbase/app.py:189-214`), et l'import refuserait ensuite la
   cible.
7. Échec à l'une de ces étapes : NO-GO, retour arrière avant ouverture (8.2).
   La base reste en l'état pour analyse ; elle n'est ni vidée ni réutilisée
   sans décision.

### 6.7 Contrôles de conservation (porte G3)

Sources : la preuve `report` de l'import (D-0026, point 4 : relecture typée,
empreintes, archive des rowids, recomptage des anomalies et des tableaux,
rapport des comptes) et des contrôles indépendants après commit, dans de
nouvelles sessions, comme ceux de la phase `post_commit_checks` de
`scripts/c11_real_rehearsal.py` (comptages, empreintes relues, rowids,
tableaux, anomalies, comptes, jetons et paramètres, schéma, vues). Ces
contrôles ne s'exécutent que dans le conteneur jetable du banc : pour la
production, [outil non attribué] (intention : les mêmes contrôles en lecture
seule sur la base de production, rapport agrégé dans `<privé>`). Durée : L13.

| Contrôle | Attendu |
| --- | --- |
| C-17 Comptages | 72 tables sur 72 égales au diagnostic de la capture ; total égal |
| C-18 Empreintes | 72 empreintes de table sur 72 égales à la source |
| C-19 Rowids | rowid source conservé pour 100 % des records ; `users` et `buma_article` : rowid natif égal au rowid source (D-0016, `docs/contracts/rowid.md`) |
| C-20 Relations multiples | `buma_customer.pdv`, `buma_pilotage_ticket.tag`, `buma_wallet.managers` : mêmes listes, mêmes éléments dans le même ordre ; aucune liste devenue scalaire ; l'élément de 31 caractères reste un seul élément (D-0019, D-0025) |
| C-21 Références orphelines | nombre par champ égal à C-12 ; aucune cible créée, aucune valeur modifiée |
| C-22 Fichiers absents | liste privée égale à C-12 ; aucun fichier créé |
| C-23 Doublons mobile | groupes et lignes égaux à C-09 ; aucune ligne supprimée |
| C-24 Comptes utilisateurs | `users` et `agent_users` : nombres égaux ; hash bcrypt et `tokenKey` identiques pour 100 % des records, comparés sans affichage |
| C-25 Superusers | nombre égal à `_admins` de la capture ; email, hash, `tokenKey` et avatar repris ; aucun jeton d'installation dans `_params` |
| C-26 Paramètres | nom, SMTP et durées des jetons égaux à la source, secrets présents et jamais affichés ; `meta.appURL` égal à l'URL publique des clients après la bascule (avec U-A, celle de la source ; sinon décision avant G0), car c'est la seule base des liens envoyés par email (I16) ; S3 désactivé ; `backups.cron` vide (non importé, avertissement `backup_schedule_not_imported`) ou valeur décidée avec I13 et I18 ; `rateLimiting` et `rateLimits` désactivés (`ppbase/middleware/rate_limit.py:26-30`, `ppbase/api/settings.py:94-102`), PocketBase 0.22.34 n'en ayant pas ; `batch` selon la décision de 10.4 ; environnement du service : `PPBASE_SMTP_*` conforme à I16 et `PPBASE_DEV` absent |
| C-27 Schéma après import | C-15 et C-16 rejoués : 114 définitions, 42 vues, `Pending: 0` |
| C-28 Horloge | `now()` de PostgreSQL et horloge de l'hôte PPBase postérieurs au plus grand `created` ou `updated` importé ; NTP synchronisé [outil non attribué] |
| C-29 Source inchangée | SHA-256 du `data.db` de la capture égal avant et après l'import |

C-28 : les numéros de transaction suivent la ligne de plus grand `created`
(`app/finance/service.py:44-57`) et le lookup de D-0023 trie par `created` ;
une horloge en retard sur les données importées pourrait produire des
numéros en double.

### 6.8 Copie et contrôle de `storage/`

1. Copier tous les objets de `storage/` de l'extraction, ou du staging
   vérifié de l'import (6.6, étape 5), dans `<PPBASE_DATA_DIR>/storage/`
   (septembre : 1 282 objets, dont 601 `.attrs`, 80 miniatures et 5 autres
   objets non référencés, tous conservés ; PocketBase lit dans les `.attrs`
   les attributs des fichiers, dont leur type, par exemple
   `apis/file.go:166-172`). Le dossier est un vrai dossier, propriété de
   l'utilisateur du service PPBase, sans accès pour les autres. Jamais de
   lien symbolique ni de montage partagé avec `pb_data/storage` de
   PocketBase : PPBase refuse une racine liée
   (`ppbase/services/file_storage.py:466-480`) et un partage lui permettrait
   de modifier PocketBase (principe 2).
2. **C-30** : manifeste SHA-256 et tailles de la copie égal à celui de la
   capture (manifeste de C-06) : 100 % des objets, 0 manquant, 0 en trop,
   0 différence [outil non attribué ; briques existantes :
   `storage_inventory` et `directory_manifest` de
   `scripts/c11_import.py:1030-1045` et `:1134-1154`, et la répétition
   synthétique, qui compare le `storage/` servi à l'inventaire source,
   `scripts/c11_rehearsal.py:2491`]. Durée : L14.
3. Les fichiers référencés absents (2 303 en septembre) restent absents ;
   aucun fichier de remplacement.
4. Option à décider : pré-copie de `storage/` avant le gel pour raccourcir la
   fenêtre, acceptable seulement si l'état final égale exactement le
   manifeste de la capture.
5. Le dossier de données PPBase reste privé et entre dans la sauvegarde
   (I18).

### 6.9 Contrôles avant ouverture (porte G4)

Démarrer PPBase **sous le gestionnaire de service de production** (I5) dès
cette étape : utilisateur système dédié, fichier d'environnement 0600 de 6.1,
dossier de lancement vide (6.1, étape 4), politique de redémarrage décidée,
écoute en boucle locale ou sur une adresse réservée à l'opérateur, les
clients restant en maintenance. La ligne de commande est celle des
répétitions (`scripts/c11_rehearsal.py:2284-2320`, `PPBaseServer` de
`scripts/c11_real_rehearsal.py`), sans `--db` :

```sh
<checkout BUMA>/.venv/bin/python -m ppbase serve \
  --dir <PPBASE_DATA_DIR> \
  --migrations-dir <checkout BUMA>/pb_migrations \
  --hooks app.hooks.register:register \
  --host 127.0.0.1 --port <port> \
  --no-automigrate --no-apply-migrations-on-start --no-generate-migrations
```

avec `PYTHONPATH=<checkout BUMA>`. Si PPBase est d'abord lancé hors service,
rejouer C-31, C-44 et C-41 après le passage sous service, avant G5, et le
consigner. `--generate-migrations` reste désactivé sauf décision (I2) :
actif, une modification de collection dans le tableau de bord écrit un
fichier dans le dossier des migrations (`ppbase/config.py:179-183`). Durée du
démarrage : L15.

Contrôles en lecture seule, sans écriture métier sur la production ; records
choisis en privé, jamais cités. Chaque réponse est comparée à celle de
PocketBase 0.22.34 pour la même requête, erreurs comprises. La référence
vient de C-39 le jour J ou, à défaut, de la comparaison faite sur staging
avant le jour J (scénarios ci-dessous). Règles de comparaison :

- statut HTTP identique ;
- `Content-Type` identique ; seule exception possible, la classe
  `json_content_type_charset` (10.4), si elle est décidée ;
- pour les fichiers, `Content-Type`, `Content-Disposition`,
  `Content-Length`, `Cache-Control` et `Content-Security-Policy` identiques ;
- corps JSON comparés comme valeurs : ordre des clés d'objet ignoré, traité à
  part par D-0029 ; nombres comparés par leur texte ;
- jetons comparés par leur en-tête et leurs claims, hors `iat` et `exp` ;
  `type` `authRecord` dans PocketBase et `auth` avec `refreshable` dans PPBase
  (D-0027) ;
- `clientId` de `PB_CONNECT` et horodatages d'émission normalisés ;
- tout le reste à l'identique. Toute autre différence est un échec, sauf
  correction ou décision prise avant G0.

Durée des lectures automatiques : L16 ; contrôles manuels, SDK et desktop
[durée non mesurée].

- **C-31** : `GET /api/health` répond 200. PPBase ajoute `data.version`
  (`ppbase/api/health.py:14-26`), là où PocketBase 0.22.34 ne sert que
  `data.canBackup` (`apis/health.go:23-42`) : écart ouvert (10.4).
- **C-32** : sondes legacy (contrat C3,
  `docs/contracts/authentication.md:7-12`), avec le jeton d'un compte
  `users` ou `agent_users` : `GET /isConnected` donne 200
  `{"message":"true"}`, et 400 `{"message":"false"}` sans jeton ;
  `GET /master/user/health` donne 200 avec le texte `true`, et `false` sans
  jeton. Avec le jeton administrateur, les deux sondes répondent comme sans
  jeton, car la source ne lit que le record authentifié
  (`index.pb.ts:3-10`, `master.user.ts:1-14`) : ce n'est pas un échec.
- **C-33** : connexion `users` par username et `agent_users` par email et par
  username : 200 `{token, record}`, sans `rowid`, `passwordHash` ni
  `tokenKey` dans `record` ; refresh 200 ; lecture de son propre record
  `agent_users` 200 ; mauvais mot de passe 400. Sur un mauvais mot de passe,
  PocketBase 0.22.34 répond
  `{"code":400,"message":"Failed to authenticate.","data":{}}`
  (`forms/record_password_login.go:83-85`, `apis/record_auth.go:316-317`,
  `apis/api_error.go:11-16`, `:67-89`) ; PPBase renseigne `data.identity`
  (`app/auth/login.py:108-113`, `ppbase/api/record_auth.py:286-291`) : sans
  correction ni décision avant G0 (10.4), C-33 échoue.
- **C-34** : continuité des sessions : le jeton PocketBase de 6.1 de chaque
  compte de contrôle est accepté après l'import : `POST
  /api/collections/<collection>/auth-refresh` pour `users` et `agent_users`,
  `POST /api/admins/auth-refresh` pour l'administrateur : 200, même record.
  Requête sans exposer le jeton dans la liste des processus, par exemple :

  ```sh
  curl -sS -X POST -H @<privé>/c34-users.header \
    -o <privé>/c34-users-response.json -w '%{http_code}\n' \
    http://127.0.0.1:<port>/api/collections/users/auth-refresh
  ```

  La réponse contient un nouveau jeton : elle reste dans `<privé>` et est
  détruite avec les jetons de contrôle. PocketBase 0.22.34 signe les jetons
  de record avec `tokenKey` + `recordAuthToken.secret`, type `authRecord`
  (`tokens/record.go:13-27`), et ceux d'administrateur avec `tokenKey` +
  `adminAuthToken.secret`, type `admin` (`tokens/admin.go:11-17`) ; PPBase
  vérifie la même formule avec les secrets importés (contrat
  `docs/contracts/c11-auth-import.md` §1), ce que prouve le banc synthétique
  `docs/c11-auth-runtime-report.json` (D-0027). Ensuite, l'opérateur détruit
  les fichiers des jetons de contrôle et des réponses dans `<privé>` et
  consigne la destruction, sans valeur : ces jetons restent valides jusqu'à
  leur expiration.
- **C-35** : l'administrateur se connecte (`POST
  /api/admins/auth-with-password`) et ouvre le tableau de bord `/_/` ;
  72 collections et 42 vues listées ; aucune modification.
- **C-44** : mode d'erreur PocketBase 0.22 actif [à confirmer à
  l'intégration : WS-A6]. Avec le jeton administrateur de C-35,
  `GET /api/collections/users/records/<ID inexistant de 15 caractères>`
  répond 404 avec la clé `code` à 404 et sans clé `status`, comme PocketBase
  0.22.34 : `{"code":404,"message":"The requested resource wasn't
  found.","data":{}}` (`apis/record_crud.go:171-174`,
  `apis/api_error.go:31-38`, `:67-89`). PPBase `237b403` sert `code` à côté
  de `status` (`ppbase/app.py:426-429`) : sans le mode 0.22, C-44 échoue.
- **C-36** : lectures natives avec un compte de contrôle : liste filtrée de
  `buma_transaction` avec `expand=article,author` (enveloppe `page`,
  `perPage`, `totalItems`, `totalPages`, `items`) ; une vente dont le client
  est une référence orpheline (`expand=customer`) ; un client avec une liste
  `pdv` non vide ; le wallet dont `managers` contient l'élément de 31
  caractères ; les trois vues rowid `buma_article_with_no_stock_list_view`,
  `buma_stockage_no_manager` et `buma_stock_transaction_list_view`, rowids
  égaux à la source ; une liste lisible sans compte, avec `expand` vers une
  cible dont la viewRule n'est pas publique : aucun record protégé développé
  (D-0033, `apis/record_helpers.go:153-189`) [à confirmer à l'intégration :
  WS-A6].
- **C-37** : fichiers : échantillon de fichiers présents, d'images ou de PDF
  (servis `inline` par PocketBase) et d'autres types (servis `attachment`),
  téléchargés par `/api/files/<collectionId>/<recordId>/<fichier>` : octets
  égaux au manifeste, et statut et en-têtes égaux à ceux de PocketBase
  0.22.34 selon les règles ci-dessus (aucun des 20 champs fichiers de
  l'export n'est protégé). PocketBase sert le type enregistré dans les
  `.attrs` à l'envoi, avec `inline` ou `attachment; filename=`
  (`tools/filesystem/filesystem.go:168-215`, `:367-377`, `:393-430`) ;
  PPBase `237b403` déduit le type de l'extension et n'envoie
  `Content-Disposition` qu'avec `download` (`ppbase/api/files.py:141-145`,
  `:1117-1145`) : sans correction (WS-A7) ni décision avant G0, C-37 échoue.
  Une référence de fichier absent répond 404 « The requested resource wasn't
  found. » dans les deux systèmes (`apis/file.go:213-215`,
  `apis/api_error.go:31-38` ; `ppbase/api/files.py:84-92`), dans l'enveloppe
  de C-44. Aucune miniature avant l'ouverture : la demande en crée une
  (`apis/file.go:162-193`) ; les miniatures sont testées sur staging.
- **C-38** : realtime : connexion `GET /api/realtime` reçue avec
  `PB_CONNECT`, abonnement accepté ; la réception d'événements, qui demande
  une écriture, est testée sur staging.
- **C-39** (recommandé, référence des réponses de C-31 à C-38) : mêmes
  lectures sur un PocketBase 0.22.34 lancé sur une **copie** de la capture
  avec les hooks de production, en boucle locale, jamais sur le `pb_data` de
  production [outil non attribué]. Son bootstrap supprime puis recrée les
  18 `buma_apps_roles` à chaque démarrage (`master.setup.ts:5-8`, `:72-73`,
  `:309-324`) : comparer ces records hors `created` et `updated`.
- **C-40** : mêmes lectures avec le SDK des clients (I8)
  [outil non attribué]. `scripts/c10_sdk_probe.mjs` écrit des données :
  jamais sur la production.
- **C-41** : comptages et empreintes des 72 tables de collections égaux
  avant et après ces contrôles [outil non attribué ; la répétition réelle le
  fait dans son seul conteneur, phase `after_http_checks`, L17] ; seul le
  journal de requêtes de PPBase, `_requests`, hors de ces tables, grossit
  (`ppbase/middleware/request_logger.py:1-6`).

Staging, avant le jour J et obligatoire pour la porte G0, sous
l'autorisation de 2.1 (I23) : base PostgreSQL jetable chargée depuis un
backup réel [outil non attribué : `import_snapshot` n'a pas de commande, et
les répétitions détruisent leur base à la fin], comptes de test créés dans
cette seule base. Règles d'isolement :

- aucune exposition aux clients de production : ni URL, ni DNS, ni amont de
  proxy partagés avec la production ; accès réservé aux testeurs nommés ;
- SMTP neutralisé : aucun `PPBASE_SMTP_*`, donc aucun envoi, et jamais
  `PPBASE_DEV`, qui écrirait corps, liens et jetons des emails dans les
  journaux (I16) ; ne jamais appeler `/api/settings/test/email`, qui
  utiliserait les identifiants SMTP de production importés (contrat
  `docs/contracts/c11-auth-import.md` §9) ; journaux du staging privés et
  détruits avec lui ;
- secrets de jetons distincts de la production. Le staging contient les
  `tokenKey` réelles : avec les secrets de production, il accepterait les
  jetons de production, et un jeton `admin` qu'il émettrait pour
  l'administrateur importé serait accepté par PocketBase 0.22.34, qui
  vérifie `tokenKey` + `adminAuthToken.secret` (`daos/admin.go:52-78`,
  `apis/middlewares.go:229-240`). Chargement avec des secrets propres au
  staging [outil non attribué]. La rotation de 6.12 ne suffit pas : elle ne
  change que `authToken`, et les secrets de réinitialisation, de
  vérification, de changement d'email et de fichier resteraient ceux de la
  production, qui accepte les jetons signés avec eux (contrat §1). La
  continuité des sessions est alors prouvée par le banc synthétique et par
  C-34 le jour J. Reprendre les secrets de production sur le staging exige
  une acceptation écrite du risque ;
- destruction de la base, du dossier de données et des journaux à la date
  fixée (I23), consignée.

Scénarios, résultats validés par les référents métier :

- desktop : connexion, écrans principaux, vente, retour, opérations de
  caisse ; console `executeSql` avec une requête PostgreSQL contenant `id`,
  puis lecture native de `buma_sql_table` page 1, `perPage` 1000 (D-0018) ;
  appels éventuels à `POST /sql` (I8) ;
- mobile : connexion, refresh, synchronisation, opération avec
  `mobile_message_id` rejouée deux fois (une seule écriture), rejeu d'une file
  hors ligne ; usage éventuel de `isAuthRecord` après un refresh (D-0027) ;
- administration : tableau de bord, consultation et édition de records de
  test ; filtres sur des dates vides (D-0032) ;
- SDK, fichiers et realtime : écritures, envoi et téléchargement de
  fichiers avec leurs en-têtes, miniatures, événements, avec un outil
  paramétrable (URL cible, comptes de test, aucun accès direct à la base,
  rapport neuf hors dépôt) [outil non attribué]. Ne jamais lancer
  `scripts/c10_sdk_runtime.py` tel quel, en particulier depuis un checkout de
  release : il est câblé sur l'environnement C10 (Node
  `/opt/homebrew/bin/node`, SDK pris dans `../buma_boutique_web`, serveurs
  127.0.0.1:8098 et 127.0.0.1:8090), écrit ses fixtures directement dans la
  base locale du port `55434` et réécrit `docs/c10-sdk-runtime-report.json`
  (`scripts/c10_sdk_runtime.py:53`, `:61-63`, `:88-92`, `:98-100` ;
  `scripts/c10_remaining_runtime.py:34-60`) ;
- réponses, erreurs comprises, comparées pour les cas de C-31 à C-38 à
  celles d'un PocketBase 0.22.34 lancé sur une copie du même backup avec les
  hooks de production, comme C-39, selon les règles ci-dessus
  [outil non attribué].

### 6.10 Bascule d'URL et ouverture (porte G5)

| Méthode | Principe | Retour |
| --- | --- | --- |
| U-A, même URL, amont du proxy changé (proposée) | aucun changement côté clients | immédiat : remettre l'amont PocketBase |
| U-B, changement DNS | aucun changement côté clients ; propagation selon le TTL, abaissé à l'avance ; un client resté sur l'ancien hôte trouve PocketBase gelé | inverse, même délai de propagation |
| U-C, nouvelle URL dans les clients | nouvelle version ou configuration des clients desktop et mobile | lent : nouvelle diffusion des clients |

1. Basculer l'amont vers PPBase avec la configuration préparée (6.1) : mêmes
   limites, délais, en-têtes et SSE sans mise en tampon ; CORS identique
   (`--origins` de PocketBase contre `origins` de PPBase) ; certificat
   valide.
2. **C-42** : par l'URL publique, avant la levée de la maintenance si le
   proxy le permet : santé, connexion d'un compte de contrôle, une liste, un
   fichier et une connexion realtime, 100 % passants.
3. Lever la maintenance ; noter `T_ouverture`. À partir de cet instant,
   PPBase reçoit des écritures et le retour arrière suit 8.3.
4. Rétablir les tâches externes suspendues au gel (I13), redirigées vers
   PPBase, une par une, chacune consignée ; une tâche qui ne peut pas être
   redirigée reste suspendue, sans quoi elle viserait PocketBase arrêté.
5. **C-43** : premières opérations réelles des référents métier (desktop,
   mobile, caisse) : réussies, persistées, numéros de transaction à la suite
   de ceux importés.
6. Annoncer l'ouverture ; rappeler aux référents desktop le dialecte
   PostgreSQL de la console (D-0018). PocketBase reste arrêté, `pb_data`
   intact, redémarrage automatique suspendu jusqu'à la fin de la période de
   retour arrière (8.4). Durée : L18.

### 6.11 Surveillance (porte G6)

Pendant la surveillance renforcée, d'une durée fixée par le décideur (I21) :

- disponibilité : `/api/health` vu de l'extérieur, processus PPBase,
  PostgreSQL ;
- HTTP : taux de 5xx et de 4xx par famille de routes (`/api/collections`,
  `/master`, `/api2`, fichiers, realtime) d'après les journaux du proxy,
  comparés au taux habituel de PocketBase (I9) [outil non attribué] ;
- authentification : aucun échec pour les comptes valides des référents ;
  révocation globale seulement par 6.12 ;
- PostgreSQL : violations de contraintes, interblocages, attentes de verrous,
  saturation des connexions (10 + 20 par processus PPBase, I6), requêtes
  lentes, disque et WAL ;
- métier : numéros de transaction sans doublon, rejeux mobiles sans doublon
  (retour, recharge et conversion recherchent la clé d'idempotence, D-0022),
  ouverture et fermeture des sessions de caisse, soldes cohérents selon les
  référents ;
- fichiers : envois et téléchargements, avec leurs en-têtes ;
- journal de requêtes PPBase : chaque requête est enregistrée dans
  `_requests` (`ppbase/middleware/request_logger.py:1-6`), avec des extraits
  de corps de requête et de réponse jusqu'à 32 Kio et le seul masquage des
  clés sensibles (`:31-56`) : emails, noms et montants y restent ; aucune
  rétention n'est appliquée (`logs.maxDays` importé mais ignoré, contrat
  `docs/contracts/c11-auth-import.md` §3) : volume, données personnelles et
  conservation à décider (I18) ;
- journaux du processus PPBase, **confidentiels** : asyncpg ou PostgreSQL
  peuvent y citer une valeur (`DETAIL`, erreur de conversion) malgré
  `hide_parameters` (`ppbase/db/engine.py:45-49`, D-0027) ; chaque email
  envoyé y laisse son destinataire (`ppbase/services/mail_service.py:69-72`) ;
  `PPBASE_DEV` reste désactivé ; accès restreint et conservation décidée ;
- sauvegardes : PostgreSQL et dossier de données PPBase selon I18 ;
  sauvegardes natives de PPBase seulement si `backups.cron` est renseigné
  (`ppbase/backup/automation.py:144-175`, lancées par
  `ppbase/app.py:280-284`) : à surveiller dans ce cas (rotation
  `cronMaxKeep`, archives dans le dossier de données).

Fin : le décideur clôt la surveillance ; `H_stop` est revérifiée ; PocketBase
et la capture sont conservés selon I17.

### 6.12 Révocation globale d'urgence des sessions

PPBase n'offre aucune voie API ni tableau de bord pour faire tourner un
secret de session : `PATCH /api/settings`, la procédure « Invalidate all
previously issued tokens » du tableau de bord 0.22, y répond 200 sans effet
(D-0027 ; contrat `docs/contracts/c11-auth-import.md` §3). La rotation SQL
du contrat (§7), prouvée par le banc des sessions
(`comparison.session_revocation_emergency_sql` de
`docs/c11-auth-runtime-report.json`), est **la seule révocation globale**.
Décidée par le décideur, exécutée par l'opérateur sur la base de production,
en une transaction (la requête est celle du banc,
`EMERGENCY_SESSION_REVOCATION_SQL`, `scripts/c11_auth_runtime.py:296-301`) :

```sql
BEGIN;
UPDATE "_collections"
   SET "options" = jsonb_set("options", '{authToken,secret}',
         to_jsonb(replace(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))),
       "updated" = now()
 WHERE "type" = 'auth' AND "name" IN ('users', 'agent_users', '_superusers')
RETURNING "name", length("options" -> 'authToken' ->> 'secret') AS secret_length;
-- attendu : une ligne par nom, secret_length = 64 ; sinon ROLLBACK
COMMIT;
```

- Réduire la liste à `'users', 'agent_users'` (sessions record) ou à
  `'_superusers'` (sessions admin) selon la décision. Chaque collection
  reçoit un secret aléatoire distinct, tiré par `gen_random_uuid()`, qui ne
  quitte jamais la base ; ne jamais afficher ni journaliser la colonne
  `options`.
- Effet immédiat, sans redémarrer PPBase : jetons 0.22 et jetons émis par
  PPBase refusés (401 au refresh et sur `/api/admins`, `/isConnected` 400
  `{"message":"false"}`) ; mots de passe inchangés, nouvelles connexions
  acceptées. Les flux realtime déjà ouverts reçoivent encore les événements
  jusqu'à leur reconnexion ; leur prochain POST est refusé (403), comme après
  une révocation 0.22.
- Limites : seule la rotation d'`authToken` est prouvée ; les autres
  secrets (`passwordResetToken`, `verificationToken`, `emailChangeToken`,
  `fileToken`) ne changent pas (contrat §9). Pour un seul compte, la voie API
  suffit : un admin change son mot de passe, comme en 0.22.
- Retour arrière : PocketBase garde ses anciens secrets ; les sessions
  révoquées sur PPBase redeviennent valables sur PocketBase jusqu'à leur
  `exp`, sauf révocation faite aussi côté PocketBase (Settings → Token
  options) après son redémarrage (8.3).
- Consigner la décision, l'heure, les collections et `secret_length`,
  jamais une valeur.

## 7. Critères d'arrêt (go/no-go)

Aux portes G0 à G4, tout critère non atteint est un NO-GO : retour arrière
avant ouverture (8.2), sans perte. Aux portes G5 et G6, le décideur applique
8.3.

| Porte | Critère | Seuil |
| --- | --- | --- |
| G0 | autorisations (2.1) et informations I1 à I25 | 100 % fournies |
| G0 | code de release et PPBase exigé (2.2, C-45) | commits du rapport final, arbres propres, `ppbase.models.field_multiplicity` présent |
| G0 | répétition réelle finale (`docs/c11-real-rehearsal-report.json`) | `status` passed, deux passages identiques, divergences acceptées seulement par décision |
| G0 | porte de release (annexe A) | 0 échec ; 100 % des échecs PPBase antérieurs analysés |
| G0 | outils de 10.2 nécessaires aux portes G1 à G4, dont l'import de production | écrits, relus, testés, autorisés |
| G0 | version de la production (2.2, C-02) | binaire 0.22.34, empreinte officielle |
| G0 | révision des hooks et migrations déployés (2.2) | hooks égaux au build de `530018c`, toute différence décidée ; 0 migration en attente |
| G0 | gel du schéma et diagnostic à blanc (2.4, 6.1) | règles de 2.4 respectées ; C-05 et C-07 à C-13 conformes ; L4 à L9 mesurées en mode complet |
| G0 | machines (C-46) | décision consignée pour chaque hôte |
| G0 | écarts et constats de la section 10 | 100 % corrigés ou décidés ; D-0029 accepté ou corrigé |
| G0 | staging autorisé et isolé ; scénarios desktop, mobile et administration | 100 % passants |
| G1 | machines revérifiées avant le gel (C-46) | aucune exécution planifiée dans la fenêtre, ou décision appliquée |
| G1 | requêtes transmises à PocketBase après `T_gel` (C-01) | 0 hors opérateur |
| G1 | version du binaire (C-02) | 0.22.34, empreinte officielle |
| G1 | inventaire (C-05) | 0 blocage, 1 `data.db`, WAL et journal absents ou vides |
| G1 | empreintes de capture, transfert et extraction (C-03, C-04, C-06) | 0 écart |
| G2 | intégrité et schéma (C-07) | `ok`, 0 dérive sur 114, hors règle `buma_sql_table` décidée (2.4) |
| G2 | `_migrations` (C-08) | a) 325 sur 326, seule `1785599959` absente ; b) 14 sur 14 ; c) 0 disparue, 0 nouvelle hors console |
| G2 | clés `idem:` dupliquées (C-09), records postérieurs à `T_gel` ou `T_gel'` (C-10) | 0 et 0 |
| G2 | diminutions de comptage (C-11) | toutes expliquées et acceptées |
| G2 | situations non couvertes (C-13) | 0 |
| G3 | cible et migrations (C-14 à C-16) | base neuve, onze migrations, `Pending: 0`, `ok`, 42 sur 42 |
| G3 | comptages, empreintes, rowids (C-17 à C-19) | 72/72, 72/72, 100 % |
| G3 | tableaux, orphelins, fichiers absents, doublons (C-20 à C-23) | 0 écart avec la capture |
| G3 | comptes, superusers, paramètres (C-24 à C-26) | 100 % identiques ; `meta.appURL` et `PPBASE_SMTP_*` conformes |
| G3 | schéma, horloge, source (C-27 à C-29) | conformes, `data.db` inchangé |
| G4 | copie de `storage/` (C-30) | 100 % ; 0 manquant, 0 en trop, 0 différence |
| G4 | PPBase sous le service de production (6.9) | oui, ou C-31, C-44 et C-41 rejoués après le passage sous service |
| G4 | mode d'erreur 0.22 (C-44) | 404 avec `code`, sans `status` |
| G4 | contrôles avant ouverture (C-31 à C-38) | 100 % passants selon les règles de 6.9 : statut, content type, en-têtes significatifs et corps égaux à PocketBase 0.22.34, erreurs comprises ; 0 réponse 5xx |
| G4 | continuité des sessions (C-34) | 100 % des comptes de contrôle |
| G4 | comparaison avec PocketBase (C-39), si l'outil existe | 0 écart inexpliqué |
| G4 | écritures pendant les contrôles (C-41) | 0 dans les 72 tables |
| G5 | URL publique et premières opérations (C-42, C-43) ; tâches externes redirigées (6.10) | 100 % |
| G6 | surveillance | seuils ci-dessous |

Seuils proposés pour G6, à valider par l'utilisateur (I21) ; aucun n'est une
mesure :

- toute anomalie d'intégrité (numéro en double, solde incohérent, opération
  perdue ou dupliquée) : gel immédiat de PPBase et décision de retour ;
- une erreur 5xx inexpliquée sur une écriture financière (caisse, wallet,
  vente, retour, recharge, stock) : analyse immédiate ; trois : gel et
  décision ;
- plus de 10 réponses 5xx sur 1 000 requêtes métier consécutives : gel et
  décision ;
- PPBase ou PostgreSQL indisponible et non rétabli par un redémarrage : gel
  et décision ;
- un compte valide qui ne peut plus se connecter, hors erreur de mot de
  passe : décision ;
- soupçon de fuite d'un jeton ou d'un secret : décision de révocation
  (6.12).

## 8. Retour arrière

### 8.1 Principes

- Décidé par le décideur, exécuté par l'opérateur, journalisé.
- PocketBase redémarre sur **son** `pb_data`, intact et vérifié. On ne
  restaure pas une sauvegarde par-dessus, ce qui serait une modification,
  sauf `pb_data` endommagé et décision explicite.
- PPBase est arrêté avant tout redémarrage de PocketBase (principe 1).
- La base PostgreSQL et le dossier de données PPBase sont conservés pour
  analyse, jamais supprimés sans décision.
- Après un retour, la capture n'est plus fraîche : une nouvelle bascule exige
  un nouveau backup et une nouvelle fenêtre.

### 8.2 Avant ouverture, sans écriture client reçue par PPBase

1. Arrêter PPBase s'il tourne et vérifier qu'il n'écoute plus et que son
   processus est terminé ; consigner l'arrêt.
2. PocketBase jamais arrêté, par exemple après un NO-GO en G1 sur C-01 ou
   C-02 : il n'y a ni `H_stop` à vérifier ni redémarrage ; passer à
   l'étape 5. Une sauvegarde de la méthode A déjà créée reste dans
   `pb_data/backups/`, ou dans le bucket si `backups.s3` est actif (I13) ;
   elle n'est pas supprimée sans décision.
3. PocketBase arrêté : revérifier `H_stop` (C-03) sur le `pb_data` de
   production : SHA-256 de `data.db` et manifeste de `storage/` égaux. Si
   `H_stop` n'avait pas encore été calculée, la calculer maintenant et la
   consigner. En cas d'écart, **arrêt** : ne pas redémarrer, rechercher ce
   qui a écrit dans `pb_data`.
4. Démarrer PocketBase avec sa ligne de commande inchangée (I2). Effets
   attendus, comme à chaque démarrage : son bootstrap supprime puis recrée
   les 18 `buma_apps_roles` (`master.setup.ts:5-8`, `:72-73`, `:309-324`) et
   aucune migration n'est appliquée, le dossier déployé n'en ayant aucune en
   attente (2.2) ; ce n'est pas une corruption.
5. Dans tous les cas : rétablir le redémarrage automatique du service
   PocketBase et les tâches externes suspendues au gel (I13), vers
   PocketBase, y compris celles qui avaient été redirigées vers PPBase ;
   consigner chaque action.
6. En local : `/api/health`, connexion d'un compte de contrôle, une liste.
7. Remettre l'amont du proxy, ou le DNS, vers PocketBase et lever la
   maintenance ; noter `T_retour`.
8. Les jetons émis par PocketBase avant le gel restent valides jusqu'à leur
   expiration (`tokenKey` inchangées dans `pb_data`).
9. Communiquer, analyser, replanifier ; remettre les machines dans l'état
   décidé par l'infrastructure (C-46). Durée : L19.

### 8.3 Après ouverture, PPBase ayant reçu des écritures

1. Geler PPBase immédiatement (maintenance au proxy) ; noter `T_gel2`. Plus
   aucun client n'écrit.
2. Conserver PPBase : sauvegarde complète de la base de production (par
   exemple `pg_dump` au format personnalisé), manifeste de `storage/`
   [outil non attribué, celui de C-30] et journaux, dans `<privé>`,
   empreintes consignées.
3. **Arrêter PPBase** (service de production) et vérifier qu'il n'écoute
   plus et que son processus est terminé, tâches de fond comprises
   (realtime, sauvegardes automatiques, surveillance des hooks) ; consigner
   l'arrêt. Les étapes suivantes portent sur la base ou sur la sauvegarde,
   jamais sur un serveur actif.
4. Relever les écritures reçues entre `T_ouverture` et `T_gel2` : records
   créés, modifiés ou supprimés ; métadonnées de collections modifiées (par
   exemple la vue `buma_sql_table` par `executeSql`) ; fichiers ajoutés ou
   retirés ; comptes modifiés (mot de passe, `tokenKey`) ; rotation d'urgence
   éventuelle (6.12). `updated` ne suffit pas, une suppression ne laissant
   pas de trace : comparer l'état de PPBase ligne à ligne à la capture,
   inchangée [outil non attribué]. La table `_requests` aide à reconstituer
   les appels (méthode, URL, statut, extraits masqués) sans remplacer cette
   comparaison.
5. Le décideur et les référents fixent le sort de chaque écriture : par
   défaut, ressaisie par les clients dans PocketBase après son redémarrage,
   ses hooks recalculant stocks, soldes et numéros ; ou outil de rejeu dédié,
   écrit et testé sur staging avant usage [outil non attribué]. Jamais
   d'écriture SQL directe dans `data.db`. Avant toute ressaisie, vérifier
   qu'un client ne l'a pas déjà rejouée vers PocketBase (`mobile_message_id`,
   `system_id`).
6. Puis 8.2, étape 1 (PPBase arrêté, vérifié à l'étape 3) et étapes 3 à 9.

Impact client d'un retour après ouverture (D-0027 ; contrat
`docs/contracts/c11-auth-import.md` §7, mesuré par le banc des sessions,
`docs/c11-auth-runtime-report.json`) :

- les opérations faites dans PPBase manquent dans PocketBase jusqu'à leur
  ressaisie, et leurs IDs n'y existent pas ;
- les fichiers envoyés et les changements de compte faits dans PPBase (mot de
  passe, email, vérification) n'y sont pas ; un jeton à usage 0.22 consommé
  sur PPBase reste utilisable sur PocketBase jusqu'à son `exp` ;
- PPBase émet des jetons de record de type `auth`
  (`ppbase/services/auth_service.py:19`, `:487`), que PocketBase 0.22.34 ne
  reconnaît pas : il ne charge que `admin` et `authRecord`
  (`apis/middlewares.go:229-249`, `tokens/tokens.go:5-6`). Une requête qui
  porte un jeton de record émis ou rafraîchi par PPBase y est traitée comme
  anonyme : refresh 401, record protégé 404 ; l'utilisateur doit se
  reconnecter ;
- les jetons `admin` émis par PPBase (même `tokenKey`, secret importé) sont
  acceptés par PocketBase, qui ne vérifie que `tokenKey` +
  `adminAuthToken.secret` (`daos/admin.go:52-78`), sauf après une rotation
  d'urgence de `_superusers` ;
- après une rotation d'urgence (6.12), les sessions révoquées sur PPBase
  redeviennent valables sur PocketBase jusqu'à leur `exp`, sauf révocation
  faite aussi côté PocketBase ;
- les jetons émis par PocketBase avant le gel restent valides jusqu'à leur
  expiration.

### 8.4 Fin de la période de retour arrière

Déclarée par le décideur (I21). Ensuite, le retour n'est plus planifié ; le
`pb_data` de PocketBase et la capture sont archivés en lecture seule selon
I17, jamais supprimés sans décision ; le service PocketBase reste arrêté, son
redémarrage automatique désactivé par décision consignée.

## 9. Journal de bascule

Fichier privé `<privé>/journal.md` (0600), tenu par l'opérateur : une ligne
par action, heures UTC. Un résumé public n'en reprend que des agrégats et des
empreintes de fichiers.

| Horodatage UTC | Étape ou contrôle | Action, sans secret | Résultat | Preuve | Opérateur | Vérificateur | Décision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<AAAA-MM-JJ hh:mm:ss>` | 6.2, C-01 | maintenance activée | `T_gel` noté | journal du proxy | `<initiales>` | `<initiales>` | — |
| `<…>` | 6.3, C-03 | `H_stop` calculée | empreintes consignées | `<privé>/…` | `<…>` | `<…>` | — |
| `<…>` | G2 | relecture du diagnostic | conforme ou écart | `<privé>/…` | `<…>` | `<…>` | GO ou NO-GO |

À consigner au minimum : autorisations et méthodes retenues ; décisions sur
les écarts et constats de la section 10 ; décisions de l'infrastructure sur
les machines (C-46) ; diagnostic à blanc, ses durées et sa référence ;
commits BUMA et PPBase (C-45) ; `T_gel`, capture, arrêt de PocketBase,
`H_stop` ; chaque redémarrage sous gel et son `T_gel'` ; chaque suspension et
chaque rétablissement d'une tâche externe ou d'un redémarrage automatique ;
empreinte de la capture ; chaque rapport et son empreinte ; chaque contrôle
C-01 à C-46 et chaque porte G0 à G6 avec sa décision ; destruction des
jetons de contrôle ; `T_ouverture` ; incidents ; toute rotation d'urgence
(6.12), sans valeur ; en cas de retour, `T_gel2`, l'arrêt de PPBase,
`T_retour` et le relevé des écritures ; fin de la période de retour arrière.

## 10. Points ouverts avant une bascule

### 10.1 Intégration encore attendue à la rédaction

- WS-A6 (PPBase) : expand filtré par la viewRule de la cible (D-0033), mode
  d'erreur PocketBase 0.22 activé par BUMA dans `app/hooks/register.py`
  (C-44), emails des comptes visibles par la manageRule (10.4).
- WS-A7 (PPBase) : service des fichiers : miniature par défaut,
  `Content-Disposition`, `Content-Type` des `.attrs` (C-37).
- WS-G (BUMA) : `scripts/c11_real_rehearsal.py`, de la branche `c11/ws-g`, et
  le rapport final `docs/c11-real-rehearsal-report.json` (deux passages, code
  intégré), dont les valeurs remplacent l'aperçu dans 5.1 et 5.2.
- Publication de D-0030 à D-0033 par l'orchestrateur.

### 10.2 Outils non attribués

- **import de production** (6.6, étape 5) et retrait des graines C5 de la
  base neuve (6.6, étape 4) : sans eux, aucune bascule ;
- contrôles de conservation indépendants sur la base de production (6.7) et
  empreintes des 72 tables avant et après les contrôles (C-41) ;
- manifeste SHA-256 et tailles d'un dossier : copie à froid (6.3),
  `storage/` de `H_stop` (C-03), copie de `storage/` de PPBase (C-30) et
  sauvegarde de 8.3 ;
- preuve de révision : hooks déployés contre un build de `530018c`,
  migrations déployées contre la source et `_migrations` (2.2) ;
- `_migrations` par catégorie, comparé à sa référence privée (C-08 b et c) ;
- prise en charge par l'importeur d'une dérive de `buma_sql_table`, si
  l'utilisateur la retient (2.4) ;
- plus grand `created` ou `updated` comparé à `T_gel` (C-10) ;
- contrôle d'horloge (C-28) ;
- comparaison avec un PocketBase sur copie (C-39) et lectures SDK sans
  écriture (C-40) ;
- chargement du staging depuis un backup réel, avec des secrets propres au
  staging, et parcours SDK, fichiers et realtime paramétrable (6.9) ;
- taux d'erreurs habituel de PocketBase (6.11) ;
- relevé des écritures PPBase après ouverture et outil de rejeu éventuel
  (8.3) ;
- comparaison des routes enregistrées par PPBase avec celles de 0.22.34
  (10.4).

### 10.3 Décisions qui touchent l'exploitation ou les clients

Toujours en vigueur : D-0016 (rowid), D-0018 (console PostgreSQL), D-0021
(historique réel), D-0022 (index d'idempotence source), D-0023 (ordre du
lookup). D-0019 est résolu par D-0025. D-0030 à D-0033 sont des projets de
l'orchestrateur, publiés sous ces numéros.

| Décision | Effet pour les clients ou l'exploitation | Dans ce runbook |
| --- | --- | --- |
| D-0024 | version source 0.22.34 validée ; version fournie avec le backup, jamais devinée ; seuls les binaires officiels linux amd64 sont comparés | `--source-version 0.22.34` (6.5) ; C-02 |
| D-0025 | relations à `maxSelect` nul servies en listes ; écriture API vers une cible absente refusée en 400, y compris la mise à jour d'un record qui porte un orphelin historique ; suppressions qui nettoient les références, comme en production ; PPBase corrigé exigé ; `1789236850` sans effet sur une base neuve, à appliquer serveur arrêté ; écarts PPBase restants listés (dont `minSelect` non appliqué, tri sur un champ multiple, expand `_via_` absent, aucun hook de modèle pendant le nettoyage) | C-45, C-15, C-20, C-36 ; 10.4 |
| D-0026 | historiques importés tels quels, anomalies comprises, sans troncature ni conversion ; une seule transaction ; SIGTERM et SIGHUP annulent proprement sous `cancelled_by_termination` ; après SIGKILL ou OOM, seul le staging privé reste | 6.6 ; C-17 à C-23 ; L11, L12 |
| D-0027 | sessions 0.22 reprises, sans reconnexion forcée ; nouveaux jetons de type `auth` (SDK JS 0.21 : `isAuthRecord` faux, à vérifier sur les clients, I8) ; `PATCH /api/settings` ne révoque rien ; emails par `PPBASE_SMTP_*` seulement, liens sur `meta.appURL` ; gel avant le backup frais ; journaux PPBase confidentiels | 6.1, C-26, C-33, C-34, 6.11, 6.12, 8.3 |
| D-0028 | extraction plus stricte que PocketBase ; à lancer dans `tmux` ou `screen` | 6.4 ; L5 |
| D-0029 | ordre des clés JSON (7 objets réels) et filtres `= null` sur 100 valeurs SQL NULL différents de PocketBase ; acceptation de l'utilisateur demandée | 2.1, G0 ; règles de comparaison de 6.9 |
| D-0030 | règles `users` en multi-match : un utilisateur ordinaire ne crée ni ne modifie plus de compte ; `buma_pdv.deleteRule` autorise, comme PocketBase, la suppression des PDV sans client ni agent ; écart restant : `data.rule` au lieu de `data: {}` sur une création refusée | staging (6.9) ; 10.4 |
| D-0031 | colonnes auth cachées non filtrables (400 opaque) ; plus d'erreur 500 sur une valeur `@request` absente ; `totalPages` 0 et pas de clé `expand` vide ; écarts restants : `?=` et `?!=` contre une valeur absente, modificateurs `:lower` et `:changed` acceptés | C-36 ; 10.4 |
| D-0032 | dates vides servies `""`, filtrées et triées comme PocketBase ; écarts restants : colonne de vue NULL par LEFT JOIN (`<`, `<=`, `!~`, tri ASC), date illisible refusée en 400 ; filtres des clients non vérifiés | staging (6.9) ; 10.4 |
| D-0033 | l'expand applique la viewRule de la collection liée (49 chemins anonymes dans le schéma) | C-36 [à confirmer à l'intégration : WS-A6] |

### 10.4 Écarts PPBase restants, à corriger ou à décider avant G0

État à la rédaction (PPBase `237b403`, répétition synthétique
`docs/c11-synthetic-rehearsal-report.json`, clés
`comparison_with_pocketbase.divergence_classes.<classe>`, mesurée sur PPBase
`526397a`). Chacun reçoit une correction, ou une décision D-XXXX avec impact
client et test de régression (`AGENTS.md`).

| Écart | PocketBase 0.22.34 | PPBase | Impact client | Suite |
| --- | --- | --- | --- | --- |
| enveloppe des erreurs natives | `{code, message, data}` (`apis/api_error.go:11-16`) | `{status, message, data}` avec `code` ajouté (`ppbase/app.py:426-429`, `:534-578`) | client qui lit `code` ou `status` d'une erreur native | mode 0.22 de WS-A6 (C-44) |
| libellés de refus | « Only admins can perform this action. » (`apis/record_crud.go:57`, `:155`) | « Only superusers… » | client qui affiche ou compare le message | mode 0.22 de WS-A6 |
| échec de connexion | `data: {}` (C-33) | `data.identity` (`ppbase/api/record_auth.py:286-291`) | client qui lit `data` d'un échec de connexion | ouvert (classe `failed_login_identity_detail`) |
| `Content-Type` JSON | `application/json; charset=UTF-8` (mesuré) | `application/json` | client qui compare la chaîne exacte | ouvert (classe `json_content_type_charset`) |
| type, disposition et miniature des fichiers | type des `.attrs`, `inline` ou `attachment; filename=` (`tools/filesystem/filesystem.go:367-377`, `:393-430`) ; miniature 100x100 par défaut (`apis/file.go:26-27`, `:162-193`) | type par l'extension, `Content-Disposition` seulement avec `download` (`ppbase/api/files.py:141-145`, `:1117-1145`) ; original servi pour `thumb=100x100` | nom et mode d'ouverture dans le navigateur ; taille des vignettes | WS-A7 (C-37) |
| emails des comptes et manageRule | visibles par les admins, le propriétaire et la manageRule (`apis/record_helpers.go:216-270`) | superusers et propriétaire | session `isAdmin` sans les emails des autres comptes | WS-A6 |
| `GET /api/health` | `data.canBackup` (`apis/health.go:23-42`) | `data.version` en plus (`ppbase/api/health.py:14-26`) | sonde qui compare le corps | ouvert, non consigné |
| routes BUMA hors source | `GET /` sert `--publicDir` sur `/*` (`examples/base/main.go:118-122`) | `GET /` JSON de santé (`app/hooks/health.py`), `/__foundation/*` (`app/hooks/poc.py`, chargé par `app/hooks/register.py:34`) | sonde ou client qui appelle `/` | ouvert : retirer de l'enregistrement de production ou documenter |
| `POST /api/batch` | absent de 0.22.34 | présent et activé par défaut (`ppbase/api/records.py:2009`, `ppbase/api/settings.py:88-93`) | voie d'écriture groupée absente de la production | ouvert, non consigné |
| type des jetons de record | `authRecord` | `auth` | `isAuthRecord` du SDK JS 0.21 | D-0027 : à vérifier sur les clients (I8) |
| révocation globale | `PATCH /api/settings` révoque | 200 sans effet | procédure 0.22 inopérante | D-0027 : rotation SQL (6.12) |
| forme de `GET /api/settings`, pages de confirmation des liens email, `POST /api/files/token` sans auth, corps des refus des jetons à usage | 0.22 | forme actuelle ; pages absentes ; 401 au lieu de 400 ; autres corps | outils d'administration, liens ouverts dans le navigateur | contrat `docs/contracts/c11-auth-import.md` §4 et §5 |
| identité realtime | ID seul | `collectionId:id` | aucun ID partagé dans le backup réel | D-0027, écart délibéré |
| journal de requêtes | `logs.db` séparé | table `_requests`, sans rétention appliquée | données personnelles dans la base | 6.11, I18 |
| TLS vers la base | sans objet (SQLite) | désactivé (`ppbase/db/engine.py:50`) | aucun ; exploitation | I6 |

Autres constats sans décision : SMTP de PPBase lu dans son seul environnement
(I16, D-0027) ; règle `buma_sql_table` si la console a servi depuis l'export
(2.4).

## 11. Références

PocketBase, code officiel au tag `v0.22.34` (copie en lecture seule de la
session serveur, `/root/buma-c11-tools/pocketbase-src`) :

- `core/base_backup.go:30-84` : sauvegarde dans une transaction qui bloque
  les écritures, checkpoint WAL `TRUNCATE` aux erreurs ignorées, exclusions,
  espace disque, fichiers S3 ; `:49-51` : refus d'une sauvegarde
  concurrente ; `:267-330` : sauvegardes automatiques et rotation.
- `apis/backup.go:25-31` : routes de sauvegarde ; `:115-127` :
  téléchargement par jeton de fichier admin, contexte de 10 minutes.
- `forms/backup_create.go:11`, `:39-40` : nom d'une sauvegarde.
- `pocketbase.go:158-183` et `core/base.go:381-404` : arrêt propre ;
  `pocketbase.go:187-207` : `--dir`, `--encryptionEnv`, `--dev`.
- `examples/base/main.go:25-87` : options du binaire officiel, dont
  l'automigration (`:57-63`), `--publicDir` (`:65-71`, `:129-137`) et
  `--indexFallback` (`:73-79`) ; `:118-122` : fichiers statiques sur `/*`.
- `cmd/serve.go:20-75` : `serve`, domaines, `--http`, `--https`,
  `--origins` ; `apis/serve.go:66-69` et `:254-278` : migrations appliquées
  à chaque démarrage ; `:137-140` : cache `autocert`.
- `plugins/jsvm/jsvm.go:150-157` et `:475-504` : fichier de types réécrit
  au démarrage s'il diffère ; `:175-213` : migrations JS chargées depuis
  `--migrationsDir`.
- `plugins/migratecmd/migratecmd.go:84-100` et
  `plugins/migratecmd/automigrate.go:65-87` : automigration, ligne de
  `_migrations` et fichier écrits à chaque changement de collection.
- `core/base.go:39-41`, `:517-530` : `storage/` local sous `pb_data` ;
  `:495-509` : SMTP des paramètres ou `sendmail` ; `:538-552` : sauvegardes
  locales ou S3.
- `mails/admin.go:22-26` et `mails/record.go:175-184` : liens des emails
  construits sur l'URL de l'application.
- `apis/health.go:23-42` : corps de `/api/health`.
- `apis/record_crud.go:57`, `:155` : refus « Only admins » ; `:171-174` :
  404 d'un record introuvable.
- `apis/file.go:26-27`, `:162-193` : miniatures par défaut et créées à la
  demande ; `:213-215` et `apis/api_error.go:31-38` : 404 pour un fichier
  absent du stockage.
- `tools/filesystem/filesystem.go:168-215` : type détecté et enregistré à
  l'envoi ; `:367-377` et `:393-430` : en-têtes servis
  (`Content-Disposition`, `Content-Type`, `Content-Security-Policy`,
  `Cache-Control`).
- `apis/api_error.go:11-16` et `:67-89` : enveloppe d'erreur `code`,
  `message`, `data` ; `forms/record_password_login.go:83-85` et
  `apis/record_auth.go:316-317` : mauvais mot de passe ;
  `apis/record_auth.go:46` et `apis/middlewares.go:94-110` : refresh limité
  à la collection du record.
- `apis/record_helpers.go:153-189` : expand filtré par la viewRule de la
  cible ; `:216-270` : visibilité des emails des comptes.
- `tokens/tokens.go:5-6`, `tokens/record.go:13-27`, `tokens/admin.go:11-17`,
  `apis/middlewares.go:217-254`, `daos/admin.go:52-78` : types, signature et
  lecture des jetons ; `models/settings/settings.go:97-115` : `cronMaxKeep`
  et durées par défaut des jetons d'authentification (14 jours).
- Comparaison : `v0.23.0`, `core/record_tokens.go:13` (type `auth`) et
  `core/settings_model.go:127` (limites de débit, absentes de 0.22.34).

PPBase `237b403` (`fix/d0019-relation-maxselect` à la rédaction : correction
D-0019, WS-A5, WS-A2 ; avant WS-A6 et WS-A7, dont les lignes pourront
bouger) : `ppbase/__main__.py:667-674` ; `ppbase/config.py:29-30`, `:39`,
`:86-92`, `:95`, `:109`, `:118-123`, `:125-171`, `:179-183` ;
`ppbase/app.py:70-74`, `:189-214`, `:258-278`, `:280-284`, `:426-429`,
`:492-532`, `:534-578` ; `ppbase/api/deps.py:89-101` ;
`ppbase/api/files.py:84-92`, `:141-145`, `:1117-1145` ;
`ppbase/api/health.py:14-26` ; `ppbase/api/record_auth.py:286-291` ;
`ppbase/api/records.py:2009` ; `ppbase/api/settings.py:88-93`, `:94-102` ;
`ppbase/backup/automation.py:144-175` ; `ppbase/db/engine.py:45-50` ;
`ppbase/middleware/rate_limit.py:26-30` ;
`ppbase/middleware/request_logger.py:1-6`, `:31-56` ;
`ppbase/services/app_url.py:32-51` ; `ppbase/services/auth_service.py:19`,
`:487` ; `ppbase/services/file_storage.py:466-480` ;
`ppbase/services/hook_manager.py:315-326` ;
`ppbase/services/mail_service.py:49-72`.

Dépôt BUMA : `docs/server-session-plan.md`, `docs/c11-preparation.md`,
`docs/c11-master-diagnostic.md`, `docs/c11-master-initial-diagnostic.json`,
`docs/c11-idempotency-source-check.json`, `docs/c11-volume-benchmark-report.json`,
`docs/c11-synthetic-rehearsal-report.json`, `docs/c11-auth-runtime-report.json`,
`docs/c11-pb-0.22.34-reference-report.json`, `docs/c11-real-rehearsal-report.json`
(attendu), `docs/next-agent-handoff.md`, `docs/decisions-and-divergences.md`
(D-0016, D-0018 à D-0029), `docs/contracts/authentication.md`,
`docs/contracts/c11-auth-import.md`, `docs/contracts/legacy-sql.md`,
`docs/contracts/rowid.md`, `docs/contracts/routes.json`,
`docs/contracts/settings-referentials.md`, `docs/test-matrix.md`,
`docs/progress/ledger.md`, `scripts/c11_extract.py`, `scripts/c11_import.py`,
`scripts/c11_auth_import.py`, `scripts/c11_snapshot_audit.py`,
`scripts/c11_idempotency_source_check.py`, `scripts/c11_rehearsal.py`,
`scripts/c11_auth_runtime.py`, `scripts/c11_idempotency_runtime.py`,
`scripts/c11_relation_columns_runtime.py`, `scripts/c11_volume_benchmark.py`,
`scripts/c11_real_rehearsal.py` (branche `c11/ws-g`),
`scripts/c10_sdk_runtime.py`, `scripts/c10_remaining_runtime.py`,
`scripts/verify_complete_schema.py`, `scripts/verify_view_queries.py`,
`app/auth/login.py`, `app/finance/service.py`, `app/legacy/http.py`,
`app/hooks/register.py`, `app/hooks/health.py`, `app/hooks/poc.py`,
`pb_migrations/1789236842_buma_master_views.py`,
`pb_migrations/1789236846_c5_reference_defaults.py`,
`pb_migrations/1789236850_c11_relation_multiple_columns.py`.
Source gelée `buma_master_hooks@530018c` : `src/entries/index.pb.ts`,
`src/entries/master.article.ts`, `src/entries/master.pb.ts`,
`src/entries/master.rupture.ts`, `src/entries/master.setup.ts`,
`src/entries/master.user.ts`, `migrationsDir/`, `package.json`.

## Annexe A — porte de release et répétition réelle

Aux commits de release, sur une machine de préparation et non sur la
production, avec un rapport neuf pour chaque banc ; ces bancs créent leur
propre conteneur PostgreSQL jetable et ne visent jamais une base existante :

```sh
BUMA_SCHEMA_EXPORT=<export corrigé> PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/python -m ruff check app tests scripts \
  pb_migrations/1789236849_c11_source_transaction_idempotency.py \
  pb_migrations/1789236850_c11_relation_multiple_columns.py
.venv/bin/python -m ruff format --check app tests scripts \
  pb_migrations/1789236849_c11_source_transaction_idempotency.py \
  pb_migrations/1789236850_c11_relation_multiple_columns.py
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_idempotency_runtime \
  --report <nouveau rapport>.json
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_rehearsal \
  --pocketbase <binaire pocketbase 0.22.34> \
  --schema <export corrigé> \
  --report <nouveau rapport>.json
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_auth_runtime \
  --pocketbase <binaire pocketbase 0.22.34> \
  --schema <export corrigé> \
  --report <nouveau rapport>.json
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_relation_columns_runtime \
  --legacy-ppbase <checkout PPBase> --legacy-ppbase-commit c6821aa \
  --report <nouveau rapport>.json
```

Ruff couvre, outre `app tests scripts`, chaque migration ajoutée depuis
D-0022 ; les anciennes migrations générées restent hors de son périmètre.
`scripts.c11_rehearsal` accepte la version que déclare le binaire si le
constructeur de fixture et l'importeur la valident
(`scripts/c11_rehearsal.py:2565-2576`) : utiliser 0.22.34, version de la
production. Les cinq bancs rendent un rapport `passed`.

Répétition réelle, sur le paquet base de données (`data.db` sans `storage/`)
et son manifeste privé, dans un PostgreSQL jetable du serveur de session
seulement, sauf autorisation propre (2.1) ; lancée par chemin avec
l'interpréteur isolé, depuis la racine du checkout BUMA, jamais depuis le
dossier du backup :

```sh
.venv/bin/python -I -B scripts/c11_real_rehearsal.py \
  --ppbase <checkout PPBase de release> \
  --data <paquet base de données> \
  --manifest <manifeste privé> \
  --schema <export corrigé> \
  --source-version 0.22.34 \
  --source-migrations <source gelée>/migrationsDir \
  --report <nouveau rapport public>.json \
  --private-dir <nouveau dossier privé>
```

Deux passages par défaut (`--passes`), conteneur limité par défaut à 2 Gio et
2 CPU (`--memory`, `--cpus`) ; `--accept-divergence CODE=D-NNNN` seulement
pour un écart décidé. Code de sortie 0 si le rapport est `passed` et que son
contrôle de confidentialité passe, 1 sinon ; le rapport public n'est écrit
qu'après ce contrôle. Options relevées sur `c11/ws-g` (`d792a50`), à
revérifier par `--help` au commit de release. Le rapport final publié est
`docs/c11-real-rehearsal-report.json` (2.2).

Côté PPBase, `pytest -p no:cacheprovider -q tests` au commit de release :
aucun échec nouveau, et chacun des échecs antérieurs analysé et consigné
avant G0, attente du test comparée au comportement de PocketBase 0.22.34,
source citée, ou corrigé. Au commit `c6821aa`, la phase 0 de la session
serveur en a relevé dix (`docs/progress/ledger.md`). À `237b403`,
l'orchestrateur en a relevé cinq, tous parmi ces dix, à consigner au ledger :
collision de routes
(`test_flask_like_pb_routing::test_route_collision_with_core_routes_is_blocking`),
routeur des hooks (`test_hooks_api::test_hooks_api_router_is_registered`) et
options ou durées de jeton attendues (deux tests de
`test_per_collection_auth_options`,
`test_system_collections_bootstrap::TestDefaultUsersCollection::test_users_has_auth_options`) ;
WS-A2 a corrigé les 403 rendus au lieu de 401 (contrat
`docs/contracts/c11-auth-import.md` §5). Un échec non analysé touchant
l'authentification, l'isolement des jetons ou le routage est un NO-GO.

## Annexe B — empreintes des binaires officiels PocketBase 0.22.34

Release officielle `v0.22.34`, fichier `checksums.txt` :

| Archive | SHA-256 de l'archive |
| --- | --- |
| `pocketbase_0.22.34_linux_amd64.zip` | `50450ee1251b5b6fd6a747dfde4b0d2a86102ac3e65b91de31950375a13036da` |
| `pocketbase_0.22.34_linux_arm64.zip` | `5a0464d51cec9dba3a2e12034f0274c113bb1a78cdaf8b8db5bc998971b4ff20` |

Binaire `pocketbase` contenu dans l'archive `linux_amd64`, vérifié pendant la
phase 0 et recontrôlé par WS-F : SHA-256
`f164a6333e674fec53fbaf49fffba884654de3a6e712f220d0bbe1c54f2ee04d`. Pour une
autre architecture, prendre le binaire de l'archive officielle correspondante
et comparer de la même façon.
