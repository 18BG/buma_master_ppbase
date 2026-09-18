# BUMA Master — PPBase Python

Réécriture progressive du backend PocketBase TypeScript
`buma_master_hooks` vers PPBase/Python/PostgreSQL.

## Source gelée

- dépôt source : `/Users/omarsisss/Documents/wproject/protech/buma_master_hooks`
- branche : `fix/uv-paani-sup`
- commit : `530018cd20c2acbe88f38b3639e500b5271e2eb2`
- migrations PocketBase analysées : `migrationsDir/` (326 fichiers)

Le dépôt source reste séparé et ne doit pas être modifié par ce projet.

## Démarrage foundation

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
make postgres-up
make test
make check
```

Le serveur PPBase applique automatiquement les migrations en attente au
démarrage (`auto_migrate=True` par défaut) :

```bash
make start
```

## Tester une base vide

Les commandes normales utilisent `.env` et ne demandent pas de répéter
`--db`/`--dir` :

```bash
cp .env.example .env
make postgres-up
make start
```

`make migrate-up` est donc facultatif : il sert seulement à appliquer les
migrations explicitement avant de lancer le serveur. `make migrate-status`
permet de consulter l'état. `make verify-all` ne modifie pas la base :
il contrôle les 114 définitions et les 42 requêtes de vues. Ces vérifications
sont une preuve de validation, pas une étape obligatoire à chaque démarrage du
serveur.

Pour tester une autre base, change seulement `PPBASE_DATABASE_URL` dans `.env`.

## Règle de progression

Le portage est réalisé par lots limités. Chaque lot doit lire le TypeScript
source, ajouter ses tests, passer la gate de validation et être documenté dans
`docs/migration-ppbase-tasklist.md` avant le lot suivant.

Voir [docs/implementation-plan.md](docs/implementation-plan.md).

Pour comprendre le rôle de chaque fichier et distinguer le socle des éléments
encore en validation, voir [docs/project-structure.md](docs/project-structure.md).
