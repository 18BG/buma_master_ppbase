# Différence entre les exports PocketBase

Ancien export :
`/Users/omarsisss/Downloads/buma-master-pb_schema.json`

Nouvel export corrigé :
`/Users/omarsisss/Downloads/buma-master-pb_schema-corrected.json`

Hashes :

- ancien : `facf931f99c87713025c75fc604330560dd67af8882a67979d6ce1b7a295a4ea` ;
- corrigé : `9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d`.

## Corrections demandées

Les deux vues suivantes ont remplacé `buma_pdv.parent` par `buma_pdv.agent` :

- `buma_customer_pdv_list_selector_view` ;
- `buma_customer_pdv_list_view`.

Leur champ exposé a également changé de `parent` à `agent`, avec une relation
vers `buma_agent`.

## Changements secondaires de l'export

PocketBase a aussi recalculé des métadonnées lors de l'enregistrement/export :

- `buma_caisse_open_close_listview` : IDs internes des champs régénérés et
  valeurs de `state` élargies ;
- `buma_customer_pdv_with_all_pdv` : IDs internes régénérés et minimum de
  `solde` passé de `0` à `-999999999` ;
- `buma_pdv_list_selector_for_agent_view` et
  `buma_pdv_list_selector_for_client_view` : IDs internes régénérés et valeurs
  de `network` élargies ;
- `buma_pdv_turnover` : IDs internes régénérés et valeurs de `network` élargies.

Ces cinq changements ne sont pas des corrections SQL de notre part : ils sont
présents dans le nouvel export et doivent donc être considérés comme faisant
partie de sa forme actuelle. La baseline PPBase officielle est générée depuis
cet export corrigé.
