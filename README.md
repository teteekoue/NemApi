# NemApi

**Passerelle locale compatible OpenAI pour tes abonnements web.**
NemApi expose `http://127.0.0.1:8090/v1/chat/completions` et sert les réponses d'une
**interface web de fournisseur** (DeepSeek, Qwen, Claude, Gemini, ChatGPT, Kimi, Z.ai),
pilotée par une extension Chromium. N'importe quel client OpenAI — Cline, Aider,
Continue, Qwen Code, MonkeyCode, un `curl`, un script — peut donc utiliser ces sept
fournisseurs sans clé API, sans carte bancaire, en passant par l'abonnement web.

```
client OpenAI  ──►  NemApi (FastAPI, :8090)  ──►  extension Chromium  ──►  onglet du fournisseur
                    file de jobs, outils,          écrit / lit le DOM        (deepseek.com, chat.qwen.ai…)
                    contexte incrémental
```

Fonctionne en local, sur le réseau local, ou depuis l'extérieur via un tunnel
Cloudflare (utile pour les agents en nuage).

---

## Sommaire

- [Prérequis](#prérequis)
- [Installation](#installation)
- [Première requête](#première-requête)
- [Utiliser NemApi depuis un client](#utiliser-nemapi-depuis-un-client)
- [Agents de code](#agents-de-code)
- [Fonctions de l'API](#fonctions-de-lapi)
- [Fournisseurs et modèles](#fournisseurs-et-modèles)
- [Clés API et sécurité](#clés-api-et-sécurité)
- [Réglages](#réglages)
- [Pages de l'interface](#pages-de-linterface)
- [Dépannage](#dépannage)
- [Développement](#développement)
- [Licence](#licence)

---

## Prérequis

| Élément | Version |
|---|---|
| Python | 3.9 → 3.13 |
| Navigateur | Chromium / Chrome / Edge / Brave, **103 ou plus** |
| Un compte web | sur chaque fournisseur que tu veux utiliser |

Le navigateur doit rester ouvert : c'est lui qui exécute les requêtes. Un onglet
par fournisseur, connecté à ton compte.

---

## Installation

### 1. Le proxy

```bash
cd proxy
python3 -m venv .venv && source .venv/bin/activate   # Windows : .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Le proxy écoute sur **`0.0.0.0:8090`** (donc aussi sur ton réseau local) et affiche
son adresse au démarrage :

```
NemApi v5.6.4 (FastAPI) — http://0.0.0.0:8090/
  Admin UI   → http://127.0.0.1:8090/
  Réseau     → http://192.168.1.72:8090/v1
  API        → POST /v1/chat/completions   (docs: /docs)
  Données    → /home/…/proxy/data
```

> Lance **un seul processus** : la file de jobs et l'état des onglets vivent en mémoire.

### 2. L'extension

1. `chrome://extensions` → active le **mode développeur**
2. **Charger l'extension non empaquetée** → choisis le dossier `extension/`
3. Épingle l'icône pour voir son état

Un paquet prêt à charger est aussi disponible dans [`releases/`](releases/).

### 3. Les onglets

1. Ouvre un onglet par fournisseur et **connecte-toi**
2. Va sur <http://127.0.0.1:8090> : le tableau de bord montre les onglets détectés
3. Si besoin, choisis-les à la main dans **Configuration** (par défaut, le premier
   onglet de chaque fournisseur est sélectionné automatiquement)

C'est tout : l'extension et le proxy se trouvent tout seuls sur le port 8090.

---

## Première requête

```bash
curl -sS http://127.0.0.1:8090/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"Bonjour"}]}'
```

La réponse est au format OpenAI. `/chat` offre le même test dans le navigateur
(trombone pour joindre une image).

---

## Utiliser NemApi depuis un client

Le corps de la requête est **strictement OpenAI** : seul `model` est requis, et le
nom du modèle **choisit le fournisseur** (`deepseek-chat` → DeepSeek,
`qwen-plus` → Qwen, `gpt-4o` → ChatGPT…). Aucun champ propriétaire, aucun `provider`.

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8090/v1"
export OPENAI_API_KEY="nemapi-token…"   # n'importe quelle valeur si la protection est désactivée
export OPENAI_MODEL="qwen-chat"
```

### Champs utiles

| Champ | Effet |
|---|---|
| `model` | Le modèle, et donc le fournisseur |
| `messages` | Historique au format OpenAI (texte, ou `image_url` en base64) |
| `tools` / `functions` | Outils OpenAI ; la réponse revient en `tool_calls` |
| `stream` | `true` → SSE · `false` → JSON · absent → réglage global (`Accept: text/event-stream` force le flux) |

Un client peut déclarer son nom avec l'en-tête `X-NemApi-Agent` (par exemple
`qwen-code`, `monkey-code`, `nemesis`) : les intégrations d'agents s'appliquent alors
à ses requêtes seulement.

### Depuis une autre machine

Le proxy écoute sur toutes les interfaces (`http://192.168.x.x:8090/v1`). En dehors
du réseau local, expose-le avec un tunnel :

```bash
cloudflared tunnel --url http://127.0.0.1:8090
# → https://<mots-aleatoires>.trycloudflare.com/v1
```

---

## Agents de code

Page **Intégration agents IA** (<http://127.0.0.1:8090/integrations>) : une carte par
agent, **désactivée par défaut**. Tant qu'une intégration est désactivée, NemApi se
comporte comme un proxy classique (tout le prompt est retapé à chaque requête).

| Agent | Ce que l'intégration apporte |
|---|---|
| **Qwen Code** | une seule discussion web, **contexte incrémental** (seul le nouveau message est tapé), appels d'outils OpenAI, requêtes annexes répondues en local, suite automatique des réponses coupées, keep-alive en streaming |
| **MonkeyCode** | le même socle, plus l'**API Responses** (`POST /v1/responses`) et les chemins **sans `/v1`** |
| **Nemesis** | discussion unique et appels d'outils natifs (pas de contexte incrémental : l'agent gère son contexte) |

**Pourquoi le contexte incrémental compte** — les agents renvoient tout l'historique à
chaque tour. Sans précaution, on retape l'historique entier : la discussion web double
de taille à chaque tour. Avec l'intégration, NemApi compare l'historique reçu à ce que
la discussion contient déjà et **ne tape que ce qui manque** (une trentaine de tours :
≈ 10 000 caractères tapés au lieu de 123 000). L'état est conservé dans
`proxy/data/sessions.json` ; `POST /sessions/reset` (ou « Réinitialiser » sur la page)
force un renvoi complet.

**MonkeyCode en pratique** — il tourne chez son éditeur, il lui faut donc une adresse
publique : lance le tunnel ci-dessus, puis dans la console MonkeyCode :

```
Interface   : OpenAI Chat   (ou OpenAI Responses — les deux sont servis)
URL de base : https://<tunnel>.trycloudflare.com/v1
Modèle      : deepseek-chat, qwen-chat, gemini-chat, glm-chat…
```

Évite un nom de modèle contenant `claude` ou `codex` (MonkeyCode en déduirait un autre
protocole). Le tunnel doit rester ouvert : c'est la porte d'entrée de chaque tour.

**NEMESIS** — ajoute l'en-tête `X-NemApi-Agent: nemesis` (patch fourni :
[`docs/nemesis-nemapi.patch`](docs/nemesis-nemapi.patch)) :

```yaml
provider:
  type: nemapi
  model: deepseek-chat

nemapi:
  url: "http://127.0.0.1:8090/v1"
  # token: nemapi-token…
```

---

## Fonctions de l'API

- **Fournisseurs en parallèle**, une file FIFO par fournisseur (un onglet = un job).
- **Anti-doublon** : une requête identique renvoyée en moins de 2,5 s réutilise le job.
- **Annulation** : client déconnecté avant l'envoi → job annulé ; job expiré → fournisseur libéré.
- **Appels d'outils** : le modèle est invité à écrire `<tool>{…}</tool>` dans un bloc
  ```` ```tool ```` (un bloc de code traverse le rendu sans abîmer `\`, `$`, guillemets ni URL).
  À la lecture, NemApi comprend aussi les dialectes natifs (DSML de DeepSeek, jetons V3
  multi-appels, XML de Qwen3-Coder, `<invoke>`), et **le texte restitué ne contient
  jamais de balisage d'appel**.
- **Images** : `image_url` en **data: URL base64** (20 Mo max, 8 par requête), sur le
  dernier message `user`. L'image passe par l'`input[type=file]` de la page, comme une
  pièce jointe manuelle ; une image refusée fait **échouer** la requête plutôt que de
  partir sans elle.
- **Streaming SSE** immédiat, avec un battement de cœur toutes les 15 s (un tunnel
  Cloudflare coupe une réponse muette au bout de 100 s).

### Codes d'erreur

| Code | `error.type` | Signification |
|---|---|---|
| 400 | `invalid_request` | JSON invalide, modèle inconnu, messages manquants |
| 401 | `unauthorized` | Clé API manquante ou invalide |
| 409 | `provider_not_selected` | Aucun onglet sélectionné pour ce fournisseur |
| 500 | `automation_error` | L'extension a échoué (compositeur introuvable, image refusée…) |
| 503 | `extension_unavailable` | Extension non connectée |
| 504 | `timeout` | Pas de réponse dans le délai imparti |

---

## Fournisseurs et modèles

| Fournisseur | Modèle canonique | Alias acceptés |
|---|---|---|
| DeepSeek | `deepseek-chat` | `deepseek-coder`, `deepseek-v3`, `deepseek-r1`, `chat` |
| Qwen | `qwen-chat` | `qwen-plus`, `qwen3-coder-plus`, `qwen-max`, `plus` |
| Claude | `claude-chat` | `claude-sonnet`, `sonnet`, `haiku`, `opus` |
| Gemini | `gemini-chat` | `gemini-2.5-flash`, `gemini-pro`, `flash` |
| ChatGPT | `gpt-chat` | `chatgpt`, `gpt-4o`, `gpt-4.1`, `gpt-5`, `o1`, `o3` |
| Kimi | `kimi-chat` | `kimi`, `kimi-k2`, `moonshot` |
| Z.ai | `glm-chat` | `glm`, `glm-4`, `glm-5`, `zai`, `chatglm` |

Les identifiants inconnus sont reconnus par mot-clé (`claude-opus-4-1` → Claude,
`gpt-4o-mini` → ChatGPT). Le modèle **réellement** utilisé reste celui choisi dans
l'interface web du fournisseur : NemApi envoie la requête au bon onglet, rien de plus.

---

## Clés API et sécurité

La protection par clé est **facultative** et se règle dans **Clés API**
(<http://127.0.0.1:8090/api-keys-page>) : « Activer » crée une clé, « Désactiver » les
met de côté sans les perdre.

```
Authorization: Bearer nemapi-token{suite}
# ou
X-API-Key: nemapi-token{suite}
```

Quelques règles :

1. Active la protection dès que le proxy est joignable depuis le réseau local.
2. Les pages d'administration ne sont **pas** protégées par clé : lance
   `python run.py --host 127.0.0.1` si tu ne fais pas confiance au réseau.
3. Les URL d'images distantes sont refusées : l'extension ne va jamais chercher une URL
   arbitraire avec les cookies de ton compte.
4. Clés et réglages vivent dans `proxy/data/config.json`, **jamais versionné**.

---

## Réglages

Options de ligne de commande : `--host`, `--port`. Variables d'environnement :

| Variable | Défaut | Rôle |
|---|---|---|
| `NEMAPI_HOST` | `0.0.0.0` | Interface d'écoute |
| `NEMAPI_PORT` | `8090` | Port |
| `NEMAPI_DATA_DIR` | `proxy/data` | Dossier de `config.json` / `stats.json` / `sessions.json` |
| `NEMAPI_JOB_TIMEOUT` | `900` | Délai max (s) d'une réponse (une très longue réponse peut prendre des minutes) |
| `NEMAPI_MAX_CONTINUATIONS` | `4` | Suites automatiques d'un appel d'outil coupé (0 = désactivé) |
| `NEMAPI_EXTENSION_WAIT` | `30` | Attente (s) de l'extension si elle est déconnectée |
| `NEMAPI_HEARTBEAT_TTL` | `15` | Extension considérée en ligne si vue depuis moins de N s |
| `NEMAPI_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |

Les réglages du quotidien (onglets, nouvelle discussion, lecture markdown, streaming,
intégrations) se font dans l'interface web, pas en variables.

---

## Pages de l'interface

| Page | URL | Contenu |
|---|---|---|
| Tableau de bord | `/` | état, activité, mode de lecture des réponses |
| Configuration | `/config` | onglets par fournisseur, streaming, nouvelle discussion |
| Intégrations | `/integrations` | agents, fonctions, clients récents, sessions |
| Chat test | `/chat` | conversation de test, images |
| Analytics | `/analytics` | activité, tarifs de référence des API |
| Clés API | `/api-keys-page` | protection et clés |

Côté machine : `/health` (proxy + extension), `/status`, `/v1/models`,
`/v1/chat/completions`, `/v1/responses`, `/job` et `/extension/*` (extension),
`/docs` (Swagger complet).

---

## Dépannage

| Symptôme | Cause / solution |
|---|---|
| `extension_unavailable` (503) | Extension non chargée ou proxy arrêté : `curl http://127.0.0.1:8090/health` |
| `provider_not_selected` (409) | Ouvre l'onglet du fournisseur, connecte-toi, choisis-le dans **Configuration** |
| `unauthorized` (401) | Clé absente : ajoute `Authorization: Bearer …`, ou désactive la protection |
| La requête ne finit que quand tu regardes l'onglet | L'onglet était gelé : laisse-le dans une fenêtre ouverte (l'extension tient un verrou et passe par un Worker) |
| Réponse tronquée ou mal lue | Recharge l'onglet du fournisseur (**F5**) : l'extension doit être réinjectée |
| `automation_error` à la saisie | Le site a changé son interface : F5, puis signale le message exact (il donne les longueurs et le premier écart) |
| `ModuleNotFoundError: fastapi` | `pip install -r proxy/requirements.txt` |

Après une mise à jour : **recharge l'extension** dans `chrome://extensions`, puis
**F5** les onglets des fournisseurs — sinon l'ancien script reste actif.

---

## Développement

```bash
cd proxy
pip install -r requirements-dev.txt
python -m pytest -q            # suite complète
```

Les tests démarrent un vrai serveur Uvicorn et une **extension simulée** (complétions
JSON/SSE, outils, parallélisme, anti-doublon, timeouts, clés, contexte incrémental,
frontière web). Certains tests optionnels ouvrent un vrai Chromium via Playwright, et
deux harnais jsdom rejouent l'extension réelle sur un compositeur simulé.

- **`AGENTS.md`** — architecture, invariants, sous-systèmes fragiles, conventions.
- **`FIXES.md`** — l'historique technique : chaque version, son symptôme, sa cause, son correctif.
- **`docs/`** — patch client NEMESIS.
- **`releases/`** — paquets `extension/` prêts à charger.

Pour contribuer : les conventions sont dans `AGENTS.md` §7 (code, commentaires, logs et
identifiants en anglais ; documentation en français ; un correctif = un test).

---

## Licence

**Propriétaire, tous droits réservés.** Ce projet n'est pas libre : voir
[`LICENSE`](LICENSE). Aucune copie, modification ou distribution n'est autorisée sans
accord écrit du titulaire des droits.
