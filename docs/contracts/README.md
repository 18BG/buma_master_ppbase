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
- `legacy-sql.md` : preuves SQL et comportements source défectueux reproduits ;
  PostgreSQL accepté pour la console, étude de `/sql` reportée (D-0018/D-0020).
- `legacy-records.md` : hooks CRUD natifs legacy et leurs gardes source.
- `legacy-caisse.md` : dix routes caisse legacy, preuves HTTP/PostgreSQL et
  limites de comparaison encore ouvertes.
- `legacy-remaining.md` : 18 routes client/PDV, commerce et master, comparaison
  exécutée sur PocketBase 0.22.27 et PPBase, défauts préservés.
- `rowid.md` : recherche SQLite/PocketBase sourcée, usages exacts BUMA,
  preuve API et correction des trois vues.

Toute divergence entre l'inventaire et le runtime doit être documentée dans
`docs/decisions-and-divergences.md` avant le portage concerné.
- `legacy-stockage.md` : contrats et défauts préservés du sous-lot `/api2/stockage`.

Statut courant : **C10 terminé le 2026-09-28**, sur décision explicite de
l'utilisateur après confirmation de la correction PPBase (D-0020). La clôture
historique D-0017 reste retirée. PostgreSQL pour la console et le report de
`/sql` restent acceptés sous D-0018 ; la route est conservée. D-0016 rowid et
les preuves acquises restent valides ; aucun nouveau test desktop n'est
inventé. Voir le dernier ledger et `c10-runtime-report.json`. C11 reste ouvert.
