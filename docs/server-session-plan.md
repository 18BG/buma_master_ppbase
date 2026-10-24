# Plan de la session serveur — finir C11 en 12 heures

Rédigé le 2026-10-24 sur le Mac, avant passage sur le serveur. `AGENTS.md`
prime sur ce plan ; `docs/next-agent-handoff.md` donne le contexte.

## 1. Objectif et définition de « terminé »

Amener C11 à l'état **prêt pour la bascule** en 12 heures au plus :

1. tous les blocages d'import mesurés (D-0019, D-0021) résolus dans le code,
   avec tests, preuves runtime et décisions documentées ;
2. une répétition **complète sur le vrai backup** réussie dans un PostgreSQL
   jetable du serveur, avec contrôles de conservation (comptes, empreintes,
   rowids, relations, fichiers, auth) et lectures HTTP ;
3. un runbook de bascule et de retour arrière, chronométré par cette répétition.

La bascule de production reste **hors périmètre** : elle exige une
autorisation distincte de l'utilisateur, un backup frais et une fenêtre de
maintenance. Rien à moitié : une tâche commencée est terminée avec ses tests
et sa documentation, ou consignée comme ouverte dans le ledger.

## 2. Autorisations de l'utilisateur (2026-10-24)

- Orchestration multi-agents (workflows et sous-agents) pour aller vite, avec
  cohérence et vérification indépendante.
- Commits et push au fil de l'eau sur des branches dédiées : BUMA
  `features/auth` ; PPBase `fix/d0019-relation-maxselect`, créée depuis
  `pocketbase-auth-parity-v0404`. Jamais `main`, jamais de force push.
- Modifier le framework PPBase (`../ppbase`) si nécessaire, notamment pour
  D-0019. **La référence de PPBase est PocketBase** : avant de coder, étudier la
  documentation officielle (https://pocketbase.io/docs) et le code source de la
  version actuelle (https://github.com/pocketbase/pocketbase, dernier tag
  publié), puis citer tag, fichier et ligne dans la décision. Pour interpréter
  les données 0.22 de BUMA, suivre la logique de mise à niveau de PocketBase
  lui-même (migration 0.22 → 0.23). Le comportement visible par les clients
  BUMA reste celui de la source gelée sur PocketBase 0.22.34.
- Faute de data pour envoyer 1,5 Go, l'utilisateur envoie un **paquet base de
  données** de 63 Mo au lieu du ZIP :
  `~/protech/c11-master-20260928-db-bundle.tar.xz`, SHA-256
  `6dae14cd662aebb687d67636eb7d556179ba89f9a3503a99ccd210d254eeef0e`. Il
  contient `data.db` (SHA-256
  `3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd`), le
  manifeste privé de l'archive, le relevé privé des fichiers et le README du
  backup ; ni `storage/` (4,6 Go) ni `logs.db` (jamais importé). Son import est
  autorisé **uniquement** dans un PostgreSQL jetable de ce serveur. Tant qu'il
  est absent, seules les étapes qui en dépendent attendent.
- Déjà actés : D-0022 (index source) et D-0023 (ordre du lookup accepté).
- Choix par défaut, conformes à D-0009 et D-0021 sauf contre-ordre : importer
  telles quelles les références orphelines et les références de fichiers
  absents ; reprendre les secrets de jetons pour que les sessions continuent.

Toujours interdit : toute action sur la production ou ses serveurs ;
dédupliquer, aplatir une liste, inventer une cible, un ID, un rowid ou un
fichier ; modifier la source TypeScript ; publier des valeurs réelles, des
secrets ou des hashes (rapports publics agrégés, détails dans `backups/`,
ignoré par Git) ; supprimer des données de l'utilisateur.

## 3. Mise en place (par l'utilisateur, avant de lancer la session)

Le serveur doit avoir une clé SSH avec accès aux dépôts GitHub `18BG` et
`ssakone`, Python 3.11 ou plus, Docker, Node 22 ou plus et Git.

```sh
mkdir -p ~/protech && cd ~/protech
git clone --branch features/auth git@github.com:18BG/buma_master_ppbase.git buma-master-ppbase
git clone --branch pocketbase-auth-parity-v0404 git@github.com:ssakone/ppbase.git ppbase
git clone --branch fix/uv-paani-sup git@github.com:ssakone/buma_master_hooks.git buma_master_hooks
```

L'export JSON est copié dans `~/protech/` (fait le 2026-10-24). Le paquet base
de données, préparé sur le Mac, s'envoie depuis son Terminal :

```sh
scp ~/Documents/wproject/protech/buma-master-ppbase/backups/c11-master-20260928-db-bundle.tar.xz SERVEUR:~/protech/
ssh SERVEUR 'chmod 600 ~/protech/c11-master-20260928-db-bundle.tar.xz'
```

Environnement Python sur le serveur, par l'utilisateur ou, à défaut, par la
session en phase 0 :

```sh
cd ~/protech/buma-master-ppbase
git pull
python3 --version   # 3.11 ou plus
python3 -m venv .venv
.venv/bin/python -m pip install -e '../ppbase[dev]' -e '.[dev]'
echo 'export BUMA_SCHEMA_EXPORT="$HOME/protech/buma-master-pb_schema-corrected.json"' >> ~/.bashrc
source ~/.bashrc
```

Lancer Claude Code dans `~/protech/buma-master-ppbase`, dans un mode qui
n'attend pas de validation manuelle (sinon la session se bloque à la première
demande pendant la nuit), avec « Dynamic workflow size » relevé dans
`/config`. Écrire ensuite :

