# NemApi v5.5.0

**Passerelle locale compatible OpenAI** pour DeepSeek, Qwen, Claude, Gemini, ChatGPT, Kimi et Z.ai. Elle pilote l'interface web de chaque fournisseur via une extension navigateur Chromium (DOM).

> Port officiel : **8090** → `http://127.0.0.1:8090/v1`
>
> Navigateur : **Chromium / Chrome 103+** (validé sur Chrome 103 et 155).

## v5.5.0 — la version stable

Une seule idée : **garder la logique d'envoi, de détection et d'extraction de la
v5.3.1**, qui fonctionne, et ne corriger que ce qui la ralentissait ou la
bloquait. La réécriture de la v5.4.0 a été **annulée** (voir `FIXES.md` §14) :
elle rendait l'onglet du fournisseur muet en pleine requête.

**Corrigé dans cette version :**

- ✅ **Onglet en arrière-plan** — la requête ne « se débloque » plus quand on regarde l'onglet. Chrome étouffe les timers d'une page cachée (1 s, puis **1 par minute**) et peut la **geler** : les attentes passent par un **Worker dédié** (non étouffé) avec repli `MessageChannel`, l'onglet tient un **Web Lock**, et les onglets pilotés sont exclus de la mise en veille mémoire. Un onglet **visible** garde son timer ordinaire : le cas courant ne change pas du tout
- ✅ **Plus de trou de 20 s entre deux requêtes** — l'événement de réveil du long-poll est vidé **avant** l'inspection des files, et un résultat réveille la boucle de poll immédiatement au lieu d'attendre l'alarme de 30 s
- ✅ **Le premier envoi d'un agent (95 KB)** — l'échelle de tentatives d'écriture passe de 8 × 3 compositeurs à 3, un prompt de plus de 8 000 caractères est écrit **en une fois**, l'attente d'upload ne tourne que s'il y a un fichier, et la détection de démarrage compare **deux mesures comparables** (avant : une signature comparée au markdown de la réponse précédente) avec 90 s de fenêtre pour un site qui préremplit un gros contexte
- ✅ **Plus de `Tab is already running another job`** — un routeur périmé laissé dans un onglet par un rechargement d'extension est remplacé automatiquement par la nouvelle version, sans F5
- ✅ **Une ligne `Timing` par requête** dans le journal (`typed` / `start` / `answer`) : une requête lente est attribuable d'un coup d'œil

**Mise à jour :** recharger l'extension dans `chrome://extensions`, puis
redémarrer le proxy. Le premier job remplace seul le routeur de l'onglet.

