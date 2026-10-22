# Handoff agent suivant — BUMA Master PPBase

Date de passation : 2026-10-22.

## Livraison Git autorisée le 22 octobre 2026

- Dépôt : `https://github.com/18BG/buma_master_ppbase`, branche `features/auth`.
- Le commit de passation contenant cette note rassemble le code, les migrations,
  les tests et les rapports C10/C11 auparavant non commités. L'utilisateur a
  explicitement demandé leur commit et leur push ; `main` n'est pas la branche
  de livraison.
- État avant publication : HEAD local et distant `46fc6cd`, 33 fichiers suivis
  modifiés et 61 fichiers non suivis, dont cette note. Ces nombres décrivent
  l'état préalable, pas des modifications à recréer sur un clone récent.
- Pour récupérer la livraison sur une autre machine :

  ```sh
  git clone --branch features/auth git@github.com:18BG/buma_master_ppbase.git
  ```

- Vérifier la livraison avec `git log -1`, `git status --short --branch` et
  `git ls-remote --heads origin features/auth`. Ne pas écraser un checkout
  existant modifié pour le synchroniser.
- La source gelée `../buma_master_hooks` est propre sur `fix/uv-paani-sup@530018c`.
- Les données réelles restent locales : ZIP hors dépôt, backup extrait de 8 Go
  ignoré par Git (`backups/`). `.env`, `pb_data/` et le venv sont également
  exclus ; ne pas les publier sur GitHub.

## Dépendances à transmettre séparément pour reproduire le runtime

- Le venv BUMA charge PPBase 0.3.1 **editable** depuis
  `/Users/omarsisss/Documents/wproject/protech/ppbase`.
  Ce dépôt sibling contient des modifications locales ; il n'est ni inclus
  ni poussé avec BUMA. Lors du contrôle de passation : HEAD `5cc3dc0`, branche
  `pocketbase-auth-parity-v0404`, 15 fichiers suivis modifiés et trois non suivis.
  Une installation arbitraire de PPBase depuis PyPI ne reproduit pas
  nécessairement cet environnement. Vérifier/récupérer sa révision et ses
  corrections avant de revendiquer les mêmes résultats runtime.
- Source gelée : checkout `../buma_master_hooks` au commit
  `530018cd20c2acbe88f38b3639e500b5271e2eb2`, à lire sans le modifier.
- Export corrigé local :
  `/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json`, SHA-256
  `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`.
- Les archives, secrets et données réelles ne sont nécessaires qu'aux
  vérifications qui en dépendent, à transmettre séparément dans un périmètre
  explicitement autorisé. Le code et les rapports agrégés sont dans BUMA.

## Checkpoints

- C0–C9 : fermés dans les limites documentées.
- C10 : fermé le 2026-09-28 par D-0020, avec D-0018 maintenue. Ne pas reprendre C10.
- C11 : ouvert. Le diagnostic read-only du backup est terminé ; aucun import PostgreSQL ni cutover.

## Backup diagnostiqué

- Archive : `/Users/omarsisss/Documents/wproject/protech/master-backup.zip`.
- Version PocketBase fournie par l’utilisateur : `0.22.34` ; aucun binaire dans l’archive.
- ZIP SHA-256 : `541ca900839207c53dedb19b51e329bbec89c8e98774043c7b9dab2bbcd61295`.
- `data.db` SHA-256 : `3539f3eb1c8659421526ede0e47ccc60482aaf0abd2c114282c05719d02b1afd`.
- Schéma : 114 définitions conformes à l’export corrigé (70 base, 2 auth, 42 vues).
- Données : 1 188 247 records ; 1 282 objets storage, 4 890 458 144 octets.
- Copie locale inchangée : `backups/c11-master-20260928/`.

## Blocages qui doivent être conservés

- D-0019 : `buma_customer.pdv`, `buma_pilotage_ticket.tag` et `buma_wallet.managers` sont des relations multiples dans PocketBase mais scalaires dans le DDL PPBase actuel. Ne jamais aplatir les arrays.
- 877 groupes `(type, author, mobile_message_id)` de type RETURN couvrent 2 391 transactions avec des valeurs métier différentes. Ne pas dédupliquer, changer les clés ni désactiver l’index C4 silencieusement.
- 195 795 références relationnelles sans cible sont présentes dans le backup source, dont 115 325 ventes vers `buma_customer_old` vide. Ne pas inventer de cibles ni rediriger automatiquement.
- 2 303 fichiers référencés sont absents de l’archive. Ne pas créer de fichiers de remplacement ni supprimer les références.

## Point d’entrée et suite

Lire d’abord `AGENTS.md` et sa procédure de handoff obligatoire (baseline,
plan, tasklist, ledger et contrats/inventaire), puis `docs/c11-master-diagnostic.md` et
`docs/c11-master-diagnostic-report.json`. Les rapports complémentaires sont
`docs/c11-master-detailed-audit.json`, `docs/c11-master-duplicate-analysis.json`
et `docs/c11-master-preservation-report.json`.

La prochaine tâche est de concevoir et tester une stratégie d’import qui
préserve les arrays, les transactions dupliquées, les références orphelines,
les rowids et les fichiers manquants. Toute répétition réaliste doit rester
isolée et sans production. L’import de production et la bascule exigent une
autorisation distincte.

Validation de publication : suite de 157 tests locaux rejouée le 2026-10-22.
Le runtime avec les données réelles n'est pas rejoué : le diagnostic du
28 septembre reste une lecture SQLite `ro&immutable=1`. Les cinq erreurs
Ruff des scripts préparatoires C10 sont corrigées uniquement par formatage
et suppression d'imports inutilisés, sans nouveau scénario SQL ou clôture.