> Lis AGENTS.md, puis docs/server-session-plan.md, et exécute ce plan jusqu'au
> bout en orchestrant des workflows et des sous-agents (ultracode).

## 4. Phase 0 — départ vérifié (orchestrateur seul, 45 minutes environ)

0. Environnement : si `.venv` manque, vérifier Python 3.11 ou plus, créer le
   venv et installer `-e '../ppbase[dev]' -e '.[dev]'`. Si `BUMA_SCHEMA_EXPORT`
   n'est pas défini, chercher le JSON dans `~/protech/` et le passer à chaque
   commande (l'état du shell ne persiste pas entre deux commandes). Une
   installation qui exige `sudo` est consignée et demandée à l'utilisateur.
1. Procédure de handoff d'`AGENTS.md`. `git -C ../buma_master_hooks rev-parse
   HEAD` doit valoir `530018cd20c2acbe88f38b3639e500b5271e2eb2` ; sinon,
   `checkout --detach` de ce commit.
2. Empreintes SHA-256 : export `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`
   et, dès qu'il est présent, paquet base de données
   `6dae14cd662aebb687d67636eb7d556179ba89f9a3503a99ccd210d254eeef0e`.
3. `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q` :
   **175 tests attendus**. Les tests du vérificateur adaptés à D-0022 n'ont
   jamais tourné avec l'export (illisible sur le Mac) : s'ils échouent, les
   corriger en premier. Puis Ruff et format.
4. Docker : `docker info`, image `postgres:16-alpine`. Rejouer
   `python -m scripts.c11_idempotency_runtime --report /tmp/<nouveau>.json`.
5. Télécharger les binaires PocketBase officiels **0.22.27 et 0.22.34** pour
   l'architecture du serveur, hors du dépôt, et noter leurs empreintes.
   Rejouer `python -m scripts.c11_rehearsal` avec 0.22.27 avant toute
   modification.
6. Données réelles, dès que le paquet est présent : vérifier son empreinte,
   l'extraire dans `backups/c11-master-20260928/` (dossiers 0700, fichiers
   0600) et contrôler que `data.db` vaut
   `3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd`. Ne
   jamais lancer d'interpréteur depuis ce dossier.
7. Rejouer `scripts/c11_idempotency_source_check.py` sur cette copie : mêmes
   valeurs que `docs/c11-idempotency-source-check.json`.

Un écart sur le code, la source gelée ou l'export arrête la session et est
consigné dans le ledger. L'absence du paquet n'est pas un écart : les étapes 6
et 7 et le chantier WS-G attendent son arrivée, tout le reste avance.

## 5. Chantiers

| Chantier | Contenu | Dépend de | Fichiers possédés |
| --- | --- | --- | --- |
| WS-A | D-0019 dans PPBase | — | `../ppbase`, branche dédiée |
| WS-B | Importeur, diagnostic, mode sans octets de fichiers | WS-A pour les tableaux | `scripts/c11_import.py`, `scripts/c11_snapshot_audit.py`, `tests/test_c11_import.py` |
| WS-C | Comptes, paramètres, sessions | — | nouveau `scripts/c11_auth_import.py` et ses tests |
| WS-D | Référence 0.22.34 et fixture | — | `scripts/c11_fixture.py`, `scripts/c11_rehearsal.py` |
| WS-E | Migration BUMA D-0019, vérificateurs | WS-A | `pb_migrations/1789236850_*`, `scripts/verify_*` |
| WS-F | Runbook de bascule et retour arrière | WS-G pour les durées | `docs/c11-cutover-runbook.md` |
| WS-G | Répétition complète sur données réelles | WS-A à WS-E fusionnés | `docs/c11-real-rehearsal-*.json` |

### WS-A — D-0019 dans PPBase

- Établir dans le code PocketBase la sémantique 0.22.34 (`RelationOptions`,
  `IsMultiple` pour `maxSelect` nul), celle de la version actuelle (champ
  relation) et la migration de mise à niveau 0.22 → 0.23 ; lire la
  documentation actuelle des champs relation.
- Dans PPBase, `opts.get("maxSelect", 1) or 1` rend `null` égal à 1 dans
  `db/schema_manager.py` (DDL et détection de changement de type),
  `models/field_types.py` (validation), `api/files.py`, `backup/canonical.py`
  et `services/filter_parser.py`. Vérifier aussi expand, sérialisation,
  realtime et l'édition de collection dans le dashboard.
- Le format 0.22 (`options.maxSelect` nul) doit produire une relation
  multiple, sans changer la sémantique du format actuel.
- Longueur : PPBase stocke les relations en `VARCHAR(15)` ou `VARCHAR(15)[]`,
  PocketBase en texte ; un élément de `buma_wallet.managers` fait 31
  caractères. Choisir la solution fidèle à PocketBase et la documenter.
- Tests PPBase unitaires et d'intégration PostgreSQL, puis commit et push.

### WS-B — Importeur, diagnostic et mode sans octets de fichiers

- Conserver et signaler, au lieu de bloquer, les 195 795 références orphelines
  et les 2 303 fichiers absents : aucune cible ni aucun fichier inventé,
  aucune référence supprimée, rapport public agrégé et détails privés.
- Contrôler chaque valeur contre son type cible (longueur, tableau, JSON,
  nombre) avant l'INSERT, avec un code de blocage explicite.
- Relations multiples : copier les tableaux à l'identique (ordre et contenu)
  selon la sémantique fixée par WS-A.
- Version : n'accepter 0.22.34 qu'avec la preuve de WS-D.
- Performance : `load_isolated` exécute environ 3,6 millions d'instructions.
  Regrouper les insertions tout en gardant une vérification exacte (relecture
  typée par lots, empreinte par table) ; mesurer durée et mémoire, sur un jeu
  synthétique de volume comparable (environ 1,2 million d'enregistrements)
  tant que le backup est absent.
- Robustesse : transaction unique, staging supprimé en cas d'échec, reprise
  propre.
- Mode sans octets de fichiers, pour le paquet : diagnostic et répétition
  acceptent le manifeste privé (`c11-extraction-manifest.json`) à la place de
  `storage/`. Les références et tailles sont contrôlées, aucun octet n'est
  copié, et le rapport le dit explicitement. La copie octet pour octet reste
  prouvée sur la fixture synthétique et se fera à la bascule.

### WS-C — Comptes, paramètres et sessions

- `_admins` de PocketBase 0.22 vers `_superusers` de PPBase : hash bcrypt et
  tokenKey conservés.
- `_params.settings` : reprendre ce que PPBase sait représenter (nom, URL,
  SMTP, durées et secrets des jetons) ; ne jamais journaliser de secret.
- Continuité des sessions : PocketBase 0.22 signe avec `tokenKey` +
  `settings.recordAuthToken.secret` (type `authRecord`) ; PPBase vérifie
  `token_key` + secret `authToken` de la collection et accepte `authRecord`.
  Importer le secret dans chaque collection auth et prouver qu'un jeton émis
  par PocketBase 0.22.34 avant l'import est accepté par PPBase après l'import,
  y compris pour l'administrateur. Vérifier claims et algorithme dans le code.
- `_externalAuths` est vide ; `logs.db` n'est pas importé, à documenter.

### WS-D — Référence 0.22.34 et fixture

- Montrer, binaire 0.22.34 contre 0.22.27, que le stockage SQLite lu par
  l'importeur est identique (tableaux, rowid, dates, JSON, bcrypt, fichiers),
  puis ouvrir la porte de version de façon sourcée.
- Étendre la fixture synthétique à ce que contient le vrai backup : relations
  multiples non vides, orphelins, fichier absent, doublons mobile RETURN,
  élément de 31 caractères, administrateur, paramètres et jetons.
- `scripts/c11_rehearsal.py` (ligne 235) affirme aujourd'hui que les trois
  colonnes sont `character varying`, c'est-à-dire le défaut D-0019 : à aligner
  sur la correction.

### WS-E — Migration BUMA D-0019 et vérificateurs

- Migration `1789236850` : convertir sans perte les trois colonnes d'une base
  déjà migrée (`''` en `{}`, `'x'` en `{x}`), refuser toute donnée non
  représentable, tester rollback et replay ; aucun effet sur une base créée
  après la correction PPBase.
- Aligner vérificateurs et répétition ; rejouer `c11_idempotency_runtime`
  (C4, C7, C9) et les 42 vues.

### WS-F — Runbook

`docs/c11-cutover-runbook.md` : backup frais (version, empreintes), gel des
écritures, import, contrôles (comptes, empreintes, échantillons HTTP et SDK,
desktop), bascule d'URL, surveillance, retour arrière (PocketBase intact,
retour d'URL), durées mesurées par WS-G et critères d'arrêt.

### WS-G — Répétition réelle

- Démarre quand le paquet est déposé et vérifié. Sans lui : tout préparer
  (scripts, commandes, contrôles attendus), consigner l'attente et rendre la
  main ; l'utilisateur dira « le paquet est là, lance la répétition réelle ».
- Après fusion de WS-A à WS-E : import complet des 1 188 247 enregistrements
  dans un PostgreSQL jetable, en mode sans octets de fichiers. Rapport agrégé :
  comptes par table, empreintes, rowids, tableaux, orphelins et références de
  fichiers conservés, durée, mémoire, disque.
- Serveur PPBase isolé sur cette base : lectures publiques, liste, filtre et
  expand, connexion d'un superuser synthétique créé dans la seule base
  jetable, continuité de jeton sur fixture. Les téléchargements de fichiers
  restent prouvés sur la fixture synthétique, faute d'octets réels.
- Corriger et rejouer jusqu'au succès ; détruire la base jetable à la fin.

## 6. Orchestration

- Vagues : (1) phase 0 ; (2) WS-A, WS-B hors tableaux, WS-C, WS-D et WS-F en
  parallèle ; (3) intégration de WS-A, puis WS-E, fin de WS-B et intégration
  de WS-C ; (4) répétition synthétique complète et test de volume, puis WS-G
  dès que le backup est là ; (5) documentation finale.
- Chaque agent travaille dans un worktree, sur une sous-branche
  `c11/<chantier>`, et ne modifie que ses fichiers. Ledger, tasklist,
  décisions, AGENTS et note de passation sont édités **par l'orchestrateur
  seul**, à partir des rapports des agents.
- Chaque agent rend : changements, tests exécutés et résultats, preuves,
  projet d'entrée D-xxxx et limites.
- Avant chaque fusion : relecture par un agent **indépendant**, qui reçoit le
  diff et `AGENTS.md`, pas la conclusion de l'auteur ; puis suite complète,
  Ruff, `c11_idempotency_runtime` et répétition synthétique ; enfin push.
- Numéros réservés : migrations à partir de `1789236850`, décisions à partir
  de D-0024, attribués par l'orchestrateur.
- Le venv BUMA charge `../ppbase` en editable : l'orchestrateur garde ce
  dossier sur la branche de correction intégrée.
- Les scripts `c4`, `c7` et `c9` visent par défaut la base locale `55434` et le
  port `8090`. Les rejouer par le banc jetable
  (`scripts/c11_idempotency_runtime.py`) ou en surchargeant `DATABASE_URL`,
  `BASE_URL` et, pour C9, `REPORT` et `TARGET`.
- Mettre à jour le ledger à la fin de chaque vague : une reprise après
  compaction ou redémarrage repart de là.

## 7. Calendrier indicatif

| Heures | Étape |
| --- | --- |
| 0:00 – 0:45 | Phase 0 |
| 0:45 – 5:00 | Vague 2 en parallèle |
| 5:00 – 8:00 | Intégration, WS-E, fin de WS-B |
| 8:00 – 11:00 | Répétition synthétique complète et test de volume ; répétition réelle si le backup est là |
| 11:00 – 12:00 | Corrections finales, documentation, push |

Si le temps manque, terminer proprement ce qui est commencé et consigner le
reste ; ne jamais laisser une branche ou une base dans un état intermédiaire
non documenté.

## 8. Fin de session

Ledger et tasklist complets (périmètre, fichiers, commandes, résultats,
comparaisons à la source et à PocketBase, écarts, blocages), décisions D-0024
et suivantes, AGENTS et note de passation à jour, tout poussé. Restent à
l'utilisateur : déposer le paquet base de données s'il ne l'a pas encore fait,
puis autorisation de production, backup frais, fenêtre de bascule, copie et
contrôle des fichiers sur la machine de bascule, et tests desktop et mobile
sur staging.
