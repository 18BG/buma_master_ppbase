# Préparation C11 — diagnostic et répétition isolée

Actualisation du 2026-10-25 (fin de la session serveur) : la répétition complète sur le
vrai backup est faite, sans octets de fichiers, dans un PostgreSQL jetable : deux
passages passés et identiques (D-0041, `c11-real-rehearsal-report.json`, outil
`scripts/c11_real_rehearsal.py`). Les outils de diagnostic et de répétition sont
`scripts/c11_extract.py` (extraction vérifiée du ZIP complet, D-0028),
`scripts/c11_import.py` (diagnostic, mode manifeste, `import_snapshot` : records puis
comptes et paramètres dans une transaction, D-0026, D-0027) et
`scripts/c11_snapshot_audit.py` ; `import_snapshot` refuse toute base autre que la base
jetable de répétition : l'outil d'import de production reste à écrire (runbook, 10.2). La
procédure de bascule et de retour arrière, avec ses
durées mesurées et ses portes, est `c11-cutover-runbook.md`. Les sections ci-dessous
restent l'historique de la préparation. C11 reste ouvert : voir l'entrée du ledger du
2026-10-25 pour les conditions avant toute bascule.

Actualisation du 2026-09-28 : le diagnostic autorisé du vrai ZIP Master est
terminé, en lecture seule. Voir **`c11-master-diagnostic.md`** : archive/schéma
vérifiés, incompatibilités d'import et anomalies historiques mesurées.
Aucun import PostgreSQL ni bascule. La version 0.22.34 a été fournie par
l'utilisateur. Les preuves du 26 septembre ci-dessous restent l'historique
de la préparation synthétique, sans validation rétroactive du backup réel.

État au 2026-09-26 : outillage préparé et scénario synthétique validé. **C11
reste ouvert**. L'utilisateur télécharge le backup Master ; son chemin, son
contenu et sa version PocketBase ne sont pas encore connus. Aucun backup réel
n'a été ouvert ou importé. Les fichiers BUMO/ACME/PPBase repérés dans Downloads
ne sont pas assimilés à Master. Inventaire : `c11-artifact-diagnostic.json`.

L'autorisation porte uniquement sur la préparation et les tests isolés.
Actualisation du 2026-09-28 : C10 est clôturé par sa session d'origine sur
décision explicite de l'utilisateur (D-0020). D-0018 reste inchangée ; aucun
scénario console ou `/sql` n'est repris dans C11. Les preuves ci-dessous restent
celles de la préparation du 26 septembre ; l'import de production et la
bascule ne sont pas autorisés par cette actualisation.

## Entrées et provenance

- Source TypeScript propre, en lecture seule : `fix/uv-paani-sup@530018c`.
- Export corrigé : SHA-256
  `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`.
- 72 collections base/auth, 42 vues, 20 champs fichiers.
- Référence **synthétique** créée par PocketBase 0.22.27 lui-même : import de
  l'export par `Dao.importCollections`, sauvegardes `Record`, mots de passe par
  `setPassword`. Aucun bootstrap BUMA, hook métier ou appel externe chargé.
- Version réelle de production : à fournir/identifier avec le backup. La
  référence 0.22.27 n'est pas une preuve de cette version.

## Outils

`scripts/c11_import.py` expose uniquement un **diagnostic**, sans commande
apply et sans URL PostgreSQL. Les rapports sont créés en mode 0600, sans
écrasement et sans valeurs des records, hashes de mots de passe ou clés auth.
Les chemins suivants sont des exemples à remplacer par le backup terminé.

Pour une grosse archive ZIP, commencer sans décompression :

```sh
.venv/bin/python -m scripts.c11_import \
  --archive /chemin/backup-master.zip \
  --report /private/tmp/c11-archive-diagnostic.json
```

Ce contrôle calcule le SHA-256 en flux, relève la taille compressée et la taille
décompressée déclarée, le nombre de membres et la présence d'un unique data.db.
Il refuse chemins dangereux, doublons, liens symboliques, chiffrement et WAL
non vide. **Il ne valide ni CRC, ni schéma, ni données SQLite**. Ne pas lancer
l'extraction d'une archive incomplète ; vérifier l'espace disque avant de
préparer une copie. Prévoir la copie extraite, une seconde copie de staging,
PostgreSQL et ses index/WAL : la taille ZIP seule ne dimensionne pas l'import.

Pour un backup extrait et fermé, contenant `data.db` et `storage/` :

```sh
.venv/bin/python -m scripts.c11_import \
  --data /chemin/backup-master \
  --schema /Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json \
  --source-version VERSION_CONFIRMEE \
  --report /private/tmp/c11-data-diagnostic.json
```

Version absente ou différente de celle validée : blocage explicite, jamais
devinée à partir du nom du ZIP. Le lecteur SQLite utilise
`mode=ro&immutable=1`, refuse les journaux actifs et contrôle l'intégrité et
l'empreinte du fichier. Il compare collections, IDs, champs, index, règles et
options avec l'export, puis inspecte les valeurs, relations, fichiers et clés
d'idempotence. Les IDs techniques des champs de **vues** régénérés par PB lors
d'un import de schéma sont seuls exclus de la comparaison ; expressions SQL,
types et autres attributs restent contrôlés. Aucune vue n'est importée comme
une table de records.

Les lignes sont lues progressivement. Les fichiers sont hashés en flux ; leur
inventaire reste en mémoire. Le diagnostic de volumes importants, sa durée et
sa consommation mémoire restent à mesurer sur le backup fourni.

## Préservation et échecs

