# Tester toi-même l'import du backup, sur ton Mac

Cette page sert à importer ton backup PocketBase 0.22.34 dans une base
PostgreSQL que tu crées et que tu gardes, puis à démarrer PPBase avec les hooks
BUMA et à y brancher tes applications. C'est un test local : ce n'est pas la
bascule de production.

Il te faut : macOS, Python 3.12, Docker Desktop, Node.js 20 ou plus (22
recommandé ; une fois, pour construire le tableau de bord), le ZIP complet du backup et l'export du schéma
`buma-master-pb_schema-corrected.json`.

## 1. Récupérer les deux dépôts, côte à côte

Les deux dossiers doivent être dans le même dossier parent : `buma-master-ppbase`
et `ppbase`.

```sh
git clone git@github.com:18BG/buma_master_ppbase.git buma-master-ppbase  # si tu ne l'as pas
git clone git@github.com:ssakone/ppbase.git ppbase                        # si tu ne l'as pas
git -C buma-master-ppbase fetch && git -C buma-master-ppbase switch features/auth
git -C buma-master-ppbase pull
git -C ppbase fetch && git -C ppbase switch fix/d0019-relation-maxselect
git -C ppbase pull
```

## 2. Préparer Python (une fois)

```sh
cd buma-master-ppbase
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ../ppbase -e ".[dev]"
```

Si `.venv` existe déjà, `source .venv/bin/activate` puis la ligne `pip install`
suffisent.

## 3. Construire le tableau de bord de PPBase (une fois)

Depuis une copie du code source, PPBase ne sert le tableau de bord (`/_/`) qu'une
fois celui-ci construit :

```sh
(cd ../ppbase/admin-ui && npm ci && npm run build)
```

## 4. Démarrer un PostgreSQL 16 vide

Choisis le mot de passe, le nom de la base et le port local :

```sh
docker run -d --name buma-local-pg \
  -e POSTGRES_USER=buma -e POSTGRES_PASSWORD=choisis-un-mot-de-passe \
  -e POSTGRES_DB=buma_test -p 127.0.0.1:5433:5432 postgres:16-alpine
```

Attends quelques secondes. L'image `postgres:16-alpine` trie le texte comme
PocketBase (octet par octet). La base vit dans ce conteneur : `docker stop` et
`docker start buma-local-pg` la gardent. Si le mot de passe contient `@ : / ? #`,
écris-le encodé dans l'adresse (`@` devient `%40`), ou choisis-en un plus simple.

## 5. Lancer l'import

```sh
source .venv/bin/activate
export BUMA_SCHEMA_EXPORT=/chemin/vers/buma-master-pb_schema-corrected.json
python -m scripts.c11_local_import ~/Downloads/backup-master.zip \
  postgresql://buma:choisis-un-mot-de-passe@127.0.0.1:5433/buma_test
```

Deux choses seulement : le backup et l'adresse de la base. Le reste est
automatique :

- la base doit être entièrement vide, sinon la commande refuse sans rien écrire :
  elle ne peut jamais écraser ni mélanger des données. Choisir quelle base lui
  donner, c'est ta décision ;
- `pb_data/storage` (les fichiers de PPBase, à la racine du dépôt) doit être absent
  ou vide ;
- le ZIP est extrait et vérifié (CRC-32 et SHA-256 de chaque fichier) dans un
  dossier neuf `backups/c11-local-…/` ;
- les 12 migrations BUMA sont appliquées, puis les lignes de départ qu'elles
  viennent d'ajouter (stockage principal, moyens de paiement, client et
  fournisseur « divers », rôles) sont retirées, et seulement elles, car le
  backup les contient déjà ;
- le backup est diagnostiqué, puis importé en une seule transaction : records,
  administrateur, comptes, secrets des sessions et paramètres ;
- les fichiers (photos, documents) sont copiés dans `pb_data/storage`, chacun
  vérifié par sa taille et son SHA-256 ;
