# Diagnostic du backup Master — 28 septembre 2026

**Archive lisible, schéma conforme, import complet bloqué.** Les étapes 1 et 2
autorisées sont terminées : inventaire et diagnostic d'une copie privée.
Aucun import PostgreSQL, démarrage PocketBase, test client réel ou correction
de données effectué. Rapport consolidé : `c11-master-diagnostic-report.json`.

## Provenance, intégrité et volume

- Archive fournie : `/Users/omarsisss/Documents/wproject/protech/master-backup.zip`.
- Version PocketBase : **0.22.34**, communiquée par l'utilisateur pendant ce
  diagnostic. Le ZIP ne contient pas de binaire ni de manifeste de version.
  La répétition synthétique antérieure utilisait 0.22.27 ; elle ne devient pas
  une preuve de runtime 0.22.34.
- Archive : **1 555 849 147 octets** ; décompressé : **8 526 465 264 octets**.
  Espace libre avant extraction : environ 260 Go.
- **1 290 fichiers**, CRC valides et SHA-256 individuels conservés dans le
  manifeste privé. Le SHA-256 du ZIP est
  `541ca900839207c53dedb19b51e329bbec89c8e98774043c7b9dab2bbcd61295`.
- `data.db` : **324 898 816 octets**, `PRAGMA integrity_check = ok` ;
  lecture `mode=ro&immutable=1`. Les WAL archivés sont vides.
- `logs.db` : **3 310 325 760 octets**. Bytes/CRC contrôlés ; aucun contenu de
  journal consulté, aucune restauration des logs.
- `storage/` : **1 282 objets**, **4 890 458 144 octets**.
- Source métier : **1 188 247 records**, répartis dans les 72 collections
  base/auth ; 42 vues. **Aucun écart** entre les 114 définitions et l'export
  corrigé : IDs, champs, index, règles et options, avec la règle existante
  concernant les IDs techniques de champs des vues.
- Le ZIP date `data.db` du 15 septembre 2026 à 23:18:12 et `logs.db` du
  16 septembre à 00:00:00, sans fuseau. Les dernières ventes/transactions
  relevées datent du 15 septembre. Ce backup peut servir à une répétition ;
  ces indications ne prouvent pas l'heure exacte d'une capture cohérente et
  ne constituent pas une sauvegarde fraîche de bascule.

La copie privée est dans `backups/c11-master-20260928/`, ignoré par Git :
répertoires 0700, fichiers 0600. Les 1 290 membres et le ZIP ont été rehashés
après analyse : **aucun octet modifié**. Les rapports publics ne contiennent
ni nom de client, ni identifiant utilisateur, ni hash de mot de passe/token,
ni nom des fichiers personnels. Le détail des chemins manquants reste privé
dans `c11-private-file-findings.json` sous la copie extraite.

## Blocages techniques de l'importeur

### Relations multiples — D-0019 confirmé

| Champ | Records | Listes non vides | Références présentes |
| --- | ---: | ---: | ---: |
| buma_customer.pdv | 5 826 | 331 | 331 / 331 |
| buma_pilotage_ticket.tag | 4 | 4 | 4 / 4 |
| buma_wallet.managers | 11 | 1 | 0 / 1 |

Les 5 841 listes sont du JSON valide, avec 0 ou 1 élément dans ce snapshot.
**Une liste à un seul élément reste une liste** : aucune conversion scalaire
n'est permise. Le code DDL PPBase relu traite encore `maxSelect: null` comme
1 et produit une colonne scalaire. Le constat physique PostgreSQL demeure
celui de la répétition du 26 septembre ; aucune base n'est interrogée ici.

La liste `managers` non vide contient un élément de 31 caractères composé de
deux IDs séparés par une virgule ; chacun existe dans users. Ce constat ne
permet pas de scinder cette valeur automatiquement : cela changerait le
contenu et potentiellement les permissions legacy.

### Unicité C4 incompatible avec l'historique

**Actualisation du 2026-10-23 (D-0022) : blocage levé côté cible.** L'index
unique mobile était propre à PPBase ; la production avait appliqué toutes les
migrations source sauf `1785599959`, dont l'index ne porte que sur les clés
`idem:` (absentes du backup). La migration `1789236849` rétablit cet index
source ; les 2 391 transactions s'importent telles quelles, sans
déduplication. Preuves : `c11-idempotency-source-check.json` et
`c11-idempotency-runtime-report.json`. Le texte ci-dessous reste le constat du
28 septembre.

La clé `(type, author, mobile_message_id)` est dupliquée dans **877 groupes**
de type `RETURN`, soit **2 391 transactions**, jusqu'à 16 par groupe.
Il faudrait éliminer 1 514 lignes pour obtenir arbitrairement une seule ligne
par clé : **cela n'a pas été fait et n'est pas une solution acceptée**.

