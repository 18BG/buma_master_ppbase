# C10 — Hooks CRUD natifs legacy

Source : `index.pb.ts`, `caisse.ts`, `master.employee.ts` et
`master.middleware.ts`, commit `530018c`.

Le hook de création source écrit `author` et `status=true` sur sa liste de
collections. Le hook caisse ajoute `state=CLOSE`. Le hook `buma_employee_debt`
vérifie l'employé et le montant, débite l'employé avant l'écriture de la dette,
puis ajoute `author`, `status=true`, `state=UNPAID`.

Les règles de suppression sont portées pour SIM, caisse, article, stockage,
client, wallet, fournisseur, employé et agent. La mise à jour d'une SIM protège
les champs critiques seulement lorsqu'une transaction existe.

`scripts/c10_native_hooks_runtime.py` vérifie les créations natives, dette et
paiements partiels, SIM, neuf gardes de suppression, relations et nettoyage.


PocketBase valide aussi la longueur exacte de 15 caractères d'un ID fourni par
le formulaire avant d'appeler ces hooks. `legacy_records.py` reproduit ce garde
générique ; les IDs générés en interne restent inchangés.

Le hook dette participe à la transaction du formulaire CRUD PocketBase : une
erreur SQL lors de l'INSERT dette annule le débit employé du hook. Ce point a
été vérifié sur PocketBase 0.22.27 (trigger SQLite) puis PPBase (trigger
PostgreSQL), avec succès sans trigger et rejet avec trigger.