**Correctifs v5.3.1 :**
- ✅ **La discussion web ne se rouvre plus à chaque requête** : la persistance devient le défaut pour **tous** les clients (le « Chat test » de l'interface comprise). Le mode classique ne rafraîchit plus l'URL ; seul le réglage explicite « Nouvelle discussion à chaque requête » peut le réactiver
- ✅ **Première requête après l'ouverture d'un onglet** : l'extension rapportait ses onglets **après** avoir lu la configuration, si bien que le premier job partait avec une sélection d'onglet périmée (« No tab configured ») et n'aboutissait qu'au cycle suivant. L'ordre est inversé

**Nouveautés v5.3.0 :**
- ✅ **Un seul protocole pour tout le monde** : le corps de la requête est désormais **strictement OpenAI**. Les champs `fresh_chat` et `provider` ont disparu — un client qui les envoie est ignoré, pas obéi. L'agent se déclare par un **en-tête HTTP** (`X-NemApi-Agent`), que tout client sait envoyer
- ✅ **Qwen Code : les trois points bloquants sont isolés et réglables** — « Plus de nouvelle URL » (la discussion ne se rouvre jamais), « Contexte incrémental » (le contexte est géré dans la discussion) et « Appels d'outils » (le tableau `tools` OpenAI est honoré, la réponse revient en `tool_calls`)
- ✅ **Nemesis vraiment intégré** : le client n'envoie son prompt système qu'une fois, il était donc reconnu au premier tour seulement. NemApi retient désormais **la discussion attribuée** à l'agent (et l'en-tête `X-NemApi-Agent` reste la voie recommandée) — le correctif côté client est fourni dans `docs/nemesis-nemapi.patch`
- ✅ **Analytics refaite** : activité quotidienne sur 14 jours (graphique SVG), tarifs de référence **vérifiés sur les pages tarifaires officielles** (avec le prix du cache de contexte et la date de vérification)
- ✅ **Interface web reprise** : design system à jetons, shell plus lisible, icônes et accessibilité (lien d'évitement, `aria`, focus visible), pages Analytics / Intégrations / Configuration / Clés API / Tableau de bord refaites

**Nouveautés v5.2.0 :**
- ✅ **Intégration NEMESIS** (2ᵉ agent de la page « Intégration agents IA ») : une seule discussion web (plus jamais d'URL neuve) et **appels d'outils natifs** — les appels JSON de l'agent sont renvoyés au format OpenAI `tool_calls`. Pas de contexte incrémental : NEMESIS n'envoie que ce dont il a besoin
- ✅ **Images sur les 7 fournisseurs** : DeepSeek n'était pas le seul à savoir envoyer une image — l'upload passe maintenant par le même mécanisme pour Qwen, Claude, Gemini, ChatGPT, Kimi et Z.ai
- ✅ **Page « Intégration agents IA » réorganisée** : une carte par agent (chips de synthèse, fonctions, configuration à copier), section « Agents » dans le menu

**Nouveautés v5.0.0 :**
- ✅ **Nouvel onglet « Intégration agents IA »** (menu ☰ → `http://127.0.0.1:8090/integrations`) : une carte par agent de code (Qwen Code pour l'instant), **désactivée par défaut**. Activée, elle allume pour les requêtes de cet agent le contexte incrémental et toutes les fonctions agent ajoutées depuis la v4.2 ; désactivée, NemApi garde son **comportement classique** (v4.0)
- ✅ Le réglage « Contexte incrémental » quitte la page Configuration (il fait partie de l'intégration)
- ✅ **Interface web entièrement refaite** : menu commun, thème clair/sombre, mobile, notifications et fenêtres de confirmation intégrées, rafraîchissement sans clignotement, bandeau « proxy injoignable »

**Nouveautés v4.1 :**
- ✅ **Proxy réécrit en FastAPI** (asyncio + Uvicorn) : modulaire, plus rapide, sans threads bloqués
- ✅ **Projet scindé en deux** : `extension/` (navigateur) et `proxy/` (serveur + interface web)
- ✅ **Plus robuste** : jobs abandonnés annulés, slots bloqués libérés automatiquement, écriture atomique de la config
- ✅ **Documentation interactive de l'API** sur `http://127.0.0.1:8090/docs`
- ✅ **Tests automatisés** (extension simulée + vrais navigateurs Playwright)

---

## 📁 Structure

```
NemApi/
├── extension/                 # Extension Chromium (Manifest V3) — à charger dans le navigateur
│   ├── manifest.json
│   ├── background.js          # boucle de poll /job, gestion des onglets
│   ├── content.js
│   ├── providers/             # base.js + un fichier par fournisseur
│   └── icons/
│
└── proxy/                     # Serveur FastAPI + interface web d'administration
    ├── run.py                 # point d'entrée : python run.py
    ├── requirements.txt
    ├── nemapi/
    │   ├── main.py            # fabrique de l'application FastAPI
    │   ├── schemas.py         # corps de requêtes (Pydantic)
    │   ├── core/              # settings, providers & alias, erreurs, sécurité, contexte
    │   ├── services/          # jobs (file async), extension, config, stats, logs, completions, integrations
    │   ├── routers/           # openai (/v1), extension, admin, api_keys, integrations, pages
    │   └── utils/             # tools_format (prompt / tool-calls), format OpenAI/SSE, JSON
    ├── web/                   # admin, config, integrations, chat, analytics, api-keys (.html) + admin.css/admin.js, icônes
    ├── data/                  # config.json + stats.json (créés au démarrage, NON versionnés)
    ├── tools/                 # chat.py (client de test), debug_proxy.py, bench.py
    └── tests/                 # pytest
```

---

## 🚀 Installation

### 1. Démarrer le proxy

```bash
cd proxy
python3 -m venv .venv && source .venv/bin/activate    # optionnel (Windows : .venv\Scripts\activate)
pip install -r requirements.txt
python run.py
```

Le proxy démarre sur `http://0.0.0.0:8090`, donc il est aussi accessible depuis votre réseau local.

Options :

```bash
python run.py --host 127.0.0.1      # écouter seulement en local
python run.py --port 8090           # port (8090 par défaut)
```

| Variable d'environnement | Défaut | Rôle |
|---|---|---|
| `NEMAPI_HOST` | `0.0.0.0` | Interface d'écoute |
| `NEMAPI_PORT` | `8090` | Port |
| `NEMAPI_DATA_DIR` | `proxy/data` | Dossier de `config.json` / `stats.json` |
| `NEMAPI_JOB_TIMEOUT` | `900` | Délai max (s) d'attente d'une réponse de l'extension (une longue réponse — gros fichier — peut prendre plusieurs minutes) |
| `NEMAPI_MAX_CONTINUATIONS` | `4` | Nombre max de « suites » demandées quand une réponse est coupée au milieu d'un appel d'outil (0 = désactivé) |
| `NEMAPI_EXTENSION_WAIT` | `30` | Délai (s) d'attente de l'extension si elle est déconnectée |
| `NEMAPI_HEARTBEAT_TTL` | `15` | Extension considérée en ligne si vue depuis moins de N s |
| `NEMAPI_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

> ⚠️ Lancez **un seul processus** : la file de jobs et l'état des onglets sont gardés en mémoire.

### 2. Installer l'extension navigateur (Chromium / Chrome / Edge / Brave)

1. Ouvrez `chrome://extensions` et activez le **mode développeur**
2. Cliquez sur **Charger l'extension non empaquetée**
3. Sélectionnez le dossier **`extension/`**

### 3. Configurer les onglets

1. Ouvrez un onglet par fournisseur (DeepSeek, Qwen, Claude, Gemini, ChatGPT, Kimi, Z.ai)
2. Connectez-vous à chaque fournisseur
3. Allez sur http://127.0.0.1:8090/config (ou l'IP de votre machine sur le réseau local)
4. Sélectionnez l'onglet de chaque fournisseur (avec l'auto-config activée, le premier onglet trouvé est choisi automatiquement)

---

## 🔐 Sécurité avec les clés API

### Activer la protection

1. Allez dans l'onglet **Clés API** (http://127.0.0.1:8090/api-keys-page)
2. Cliquez sur **Activer**
3. Une clé par défaut est générée (ou vos anciennes clés sont restaurées si vous aviez désactivé la protection)

### Créer une clé

1. Dans **Clés API**, entrez un nom (optionnel)
2. Cliquez sur **Nouvelle clé** : elle est copiée automatiquement dans le presse-papiers ⚡

Format : `nemapi-token{suite_aléatoire}`

### Utiliser une clé

```
Authorization: Bearer nemapi-token{votre_cle}
```

Le proxy accepte aussi `X-API-Key: nemapi-token…`. Quand la protection est active, elle s'applique à tous les endpoints `/v1/*`.

### Désactiver la protection

Cliquez sur **Désactiver** : les clés sont mises de côté et seront restaurées à la prochaine activation.

> Les clés sont stockées dans `proxy/data/config.json`. Ce fichier n'est **jamais** versionné (`.gitignore`).

---

## 📡 Accès depuis le réseau local

Le proxy écoute sur **toutes les interfaces** (`0.0.0.0:8090`) et affiche votre IP locale au démarrage.

```
http://192.168.1.70:8090/v1/chat/completions
```

1. **Machine hôte** : le proxy et le navigateur avec l'extension tournent ici
2. **Machines clientes** : utilisez l'IP de la machine hôte au lieu de `127.0.0.1`
3. **Protection API active** : ajoutez l'en-tête `Authorization: Bearer {votre_cle}`

---

## 💬 Interface de chat

L'interface de test (http://127.0.0.1:8090/chat) permet de :
- choisir un modèle parmi les fournisseurs configurés
- ajouter un message système optionnel
- coller une clé API pour tester les requêtes authentifiées
- envoyer des messages et voir les réponses

---

## 📖 Utilisation de l'API

### Format compatible OpenAI

Le corps de la requête est **strictement OpenAI**. Seul le champ `model` est requis ; le fournisseur est déduit du nom du modèle :

```bash
# Nom de modèle seul
curl -sS http://127.0.0.1:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer nemapi-token{votre_cle}" \
  -d '{"model":"qwen-plus","messages":[{"role":"user","content":"Bonjour"}],"stream":false}'

# Format provider/model
curl -sS http://127.0.0.1:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen/qwen-chat","messages":[{"role":"user","content":"Bonjour"}]}'
```

### Champs du corps

| Champ | Effet |
|---|---|
| `model` | Le modèle. Il **choisit** le fournisseur (`deepseek-chat` → DeepSeek, `qwen-plus` → Qwen, `gpt-4o` → ChatGPT…). |
| `messages` | L'historique de la conversation, au format OpenAI. |
| `tools` / `functions` | Outils au format OpenAI ; les appels détectés sont renvoyés en `tool_calls`. |
| `stream` | `true` : SSE · `false` : JSON · absent : réglage global (un en-tête `Accept: text/event-stream` force le SSE). |

**Aucun champ propriétaire.** Ni `fresh_chat`, ni `provider` : un client qui les envoie est ignoré, pas obéi. Le protocole
est le même pour tout le monde — c'est ce qui permet à n'importe quel client OpenAI (Qwen Code, Cline, Aider, un
`curl`, un script) de parler à NemApi sans configuration.

### Comment l'agent est déclaré

L'agent se déclare par un **en-tête HTTP**, jamais par le corps :

```bash
-H "X-NemApi-Agent: qwen-code"    # ou nemesis
```

Sans cet en-tête, NemApi reconnaît l'agent par son User-Agent (`QwenCode/…`) ou par son prompt système. L'en-tête est
la voie recommandée : il est reconnu à **chaque** requête.

### Qui ouvre une nouvelle discussion ?

Jamais le client. C'est le proxy qui décide :

| Situation | Nouvelle discussion ? |
|---|---|
| Mode classique (aucune intégration active) | **jamais par défaut** — la discussion reste la même ; l'option **Configuration → Nouvelle discussion à chaque requête** peut la rouvrir |
| Intégration active avec « Plus de nouvelle URL » / « Discussion unique » | **jamais** — la discussion reste la même |
| Intégration active avec « Contexte incrémental » | **jamais** — et le contexte est géré dans la discussion |

### Images (multimodal)

Une requête peut joindre des images au format OpenAI, sur le **dernier** message `user` :

```json
{
  "model": "deepseek-chat",
  "messages": [{
    "role": "user",
    "content": [
      { "type": "text", "text": "Que montre cette capture ?" },
      { "type": "image_url", "image_url": { "url": "data:image/png;base64,iVBORw0…" } }
    ]
  }]
}
```

- **data: URL base64 obligatoire** — les URL distantes sont refusées (400 `invalid_image`) : l'extension ne doit jamais aller chercher une URL arbitraire avec les cookies du fournisseur.
- Limites : 20 Mo par image, 8 images par requête.
- Seul le **dernier** message `user` porte les images : la discussion web contient déjà celles des tours précédents.
- L'image est uploadée via l'`input[type=file]` de l'interface web, comme une pièce jointe manuelle ; **le base64 n'est jamais tapé en texte** dans le prompt.
- Test direct depuis `http://127.0.0.1:8090/chat` (bouton trombone, glisser-déposer, Ctrl+V).
- ✅ Les **7 fournisseurs** acceptent les images (DeepSeek, Qwen, Claude, Gemini, ChatGPT, Kimi, Z.ai) : l'upload passe par le `<input type=file>` de la page, puis le prompt attend la fin de l'upload, il est réécrit si la page l'efface, et l'envoi n'est déclenché qu'une fois le bouton Envoyer actif (cf. `AGENTS.md` §4.3).
- ⚠️ Si la page ne prend pas l'image, la requête **échoue** (`500 automation_error`) au lieu d'envoyer un message sans son image : une image perdue en silence est pire qu'un échec franc. `chatglm.cn` (智谱清言) est le cas connu où le chemin JS ne peut pas aboutir — son front-end ignore les événements `input`/`change` sur son `<input type=file>` (cf. `AGENTS.md` §4.3.2).

### Codes d'erreur

| Code | `error.type` | Signification |
|---|---|---|
| 400 | `invalid_request` | JSON invalide, modèle inconnu, messages manquants |
| 401 | `unauthorized` | Clé API manquante ou invalide |
| 409 | `provider_not_selected` | Aucun onglet sélectionné pour ce fournisseur |
| 500 | `automation_error` | L'extension a échoué (éditeur introuvable, etc.) |
| 503 | `extension_unavailable` | Extension non connectée |
| 504 | `timeout` | Pas de réponse de l'extension dans le délai imparti |

---

## 🛠 Configuration avec des clients / agents de code

### Qwen Code (recommandé)

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8090/v1"
export OPENAI_API_KEY="nemapi-token{votre_cle}"   # ou n'importe quelle valeur non vide si la protection est désactivée
export OPENAI_MODEL="qwen-chat"                   # ou deepseek-chat, claude-chat, gemini-chat, gpt-chat, kimi-chat, glm-chat
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
        "baseUrl": "http://127.0.0.1:8090/v1",
        "description": "Qwen via extension navigateur",
        "envKey": "NEMAPI_KEY"
      }
    ]
  },
  "env": { "NEMAPI_KEY": "nemapi-token{votre_cle}" },
  "security": { "auth": { "selectedType": "openai" } },
  "model": { "name": "nemapi-qwen" }
}
```

### Intégration agents IA (v5) 🆕

Page **Intégration agents IA** (menu ☰ ou `http://127.0.0.1:8090/integrations`). Chaque agent de code a sa carte et son interrupteur, **désactivé par défaut**. Tant qu'une intégration est désactivée, NemApi se comporte comme le proxy classique v4.0 — sauf sur un point : la discussion web reste **persistante** (l'URL n'est plus rouverte à chaque requête). Tout le prompt est tapé à chaque requête, les requêtes annexes sont envoyées au navigateur, pas de suite automatique, pas de keep-alive. Le réglage « Nouvelle discussion à chaque requête » de la page Configuration peut réactiver le rafraîchissement d'URL.