Les 877 groupes ont tous des valeurs métier différentes même en excluant
`id`, `created` et `updated`. Ce ne sont pas des copies identiques que l'on
pourrait simplement dédupliquer. Aucun doublon trouvé pour la clé
`(type, author, system_id)` non vide.

L'index unique de `1789236845_c4_transaction_idempotency.py` rejetterait ces
lignes. La source `master.idempotency.ts` recherche un record existant et
renvoie le premier trouvé ; `master.retour.functions.ts` réutilise ce lookup.
Ces lectures ne justifient ni suppression ni changement de clé de l'historique.
La stratégie de conservation des anciennes lignes et d'idempotence future
reste à définir et tester avant une répétition complète.

## État historique à conserver et expliquer

Le contrôle indépendant parcourt **191 champs relationnels**, soit
**4 896 135 références**, et trouve **195 795 références sans cible** dans cinq
champs. Ce total n'est pas un nombre de records distincts global.

| Champ | Références sans cible | Observation |
| --- | ---: | --- |
| buma_sale.customer | 115 325 | Le schéma pointe vers buma_customer_old, vide. 115 168 de ces références existent dans buma_customer ; 157 n'y correspondent pas non plus. |
| buma_sim_balance_activity.author | 80 461 | Valeur littérale `1`, sans utilisateur correspondant. |
| buma_article.categorie | 7 | Valeur littérale `0`, sans catégorie correspondante. |
| buma_customer.type | 1 | Valeur littérale `PERSONNE`, sans type relationnel correspondant ; cette valeur est aussi présente dans le setup gelé. |
| buma_wallet.managers | 1 | Deux IDs joints par une virgule dans un élément de liste. |

Ces anomalies sont présentes dans le backup ; elles n'ont pas été introduites
par PPBase. Le diagnostic les signale. Créer des faux records, remplacer les
valeurs ou rediriger la relation client changerait le legacy et n'est pas
autorisé. Il faut distinguer leur conservation fidèle de leur éventuelle
correction métier, qui serait une décision séparée.

## Fichiers et authentification

Les 20 champs fichiers référencent **2 899 chemins distincts** :

- **596 fichiers référencés présents** ;
- **2 303 fichiers référencés absents** : 2 302 dans `buma_ressources.file`
  et 1 dans `buma_wallet.image` ; aucun de leurs noms n'a été retrouvé ailleurs
  dans `storage/` ;
- **686 objets supplémentaires** : 601 fichiers de métadonnées `.attrs`,
  80 miniatures et 5 autres objets. Ils ont tous été conservés ; le fait de
  ne pas être référencés directement ne signifie pas qu'ils sont inutiles.

La configuration archivée indique S3 désactivé, tant pour les fichiers que
pour les sauvegardes. Aucune connexion distante n'a été tentée. L'absence
dans ce ZIP ne prouve pas l'absence sur l'ancien serveur ou dans un autre
backup : il faudra retrouver ces objets ou documenter leur absence, sans
substitution ni suppression de référence.

**20 users, 3 agent_users et 1 compte administrateur historique**. Les 23
records auth ont un hash bcrypt non vide et une tokenKey de 50 caractères.
Cela prouve la présence du matériel d'authentification, **pas un login testé**.
Paramètres/secrets et compte administrateur sont conservés dans le SQLite
privé, sans activation ni import. `_externalAuths` est vide.

## Preuves et suite

- `c11-master-archive-inventory.json`, `c11-master-extraction-report.json` :
  inventaire, taille, CRC et extraction privée.
- `c11-master-initial-diagnostic.json` : premier diagnostic, version encore
  inconnue au moment du lancement. Il s'arrête au premier défaut par record ;
  ses comptes partiels de relations/fichiers sont supplantés par l'audit détaillé.
- `c11-master-detailed-audit.json` : audit indépendant de tous les champs,
  y compris ceux masqués par une relation multiple ou un premier fichier absent.
- `c11-master-duplicate-analysis.json`, `c11-master-legacy-clues.json` et
  `c11-master-multirelation-field-check.json` : analyses complémentaires.
- `c11-master-preservation-report.json` : ZIP et copie inchangés après analyse.
- `scripts/c11_snapshot_audit.py` et ses trois régressions : analyse read-only
  qui continue après un premier défaut, sans rendre les données importables.
- Suite : **157 tests passants**, Ruff/format ciblés passants. Aucun runtime
  BUMA/PocketBase/PostgreSQL, HTTP, SDK ou desktop lancé dans ce diagnostic.

**Décision : diagnostic terminé, C11 non clôturé.** La suite consiste à
concevoir une stratégie d'import qui conserve les listes et les 2 391
transactions concernées, garde les valeurs historiques, et traite explicitement
les fichiers absents. Une répétition complète sur données réelles exige ce
travail préalable ; aucun import ni bascule n'est déclenché par ce rapport.
