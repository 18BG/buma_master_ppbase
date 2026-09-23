# C10 — Sous-lot `/api2/stockage`

Source : `src/entries/stockage.ts`, commit `530018c`.

- `POST /api2/stockage/transfer` écrit le transfert, chaque débit/crédit et
  chaque transaction séquentiellement. Il ne vérifie pas un droit métier dans
  le handler, peut produire un stock négatif et conserve les effets précédents
  lorsqu'un article ultérieur échoue.
- `GET /api2/stockage/stock_info/:name` appelle `pathParam("stockId")` malgré
  le template `:name`; la lecture reçoit donc une chaîne vide et renvoie les
  agrégats du stockage vide (souvent zéro). Cette faute est conservée.
- `POST /api2/stockage/add_article` ajoute des lignes avec `status=false`, car
  le source construit un Record interne sans le champ requis `status`; le
  garde renvoie 401 uniquement si `stockage.update === false`.
- `POST /api2/stockage/remove_article` n'ajoute aucun garde permission et
  supprime les premières lignes à quantité zéro ; une exception est journalisée
  par la source sans corps de réponse.
- `POST /api2/stockage/convert` exige `canManage && canConvert`, applique
  `Math.ceil(quantity*0.05)` pour NAFAMA, et renvoie `200 {message:"okay"}` même
  après une erreur attrapée.

`scripts/c10_stockage_runtime.py` vérifie ces cinq routes avec fixtures isolées,
les sauvegardes partielles, le stock négatif, la conversion NAFAMA, les flags de
permission et le nettoyage.