**Qwen Code** — une fois activée, l'intégration s'applique **uniquement aux requêtes de Qwen Code** (les autres clients, la page Chat test, les scripts… restent en mode classique). Fonctions, toutes activées par défaut et réglables une à une :

| Fonction | Effet |
|---|---|
| Plus de nouvelle URL | la requête ne rouvre **jamais** le chat web : l'URL de la discussion reste la même d'un tour à l'autre (indépendant du réglage global) |
| Contexte incrémental | une seule discussion ; seuls les nouveaux messages sont tapés (détails ci-dessous) |
| Appels d'outils | le tableau `tools` OpenAI de l'agent est transmis au modèle avec son contrat d'appel, et la réponse est renvoyée au format OpenAI `tool_calls` |
| Requêtes annexes en local | mémoire, suggestions, `respond_in_schema` : réponse neutre immédiate ; classifieur de sécurité refusé |
| Suite automatique des réponses coupées | un appel d'outil coupé (gros fichier) est complété dans la même discussion puis recollé |
| Connexion maintenue | en streaming, en-têtes envoyés tout de suite puis un signal toutes les 15 s |

Les trois premières fonctions sont le socle de l'intégration : sans elles, Qwen Code rouvrirait un chat vide à chaque
requête et renverrait tout son contexte — lent et coûteux.

