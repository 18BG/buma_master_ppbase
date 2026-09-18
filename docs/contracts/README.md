# Contrats source

Les fichiers de ce dossier décrivent le contrat observé dans la source
TypeScript gelée. Ils ne remplacent pas l'export du schéma PocketBase.

- `source-inventory.json` : inventaire généré depuis le commit source gelé ;
- `routes.json` : matrice route/auth/réponse à compléter par domaine ;
- `collections.json` : collections et vues à réconcilier avec l'export réel ;
- `hooks.json` : hooks et bootstrap observés ;
- `transaction-invariants.md` : invariants financiers et transactionnels.

Toute divergence entre l'inventaire et le runtime doit être documentée dans
`docs/decisions-and-divergences.md` avant le portage concerné.
