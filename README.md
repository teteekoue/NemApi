# NemApi v3.0

**Passerelle locale OpenAI-compatible** pour DeepSeek, Qwen, Claude et Gemini via extension Firefox (DOM).

**Nouveautes v3.0 :**
- ✅ **Multi-thread par provider** : Gere plusieurs requetes simultanees pour differents providers
- ✅ **Acces reseau local** : L'API est accessible depuis d'autres machines sur votre reseau local
- ✅ **Systeme de cles API** : Securisez votre endpoint avec des cles API
- ✅ **Persistance des configurations** : Toutes les configurations sont sauvegardees et restaurees au redemarrage
- ✅ **Interface de chat amelioree** : Testez directement depuis l'interface avec support des cles API

---

## 🚀 Installation

### 1. Demarrer le proxy

```bash
python3 proxy.py
```

Le proxy demarrera sur `http://0.0.0.0:8080` (accessible depuis votre reseau local).

### 2. Installer l'extension Firefox

1. Ouvrez `about:debugging` dans Firefox
2. Cliquez sur "Charger un module temporaire"
3. Selectionnez `extension/manifest.json`

### 3. Configurer les onglets

1. Ouvrez un onglet par fournisseur (DeepSeek, Qwen, Claude, Gemini)
2. Connectez-vous a chaque fournisseur
3. Allez sur http://127.0.0.1:8080/ (ou l'IP de votre machine sur le reseau local)
4. Selectionnez l'onglet correspondant pour chaque provider

---

## 🔐 Securite avec les Cles API

### Activer la protection

1. Allez dans l'onglet **Cles API** (http://127.0.0.1:8080/api-keys-page)
2. Cliquez sur **Activer** la protection
3. Une cle par defaut sera generee automatiquement

### Creer une nouvelle cle

1. Dans l'onglet **Cles API**, entrez un nom optionnel
2. Cliquez sur **Nouvelle cle**
3. **La cle est automatiquement copiee dans votre presse-papiers** ⚡
4. **Important** : La cle ne sera plus affichee apres, copiez-la immediatement !

### Format des cles

Les cles sont generees au format : `nemapi-token{suite_aleatoire}`

Exemple : `nemapi-tokenabc123def456ghi789jkl012mno345`

### Utiliser une cle API

Ajoutez l'en-tete HTTP suivant a vos requetes :

```
Authorization: Bearer nemapi-token{votre_cle}
```

### Desactiver la protection

1. Dans l'onglet **Cles API**, cliquez sur **Desactiver**
2. La protection sera desactivee mais les cles seront conservees
3. Vous pourrez reactiver la protection plus tard

---

## 📡 Acces Reseau Local

Le proxy est configure pour ecouter sur **toutes les interfaces reseau** (`0.0.0.0:8080`).

### Trouver votre adresse IP locale

Le proxy affiche automatiquement votre adresse IP locale dans l'interface d'administration.

Exemple d'URL d'acces depuis une autre machine :
```
http://192.168.1.70:8080/v1/chat/completions
```

### Configuration requise

1. **Sur la machine hote** : Le proxy doit etre demarre
2. **Sur les machines clientes** : Utilisez l'IP de la machine hote au lieu de `127.0.0.1`
3. **Avec protection API activee** : Ajoutez l'en-tete `Authorization: Bearer {votre_cle}`

---

## 💬 Interface de Chat

L'interface de test de chat (http://127.0.0.1:8080/chat) permet maintenant :

- Selectionner un modele parmi les providers configures
- Ajouter un message systeme optionnel
- **Coller une cle API** pour tester les requetes authentifiees
- Envoyer des messages et voir les reponses en temps reel

### Utilisation avec cle API

1. Si la protection API est activee, la barre de cle API apparait automatiquement
2. Collez votre cle API dans le champ dedie
3. Envoyez vos messages normalement

---

## 📖 Utilisation de l'API

### Format compatible OpenAI (recommande)

Seul le champ `model` est requis. Le provider est deduit automatiquement du nom du modele :

```bash
# Avec le nom du modele seul
curl -sS http://192.168.1.70:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer nemapi-token{votre_cle}" \
  -d '{"model":"qwen-plus","messages":[{"role":"user","content":"Bonjour"}],"stream":false}'

# Ou avec le format provider/model
curl -sS http://192.168.1.70:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer nemapi-token{votre_cle}" \
  -d '{"model":"qwen/qwen-chat","messages":[{"role":"user","content":"Bonjour"}]}'
```

### Format herite (sans cle API si protection desactivee)

```bash
curl -sS http://192.168.1.70:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"provider":"qwen","model":"qwen-plus","messages":[{"role":"user","content":"Bonjour"}]}'
```

---

## 🛠 Configuration avec des clients / agents de coding

### Qwen Code (recommande)

Qwen Code parle le protocole OpenAI. Pointez-le vers NemApi :

```bash
export OPENAI_BASE_URL="http://192.168.1.70:8080/v1"
export OPENAI_API_KEY="nemapi-token{votre_cle}"  # ou n'importe quelle valeur non-vide si protection desactivee
export OPENAI_MODEL="qwen-chat"         # ou deepseek-chat, claude-chat, gemini-chat
# puis
qwen
```

Ou via `~/.qwen/settings.json` :

```json
{
  "modelProviders": {
    "openai": [
      {
        "id": "nemapi-qwen",
        "name": "NemApi Qwen (DOM)",
        "baseUrl": "http://192.168.1.70:8080/v1",
        "description": "Qwen via extension navigateur",
        "envKey": "NEMAPI_KEY"
      }
    ]
  },
  "env": {
    "NEMAPI_KEY": "nemapi-token{votre_cle}"
  },
  "security": {
    "auth": {
      "selectedType": "openai"
    }
  },
  "model": {
    "name": "nemapi-qwen"
  }
}
```

