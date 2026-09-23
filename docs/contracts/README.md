# Contrats source

Les fichiers de ce dossier décrivent le contrat observé dans la source
TypeScript gelée. Ils ne remplacent pas l'export du schéma PocketBase.

Le critère d'acceptation est la parité observable avec PocketBase : routes,
payloads, réponses, codes HTTP, auth/permissions, hooks, CRUD SDK, effets
persistés, rollback et concurrence. Une réponse HTTP 200 ne suffit pas ; toute
divergence doit être documentée et testée avant la clôture d'un checkpoint.

- `source-inventory.json` : inventaire généré depuis le commit source gelé ;
- `routes.json` : matrice route/auth/réponse à compléter par domaine ;
- `collections.json` : collections et vues à réconcilier avec l'export réel ;
- `hooks.json` : hooks et bootstrap observés ;
- `transaction-invariants.md` : invariants financiers et transactionnels.
- `commerce.md` : les 29 contrats C9, les preuves runtime et leurs limites
  explicites (notamment D-0009).
- `legacy-sql.md` : première preuve C10 SQL et comportements source défectueux
  reproduits ; C10 global reste ouvert.
- `legacy-records.md` : hooks CRUD natifs legacy et leurs gardes source.

Toute divergence entre l'inventaire et le runtime doit être documentée dans
`docs/decisions-and-divergences.md` avant le portage concerné.
- `legacy-stockage.md` : contrats et défauts préservés du sous-lot `/api2/stockage`.
