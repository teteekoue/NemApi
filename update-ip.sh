#!/data/data/com.termux/files/usr/bin/bash
# NemApi — met à jour l'IP du proxy dans l'extension et la copie vers le stockage partagé.
# À lancer au démarrage de Termux, dans le dossier ~/storage/downloads/NemApi-chromuim.

set -e
cd "$(dirname "$0")"

# 1) Détecter l'IP locale non-loopback de Termux
IP=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1)
if [ -z "$IP" ]; then
  IP=$(ip -4 addr show 2>/dev/null | grep -oP 'inet \K[0-9.]+' | grep -v '^127\.' | head -1)
fi
if [ -z "$IP" ]; then
  echo "[ERREUR] Impossible de détecter l'IP de Termux."
  exit 1
fi

PORT=8090
NEW="http://${IP}:${PORT}"

# 2) Lire l'ancienne IP (si présente) pour comparer
OLD=$(grep -oP 'const PROXY = "\K[^"]+' extension/background.js 2>/dev/null | head -1 || true)

# 3) Patcher background.js et manifest.json
sed -i -E "s|http://[0-9.]+:${PORT}|${NEW}|g" extension/background.js
sed -i -E "s|http://[0-9.]+:${PORT}/\*|${NEW}/*|g" extension/manifest.json
sed -i "s|http://localhost:${PORT}/\*|${NEW}/*|g" extension/manifest.json

# 4) Re-copier l'extension vers le dossier partagé (déjà à jour, mais sûr)
if [ "$(pwd)" != "$HOME/storage/downloads/NemApi-chromuim" ]; then
  cp -r extension "$HOME/storage/downloads/NemApi-chromuim/"
fi

# 5) Rapport
echo "───────────────────────────────────────────"
echo " NemApi — mise à jour de l'adresse du proxy"
echo "───────────────────────────────────────────"
if [ "$OLD" = "$NEW" ]; then
  echo " ✅ IP inchangée : $NEW"
  echo "    Rien à faire, l'extension est déjà bonne."
else
  echo " ⚠️  IP changée :"
  echo "     avant : ${OLD:-<non définie>}"
  echo "     après : $NEW"
  echo ""
  echo " 👉 Action requise dans ChromeOS :"
  echo "    1. Ouvre chrome://extensions"
  echo "    2. Sur NemApi → clique ↻ (Recharger)"
  echo "    3. Ferme puis rouvre l'onglet du provider IA"
fi
echo "───────────────────────────────────────────"
echo " Proxy à lancer : python3 proxy.py (dans ce dossier)"
echo "───────────────────────────────────────────"