### Cursor / Continue / Aider / Cline

1. Endpoint personnalise : `http://192.168.1.70:8080/v1`
2. Modele correspondant :
   - `qwen-chat` / `qwen-plus` / `qwen3-coder-plus` → Qwen
   - `deepseek-chat` / `deepseek-coder` → DeepSeek
   - `claude-chat` / `claude-sonnet` → Claude
   - `gemini-chat` / `gemini-2.5-flash` → Gemini
3. **Cle API** : `nemapi-token{votre_cle}` (si protection activee)

---

## 📋 Exemples de modeles / alias supportes

| Provider  | Canonical     | Alias acceptes                                      |
|-----------|---------------|-----------------------------------------------------|
| DeepSeek  | deepseek-chat | deepseek-coder, deepseek-v3, deepseek-r1, chat      |
| Qwen      | qwen-chat     | qwen-plus, qwen2.5-plus, qwen3-coder-plus, plus     |
| Claude    | claude-chat   | claude-sonnet, claude-3-sonnet, sonnet, haiku       |
| Gemini    | gemini-chat   | gemini-2.5-flash, gemini-pro, flash                 |

**Note** : le modele reellement utilise reste celui choisi dans l'interface web du fournisseur. NemApi route uniquement vers le bon onglet.

---

## ⚡ Fonctionnalites Avancees

### Gestion Multi-thread

- **Requetes simultanees pour differents providers** : Le proxy peut gerer plusieurs requetes en parallele si elles sont pour des providers differents
- **Mise en file d'attente pour le meme provider** : Si une requete est deja en cours pour un provider, les nouvelles requetes pour ce meme provider seront mises en attente

### Persistance

Toutes les configurations sont automatiquement sauvegardees dans `config.json` :
- Cles API (stockees de maniere securisee avec hachage SHA-256)
- Parametres du proxy (stream, auto-config, fresh-chat, premium-md)
- Etat des onglets selectionnes

### Streaming SSE

Le streaming est active par defaut. Desactivez-le avec `"stream": false` ou via l'interface d'administration. Quand des tool_calls sont detectes, la reponse est forcement non-streamee (convention OpenAI).

---

## 📊 Tableau de bord

Accedez au tableau de bord a http://127.0.0.1:8080/ pour :
- Voir les statistiques (requetes, tokens, uptime)
- Configurer les onglets fournisseurs
- Gerer les cles API
- Tester avec l'interface de chat
- Voir les logs en temps reel

---

## 🔧 Administration

### Endpoints d'administration

| Endpoint | Methode | Description |
|----------|---------|-------------|
| `/` | GET | Tableau de bord |
| `/config` | GET | Configuration des onglets |
| `/chat` | GET | Interface de test de chat |
| `/api-keys-page` | GET | Gestion des cles API |
| `/analytics` | GET | Statistiques detaillees |
| `/providers` | GET | Liste des providers |
| `/settings` | GET/POST | Parametres du proxy |
| `/api-keys` | GET/POST | Gestion des cles API |
| `/api-keys/enable` | POST | Active la protection |
| `/api-keys/disable` | POST | Desactive la protection |
| `/stats` | GET | Statistiques globales |
| `/extension/state` | GET | Etat de l'extension |

---

## 🎯 Cas d'usage

### Developpement local avec plusieurs agents

1. Demarrez le proxy sur votre machine
2. Configurez chaque agent (Cursor, Qwen Code, etc.) avec l'URL du proxy
3. Utilisez des cles API differentes pour chaque agent si necessaire
4. Tous les agents peuvent utiliser le proxy simultanement pour differents providers

### Equipe utilisant le meme proxy

1. Demarrez le proxy sur une machine centrale
2. Partagez l'IP et le port avec votre equipe
3. Chaque membre de l'equipe peut :
   - Utiliser le proxy sans authentification (si protection desactivee)
   - Ou utiliser sa propre cle API (si protection activee)

---

## ⚠️ Securite

### Recommandations

1. **Activez toujours la protection par cle API** si le proxy est accessible depuis votre reseau local
2. **Ne partagez pas vos cles API** publiquement
3. **Utilisez un pare-feu** pour limiter l'acces a votre reseau local si necessaire
4. **Changez regulierement vos cles API** pour une securite optimale

### Bonnes pratiques

- Une cle API par application/agent
- Nommez vos cles pour les identifier facilement
- Supprimez les cles inutilisees
- Desactivez la protection uniquement pour le developpement local temporaire

---

## 🐛 Depannage

### Le proxy ne repond pas

- Verifiez que le proxy est demarre : `python3 proxy.py`
- Verifiez que l'extension Firefox est chargee
- Verifiez que vous avez des onglets ouverts et connectes pour les providers

### Erreur "No provider tab is selected"

- Allez sur http://127.0.0.1:8080/config
- Selectionnez un onglet pour chaque provider que vous voulez utiliser

### Erreur "Cle API requise"

- Activez la protection dans l'onglet **Cles API**
- Ou desactivez la protection si vous ne voulez pas utiliser de cles
- Ajoutez l'en-tete `Authorization: Bearer {votre_cle}` a vos requetes

### L'onglet API Keys a disparu

- L'onglet est toujours present, mais peut etre masque visuellement
- Rafraichissez la page ou redemarrez le proxy
- Avec les dernieres mises a jour, l'onglet reste toujours visible avec un badge indiquant l'etat (ON/OFF)

---

## 📜 Licence

MIT License
