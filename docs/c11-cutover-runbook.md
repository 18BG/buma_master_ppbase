# Runbook C11 — bascule et retour arrière (brouillon)

**Brouillon WS-F du 2026-10-24, non exécuté.** Ce document décrit la bascule
des clients BUMA Master de PocketBase 0.22.34 (SQLite) vers PPBase et
PostgreSQL, puis le retour arrière. Il n'autorise rien : l'import de
production et la bascule exigent une autorisation distincte de l'utilisateur,
un backup frais et une fenêtre de maintenance (`docs/server-session-plan.md`,
sections 1 et 8). `AGENTS.md` prime sur ce runbook. Aucune de ses étapes n'a
été exécutée, ni sur la production ni ailleurs.

## Conventions

- **[durée : mesurée par WS-G]** : durée à reporter depuis la répétition
  réelle WS-G. Ce runbook n'estime aucune durée.
- **[durée : non mesurable par WS-G]** : étape propre à la production (gel,
  capture, réseau, proxy, redémarrage de PocketBase), à chronométrer lors
  d'un essai autorisé par l'utilisateur ou le jour J.
- **[commande à confirmer à l'intégration]** : outil encore en cours
  d'écriture (WS-B, WS-B2, WS-C, WS-D, WS-E, WS-G). Seule l'intention est
  décrite ; la syntaxe sera fixée à l'intégration.
- **[outil non attribué]** : contrôle nécessaire pour lequel aucun outil
  n'existe et aucun chantier n'est responsable ; à décider (section 10).
- `<…>` : valeur fournie le jour J. `I1` à `I21` renvoient aux informations
  de la section 3, `G0` à `G6` aux portes de la section 7, `C-01` à `C-43`
  aux contrôles décrits en section 6.
- `<privé>` : dossier privé de la bascule (dossiers 0700, fichiers 0600), hors
  de tout dépôt Git ou dans `backups/` (ignoré par Git). Les rapports publics
  ne contiennent que des agrégats et des empreintes de fichiers : jamais d'ID,
  de nom, d'email, de mot de passe ou de hash de mot de passe, de jeton, de
  secret, de montant, ni d'URL contenant des identifiants.
- Les commandes du dépôt se lancent depuis la racine du checkout BUMA de
  release, avec son venv (`.venv/bin/python`), jamais depuis le dossier de la
  capture ; les chemins de données sont passés en arguments.

## 1. Principes

1. **Un seul backend accepte des écritures à tout instant.** PocketBase est
   gelé avant la capture et reste arrêté ; PPBase n'est ouvert qu'après la
   porte G4. Pas de bascule partielle des clients, pas de double écriture.
2. **PocketBase reste intact** : `pb_data`, binaire, hooks, migrations et
   ligne de commande ne sont ni modifiés, ni restaurés, ni nettoyés, ni mis à
   jour (`pocketbase update`). Toute analyse porte sur une copie.
3. **Copie à l'identique** : aucun doublon supprimé, aucune liste aplatie,
   aucune cible, aucun ID, rowid ou fichier inventé, aucune référence
   supprimée ou redirigée (D-0019, D-0021, D-0022, D-0023). Choix par défaut
   de l'utilisateur : références orphelines et références de fichiers absents
   importées telles quelles ; secrets de jetons repris pour que les sessions
   continuent.
4. **Tout écart arrête la procédure** à la porte courante. On ne corrige ni
   code ni donnée pendant la fenêtre : on applique le retour arrière
   (section 8) ou on reporte.
5. **Chaque porte est relue par un vérificateur** qui n'a pas produit ses
   preuves, puis décidée par le décideur ; tout est consigné (section 9).
6. Les agents ne touchent pas la production : ce runbook est exécuté par
   l'équipe nommée par l'utilisateur (section 4).

Hors périmètre : stockage S3 (désactivé dans les paramètres du backup de
septembre), OAuth2 (`_externalAuths` vide), import de `logs.db` (conservé
dans la capture, jamais importé) et compatibilité SQLite de `POST /sql`
(reportée par D-0018 ; la console desktop envoie du SQL PostgreSQL).

## 2. Autorisations et prérequis (porte G0)

### 2.1 Autorisations distinctes de l'utilisateur

Écrites, datées et recopiées dans le journal :

- l'import de production et la bascule, nommément, pour une fenêtre donnée ;
- la fenêtre de maintenance (début, fin au plus tard, fuseau) et son annonce ;
- les méthodes de gel (6.2), de capture (6.3) et de bascule d'URL (6.10) ;
- le plan de retour arrière : durée de la période de retour arrière et sort
  des écritures reçues par PPBase en cas de retour (8.3) ;
- les seuils d'arrêt de la section 7 ;
- les personnes de chaque rôle (section 4).

### 2.2 Prérequis techniques acquis avant le jour J

- C11 intégré : WS-A à WS-G fusionnés après relecture indépendante,
  décisions D-0024 et suivantes rédigées, migration D-0019 `1789236850`
  (WS-E) intégrée [commande à confirmer à l'intégration].
- Répétition réelle WS-G passante (rapport `docs/c11-real-rehearsal-*.json`)
  et durées reportées en section 5 [commande à confirmer à l'intégration].
- Commits de release figés pour BUMA et PPBase ; sur ces commits, la porte
  de l'annexe A passe intégralement.
- Production identifiée (I1, I2) : PocketBase **0.22.34**. Le ZIP de
  septembre ne contenait aucune indication de version : la preuve vient du
  binaire en service (`pocketbase --version` et SHA-256 comparés au binaire
  officiel de la release, annexe B). Hooks déployés construits depuis la
  source gelée `530018c`. `_migrations` limité aux 325 migrations source
  appliquées en septembre : toutes sauf `1785599959`
  (`docs/c11-idempotency-source-check.json`). Une autre version, un build
  personnalisé ou une autre révision arrête la préparation : la parité n'est
  établie que pour cette référence.
- Tests desktop, mobile et administration passants sur staging (6.9) : base
  jetable chargée depuis un backup réel, comptes de test créés dans cette
  seule base.
- PostgreSQL de production en version 16, celle des répétitions ; une autre
  version majeure impose une répétition supplémentaire.
- Horloges synchronisées (NTP) sur l'hôte PPBase et sur le serveur
  PostgreSQL (contrôle C-28).
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

- Hôte PocketBase, capture A : PocketBase recommande un espace libre d'au
  moins deux fois la taille de `pb_data` (`core/base_backup.go:38-39`,
  v0.22.34).
- Machine de bascule : capture, extraction, copie de staging de l'importeur,
  `storage/` de PPBase et PostgreSQL (données, index, WAL) [taille : mesurée
  par WS-G].

## 3. Informations à fournir par l'utilisateur

Infrastructure de production :

- **I1** — Hôte PocketBase : système, architecture, gestionnaire de service
  et politique de redémarrage automatique, chemin de `pb_data`.
- **I2** — Binaire officiel ou build personnalisé, et ligne de commande
  exacte de PocketBase : `--dir`, `--encryptionEnv`, `--hooksDir`,
  `--hooksWatch`, `--migrationsDir`, `--automigrate`, `--publicDir`,
  `--queryTimeout`, `serve` avec ou sans domaines, `--http`, `--https`,
  `--origins` (v0.22.34 : `pocketbase.go:187-207`,
  `examples/base/main.go:25-87`, `cmd/serve.go:20-75`) ; preuve de la
  version et de la révision des hooks (2.2).
- **I3** — Paramètres chiffrés (`--encryptionEnv`) ? Si oui, WS-C a besoin de
  la clé, transmise hors dépôt. Les paramètres du backup de septembre étaient
  lisibles en JSON : l'audit y a lu l'état S3.
- **I4** — TLS terminé par PocketBase (`serve <domaine>`, certificats dans
  `pb_data/.autocert_cache`, `apis/serve.go:137-140`) ou par un proxy ? Le ZIP
  de septembre ne contenait aucun membre `.autocert_cache/` : c'est un indice,
  pas une preuve.
- **I5** — Hôte PPBase et machine de bascule : système, Python 3.11 ou plus,
  CPU, mémoire, disque, réseau vers l'hôte PocketBase, utilisateur système et
  gestionnaire de service de PPBase.
- **I6** — PostgreSQL de production : hébergement, version, base neuve
  dédiée, rôle propriétaire, accès réseau restreint.
- **I7** — Proxy, DNS et certificats : logiciel et configuration actuelle
  (taille maximale des corps, alors que `buma_ressources.file` accepte des
  fichiers jusqu'à 120 242 880 octets ; délais ; en-têtes ; SSE), TTL DNS,
  moyen de changer l'amont.

Clients :

- **I8** — URL de base utilisées par chaque client : desktop (dont la console
  SQL), application(s) mobile(s), tableau de bord d'administration, scripts ;
  configurables ou codées en dur ; versions déployées et SDK utilisés.
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
  `backups.cronMaxKeep`) et tâches externes qui écrivent dans PocketBase.

Accès et comptes :

- **I14** — Accès nécessaires (SSH, administration, PostgreSQL) et gestion
  des secrets (coffre ou fichier d'environnement 0600) ; aucun secret par
  ticket ou messagerie.
- **I15** — Comptes de contrôle : un compte `users`, un compte `agent_users`
  et l'administrateur PocketBase ; mots de passe détenus par leurs
  titulaires ; accord pour conserver un jeton émis avant le gel.
- **I16** — SMTP actif dans PocketBase ? Une fois les paramètres repris,
  PPBase pourra envoyer des emails réels.

Sauvegardes, rétention et décisions :

- **I17** — Emplacement privé de la capture ; durée de conservation du
  `pb_data` de PocketBase et de la capture après la bascule (jamais supprimés
  sans décision).
- **I18** — Sauvegarde PostgreSQL et du dossier de données PPBase après la
  bascule : outil, fréquence, essai de restauration.
- **I19** — Personnes et suppléants : décideur, opérateur, vérificateur,
  infrastructure, référents métier (desktop, mobile, caisse), communication ;
  canal d'astreinte.
- **I20** — Fichiers absents (2 303 en septembre) : recherche préalable sur
  un ancien serveur ou un autre backup, ou absence documentée (D-0021). Par
  défaut, références conservées telles quelles.
- **I21** — Durée de la surveillance renforcée et de la période de retour
  arrière ; validation des seuils de la section 7.

## 4. Rôles et responsabilités

| Rôle | Responsabilités | Personne |
| --- | --- | --- |
| Décideur (utilisateur) | autorisations ; go/no-go à chaque porte ; déclenchement et fin du retour arrière ; sort des écritures reçues par PPBase | I19 |
| Opérateur de bascule | exécute les étapes, tient le journal, n'improvise aucune correction | I19 |
| Vérificateur | relit les preuves de chaque porte, sans les avoir produites | I19 |
| Infrastructure | proxy, DNS, TLS, services, accès, sauvegardes | I19 |
| Référents métier | contrôles desktop, mobile et caisse ; premières opérations réelles | I19 |
| Communication | annonce, information pendant la fenêtre, ouverture ou retour | I19 |
| Agents Claude | préparation, outillage et relecture hors production ; aucune action sur la production | — |

## 5. Chronologie et durées

| N° | Étape | Section | Durée |
| --- | --- | --- | --- |
| 1 | Gel des écritures et vérification | 6.2 | [durée : non mesurable par WS-G] |
| 2 | Capture du backup fermé, arrêt de PocketBase, référence d'intégrité | 6.3 | [durée : non mesurable par WS-G] |
| 3 | Transfert de la capture et empreinte | 6.4 | [durée : non mesurable par WS-G] |
| 4 | Inventaire de l'archive | 6.4 | [durée : mesurée par WS-G], ZIP complet requis |
| 5 | Extraction vérifiée | 6.4 | [durée : mesurée par WS-G], ZIP complet requis |
| 6 | Diagnostic, audit, contrôle d'idempotence | 6.5 | [durée : mesurée par WS-G] |
| 7 | Base neuve, migrations, vérification du schéma | 6.6 | [durée : mesurée par WS-G] |
| 8 | Import des données | 6.6 | [durée : mesurée par WS-G] |
| 9 | Comptes, paramètres et sessions | 6.6 | [durée : mesurée par WS-G] |
| 10 | Contrôles de conservation | 6.7 | [durée : mesurée par WS-G] |
| 11 | Copie et contrôle de `storage/` | 6.8 | [durée : mesurée par WS-G], ZIP complet requis |
| 12 | Démarrage de PPBase et contrôles avant ouverture | 6.9 | [durée : mesurée par WS-G] |
| 13 | Bascule d'URL et contrôles d'ouverture | 6.10 | [durée : non mesurable par WS-G] |
| 14 | Retour arrière avant ouverture | 8.2 | [durée : non mesurable par WS-G] |

En mode paquet (sans `storage/`), WS-G ne mesure ni le hachage des fichiers
de l'étape 6 ni les étapes 4, 5 et 11 : elles demandent la répétition avec le
ZIP complet, devant l'utilisateur. L'étape 11 peut se dérouler en parallèle
des étapes 6 à 10 si les disques de la machine de bascule le permettent ; à
décider après mesure. La fenêtre de maintenance couvre au moins les étapes 1
à 13, plus une marge fixée par le décideur, et doit laisser le temps de
l'étape 14 avant sa fin.

## 6. Procédure

### 6.1 Préparation, avant la fenêtre

1. Installer sur la machine de bascule les checkouts BUMA et PPBase aux
   commits de release, avec un seul venv comme sur le serveur de session
   (`docs/server-session-plan.md`, section 3) ; consigner les deux commits et
   `pip freeze`. Y copier aussi l'export corrigé, dont le SHA-256 est contrôlé
   par l'outillage (`scripts/c11_import.py:22`, `:104-107`), et un checkout en
   lecture seule de la source gelée `530018c` (contrôle C-08).
2. Préparer un fichier d'environnement de production 0600, hors dépôt :
   `PPBASE_DATABASE_URL`, `PPBASE_DATA_DIR`, `PPBASE_HOST`, `PPBASE_PORT`,
   `PPBASE_MIGRATIONS_DIR`, `PPBASE_AUTO_MIGRATE=false`,
   `PPBASE_STORAGE_BACKEND=local` et `BUMA_MASTER_ENABLE_INTERNAL_JOBS=false`
   (la source gelée n'a aucun cron actif : le seul `cronAdd`, dans
   `master.rupture.ts:222`, est commenté). Reprendre l'équivalent PPBase de
   `--origins` (`origins`, `ppbase/config.py:94`) si PocketBase n'utilise pas
   la valeur par défaut `*` (`cmd/serve.go:55-60`).
3. Le dossier de lancement ne contient ni `.env` ni `pb_hooks/` : PPBase lit
   `./.env` (`ppbase/config.py:118-119`) et charge les fichiers `*.py` de
   `./pb_hooks` (`ppbase/config.py:108`,
   `ppbase/services/hook_manager.py:315-326`). Ne pas utiliser les cibles
   `make`, qui visent par défaut la base locale du port `55434`. Ne pas
   passer l'URL PostgreSQL par `--db` : le mot de passe serait visible dans la
   liste des processus ; les outils cités lisent `PPBASE_DATABASE_URL`.
4. Créer la base PostgreSQL de production neuve et son rôle (I6) : aucune
   donnée, aucun serveur PPBase dessus.
5. Préparer sans l'activer la configuration du proxy vers PPBase et la page
   de maintenance ; reprendre à l'identique les limites, délais, en-têtes et
   réglages SSE de la configuration PocketBase (I7).
6. Juste avant le gel, chaque titulaire d'un compte de contrôle (I15) obtient
   un jeton PocketBase par une connexion normale ; l'opérateur le conserve
   dans `<privé>` (0600) pour le contrôle C-34, jamais dans le journal.
7. Communication (I12) : annonce ; demander aux utilisateurs mobiles de
   synchroniser leurs opérations en attente et de ne rien saisir pendant la
   fenêtre.

### 6.2 Gel des écritures côté PocketBase (méthode à confirmer, I11)

| Méthode | Principe | Limites |
| --- | --- | --- |
| G-A, maintenance au proxy (proposée) | le proxy répond « maintenance » à tous les clients ; seul l'opérateur atteint PocketBase, en local ou depuis une adresse autorisée | suppose qu'aucun client n'atteint PocketBase sans passer par le proxy ; bloque aussi les connexions |
| G-B, arrêt du service | arrêt propre de PocketBase avant la capture | impose la capture B (6.3) |
| Écartée : bloquer seulement POST, PUT, PATCH et DELETE | les lectures restent servies | une lecture peut écrire : miniature créée à la demande dans `storage/` (`apis/file.go:162-193`) |
| Écartée : règles d'API réservées aux admins | les collections deviennent inaccessibles | modifie `_collections` de PocketBase (principe 2) et ne couvre pas les routes personnalisées des hooks |

1. Activer le gel ; noter `T_gel` (UTC).
2. Suspendre les tâches externes qui écrivent (I13) et le redémarrage
   automatique du service PocketBase, sans toucher à `pb_data`. Ne pas
   modifier `pb_hooks` : `--hooksWatch`, vrai par défaut, redémarre PocketBase
   à chaque changement (`examples/base/main.go:33-39`).
3. Vérifier qu'aucune sauvegarde automatique ne tourne ni n'est planifiée
   pendant la fenêtre (`backups.cron`, `core/base_backup.go:267-283`) : une
   seconde sauvegarde simultanée est refusée (`core/base_backup.go:49-51`).
4. **C-01** : dans les journaux du proxy, 0 requête transmise à PocketBase
   après `T_gel`, hors actions de l'opérateur.

### 6.3 Capture d'un backup fermé (méthode à confirmer)

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
3. Copier le ZIP depuis `pb_data/backups/` vers `<privé>` par le système de
   fichiers, en lecture. Le téléchargement HTTP exige un jeton de fichier
   admin et son contexte est limité à 10 minutes (`apis/backup.go:115-127`).
4. Arrêter PocketBase proprement (SIGTERM : `pocketbase.go:158-183` ;
   fermeture des bases : `core/base.go:381-404`) et vérifier la fin du
   processus.

Méthode B — copie à froid, sous gel G-A ou G-B :

1. Arrêter PocketBase proprement et vérifier la fin du processus.
2. `data.db-wal` et `data.db-journal` doivent être absents ou vides. Sinon,
   ne rien supprimer ni tronquer : redémarrer PocketBase sous gel, l'arrêter
   proprement et recontrôler, ou passer à la méthode A.
3. Copier `data.db`, `storage/` et, pour archive seulement, `logs.db` vers
   `<privé>` ; manifeste SHA-256 et tailles de chaque fichier, calculé sur la
   source arrêtée et sur la copie, puis comparé : 0 écart [outil non
   attribué ; le format du manifeste privé de septembre, `{archive_sha256,
   files: [{path, bytes, sha256, crc32}]}`, peut servir de modèle].

Dans les deux cas :

- **C-02** : `pocketbase --version` donne `pocketbase version 0.22.34` et le
  SHA-256 du binaire égale celui du binaire officiel de la même architecture
  (annexe B).
- **C-03** : référence d'intégrité `H_stop` de PocketBase arrêté : SHA-256 de
  `data.db` et manifeste de `storage/` du `pb_data` de production, calculés
  après l'arrêt sans rien écrire dans `pb_data`. Méthode A : le SHA-256 de ce
  `data.db` doit égaler celui du `data.db` du ZIP ; un écart signale une
  écriture après la capture et impose une nouvelle capture. `H_stop` est
  revérifiée avant tout redémarrage de retour arrière (8.2).
- La méthode A ajoute le ZIP dans `pb_data/backups/`, dossier géré par
  PocketBase et exclu de ses propres sauvegardes ; `H_stop` ne porte que sur
  `data.db` et `storage/`.
- PocketBase reste arrêté, redémarrage automatique suspendu ; il ne
  redémarre qu'en cas de retour arrière.

### 6.4 Transfert, inventaire et extraction vérifiée

1. Copier la capture sur la machine de bascule dans `<privé>`. **C-04** :
   SHA-256 identique avant et après le transfert.
2. Inventaire sans extraction (commande existante ; elle ne vérifie ni les CRC
   ni le contenu SQLite) :

   ```sh
   .venv/bin/python -m scripts.c11_import \
     --archive <privé>/<capture>.zip \
     --report <privé>/c11-cutover-archive-inventory.json
   ```

   **C-05** : `blockers` vide, un seul `data.db`, WAL et journal vides
   (refusés sinon, `scripts/c11_import.py:76-77`) ; nombre de membres et
   taille déclarée consignés.
3. Extraction vérifiée dans un dossier neuf de `<privé>` : CRC et SHA-256 de
   chaque membre, chemins dangereux refusés, dossiers 0700 et fichiers 0600,
   manifeste privé au format de septembre, par `scripts/c11_extract.py`
   (WS-B2) [commande à confirmer à l'intégration]. **C-06** : 100 % des
   membres vérifiés, 0 écart. Avec la méthode B, la copie à froid et son
   manifeste tiennent lieu d'extraction et de C-06.

### 6.5 Diagnostic de la capture (porte G2)

Commandes existantes, sur l'extraction. Leur comportement attendu à la
bascule dépend de l'intégration : porte de version 0.22.34 sourcée (WS-D) et
références orphelines ou fichiers absents signalés au lieu de bloquer (WS-B)
[commande à confirmer à l'intégration]. Aujourd'hui, le diagnostic ne valide
que 0.22.27 (`scripts/c11_import.py:273-276`).

```sh
.venv/bin/python -m scripts.c11_import \
  --data <extraction> \
  --schema <export corrigé> \
  --source-version 0.22.34 \
  --report <privé>/c11-cutover-diagnostic.json

.venv/bin/python -m scripts.c11_snapshot_audit \
  --data <extraction> \
  --source-version 0.22.34 \
  --report <privé>/c11-cutover-audit.json \
  --private-report <privé>/c11-cutover-audit-private.json

.venv/bin/python -m scripts.c11_idempotency_source_check \
  --data <extraction> \
  --source-migrations <source gelée>/migrationsDir \
  --report <privé>/c11-cutover-idempotency-source-check.json
```

`<source gelée>` est le checkout en lecture seule de `530018c` (6.1).

- **C-07** : `PRAGMA integrity_check` donne `ok` ; 0 dérive sur les 114
  définitions de l'export corrigé ; version déclarée 0.22.34, prouvée par
  C-02.
- **C-08** : `_migrations` identique à septembre : 325 des 326 migrations
  source, seule `1785599959` absente. Toute autre différence signale un
  changement de la production depuis la source gelée : arrêt.
- **C-09** : 0 clé `idem:` dupliquée (index source, D-0022) ; doublons mobile
  mesurés et conservés (877 groupes, 2 391 lignes en septembre).
- **C-10** : aucun `created` ni `updated` postérieur à `T_gel` ; activité la
  plus récente cohérente avec la dernière activité connue (référent métier).
- **C-11** : comptages par table comparés à septembre ; toute diminution est
  expliquée et acceptée par le décideur avant de continuer.
- **C-12** : références orphelines par champ, fichiers référencés absents et
  objets non référencés mesurés (septembre : 195 795 références dans 5
  champs, 2 303 fichiers, 686 objets). Ces valeurs deviennent les attendus de
  la porte G3.
- **C-13** : comptes et paramètres : nombres de `users` et `agent_users`,
  hash bcrypt non vides et `tokenKey` présentes (septembre : 20 `users`,
  3 `agent_users`, 23 sur 23 avec hash et `tokenKey`) ; `_admins` compté (1
  en septembre) ; `_externalAuths` vide ; `_params` limité à la clé
  `settings` ; S3 désactivé. Toute autre situation n'est pas couverte par
  l'outillage : arrêt.

### 6.6 Import dans le PostgreSQL de production fraîchement migré (porte G3)

1. **C-14** : avec le fichier d'environnement chargé, la requête
   `SELECT current_database()` donne la base attendue, le schéma `public` ne
   contient aucune table et aucun serveur PPBase ne sert cette base.
2. Migrations (commandes existantes ; pour `migrate`, `--dir` désigne le
   dossier des migrations, alors que pour `serve` il désigne le dossier de
   données) :

   ```sh
   .venv/bin/python -m ppbase migrate up --dir pb_migrations
   .venv/bin/python -m ppbase migrate status --dir pb_migrations
   ```

   **C-15** : toutes les migrations de la release appliquées (dix
   aujourd'hui, de `1789236840` à `1789236849`, puis `1789236850` de WS-E),
   `Pending: 0`, `Orphaned: 0`.
3. Schéma (commandes existantes, en lecture ; les vues de contrôle sont
   temporaires, `scripts/verify_view_queries.py:57-76`) :

   ```sh
   .venv/bin/python scripts/verify_complete_schema.py --schema <export corrigé>
   .venv/bin/python scripts/verify_view_queries.py --schema <export corrigé>
   ```

   **C-16** : `"ok": true` (114 définitions ; métadonnées d'index égales à
   l'export plus l'index source de D-0022) et 42 vues sur 42 en `OK`.
4. Defaults C5 : la migration `1789236846` insère 24 lignes
   (`STOCK_PRINCIPAL`, trois moyens de paiement, `CUSTOMER_DIVERS`,
   `SUPPLIER_DIVERS` et 18 rôles applicatifs). Le backup contient leurs
   versions source, créées par le bootstrap de PocketBase
   (`master.setup.ts`), et l'importeur refuse une cible non vide. La
   répétition ne les retire que dans la base qu'elle vient de créer
   (`scripts/c11_rehearsal.py:240-245`). Même règle en production :
   uniquement dans cette base neuve, avant l'import, et journalisé [commande
   à confirmer à l'intégration].
5. Import des données (WS-B) : lecture de la copie vérifiée, verrou des 72
   tables, refus d'une cible non vide, INSERT sans hooks, relecture typée,
   empreinte par table, rowids conservés, tableaux D-0019 conservés,
   références orphelines et références de fichiers absents conservées et
   comptées, transaction unique, staging supprimé en cas d'échec [commande à
   confirmer à l'intégration]. Aujourd'hui, aucune commande d'import de
   production n'existe : `load_isolated` n'accepte que la base
   `buma_c11_rehearsal` (`scripts/c11_import.py:485-487`).
6. Comptes, paramètres et sessions (WS-C, `scripts/c11_auth_import.py`) :
   `_admins` vers `_superusers` (hash bcrypt et `tokenKey` conservés),
   `_params.settings` (nom, URL, SMTP, durées et secrets des jetons), secret
   de jeton dans chaque collection auth, aucun secret journalisé [commande à
   confirmer à l'intégration]. À faire **avant** tout démarrage de PPBase sur
   cette base : sans superuser, PPBase crée un jeton d'installation dans
   `_params` et imprime une URL d'installation dans sa console
   (`ppbase/app.py:174-199`).
7. Échec à l'une de ces étapes : NO-GO, retour arrière avant ouverture (8.2).
   La base reste en l'état pour analyse ; elle n'est ni vidée ni réutilisée
   sans décision.

### 6.7 Contrôles de conservation (porte G3)

Outils : rapport de l'importeur et vérifications de WS-B, WS-C et WS-E
[commande à confirmer à l'intégration], sauf mention contraire.

| Contrôle | Attendu |
| --- | --- |
| C-17 Comptages | 72 tables sur 72 égales au diagnostic de la capture ; total égal |
| C-18 Empreintes | 72 empreintes de table sur 72 égales à la source |
| C-19 Rowids | rowid source conservé pour 100 % des records ; `users` et `buma_article` : rowid natif égal au rowid source (D-0016, `docs/contracts/rowid.md`) |
| C-20 Relations multiples | `buma_customer.pdv`, `buma_pilotage_ticket.tag`, `buma_wallet.managers` : mêmes listes, mêmes éléments dans le même ordre ; aucune liste devenue scalaire ; l'élément de 31 caractères reste un seul élément (D-0019) |
| C-21 Références orphelines | nombre par champ égal à C-12 ; aucune cible créée, aucune valeur modifiée |
| C-22 Fichiers absents | liste privée égale à C-12 ; aucun fichier créé |
| C-23 Doublons mobile | groupes et lignes égaux à C-09 ; aucune ligne supprimée |
| C-24 Comptes utilisateurs | `users` et `agent_users` : nombres égaux ; hash bcrypt et `tokenKey` identiques pour 100 % des records, comparés sans affichage |
| C-25 Superusers | nombre égal à `_admins` de la capture ; hash et `tokenKey` repris ; aucun jeton d'installation dans `_params` |
| C-26 Paramètres | nom, URL, SMTP et durées des jetons égaux à la source ; secrets présents, jamais affichés ; S3 désactivé ; limitation de débit PPBase désactivée (`ppbase/middleware/rate_limit.py:26-30`), PocketBase 0.22.34 n'en ayant pas |
| C-27 Schéma après import | C-15 et C-16 rejoués : 114 définitions, 42 vues, `Pending: 0` |
| C-28 Horloge | `now()` de PostgreSQL et horloge de l'hôte PPBase postérieurs au plus grand `created` ou `updated` importé ; NTP synchronisé [outil non attribué] |
| C-29 Source inchangée | SHA-256 du `data.db` de la capture égal avant et après l'import |

C-28 : les numéros de transaction suivent la ligne de plus grand `created`
(`app/finance/service.py:44-57`) et le lookup de D-0023 trie par `created` ;
une horloge en retard sur les données importées pourrait produire des
numéros en double.

### 6.8 Copie et contrôle de `storage/`

1. Copier tous les objets de `storage/` de l'extraction dans
   `<PPBASE_DATA_DIR>/storage/` (septembre : 1 282 objets, dont 601 `.attrs`,
   80 miniatures et 5 autres objets non référencés, tous conservés ;
   PocketBase lit dans les `.attrs` les attributs des fichiers, dont leur
   type, par exemple `apis/file.go:166-172`). Le dossier est un vrai dossier,
   propriété de l'utilisateur du service PPBase, sans accès pour les autres.
   Jamais de lien symbolique ni de montage partagé avec `pb_data/storage` de
   PocketBase : PPBase refuse une racine liée
   (`ppbase/services/file_storage.py:466-479`) et un partage lui permettrait
   de modifier PocketBase (principe 2).
2. **C-30** : manifeste SHA-256 et tailles de la copie égal à celui de la
   capture : 100 % des objets, 0 manquant, 0 en trop, 0 différence [commande
   à confirmer à l'intégration, mode complet de WS-B].
3. Les fichiers référencés absents (2 303 en septembre) restent absents ;
   aucun fichier de remplacement.
4. Option à décider : pré-copie de `storage/` avant le gel pour raccourcir la
   fenêtre, acceptable seulement si l'état final égale exactement le
   manifeste de la capture.
5. Le dossier de données PPBase reste privé et entre dans la sauvegarde
   (I18).

### 6.9 Contrôles avant ouverture (porte G4)

Démarrer PPBase sur la base de production, joignable par l'opérateur seul
(boucle locale ou adresse autorisée), les clients restant en maintenance,
avec le même environnement que la répétition
(`scripts/c11_rehearsal.py:323-353`) et le fichier d'environnement de 6.1 :

```sh
.venv/bin/python -m ppbase serve \
  --dir <PPBASE_DATA_DIR> \
  --migrations-dir pb_migrations \
  --hooks app.hooks.register:register \
  --host 127.0.0.1 --port <port> \
  --no-apply-migrations-on-start
```

`PPBASE_AUTO_MIGRATE=false` désactive aussi la génération de fichiers de
migration (`ppbase/config.py:178-182`). La réactiver par
`--generate-migrations` est à décider (I2) : active, une modification de
collection dans le tableau de bord écrit un fichier dans le dossier des
migrations.

Contrôles en lecture seule, sans écriture métier sur la production ; records
choisis en privé, jamais cités :

- **C-31** : `GET /api/health` répond 200.
- **C-32** : sondes legacy (contrat C3, `docs/contracts/authentication.md`) :
  `GET /isConnected` sans jeton donne 400 `{"message":"false"}`, avec le
  jeton d'un compte de contrôle 200 `{"message":"true"}` ;
  `GET /master/user/health` donne 200.
- **C-33** : connexion `users` par username et `agent_users` par email et par
  username : 200 `{token, record}`, sans `rowid`, `passwordHash` ni
  `tokenKey` dans `record` ; refresh 200 ; mauvais mot de passe 400 ;
  lecture de son propre record `agent_users` 200.
- **C-34** : continuité des sessions : le jeton PocketBase de 6.1 de chaque
  compte de contrôle, administrateur compris, est accepté par `auth-refresh`
  (200, même record) [commande à confirmer à l'intégration, WS-C].
  PocketBase 0.22.34 signe les jetons de record avec `tokenKey` +
  `recordAuthToken.secret`, type `authRecord` (`tokens/record.go:13-27`), et
  ceux d'administrateur avec `tokenKey` + `adminAuthToken.secret`, type
  `admin` (`tokens/admin.go:11-17`).
- **C-35** : l'administrateur se connecte (`POST
  /api/admins/auth-with-password`) et ouvre le tableau de bord `/_/` ;
  72 collections et 42 vues listées ; aucune modification.
- **C-36** : lectures natives avec un compte de contrôle : liste filtrée de
  `buma_transaction` avec `expand=article,author` (enveloppe `page`,
  `perPage`, `totalItems`, `totalPages`, `items`) ; une vente dont le client
  est une référence orpheline (`expand=customer`) ; un client avec une liste
  `pdv` non vide ; le wallet dont `managers` contient l'élément de 31
  caractères ; les trois vues rowid `buma_article_with_no_stock_list_view`,
  `buma_stockage_no_manager` et `buma_stock_transaction_list_view`, rowids
  égaux à la source.
- **C-37** : fichiers : échantillon de fichiers présents téléchargés par
  `/api/files/<collectionId>/<recordId>/<fichier>`, octets égaux au manifeste
  (aucun des 20 champs fichiers de l'export n'est protégé) ; une référence de
  fichier absent répond 404 « The requested resource wasn't found. », comme
  PocketBase 0.22.34 (`apis/file.go:213-215`, `apis/api_error.go:32-38`).
  Aucune miniature avant l'ouverture : la demande en crée une ; elle est
  testée sur staging.
- **C-38** : realtime : connexion `GET /api/realtime` reçue avec
  `PB_CONNECT`, abonnement accepté ; la réception d'événements, qui demande
  une écriture, est testée sur staging.
- **C-39** (recommandé) : mêmes lectures sur un PocketBase 0.22.34 lancé sur
  une **copie** de la capture avec les hooks de production, en boucle
  locale, jamais sur le `pb_data` de production [outil non attribué]. Son
  bootstrap supprime puis recrée les 18 `buma_apps_roles` à chaque
  démarrage (`master.setup.ts:5-8`, `:72-73`, `:309-324`) : comparer ces
  records hors `created` et `updated`.
- **C-40** : mêmes lectures avec le SDK des clients (I8) [outil non attribué ;
  `scripts/c10_sdk_probe.mjs` écrit des données : staging seulement].
- **C-41** : comptages et empreintes des 72 tables de collections égaux avant
  et après ces contrôles ; seul le journal de requêtes de PPBase, `_requests`,
  hors de ces tables, grossit (`ppbase/middleware/request_logger.py:1-6`).

Staging, avant le jour J et obligatoire pour la porte G0 : base jetable
chargée depuis un backup réel, comptes de test créés dans cette seule base,
jamais exposée aux clients de production :

- desktop : connexion, écrans principaux, vente, retour, opérations de
  caisse ; console `executeSql` avec une requête PostgreSQL contenant `id`,
  puis lecture native de `buma_sql_table` page 1, `perPage` 1000 (D-0018) ;
  appels éventuels à `POST /sql` (I8) ;
- mobile : connexion, refresh, synchronisation, opération avec
  `mobile_message_id` rejouée deux fois (une seule écriture), rejeu d'une file
  hors ligne ;
- administration : tableau de bord, consultation et édition de records de
  test ;
- SDK, fichiers et realtime : parcours de `scripts/c10_sdk_runtime.py`
  (écritures, envoi et téléchargement de fichiers, événements), miniatures ;
- résultats validés par les référents métier.

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
4. **C-43** : premières opérations réelles des référents métier (desktop,
   mobile, caisse) : réussies, persistées, numéros de transaction à la suite
   de ceux importés.
5. Annoncer l'ouverture. PocketBase reste arrêté, `pb_data` intact.

### 6.11 Surveillance (porte G6)

Pendant la surveillance renforcée, d'une durée fixée par le décideur (I21) :

- disponibilité : `/api/health` vu de l'extérieur, processus PPBase,
  PostgreSQL ;
- HTTP : taux de 5xx et de 4xx par famille de routes (`/api/collections`,
  `/master`, `/api2`, fichiers, realtime) d'après les journaux du proxy,
  comparés au taux habituel de PocketBase (I9) [outil non attribué] ;
- authentification : aucun échec pour les comptes valides des référents ;
- PostgreSQL : violations de contraintes, interblocages, attentes de verrous,
  saturation des connexions, requêtes lentes, disque et WAL ;
- métier : numéros de transaction sans doublon, rejeux mobiles sans doublon
  (retour, recharge et conversion recherchent la clé d'idempotence, D-0022),
  ouverture et fermeture des sessions de caisse, soldes cohérents selon les
  référents ;
- fichiers : envois et téléchargements ;
- journal de requêtes PPBase : chaque requête est enregistrée dans
  `_requests`, avec des extraits de corps de requête et de réponse et un
  masquage des clés sensibles (`ppbase/middleware/request_logger.py:24-56`) ;
  volume et données personnelles à surveiller, conservation à décider.

Fin : le décideur clôt la surveillance ; `H_stop` est revérifiée ; PocketBase
et la capture sont conservés selon I17.

## 7. Critères d'arrêt (go/no-go)

Aux portes G0 à G4, tout critère non atteint est un NO-GO : retour arrière
avant ouverture (8.2), sans perte. Aux portes G5 et G6, le décideur applique
8.3.

| Porte | Critère | Seuil |
| --- | --- | --- |
| G0 | autorisations (2.1) et informations I1 à I21 | 100 % fournies |
| G0 | porte de release (annexe A) et répétition WS-G | 0 échec |
| G0 | version et révision de la production | 0.22.34 et `530018c` prouvées |
| G0 | scénarios staging desktop, mobile et administration | 100 % passants |
| G1 | requêtes transmises à PocketBase après `T_gel` (C-01) | 0 hors opérateur |
| G1 | version du binaire (C-02) | 0.22.34, empreinte officielle |
| G1 | inventaire (C-05) | 0 blocage, 1 `data.db`, WAL et journal absents ou vides |
| G1 | empreintes de capture, transfert et extraction (C-03, C-04, C-06) | 0 écart |
| G2 | intégrité et schéma (C-07) | `ok`, 0 dérive sur 114 |
| G2 | `_migrations` (C-08) | 325 sur 326, seule `1785599959` absente |
| G2 | clés `idem:` dupliquées (C-09), records postérieurs à `T_gel` (C-10) | 0 et 0 |
| G2 | diminutions de comptage (C-11) | toutes expliquées et acceptées |
| G2 | situations non couvertes (C-13) | 0 |
| G3 | cible et migrations (C-14 à C-16) | base neuve, `Pending: 0`, `ok`, 42 sur 42 |
| G3 | comptages, empreintes, rowids (C-17 à C-19) | 72/72, 72/72, 100 % |
| G3 | tableaux, orphelins, fichiers absents, doublons (C-20 à C-23) | 0 écart avec la capture |
| G3 | comptes, superusers, paramètres (C-24 à C-26) | 100 % identiques |
| G3 | schéma, horloge, source (C-27 à C-29) | conformes, `data.db` inchangé |
| G4 | copie de `storage/` (C-30) | 100 % ; 0 manquant, 0 en trop, 0 différence |
| G4 | contrôles avant ouverture (C-31 à C-38) | 100 % passants, 0 réponse 5xx |
| G4 | continuité des sessions (C-34) | 100 % des comptes de contrôle |
| G4 | comparaison avec PocketBase (C-39), si l'outil existe | 0 écart inexpliqué |
| G4 | écritures pendant les contrôles (C-41) | 0 dans les 72 tables |
| G5 | URL publique et premières opérations (C-42, C-43) | 100 % |
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
  passe : décision.

## 8. Retour arrière

### 8.1 Principes

- Décidé par le décideur, exécuté par l'opérateur, journalisé.
- PocketBase redémarre sur **son** `pb_data`, intact et vérifié. On ne
  restaure pas une sauvegarde par-dessus, ce qui serait une modification,
  sauf `pb_data` endommagé et décision explicite.
- La base PostgreSQL et le dossier de données PPBase sont conservés pour
  analyse, jamais supprimés sans décision.
- Après un retour, la capture n'est plus fraîche : une nouvelle bascule exige
  un nouveau backup et une nouvelle fenêtre.

### 8.2 Avant ouverture, sans écriture client reçue par PPBase

1. Arrêter PPBase s'il tourne.
2. Revérifier `H_stop` (C-03) sur le `pb_data` de production : SHA-256 de
   `data.db` et manifeste de `storage/` égaux. Sinon **arrêt** : ne pas
   redémarrer, rechercher ce qui a écrit dans `pb_data`.
3. Rétablir le redémarrage automatique et démarrer PocketBase avec sa ligne
   de commande inchangée (I2). Effet attendu : son bootstrap supprime puis
   recrée les 18 `buma_apps_roles` (`master.setup.ts:5-8`, `:72-73`,
   `:309-324`), comme à chaque démarrage ; ce n'est pas une corruption.
4. En local : `/api/health`, connexion d'un compte de contrôle, une liste.
5. Remettre l'amont du proxy, ou le DNS, vers PocketBase et lever la
   maintenance ; noter `T_retour`.
6. Les jetons émis par PocketBase avant le gel restent valides jusqu'à leur
   expiration (`tokenKey` inchangées dans `pb_data`).
7. Communiquer, analyser, replanifier.

### 8.3 Après ouverture, PPBase ayant reçu des écritures

1. Geler PPBase immédiatement (maintenance au proxy) ; noter `T_gel2`. Plus
   aucun backend n'écrit.
2. Conserver PPBase : sauvegarde complète de la base de production (par
   exemple `pg_dump` au format personnalisé), manifeste de `storage/` et
   journaux, dans `<privé>`, empreintes consignées.
3. Relever les écritures reçues entre `T_ouverture` et `T_gel2` : records
   créés, modifiés ou supprimés ; métadonnées de collections modifiées (par
   exemple la vue `buma_sql_table` par `executeSql`) ; fichiers ajoutés ou
   retirés ; comptes modifiés (mot de passe, `tokenKey`). `updated` ne suffit
   pas, une suppression ne laissant pas de trace : comparer l'état de PPBase
   ligne à ligne à la capture, inchangée [outil non attribué]. La table
   `_requests` aide à reconstituer les appels (méthode, URL, statut, extraits
   masqués) sans remplacer cette comparaison.
4. Le décideur et les référents fixent le sort de chaque écriture : par
   défaut, ressaisie par les clients dans PocketBase après son redémarrage,
   ses hooks recalculant stocks, soldes et numéros ; ou outil de rejeu dédié,
   écrit et testé sur staging avant usage [outil non attribué]. Jamais
   d'écriture SQL directe dans `data.db`. Avant toute ressaisie, vérifier
   qu'un client ne l'a pas déjà rejouée vers PocketBase (`mobile_message_id`,
   `system_id`).
5. Puis 8.2, étapes 2 à 7.

Impact client d'un retour après ouverture :

- les opérations faites dans PPBase manquent dans PocketBase jusqu'à leur
  ressaisie, et leurs IDs n'y existent pas ;
- les fichiers envoyés et les mots de passe changés dans PPBase n'y sont pas ;
- PPBase `c6821aa` émet des jetons de record de type `auth`
  (`ppbase/services/auth_service.py:18-19`, `:478-483`), alors que
  PocketBase 0.22.34 ne reconnaît que `admin` et `authRecord`
  (`apis/middlewares.go:229-249`, `tokens/tokens.go:5-6`) : une requête
  portant un jeton émis ou rafraîchi par PPBase y est traitée comme anonyme
  et l'utilisateur doit se reconnecter [à confirmer par WS-C à
  l'intégration] ;
- les jetons émis par PocketBase avant le gel restent valides jusqu'à leur
  expiration.

### 8.4 Fin de la période de retour arrière

Déclarée par le décideur (I21). Ensuite, le retour n'est plus planifié ; le
`pb_data` de PocketBase et la capture sont archivés en lecture seule selon
I17, jamais supprimés sans décision.

## 9. Journal de bascule

Fichier privé `<privé>/journal.md` (0600), tenu par l'opérateur : une ligne
par action, heures UTC. Un résumé public n'en reprend que des agrégats et des
empreintes de fichiers.

| Horodatage UTC | Étape ou contrôle | Action, sans secret | Résultat | Preuve | Opérateur | Vérificateur | Décision |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `<AAAA-MM-JJ hh:mm:ss>` | 6.2, C-01 | maintenance activée | `T_gel` noté | journal du proxy | `<initiales>` | `<initiales>` | — |
| `<…>` | 6.3, C-03 | `H_stop` calculée | empreintes consignées | `<privé>/…` | `<…>` | `<…>` | — |
| `<…>` | G2 | relecture du diagnostic | conforme ou écart | `<privé>/…` | `<…>` | `<…>` | GO ou NO-GO |

À consigner au minimum : autorisations et méthodes retenues ; commits BUMA et
PPBase ; `T_gel`, capture, arrêt de PocketBase, `H_stop` ; empreinte de la
capture ; chaque rapport et son empreinte ; chaque contrôle C-01 à C-43 et
chaque porte G0 à G6 avec sa décision ; `T_ouverture` ; incidents ; en cas de
retour, `T_gel2`, `T_retour` et le relevé des écritures ; fin de la période
de retour arrière.

## 10. Points ouverts avant une bascule

Dépendances des chantiers [commande à confirmer à l'intégration] :

- WS-B : mode d'import de production autorisé, empreintes par table,
  traitement des 24 defaults C5, orphelins et fichiers absents signalés,
  mode complet avec `storage/`.
- WS-B2 : extraction vérifiée `scripts/c11_extract.py`.
- WS-C : `scripts/c11_auth_import.py`, continuité des sessions administrateur
  compris, impact du type de jeton sur un retour arrière (8.3).
- WS-D : porte de version 0.22.34 du diagnostic et de la répétition.
- WS-E : migration `1789236850` et vérificateurs alignés.
- WS-G : durées, mémoire et disque. Les étapes 4, 5 et 11 de la section 5
  demandent la répétition avec le ZIP complet ; les étapes 1 à 3, 13 et 14,
  un essai sur la production.

Outils non attribués : contrôle d'horloge (C-28) ; manifeste de la copie à
froid (6.3) ; comparaison avec un PocketBase sur copie (C-39) ; lectures SDK
sans écriture (C-40) ; taux d'erreurs habituel de PocketBase (6.11) ; relevé
des écritures PPBase après ouverture et outil de rejeu éventuel (8.3).

Constats à trancher hors WS-F, sans décision à ce jour :

- PPBase enregistre des routes absentes de la source gelée : `GET /` (JSON de
  santé, `app/hooks/health.py`), là où PocketBase sert `--publicDir` sur
  `/*` (`examples/base/main.go:118-122`), et `/__foundation/*`
  (`app/hooks/poc.py`, chargé par `app/hooks/register.py:34`). Aucun écart
  n'est consigné dans `docs/decisions-and-divergences.md` : retirer ces
  routes de l'enregistrement de production ou documenter l'écart avant la
  bascule.
- Type des jetons de record : `authRecord` dans PocketBase 0.22.34
  (`tokens/tokens.go:5-6`, `tokens/record.go:18-23`), `auth` dans PPBase et
  dans PocketBase depuis `v0.23.0` (`core/record_tokens.go:13`). Le contrat
  C3 (`docs/contracts/authentication.md:63-66`) présente `authRecord` comme
  un contexte et non comme un type de claim, ce qui ne vaut pas pour 0.22.34.
  Un client qui lirait le claim `type` verrait la différence ; non vérifié
  par WS-F.
- Conservation et contenu du journal `_requests` de PPBase (6.11).

## 11. Références

PocketBase, code officiel au tag `v0.22.34` (copie en lecture seule de la
session serveur, `/root/buma-c11-tools/pocketbase-src`) :

- `core/base_backup.go:30-84` : sauvegarde dans une transaction, checkpoint
  WAL `TRUNCATE` aux erreurs ignorées, exclusions, espace disque, fichiers
  S3 ; `:49-51` : refus d'une sauvegarde concurrente ; `:267-330` :
  sauvegardes automatiques et rotation.
- `apis/backup.go:25-31` : routes de sauvegarde ; `:115-127` :
  téléchargement par jeton de fichier admin, contexte de 10 minutes.
- `forms/backup_create.go:11`, `:39-40` : nom d'une sauvegarde.
- `pocketbase.go:158-183` et `core/base.go:381-404` : arrêt propre ;
  `pocketbase.go:187-207` : `--dir`, `--encryptionEnv`, `--dev`.
- `examples/base/main.go:25-87` : options du binaire officiel ; `:118-122` :
  fichiers statiques sur `/*`.
- `cmd/serve.go:20-75` : `serve`, domaines, `--http`, `--https`,
  `--origins` ; `apis/serve.go:137-140` : cache `autocert`.
- `core/base.go:39-41`, `:517-530` : `storage/` local sous `pb_data`.
- `apis/file.go:162-193` : miniatures créées à la demande ; `:213-215` et
  `apis/api_error.go:32-38` : 404 pour un fichier absent du stockage.
- `tokens/tokens.go:5-6`, `tokens/record.go:13-27`, `tokens/admin.go:11-17`,
  `apis/middlewares.go:217-254` : types, signature et lecture des jetons ;
  `models/settings/settings.go:97-115` : `cronMaxKeep` et durées par défaut
  des jetons d'authentification (14 jours).
- Comparaison : `v0.23.0`, `core/record_tokens.go:13` (type `auth`) et
  `core/settings_model.go:127` (limites de débit, absentes de 0.22.34).

PPBase `c6821aa` (`pocketbase-auth-parity-v0404`, avant intégration de WS-A) :
`ppbase/__main__.py` (options de `serve` et `migrate`) ;
`ppbase/config.py:94`, `:101`, `:108`, `:118-119`, `:178-182` ;
`ppbase/app.py:174-199`, `:404-409` ;
`ppbase/services/file_storage.py:466-479` ;
`ppbase/services/auth_service.py:18-19`, `:478-483` ;
`ppbase/services/hook_manager.py:315-326` ;
`ppbase/middleware/request_logger.py:1-56` ;
`ppbase/middleware/rate_limit.py:26-30` ; `ppbase/api/admins.py`.

Dépôt BUMA : `docs/server-session-plan.md`, `docs/c11-preparation.md`,
`docs/c11-master-diagnostic.md`, `docs/next-agent-handoff.md`,
`docs/decisions-and-divergences.md` (D-0016, D-0018 à D-0023),
`docs/contracts/authentication.md`, `docs/contracts/rowid.md`,
`docs/contracts/settings-referentials.md`, `docs/test-matrix.md`,
`scripts/c11_import.py`, `scripts/c11_snapshot_audit.py`,
`scripts/c11_idempotency_source_check.py`, `scripts/c11_rehearsal.py`,
`scripts/c11_idempotency_runtime.py`, `scripts/verify_complete_schema.py`,
`scripts/verify_view_queries.py`, `app/finance/service.py`,
`app/hooks/register.py`, `pb_migrations/1789236846_c5_reference_defaults.py`.
Source gelée `buma_master_hooks@530018c` : `src/entries/master.setup.ts`,
`src/entries/master.pb.ts`, `src/entries/master.rupture.ts`.

## Annexe A — porte de release (commandes existantes)

Aux commits de release, sur une machine de préparation et non sur la
production, avec un rapport neuf pour chaque banc :

```sh
BUMA_SCHEMA_EXPORT=<export corrigé> PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/python -m ruff check app tests scripts
.venv/bin/python -m ruff format --check app tests scripts
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_idempotency_runtime \
  --report <nouveau rapport>.json
BUMA_SCHEMA_EXPORT=<export corrigé> \
  .venv/bin/python -m scripts.c11_rehearsal \
  --pocketbase <binaire pocketbase 0.22.27> \
  --schema <export corrigé> \
  --report <nouveau rapport>.json
```

`scripts.c11_rehearsal` exige aujourd'hui PocketBase 0.22.27
(`scripts/c11_rehearsal.py:393-394`) ; l'ajout de 0.22.34 relève de WS-D
[commande à confirmer à l'intégration]. Ces bancs créent leur propre
conteneur PostgreSQL jetable et ne visent jamais une base existante. Côté
PPBase, `pytest -p no:cacheprovider -q tests` au commit de release ne doit
présenter aucun échec nouveau par rapport aux dix échecs antérieurs consignés
par la phase 0 de la session serveur.

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
