# Handoff agent suivant — BUMA Master PPBase

Date de passation : 2026-10-22, mise à jour le 2026-10-24 pour la session serveur
et le 2026-10-25 à sa fin.

## Fin de la session serveur (2026-10-25) — à lire en premier

- État : **C11 ouvert, prêt pour la bascule sous conditions** ; entrée du ledger du
  2026-10-25 (périmètre, preuves, incidents, écarts ouverts, prochaine action).
- Preuves publiées : `docs/c11-real-rehearsal-report.json` (vrai backup, deux passages
  identiques, sans octets de fichiers), `docs/c11-synthetic-rehearsal-report.json`
  (84 réponses sur 84 identiques à PocketBase 0.22.34), `docs/c11-auth-runtime-report.json`,
  `docs/c11-pb-0.22.34-reference-report.json` ; décisions D-0024 à D-0042 ;
  runbook `docs/c11-cutover-runbook.md`.
- Dépendance : PPBase `ssakone/ppbase`, branche `fix/d0019-relation-maxselect`, révision
  `069e0bb` ou suivante (BUMA appelle `pb.set_error_compat("0.22")` et
  `pb.set_auth_record_rule_context` au démarrage). 12 migrations BUMA.
- À faire, dans l'ordre : obtenir les acceptations de l'utilisateur pour D-0029, D-0036,
  D-0039 et D-0042 (ou reprendre les corrections : branche PPBase `c11/ws-a11` non
  intégrée pour D-0036, validation de tous les champs pour D-0042) ; répétition avec le
  ZIP complet et les octets des fichiers, devant l'utilisateur ; tests des clients réels
  sur un staging ; autorisation distincte d'import de production et de bascule.
- Interdits inchangés : aucune action sur la production ; aucune déduplication, aucune
  liste aplatie, aucun record, ID, rowid ou fichier inventé ; données réelles en lecture
  seule, agrégats seulement dans les rapports publics.

## Session serveur du 2026-10-24 — plan et départ

- Plan d'exécution : **`docs/server-session-plan.md`** (autorisations de
  l'utilisateur, mise en place, chantiers, orchestration multi-agents).
- PPBase : les corrections locales auth/dashboard sont commitées et poussées
  (`c6821aa`, branche `pocketbase-auth-parity-v0404` de `ssakone/ppbase`). Il
  n'y a plus de changement local à transmettre ; la section « Dépendances »
  ci-dessous décrit l'état du 22 octobre.
- Export corrigé : chemin configurable par `BUMA_SCHEMA_EXPORT` (défaut : le
  chemin du Mac d'origine).
- Décisions récentes : D-0022 (index d'idempotence de la source) et D-0023
  (ordre du lookup sur l'historique dupliqué, écart accepté).
- Données réelles sur le serveur : un paquet base de données de 63 Mo
  (`data.db` et manifestes privés), pas le ZIP de 1,5 Go ; voir le plan.

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
- 877 groupes `(type, author, mobile_message_id)` de type RETURN couvrent 2 391 transactions avec des valeurs métier différentes. **Levé côté cible par D-0022 (2026-10-23)** : l'index unique mobile C4 était propre à PPBase ; la migration `1789236849` rétablit l'index source (`system_id` unique pour les clés `idem:`). Ces transactions s'importent telles quelles et ne doivent jamais être dédupliquées. L'ordre du lookup sur cet historique (34 groupes sur 877) est accepté par D-0023.
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

## Environnement constaté le 2026-10-23

- macOS refuse au processus de l'agent (lancé par `~/.rac/acp/node/bin/node`)
  la lecture de `~/Downloads` : l'export corrigé y est visible mais illisible
  (`Operation not permitted`). Sept tests et tous les scripts qui lisent
  l'export échouent donc tant que l'accès n'est pas accordé dans Réglages
  Système → Confidentialité et sécurité.
- Le PATH de cette session ne contient pas `/usr/local/bin` : préfixer
  `PATH=/usr/local/bin:$PATH` pour `docker`. Docker Desktop doit être démarré.
- Suite locale après D-0022 : 167 tests passants, 7 échecs dus uniquement à
  l'export illisible.

Validation de publication : suite de 157 tests locaux rejouée le 2026-10-22.
Le runtime avec les données réelles n'est pas rejoué : le diagnostic du
28 septembre reste une lecture SQLite `ro&immutable=1`. Les cinq erreurs
Ruff des scripts préparatoires C10 sont corrigées uniquement par formatage
et suppression d'imports inutilisés, sans nouveau scénario SQL ou clôture.
