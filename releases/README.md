# Archives de versions

Les sources complètes de chaque version sont attachées à sa **release** GitHub
(« Source code (zip/tar.gz) », générées par GitHub depuis le tag).

Ce dossier ne contient que ce que Git ne sait pas produire tout seul : le
paquet **`extension/`** de la version stable de référence, prêt à être chargé
dans `chrome://extensions` (mode développeur → *Charger l'extension non
empaquetée*), sans avoir à extraire tout le dépôt.

| Fichier | Version | Commit |
|---|---|---|
| `NemApi-v5.3.1-extension.zip` | v5.3.1 (stable de référence) | `1b3881e` |

Régénérer l'archive d'une version :

```bash
git archive --format=zip --prefix=NemApi-vX.Y.Z/ vX.Y.Z -o /tmp/src.zip
unzip -q /tmp/src.zip -d /tmp/x && cd /tmp/x/NemApi-vX.Y.Z
zip -qr releases/NemApi-vX.Y.Z-extension.zip extension
```