Les fonctions de staging/import ne sont appelées par la CLI que dans le
scénario **synthétique** `scripts/c11_rehearsal.py` : aucun argument `--data` ou
`--db` ne permet de lui donner un backup ou une cible existante. Le script crée
un conteneur PostgreSQL avec nom aléatoire, port loopback aléatoire, aucune
monture ni volume persistant, puis le supprime en fin d'essai.

- Copie privée exacte de `data.db` et de tout `storage/`, y compris les objets
  non référencés ; manifestes SHA-256 et tailles, vérifiés après copie.
- L'import lit **cette copie vérifiée** et refuse une cible non vide ; il ne
  fusionne, ne remplace et ne renumérote aucun record.
- Les 24 defaults C5 sont retirés uniquement par le **harness** de la cible
  neuve qu'il vient de créer. Cette suppression ne fait pas partie de
  l'importeur. Le scénario de rollback restaure exactement ces 24 lignes.
- IDs, relations, ordre des listes, booléens, nombres, JSON, noms de fichiers,
  dates UTC/millisecondes, statuts, auteurs et soldes sont copiés puis relus.
  Les valeurs non représentables sont signalées, jamais corrigées.
- `passwordHash`, `tokenKey`, `emailVisibility` sont copiés vers les colonnes
  internes PPBase correspondantes. Aucun mot de passe n'est régénéré.
- `rowid` original est copié explicitement dans `users`/`buma_article`.
  Pour **toutes** les tables importées, la correspondance collection/ID/rowid
  est aussi conservée dans la table technique `_buma_c11_rowids`, sans
  collection publique. Les trois dates auth legacy sans colonne cible y sont
  archivées en JSON ; elles ne sont pas activées comme mécanisme PPBase.
- Le SQLite original conserve aussi tous les champs et tables internes.
  `_admins`, `_params`, `_externalAuths`, secrets, paramètres et sessions
  ne sont pas restaurés dans le runtime. Leur migration exige une étude dédiée.
- INSERT SQL direct, sans hooks CRUD ni recalcul de dette/stock/numéro. Les
  contraintes et triggers PostgreSQL restent actifs. Les 72 tables sont
  verrouillées avant le contrôle de vacuité ; le deuxième import attend puis
  échoue sans dupliquer de données.
- Erreur synchrone : transaction SQL annulée et staging jetable supprimé.
  Le serveur est lancé seulement après succès et contrôles. Ce prototype ne
  revendique **pas** une atomicité DB/fichiers face à SIGKILL, panne machine ou
  disque plein. Une répétition interrompue est à jeter, jamais à publier.

## Blocage concret découvert : relations sans maxSelect

Voir D-0019. PocketBase 0.22.27 stocke des tableaux JSON pour :

| Collection | Champ | Export | Colonne PPBase observée |
| --- | --- | --- | --- |
| buma_customer | pdv | maxSelect: null | character varying |
| buma_pilotage_ticket | tag | maxSelect: null | character varying |
| buma_wallet | managers | maxSelect: null | character varying |

Le diagnostic bloque dès qu'une de ces collections contient des records,
même si la valeur est `[]`. Il signale le risque comme avertissement lorsque
ces tables sont vides. Le scénario positif n'y insère donc aucune donnée ;
un scénario négatif distinct vérifie le refus d'une relation à deux IDs.
Cela ne valide pas l'import de l'ensemble d'une base Master peuplée. Aucun
schéma, handler ou contrat C10 n'est modifié pour contourner le problème.

## Validation acquise

Rapport : `c11-rehearsal-report.json`.

- 9 migrations ; 114 définitions / 42 vues contrôlées avant et après import.
- 9 records PocketBase synthétiques dans 8 collections ; 3 fichiers / 38
  octets ; 6 relations non vides vérifiées. Les autres tables restent vides.
- rowids users 41/93, article 7001, fournisseur -17 archivé ; trous et signe
  préservés. Dates antidatées, statut false et solde -17.25 conservés.
- Échec forcé après trois INSERT : toutes les tables et les 24 seeds retrouvent
  exactement leur état initial ; table d'archive annulée et fichiers nettoyés.
- Deux imports simultanés : 9 records persistés une fois, autre tentative
  `target_not_empty` ; aucune fusion/duplication.
- Refus testés : relation orpheline, clé d'idempotence dupliquée, schéma dérivé,
  nombre invalide, fichier absent, relation multiple non représentable.
- Après import, HTTP PPBase réel : login/refresh `users`, login et lecture
  owner `agent_users` avec leurs mots de passe créés par PB ; mauvais mot de
  passe 400 ; accès caisse 200 admin/404 non-admin ; fournisseur 404 anonyme ;
  article public 200 comme sa règle source. Enveloppes auth `{token,record}`
  et liste paginée, filtre/expand article/auteur, trois fichiers identiques.
- Relecture SQL et fichiers après HTTP : aucun changement des données
  importées ; source inchangée ; serveur, conteneur et dossiers de répétition
  supprimés.

Ce sont des preuves d'import synthétique et de lecture/auth HTTP cible, **pas
une nouvelle comparaison HTTP des deux serveurs**, ni une preuve SDK/desktop,
ni une clôture C10. Continuité des anciens JWT, fichiers protégés/S3, toutes
les tables peuplées, crash-recovery et débit sur gros backup restent ouverts.

Reproduction (binaire officiel 0.22.27 local, Docker disponible) :

```sh
.venv/bin/python -m scripts.c11_rehearsal \
  --pocketbase /chemin/pocketbase-0.22.27 \
  --schema /Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json \
  --report /private/tmp/c11-nouvelle-repetition.json
```

Prochaine action : à réception du chemin du téléchargement terminé, inventaire
de l'archive en lecture seule, provenance/version et espace disque ; diagnostic
d'une copie hors ligne. Les anomalies seront présentées sans correction des
données. Import de production et bascule restent non autorisés.