Reconnaissance de l'agent, à chaque requête : en-tête `X-NemApi-Agent: qwen-code` (prioritaire ; une autre valeur force le mode classique) → `User-Agent` `QwenCode/…` (envoyé par Qwen Code) → prompt système « You are Qwen Code ». La carte indique quand Qwen Code a été vu pour la dernière fois et comment, et le tableau « Clients récents » montre le mode appliqué à chaque client.

**Nemesis** — [NEMESIS-AGENT](https://github.com/teteekoue/NEMESIS-AGENT) n'envoie que le message courant : il gère son contexte de son côté et n'a pas besoin du contexte incrémental de NemApi. Son intégration apporte les deux autres avantages :

| Fonction | Effet |
|---|---|
| Discussion unique | la requête n'ouvre **jamais** une nouvelle discussion : l'URL du chat web reste la même, l'agent continue dans la discussion ouverte (le réglage « Nouvelle discussion à chaque requête » est ignoré pour lui) |
| Appels d'outils natifs | les appels d'outils écrits par l'agent (JSON dans un bloc ` ```json `) sont renvoyés au format OpenAI `tool_calls` au lieu de rester dans le texte. Le contrat d'outils de NemApi n'est **pas** injecté : c'est l'agent qui décide du format |

Configuration (section `nemapi` du `config.yaml` de NEMESIS) :

```yaml
provider:
  type: nemapi
  model: deepseek-chat

nemapi:
  url: "http://127.0.0.1:8090/v1"
  # token: nemapi-token…   # si la protection API est active
  # agent: nemesis          # facultatif : en-tête X-NemApi-Agent
```

**Le point important.** Nemesis n'envoie son prompt système qu'une **seule fois** (il le fusionne dans son premier
message utilisateur, puis le garde pour lui). Sans signal, NemApi ne le reconnaîtrait qu'au premier tour — et reviendrait
au mode classique ensuite, en rouvrant une discussion vide à chaque requête : le contexte serait perdu.

Deux parades, cumulables :

1. **l'en-tête `X-NemApi-Agent: nemesis`** — recommandé, reconnu à chaque requête. Le correctif côté client est dans
   `docs/nemesis-nemapi.patch` (il ajoute l'en-tête et retire l'ancien champ `fresh_chat` du corps, qui n'existe plus) ;
2. **la discussion attribuée** — quand un agent ouvre une discussion et ne la rouvre jamais, NemApi retient qu'elle lui
   appartient : les requêtes suivantes, même sans aucun signal, restent dans son mode. Une requête qui porte un tableau
   `tools` n'est jamais attribuée à Nemesis (il n'en envoie jamais).

Reconnaissance : en-tête `X-NemApi-Agent: nemesis` → `User-Agent` `Nemesis-Agent/…` → prompt système « You are **NEMESIS** » (NEMESIS fusionne son prompt système dans le **premier message utilisateur**, il est donc cherché là aussi) → discussion attribuée.

Restent actifs pour tout le monde, intégration ou non : la lecture des appels d'outils (DSML, `<tool>`…), le contrat ` ````tool `, la robustesse de l'extension et la protection contre l'exécution d'un appel d'outil coupé.

> Mise à jour depuis v4.2–4.5 : l'ancien réglage `incremental_context` de `config.json` n'est **pas** repris (tout reste désactivé par défaut). Réactive l'intégration Qwen Code sur la page Intégration agents IA.

API : `GET /agent-integrations` (agents, fonctions, détection, clients, sessions) · `POST /agent-integrations/qwen-code` avec `{"enabled": true}` et/ou `{"features": {"keepalive": false}}`.

### Contexte incrémental (fonction de l'intégration Qwen Code)

Les agents (Qwen Code, Cline, Continue, Aider…) renvoient **tout l'historique** à chaque requête, alors que l'UI web garde déjà la discussion. Sans précaution, soit on ouvre un nouveau chat à chaque requête (lent, tout est retapé), soit on retape tout l'historique dans le même chat : le contexte web est doublé à chaque tour et sature vite.

Active **Intégration agents IA → Qwen Code** (désactivée par défaut, fonction « Contexte incrémental » cochée). NemApi garde alors **une seule discussion par provider et n'en ouvre jamais de nouvelle** :

1. **1ʳᵉ requête** : tout est tapé dans la discussion ouverte (prompt système, contrat des outils, historique) ;
2. **requêtes suivantes** : NemApi compare l'historique reçu avec ce que la discussion contient déjà et **ne tape que ce qui manque** (résultats d'outils, nouveau message) — tout le reste est filtré ;
3. **prompt système modifié** : seules les lignes nouvelles sont envoyées ; **nouvel outil** : seule sa définition ;
4. **rien de nouveau** (l'agent relance une requête déjà traitée) : rien n'est tapé, la réponse déjà donnée est renvoyée ; si l'agent relance pendant la génération, il attend la même réponse.

Le filtrage est positionnel : un « continue » répété ou une sortie d'outil identique à une précédente, envoyés dans un **nouveau** tour, sont bien transmis ; seuls les messages déjà présents dans la discussion sont écartés. Il tolère ce que les agents modifient en renvoyant nos réponses (espaces, ids des tool-calls, ordre des clés JSON) et les historiques compressés.

Cas d'erreur, sans jamais ouvrir de nouveau chat :

- l'extension n'a pas pu taper le message (champ introuvable…) → il sera tapé au prochain essai ;
- le message a été tapé mais la réponse n'a pas été capturée (timeout, `/stop`) → il n'est pas retapé ; si la réponse arrive en retard elle est réutilisée, sinon NemApi demande juste « Please answer my last message above. ».

**Nouvelle session de code** : ouvre toi-même une nouvelle discussion dans le navigateur puis lance ton agent. Dès que sa première requête arrive (historique sans aucune réponse, qui ne prolonge pas la conversation précédente), NemApi le détecte et tape tout — prompt système, outils, message — dans la discussion ouverte. Une requête identique reçue dans les 5 minutes est traitée comme une relance (réponse réutilisée).

**Requêtes annexes de l'agent** (Qwen Code & co.) : en plus de sa conversation, l'agent envoie au même modèle des requêtes d'arrière-plan. Elles ne touchent plus jamais la conversation principale :

| Requête annexe | Reconnue par | Traitement (fonction « Requêtes annexes en local » de l'intégration) |
|---|---|---|
| sélection de mémoires, titre de session… | un seul outil `respond_in_schema` / `tool_choice` forcé / `response_format` JSON | réponse locale immédiate, valeurs neutres du schéma (`[]`, `""`, `false`…) |
| classifieur de sécurité du mode AUTO (`shouldBlock`) | booléen « block/deny/unsafe… » dans le schéma | **refus** (HTTP 400) → l'agent revient à l'approbation manuelle (jamais d'autorisation inventée) |
| suggestions de saisie | `[SUGGESTION MODE: …]` | réponse vide |
| sous-agents mémoire (extraction, « dream », revue de skills) | leur prompt système | « rien à sauvegarder », aucun appel d'outil |
| autres tâches annexes (sous-agents, résumé de page web, /btw…) | prompt système / outils différents de la conversation principale | tapées **dans la même discussion**, encadrées `[SIDE REQUEST …]`, avec leur propre état ; le message principal suivant commence par `[BACK TO THE MAIN CONVERSATION …]` |

Fonction désactivée : toutes les requêtes annexes sont tapées dans la discussion, encadrées, sans jamais réinitialiser la session ni retaper le prompt principal.

**Appels d'outils** : en plus du format `<tool>{json}</tool>` demandé, NemApi comprend le format natif de DeepSeek (DSML : `<｜DSML｜tool_calls>`, `<｜｜DSML｜｜ calls>`, `<||DSML||…>`, `invoke`/`parameter`, `string="true|false"`, balises de fin manquantes), les jetons DeepSeek V3, le XML de Qwen3-Coder (`<function=…>`), `<invoke>` façon Anthropic, un `<tool>` non fermé, un bloc ```json nommant un outil connu ; le JSON est lu avec tolérance (retours à la ligne bruts, guillemets non échappés, virgules finales, accolades manquantes). Les balises DSML parasites sont retirées du texte. Le modèle est invité à écrire chaque appel dans un bloc de code ` ````tool ` : un bloc de code est affiché tel quel par la page, donc les `\\`, `\n`, `$`, guillemets et URL des commandes arrivent intacts même si la réponse doit être relue depuis le rendu. Côté extension, la réponse est lue depuis le **markdown d'origine** (pont `page_bridge.js` dans le monde de la page, réinjecté à chaque job). Le tableau de bord indique comment la dernière réponse a été lue ; si tu vois « ⚠ Lecture du rendu HTML », recharge l'onglet du fournisseur (F5).

Exemple (agent virtuel) : requête 1 = prompt système + outils + message (1 284 caractères) ; NemApi répond par un appel d'outil ; l'agent renvoie tout l'historique ; requête 2 = seulement `Tool result (list_files): README.md, main.py` + une consigne courte (292 caractères), dans la même discussion.

L'état de la discussion est sauvegardé dans `proxy/data/sessions.json` (un redémarrage du proxy ne retape pas l'historique). En cas de doute, **« Réinitialiser »** (ou `POST /sessions/reset`) force la prochaine requête à tout renvoyer. Suivi : `GET /sessions`. `usage` (tokens) reflète toujours la conversation complète.

Démo avec un agent de coding virtuel (SDK `openai`, outils sur un FS virtuel) :

```bash
cd proxy && pip install openai
python tools/demo_incremental.py --rounds 4                    # simulation, sans navigateur
python tools/demo_incremental.py --live http://127.0.0.1:8090  # contre ton proxy + l'extension
```

| Mode (32 requêtes d'agent) | Caractères tapés | Discussions | Contexte dans l'UI web | Copies du prompt système |
|---|---|---|---|---|
| Classique, nouveau chat | 123 256 | 32 | 6 356 | 1 |
| Classique, même chat | 123 256 | 1 | 126 056 | 32 |
| **Intégration Qwen Code** | **9 761** | **1** | **12 561** | **1** |

### Cursor / Continue / Aider / Cline

1. Endpoint personnalisé : `http://127.0.0.1:8090/v1`
2. Modèle : voir le tableau ci-dessous
3. Clé API : `nemapi-token{votre_cle}` (si la protection est active)

---

## 📋 Modèles et alias acceptés

| Fournisseur | Modèle canonique | Alias acceptés (exemples) |
|---|---|---|
| DeepSeek | `deepseek-chat` | deepseek-coder, deepseek-v3, deepseek-r1, chat |
| Qwen | `qwen-chat` | qwen-plus, qwen2.5-plus, qwen3-coder-plus, qwen-max, plus |
| Claude | `claude-chat` | claude-sonnet, claude-3.5-sonnet, sonnet, haiku, opus |
| Gemini | `gemini-chat` | gemini-2.5-flash, gemini-pro, flash |
| ChatGPT | `gpt-chat` | chatgpt, gpt-4o, gpt-4.1, gpt-5, o1, o3 |
| Kimi | `kimi-chat` | kimi, kimi-k2, moonshot |
| Z.ai | `glm-chat` | glm, glm-4, glm-5, zai, chatglm |

Les identifiants inconnus sont aussi reconnus par mot-clé (`claude-opus-4-1` → Claude, `gpt-4o-mini` → ChatGPT, `glm-4.6` → Z.ai…).

**Note** : le modèle réellement utilisé reste celui choisi dans l'interface web du fournisseur. NemApi se contente d'envoyer la requête vers le bon onglet.

---

## ⚡ Fonctionnement

- **Fournisseurs en parallèle** : les requêtes vers des fournisseurs différents sont traitées en même temps.
- **File d'attente par fournisseur** : un seul job à la fois par onglet ; les suivants attendent (FIFO).
- **Anti-doublon** : une requête identique renvoyée en moins de 2,5 s (retry d'un SDK) réutilise le même job.
- **Annulation** : si le client se déconnecte avant que son job soit parti vers le navigateur, le job est annulé.
- **Auto-réparation** : un job expiré libère automatiquement son fournisseur ; un onglet fermé est retiré de la sélection.
- **Persistance** : réglages et clés dans `proxy/data/config.json`, statistiques dans `proxy/data/stats.json` (écriture atomique).
- **Streaming SSE** : activé par défaut. Quand des `tool_calls` sont détectés, ils sont envoyés au format OpenAI streaming.

---

## 🔧 Endpoints

| Endpoint | Méthode | Description |
|---|---|---|
| `/` | GET | Tableau de bord |
| `/config` | GET | Configuration des onglets |
| `/integrations` | GET | Intégration agents IA |
| `/chat` | GET | Interface de test de chat |
| `/api-keys-page` | GET | Gestion des clés API |
| `/analytics` | GET | Statistiques détaillées |
| `/docs` | GET | Documentation interactive (Swagger) |
| `/v1/models` | GET | Liste des modèles (format OpenAI) |
| `/v1/chat/completions` | POST | Complétion (format OpenAI) |
| `/health` | GET | Santé du proxy + état de l'extension |
| `/status` | GET | État détaillé (onglets, files, réglages) |
| `/stats` · `/stats/clear` | GET · POST | Statistiques / remise à zéro |
| `/providers` | GET | Liste des fournisseurs |
| `/settings` | GET/POST | Réglages du proxy |
| `/stop` | POST | Annule tous les jobs en cours |
| `/agent-integrations` · `/agent-integrations/{id}` | GET · POST | Intégrations d'agents : état, fonctions, clients récents / activer, désactiver, régler |
| `/sessions` · `/sessions/reset` | GET · POST | Contexte incrémental : état de la discussion et économies / oublier la discussion |
| `/api-keys` | GET/POST | Liste / création de clés |
| `/api-keys/{id}` | POST/DELETE | Suppression d'une clé |
| `/api-keys/enable` · `/disable` · `/restore` | POST | Protection par clé |
| `/job` | GET/POST | (extension) récupérer un job (`?wait=N` pour le long-polling) / renvoyer le résultat |
| `/extension/tabs` · `/extension/config` · `/extension/log` · `/extension/state` | — | (extension) onglets, sélection, logs, état |

---

## 🧪 Tests et benchmark

```bash
cd proxy
pip install -r requirements-dev.txt
python -m pytest -q
```

Les tests démarrent un vrai serveur Uvicorn et une **extension simulée** : complétions JSON/SSE, tool-calls, parallélisme, anti-doublon, timeouts, annulation, clés API, persistance, etc.

`tests/test_side_queries.py` rejoue une session Qwen Code + DeepSeek (requêtes annexes, appels d'outils DSML) ; `tests/test_tool_parsing.py` couvre tous les formats d'appels d'outils ; `tests/test_extension_dom.py` (optionnel, Playwright) vérifie le pont React et la restauration KaTeX dans un vrai monde isolé Chromium. `tests/test_render_roundtrip.py` (optionnel : Playwright + `markdown-it-py`) fait passer des appels d'outils par un vrai rendu markdown (linkify, KaTeX) puis par l'extraction HTML de l'extension : sans bloc ` ````tool ` les commandes sont abîmées exactement comme dans la session DeepSeek, avec le bloc elles arrivent au caractère près.

`tests/test_integrations.py` vérifie le mode classique par défaut (même avec le User-Agent de Qwen Code), l'intégration limitée à l'agent, la détection (en-tête, User-Agent, prompt), chaque fonction et l'abandon de l'ancien réglage.

`tests/test_web_ui.py` (optionnel) ouvre toutes les pages dans un vrai Chromium : aucune erreur JavaScript ni boîte de dialogue native, menu commun et mobile, thème, interrupteurs de la Configuration, intégration Qwen Code désactivée par défaut puis activée depuis la page (fonctions, sessions, « Réinitialiser », mémorisation après rechargement), Chat test, Analytics et clés API. Il est ignoré si Playwright n'est pas installé :

```bash
pip install playwright && python -m playwright install chromium
```

Compatibilité vérifiée : Python 3.9 → 3.13, versions minimales `fastapi 0.110`, `uvicorn 0.29`, `pydantic 2.6`, `openai 1.40`.

`tests/test_incremental.py` fait tourner un **agent de coding virtuel** (SDK `openai` officiel, stream et non-stream, boucle d'outils) contre une **UI web simulée** qui garde de vraies discussions : jamais de nouvelle discussion, prompt système / outils / résultats présents une seule fois, compression d'historique, erreurs avant et après la saisie + relances du SDK, relance pendant la génération, réponse tardive après timeout, mise à jour du prompt système et des outils, redémarrage du proxy, `/stop` et changement d'onglet. `tests/test_incremental_fuzz.py` rejoue des sessions aléatoires (réponses identiques, sorties d'outils identiques, « continue » répétés, erreurs, relances, compressions) et vérifie que chaque nouveau message arrive **exactement une fois** dans la discussion.

Benchmark (sur une instance dédiée, **sans** la vraie extension) :

```bash
NEMAPI_DATA_DIR=/tmp/nemapi-bench python run.py --port 8099 &
python tools/bench.py --url http://127.0.0.1:8099 --requests 300 --concurrency 20
```

---

## 🐛 Dépannage

### Le proxy ne répond pas
- Vérifiez qu'il est démarré : `cd proxy && python run.py`
- Vérifiez que l'extension est chargée et que les onglets des fournisseurs sont ouverts
- `curl http://127.0.0.1:8090/health`

### Erreur « No provider tab is selected »
- Allez sur http://127.0.0.1:8090/config et sélectionnez un onglet pour ce fournisseur

### Erreur « Clé API requise »
- Ajoutez l'en-tête `Authorization: Bearer {votre_cle}`, ou désactivez la protection dans **Clés API**

### `ModuleNotFoundError: fastapi`
- Installez les dépendances : `pip install -r proxy/requirements.txt`

---

## ⚠️ Sécurité

1. **Activez la protection par clé API** si le proxy est accessible depuis votre réseau local
2. Les pages d'administration (`/`, `/api-keys`…) ne sont **pas** protégées par clé : lancez `python run.py --host 127.0.0.1` si vous ne faites pas confiance à votre réseau
3. Ne partagez pas vos clés et utilisez une clé par application ou agent

---

## 📜 Licence

MIT License
