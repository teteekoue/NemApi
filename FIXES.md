# NemApi v4.0 — Correctifs (export du 2026-09-02)

Version corrigée de l'extension + du proxy. Résume les changements par rapport
à la version précédente.

## 1. Réponses identiques ignorées (bug du « 2ème message »)

**Symptôme** : quand le modèle répondait exactement la même chose que la réponse
précédente (ex. `VERT` puis `VERT`), la nouvelle réponse n'était jamais livrée —
le job hangait jusqu'au watchdog (~4 min).

**Cause** : la détection de nouvelle réponse reposait sur une différence de
TEXTE (`cur !== prev`), donc une réponse identique était filtrée indéfiniment.

**Fix** (`extension/providers/gemini.js`, `extension/content.js`) : détection par
compteur de bulles DOM. Avant d'envoyer le prompt, on snapshot
`getResponseCount()` (nombre de `model-response` présents) ; tant que ce compte
n'a pas augmenté, aucune réponse n'est considérée comme nouvelle. Le texte peut
donc être identique au précédent sans problème.

## 2. Jobs fantômes en cascade

**Symptôme** : un job abandonné par le watchdog laissait le composer plein et
une génération en cours ; les jobs suivants échouaient en cascade.

**Fix** (`extension/content.js`) :
- `waitForIdle(provider, 90s)` : attend la fin d'une éventuelle génération
  précédente avant de démarrer.
- `clearEditor(provider)` : vide tout texte collé mais jamais envoyé.
- `waitForGenerationStart(provider, previous, prevCount, 25s)` : fail-fast si
  rien ne démarre en 25s (au lieu d'attendre le watchdog).

## 3. Fail-fast sur démarrage

`runJob` attend désormais qu'une bulle `model-response` apparaisse (ou que le
provider passe en état « generating ») dans les 25s ; sinon le job échoue
proprement au lieu de tourner 4 minutes.

## 4. `fresh_chat` par requête

**Avant** : `fresh_chat` était uniquement un réglage global (tout le temps
nouveau chat / jamais).

**Maintenant** (`proxy.py` + extension) : champ optionnel `fresh_chat` dans le
body de `POST /v1/chat/completions`, transporté par le job jusqu'à l'extension
(`freshChat` dans la réponse `/job`). `null/absent` = réglage global.
Cela permet aux clients stateful (ex. NEMESIS) de demander une nouvelle
discussion au premier message d'une session puis de réutiliser la même
discussion pour les suivants (le contexte DOM natif fait le reste).

## Structure livrée

- `proxy.py` — serveur proxy OpenAI-compatible (port 8090 par défaut)
- `extension/` — l'extension browser (manifest v2/v3), avec `providers/*.js`
- `admin.html` / `admin.js` / `chat.html` / … — UI d'admin du proxy
- `tools_format.py`, `chat.py`, `config.json` — helpers

## Rappel d'utilisation

Le proxy injecte `content.js` + `providers/<provider>.js` dans l'onglet ciblé
à chaque job — les modifications dans `extension/` sont prises en compte sans
recharger l'extension depuis `about:debugging` tant que le manifest ne change
pas.

## 5. Copie du code au lieu du message complet (DeepSeek & Z.ai)

**Symptôme** : quand la réponse contient des blocs de code structurés, l'interface
web affiche **deux** boutons « Copier » — un sur chaque bloc de code, et un pour
le message complet. L'automatisation cliquait souvent sur le bouton du *code*
et ne récupérait que le snippet, pas la réponse entière.

**Cause** : `tryClipboardFromCopyButton` prenait le *dernier* bouton matching
`aria-label*="Copy"` / `.ds-icon-button` dans le message, qui est fréquemment
celui d'un bloc `<pre>` / `.md-code-block`.

**Fix** :
- `extension/providers/base.js` : filtrage global des boutons de copie de
  *code-block* (`isCodeBlockCopyButton`) + scoring des candidats
  (`scoreMessageCopyButton`) pour privilégier la barre d'actions du message
  (présence de regenerate / like / share, groupe de 4–5 icônes, label
  « copy response / message », etc.).