- contrôles finaux, puis un résumé et **la commande exacte pour démarrer PPBase**.

Valeurs par défaut : version source 0.22.34, export du schéma lu dans
`BUMA_SCHEMA_EXPORT`, dossier de données `pb_data/`, port 8090. Compte 10 à 20
minutes pour le backup complet, et environ deux fois sa taille décompressée en
espace disque libre (la commande le vérifie avant de commencer).

Le backup peut aussi être un dossier déjà extrait contenant `data.db` et
`storage/`, ou `data.db` et `c11-extraction-manifest.json` sans `storage/` : dans
ce dernier cas aucun fichier n'est copié et les images répondront 404.

## 6. Démarrer PPBase et te connecter

Copie-colle la commande affichée à la fin (elle commence par `cd`). Ctrl-C
l'arrête.

- Tableau de bord : <http://127.0.0.1:8090/_/>. Connecte-toi avec l'e-mail et le
  mot de passe de l'administrateur de ton PocketBase : il est importé tel quel,
  **aucun superuser à créer**.
- Applications : adresse du serveur `http://127.0.0.1:8090` (émulateur Android :
  `http://10.0.2.2:8090`). Les comptes gardent leurs mots de passe, et les
  sessions ouvertes avant le backup restent valides jusqu'à leur expiration
  normale (ensuite, les applis redemandent la connexion).
- Un vrai téléphone ne voit pas `127.0.0.1` : remplace `--host 127.0.0.1` par
  `--host 0.0.0.0` dans la commande et utilise l'adresse IP du Mac, seulement sur
  un réseau de confiance (les données deviennent visibles sur ce réseau).
- Si le port 8090 est pris, change `--port 8090` dans la commande.
- PPBase lit aussi le fichier `.env` du dépôt s'il existe : n'y mets ni
  `PPBASE_SMTP_*` ni `PPBASE_DEV` pendant tes tests (aucun e-mail ne doit partir).

## 7. Précautions

- Ce sont les vraies données de production : garde-les sur ton Mac et
  supprime-les après tes tests (étape 9).
- N'utilise pas l'envoi d'un e-mail de test du tableau de bord : il passerait par
  le serveur SMTP de la production, dont les réglages sont importés (D-0027). Ne
  définis pas de variables `PPBASE_SMTP_*` : sans elles, PPBase n'envoie aucun
  e-mail.
- Les tâches internes de BUMA sont désactivées par la commande affichée.
- Les écarts connus avec PocketBase 0.22.34 sont dans
  `docs/decisions-and-divergences.md` ; ceux qui attendent ton acceptation sont
  rappelés dans `AGENTS.md`.

## 8. Si l'import échoue

La commande s'arrête au premier problème et dit ce qu'elle laisse :

- refus avant toute écriture (base pas vide, `pb_data/storage` pas vide, backup
  illisible…) : corrige et relance ;
- échec après les migrations ou pendant l'import : aucune donnée du backup n'est
  dans la base (la transaction unique est annulée), mais la base est migrée.
  Supprime-la, recrée-la vide, puis relance :

  ```sh
  docker rm -f -v buma-local-pg
  # puis la commande docker run de l'étape 4
  ```

- échec après l'import (copie des fichiers, contrôles finaux), très rare : la
  commande le dit ; supprime la base et `pb_data/storage`, puis relance ;
- la copie extraite du ZIP est gardée dans `backups/c11-local-…/backup` : donne
  ce dossier comme backup pour relancer sans réextraire.

## 9. Arrêter et tout supprimer

```sh
# Ctrl-C dans le terminal de PPBase, puis :
docker rm -f -v buma-local-pg          # la base
rm -rf pb_data backups/c11-local-*     # les fichiers copiés et la copie extraite
```

`rm -rf pb_data` supprime tout le dossier de données de PPBase de ce dépôt.
