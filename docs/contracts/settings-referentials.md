# C5 — settings et référentiels

## Source couverte

- `src/entries/master.settings.ts` : lecture stricte de
  `buma_config.CONFIGURATION_1` ;
- `src/entries/master.setup.ts` : defaults de stockage, moyens de paiement,
  client/fournisseur divers et rôles applicatifs ;
- `src/entries/master.constants.ts` : actions communes des rôles ;
- `src/entries/master.tables.ts` et `src/entries/buma.tables.ts` : collections
  référentielles utilisées.

## Portage

- `app/settings/service.py` expose `get_settings()` et lève
  `SettingsNotFoundError("Settings not found")` si `CONFIGURATION_1` est absent ;
- `pb_migrations/1789236846_c5_reference_defaults.py` crée de manière
  idempotente les defaults explicitement présents dans `master.setup.ts` :
  `STOCK_PRINCIPAL`, trois moyens de paiement, `CUSTOMER_DIVERS`,
  `SUPPLIER_DIVERS` et les 18 entrées `buma_apps_roles` ;
- la migration ne supprime pas les rôles existants au redémarrage : elle utilise
  `ON CONFLICT DO NOTHING` afin de ne pas détruire des données déjà présentes.

## Preuves

- migration C5 appliquée puis second passage sans migration en attente ;
- IDs source retrouvés en PostgreSQL ; 18 rôles applicatifs présents ;
- settings absent : erreur explicite `SettingsNotFoundError` ;
- suite BUMA : `26 passed`, Ruff/format OK ;
- schéma complet : 114/114 définitions intactes.

Une configuration métier `CONFIGURATION_1` n’est pas inventée ni peuplée par ce
checkpoint, car sa valeur n’est pas un default explicite du bootstrap source.