- `extension/providers/deepseek.js` : `findMessageCopyButton()` dédié qui
  exclut tout bouton à l'intérieur de `pre` / `.md-code-block` et préfère
  les toolbars AI à 4–5 `.ds-icon-button` (heuristique des exportateurs
  publics DeepSeek).
- `extension/providers/zai.js` : même logique pour chat.z.ai / ChatGLM —
  exclusion des copies intra-code et scoring des boutons d'action message.

Les autres providers (ChatGPT, Claude, Qwen…) bénéficient déjà du filtre
global de `base.js` ; leurs sélecteurs spécifiques restent inchangés.

## 6. Analyse cohérence (2026-09-02) — correctifs supplémentaires

### 6.1 Réponses identiques encore filtrées (tous providers sauf Gemini)
**Cause** : `waitForNewResponse` trouvait bien une nouvelle bulle DOM, puis
appelait `waitUntilStable(..., previous)` qui rejetait `cur === prev` à l'infini.

**Fix** (`base.js`) : dès qu'une nouvelle bulle / fingerprint est détectée,
`previous` est passé à `""` pour que la réponse (même identique) soit livrée.

### 6.2 Permissions manifest incomplètes
`match()` de Z.ai / Kimi / Qwen acceptait des domaines absents de
`host_permissions` et `content_scripts` :
- `chatglm.cn`, `bigmodel.cn` (Z.ai)
- `moonshot.cn` (Kimi)
- `qianwen.com` (Qwen)

**Fix** : domaines ajoutés au `manifest.json`.

### 6.3 `getResponseCount` manquant hors Gemini
`content.js` s'appuie sur `provider.getResponseCount()` pour détecter le démarrage
d'une génération même si le texte est identique. Seul Gemini l'exportait.

**Fix** : `getResponseCount` ajouté sur deepseek, zai, qwen, chatgpt, kimi, claude.


## 7. Version 4.0 — finalisation

- Uniformisation version **4.0 / 4.0.0** (manifest, proxy, UI, extension, README).
- Remplacement des mentions « Firefox » par « navigateur / browser / Chromium ».
- Logos providers : SVG codés en dur → **PNG officiels** (favicon / brand assets
  des sites chat.deepseek.com, chatgpt.com, claude.ai, gemini.google.com,
  kimi.ai, chat.qwen.ai, chat.z.ai), normalisés 128×128 dans
  `icons/providers/*.png`. `admin.js` pointe désormais vers `.png`.


## 8. Copie du message utilisateur au lieu de la réponse IA (tous providers)

**Symptôme** (Gemini notamment) : le clipboard interceptait le bouton « Copy »
du *prompt utilisateur* (`user-query`) au lieu de la barre d'actions de la
réponse modèle (like / dislike / share / copy).

**Cause** : recherche du bouton Copy dans un ancêtre trop large
(`last.parentElement`) qui contenait à la fois `user-query` et `model-response`.

**Fix** :
- `base.js` : exclusion stricte des boutons dans un contexte user ; scoring
  renforcé pour les toolbars assistant ; validation post-clipboard
  `looksLikeUserPrompt(clip, domAssistantText)` qui rejette un payload égal
  au dernier message utilisateur.
- `gemini.js` : extraction limitée à `<model-response>` ; scope d'actions
  calculé sans inclure `user-query` ; garde locale supplémentaire.
- Tous les providers passent `{ domAssistantText }` à
  `tryClipboardFromCopyButton` pour activer le filet de sécurité.


## 9. Vitesse extraction DeepSeek / Gemini / Kimi

**Symptôme** : récupération complète mais très lente (DeepSeek, Kimi) ;
Gemini ne renvoyait souvent que la portion code.

**Cause** : dépendance excessive au bouton Copy (3 tentatives + polling long)
et scope DOM trop étroit sur Gemini.

**Fix** :
- Priorité **React → DOM complet → clipboard (1× max)** sur les 3 providers.
- Clipboard n'est plus le chemin principal.
- Gemini : agrégation de **tout** le contenu de `<model-response>` (prose + code).
- Timings `stableMs` / sleeps réduits (≈1.5s au lieu de 2.4–2.6s).
- Poll clipboard base.js : 5 itérations rapides au lieu de 12.
