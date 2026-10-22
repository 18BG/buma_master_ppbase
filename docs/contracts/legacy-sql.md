# C10 — Sous-lot SQL

Source gelée : `index.pb.ts` et `master.article.ts`, commit `530018c`.
Référence de test : PocketBase **0.22.27**, handlers source extraits et
transpilés dans un répertoire temporaire ; aucune donnée de production.

| Route | Contrat constaté |
|---|---|
| `POST /sql` | Mot de passe littéral source ; `fields` pilote les clés et types du DynamicModel ; champs absents conservent la valeur initiale ; modèle vide : 400 générique observé. |
| `POST /api/sql2` | HTTP 200 JSON `null`, même sans `sql`. Le handler async mélange les APIs Echo/Express et ne réalise pas le SQL soumis ; ce défaut est conservé. |
| `POST /master/article/executeSql` | Route publique, remplace `options.query` et les champs de la vue `buma_sql_table`, réponse collection sans alias moderne `fields`. |
| `POST /master/article/deleteSql` | Route publique, supprime la collection nommée (métadonnées et objet SQL), réponse `Operation effectuee` ; collection absente : 400 `sql: no rows in result set`. |

La référence PocketBase 0.35.1 installée localement avait répondu sans corps
pour SQL2. Elle n'a pas été utilisée comme verdict : la vérification 0.22.27,
compatible avec les APIs source, confirme JSON `null`.

`scripts/c10_legacy_sql_runtime.py` vérifie les branches ci-dessus sur PPBase,
modifie la vue seulement après snapshot de ses options/champs, la restaure en
`finally` et ne supprime qu'une collection aléatoire créée par le test.
Preuve : `docs/c10-sql-runtime-report.json`.

Décision utilisateur du 2026-09-26 (D-0018) : la console administrateur envoie
du SQL PostgreSQL à `executeSql`, sans traduction générale SQLite attendue.
Le chemin, `{query}`, la modification de la vue et la lecture native suivante
restent à préserver. La sélection doit être utilisable comme vue avec `id` ;
la route ne devient pas un exécuteur de toutes les commandes PostgreSQL.

La compatibilité SQLite de `/sql` et la recherche de ses autres consommateurs
sont reportées. La route reste présente ; ses limites de dialecte, d'erreurs
et de `fields` ne sont pas déclarées résolues. Elles ne bloquent plus le
périmètre C10 retenu par l'utilisateur.

C10 est clôturé le 2026-09-28 sur décision explicite de l'utilisateur (D-0020),
après sa confirmation de la correction PPBase. Aucun nouveau test du parcours
desktop (`SELECT * FROM users`, POST executeSql puis GET natif page 1/perPage
1000) n'est ajouté aux preuves par cette décision. L'audit statique des appels
reste dans `docs/c10-desktop-sql-usage-audit.json` ; les limites et le report
de `/sql` sont conservés sans bloquer la clôture acceptée.
