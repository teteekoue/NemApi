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

> Depuis la v4.1 le projet est scindé en deux dossiers (voir `README.md`) :

- `proxy/` — serveur FastAPI OpenAI-compatible (port 8090) : `proxy/nemapi/`
  (code), `proxy/web/` (UI d'admin), `proxy/tools/` (`chat.py`, `debug_proxy.py`,
  `bench.py`), `proxy/data/` (config + stats, non versionné)
- `extension/` — l'extension Chromium (manifest v3), avec `providers/*.js`

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

## 6. v4.1 — Réécriture du proxy en FastAPI (2026-10-18)

L'ancien `proxy.py` (≈1300 lignes, `http.server`) est remplacé par le package
modulaire `proxy/nemapi/` (FastAPI + Uvicorn, asyncio). API 100 % compatible
avec l'extension et l'UI existantes. Bugs corrigés au passage :

- **Threads bloqués** : chaque requête bloquait un thread (`Event.wait(240)`,
  `time.sleep(0.5)` en boucle pour attendre l'extension). Remplacé par des
  `asyncio.Future` : aucune attente active.
- **Écriture disque à chaque requête** : `stats.json` était réécrit de façon
  synchrone sur chaque complétion. Désormais en mémoire + flush groupé (2 s) et
  écriture atomique (plus de fichier corrompu en cas de crash).
- **SSE lent** : `time.sleep(0.001)` par morceau de 64 caractères + flush
  unitaire. Désormais envoi groupé : ~4,5× plus rapide sur une réponse de 20 Ko.
- **Extension « déconnectée » pendant un job** : la boucle de l'extension est
  bloquée pendant la navigation fresh-chat (jusqu'à ~60 s) alors que le proxy la
  considérait hors ligne après 8 s → 503 sur les requêtes suivantes. Tolérance
  étendue tant qu'un job est en cours.
- **Onglets fermés jamais oubliés** : une sélection pointant vers un onglet fermé
  restait pour toujours, l'auto-config ne prenait donc jamais le nouvel onglet.
- **Slot provider bloqué à vie** : un job envoyé à l'extension mais jamais
  rendu bloquait son provider. Un « reaper » libère les jobs expirés.
- **Requêtes abandonnées** : si le client se déconnecte avant que son job soit
  envoyé au navigateur, le job est annulé (il ne part plus dans l'interface web).
- **`take()` perdait un cycle de poll (3 s)** quand le premier job de la file avait été annulé.
- **Activer la protection recréait une clé** au lieu de restaurer les clés
  sauvegardées lors de la désactivation.
- **Résolution de modèle** : `claude-opus-4-1`, `gpt-4o-mini`, `glm-4.6`… sont
  désormais reconnus (règles par mot-clé) ; le fuzzy-match « `low in alias` »
  qui envoyait n'importe quel mot court vers DeepSeek est supprimé.
- **Messages invalides** (`messages: ["hi"]`) → 400 propre au lieu d'un crash.
- **Port** : 8090 partout (le README indiquait 8080).


## 7. v4.2 — Contexte incrémental pour les agents (2026-10-18)

**Problème** : les agents renvoient tout l'historique à chaque requête alors que
l'UI web garde la discussion → soit un nouveau chat par requête (lent, tout est
retapé), soit un contexte web doublé à chaque tour (saturation).

**Règle** (réglage `incremental_context`, désactivé par défaut) : une seule
discussion par provider, **jamais de nouvelle discussion** (`freshChat=false`
toujours, le réglage global et le paramètre `fresh_chat` sont ignorés) ; seuls
les messages absents de la discussion sont tapés.

- `utils/history.py` : empreintes normalisées par message, `select_new()`
  (filtrage positionnel des doublons : suite normale, relance, historique
  réécrit/compressé avec ancrage sur notre dernière réponse), lignes du prompt
  système et outils filtrés individuellement, formatage du delta.
- `services/sessions.py` : état de la discussion par provider (vue de l'agent,
  messages présents, réponses, lignes système, outils), persisté dans
  `data/sessions.json`. Décision au dispatch (`Job.on_dispatch`), mise à jour
  à la fin (`Job.on_complete`), réponse tardive (`Job.on_late_result`), réponse
  sans navigateur quand rien n'est nouveau (`Job.local_result`).
- Extension : `content.js` indique si le message a été tapé avant une erreur
  (`typed`), `background.js` le transmet.

**Bugs trouvés pendant les tests et corrigés** :

- *Extension* : les erreurs remontées par `content.js` n'étaient **jamais**
  envoyées au proxy (le message ne contenait pas `provider`) ; le job restait
  bloqué jusqu'au watchdog. Corrigé.
- Une relance identique après une réponse « Done. » répétée était prise pour
  un nouveau message (mauvaise occurrence de la réponse dans la discussion) →
  choix de l'occurrence par correspondance du contexte précédent.
- Après une compression d'historique, un message répété (« continue », sortie
  d'outil identique) pouvait être filtré à tort → même correction + règle de
  secours : la dernière entrée de l'agent est toujours transmise.
- Nouvelle session de code dans une discussion ouverte à la main : l'état de
  la session précédente (persisté) faisait filtrer le prompt système et les
  outils de la 1re requête → une requête sans aucune réponse d'assistant qui ne
  prolonge pas la conversation (et n'est pas une relance < 5 min) démarre une
  nouvelle session : tout est retapé dans la discussion ouverte.
- Relance pendant la génération / après un timeout : la requête attend le job
  en cours ou réutilise la réponse tardive au lieu de retaper le tour.

**Tests** : `tests/test_incremental.py` (agent virtuel avec le SDK `openai`)
et `tests/test_incremental_fuzz.py` (sessions aléatoires ; 80 sessions × 45
tours vérifiées en plus en local) ; `tools/demo_incremental.py` (32 requêtes :
123 256 → 9 761 caractères tapés, une seule discussion).

### 7.1 Vérification finale

- **Page Configuration cassée** (depuis l'ajout du réglage) : `config.html`
  redéclarait `esc()` déjà défini dans `admin.js` → tout le script de la page
  plantait (onglets, réglages, boutons). Corrigé, et couvert par
  `tests/test_web_ui.py` (vrai Chromium, aucune erreur JS sur les 5 pages).
- Interrupteur « Contexte incrémental » écrasé en rond (flex en colonne) → corrigé.
- `sessions.json` corrompu ou mal formé → ignoré proprement (test dédié).
- `requirements.txt` : `pydantic>=2` explicite (le code utilise l'API v2).
- Suite complète validée sous Python 3.9 avec les versions minimales et sous 3.13.

### 7.2 Qwen Code + DeepSeek : appels d'outils et requêtes annexes

Analyse d'une vraie session (PDF de la discussion chat.deepseek.com) :

- **Appels d'outils affichés en texte** : DeepSeek répond parfois dans son format
  natif DSML (`<｜｜DSML｜｜ calls>` … `invoke` … `parameter string="true"`) et laisse
  des balises DSML de fermeture après des `<tool>` valides. Nouveau parseur
  (`utils/tools_format.py`) : DSML sous toutes ses formes, jetons DeepSeek V3,
  Qwen3-Coder XML, `<invoke>`, `<tool>` non fermé, JSON tolérant, conversion des
  arguments selon le schéma de l'outil, nettoyage des balises parasites.
- **Commandes shell corrompues** : `$c" … "$` rendu en formule KaTeX, `\"`
  consommés par le markdown. Cause : les content scripts tournent dans un monde
  isolé et ne voient pas la fibre React → l'extraction « markdown d'origine »
  échouait toujours et on reconstruisait depuis le HTML rendu. Ajout de
  `extension/providers/page_bridge.js` (monde MAIN) + restauration de la source
  TeX des éléments KaTeX/MathJax dans le repli HTML→MD.
- **Contexte incrémental dégradé** : les requêtes annexes de Qwen Code (sélection
  de mémoires, classifieur `shouldBlock`, suggestions, sous-agent d'extraction de
  mémoire) étaient tapées dans la discussion unique : sans réponse d'assistant
  elles passaient pour une « nouvelle session » (prompt complet retapé,
  « System instructions (update) » avec tout le prompt système), et le modèle
  confondait les rôles (réponse à la tâche principale avec le `read_file
  MEMORY.md` du sous-agent). Désormais : requêtes connues → réponse locale
  (`utils/side_queries.py`), autres tâches annexes → « voies » séparées dans la
  même discussion, encadrées, sans toucher l'état principal ; la détection de
  nouvelle session ne concerne plus que les requêtes au profil de la
  conversation principale.
- `select_new` : une vue vide n'est plus traitée comme un préfixe (une nouvelle
  voie annexe ne retape pas l'historique déjà présent).

### 7.3 Le « problème des \n » (backslashes, `$`, liens dans les appels d'outils)

Deuxième session réelle (PDF « Présentation - DeepSeek ») : le modèle n'arrive
pas à corriger un fichier, ses commandes `sed` / `python3 - <<'PYEOF'` échouent
(« option inconnue pour s », `.split('\n')` coupé par un vrai retour à la ligne,
`cp "f.bak"`, `[m.to](https://m.to/)` injecté dans le JS) et il conclut « le shell
mange les backslashes et \n ». Ce n'est pas le shell :

- **Cause 1 — la réponse était encore lue depuis le rendu HTML.** Après un
  rechargement de l'extension, Chrome n'injecte PAS les content scripts du
  manifest dans les onglets déjà ouverts ; `background.js` réinjectait les
  scripts des providers mais pas `page_bridge.js` → pas d'accès au markdown
  d'origine → reconstruction depuis le HTML, où le rendu markdown a consommé une
  couche d'échappement (`\\n` → `\n`, `\"` → `"`, `\/` → `/`), transformé
  `m.to` en lien et `$…$` en formule. Correctif : le pont est injecté (monde
  MAIN) à chaque job ; il marque `<html data-nemapi-bridge="1">`.
- **Cause 2 — rien ne protégeait le JSON quand on lit le rendu.** Le contrat
  demande maintenant d'écrire chaque appel dans un bloc de code
  ` ````tool ` (4 backticks) : un bloc de code est affiché tel quel, donc même
  le repli HTML rend le JSON au caractère près. Le parseur accepte ce bloc
  (avec ou sans `<tool>`, 3 ou 4 backticks / tildes, étiquette « tool » de
  l'en-tête du bloc retirée).
- **Cause 3 — nettoyages destructeurs.** `deepseek.js` supprimait partout les
  mots « json », « edit », « stop », « continue », « copy »… (le `continue;` du
  JS, « package.json », un outil nommé `edit`) — même dans le markdown
  d'origine ; `qwen.js`, `kimi.js`, `zai.js`, `chatgpt.js` avaient des
  suppressions similaires. Le proxy décodait les entités HTML (`&amp;`) et
  supprimait les `<details>` jusque dans le contenu des fichiers. Désormais :
  seules des lignes entières d'interface sont retirées, et jamais dans un bloc
  de code ni dans un appel d'outil (`<tool>`, DSML…).
- Repli HTML→MD : les liens auto-générés (`m.to` → `[m.to](http://m.to)`) sont
  rendus en texte ; l'en-tête d'un bloc de code DeepSeek n'est plus collé au
  code ; les numéros de ligne ne sont retirés que s'ils sont consécutifs (et
  jamais dans un appel d'outil) ; clôture de bloc plus longue si le code
  contient déjà ```.
- Diagnostic : l'extension indique comment elle a lu la réponse
  (`react` = markdown d'origine, `html`/`dom` = rendu). Le proxy l'affiche dans
  le tableau de bord, `GET /status` (`last_extraction`) et un avertissement
  dans les logs (« recharge l'onglet (F5) »).
- Tests : `tests/test_render_roundtrip.py` (vrai rendu markdown-it + linkify +
  KaTeX → Chromium → `htmlToMarkdown` → proxy) reproduit exactement les
  erreurs du PDF sans bloc de code et vérifie la restitution exacte avec ;
  lecture finale DeepSeek dans Chromium (`test_extension_dom.py`).

### 7.4 Réponses longues coupées (gros fichiers écrits par l'agent) — v4.5

Session « Bonsoir - DeepSeek.pdf » (Qwen Code + DeepSeek, `jeu-de-dames.html`) :
pour réécrire un gros fichier, la réponse s'arrêtait au milieu du JSON de
`write_file`. Le fichier écrit était tronqué alors que l'agent recevait
« Successfully overwrote file », puis il tournait en rond.

- **Cause 1 — un appel coupé était exécuté.** La réparation tolérante du JSON
  (`<tool>` jamais fermé) refermait la chaîne et les accolades, si bien que le
  contenu coupé partait comme un vrai appel. Désormais
  `utils/continuation.find_truncated_call` repère un appel inachevé (`<tool>`
  dont le JSON a dû être auto-fermé, paramètre DSML ouvert, bloc ` ````tool `
  avec un JSON incomplet). Un tel appel n'est **jamais exécuté** : l'agent
  reçoit le texte avec une note qui explique ce qui s'est passé.
- **Cause 2 — rien ne demandait la suite.** Quand la réponse est coupée dans un
  appel, le job reste ouvert. Un job « suite » passe en tête de file, **dans la
  même discussion** (`freshChat=false`, même si « nouveau chat » est activé, et
  jamais de nouvelle discussion en mode incrémental) : `[CONTINUE] …` avec la
  fin de la réponse. Le proxy recolle les morceaux en repérant les mots répétés
  (la correspondance la plus proche du début gagne, car le code est
  répétitif ; on peut aussi repartir de zéro ou ne rien répéter). Il fait ça au
  maximum `NEMAPI_MAX_CONTINUATIONS` fois (4 par défaut), puis l'agent reçoit
  un seul résultat complet. Si la suite échoue, la réponse partielle est
  rendue sans être exécutée.
- **Cause 3 — l'extension rendait la main trop tôt.** Les pauses de DeepSeek
  entre deux morceaux d'un gros fichier passaient pour la fin de la réponse,
  avec une limite fixe de 180 s. Maintenant `waitUntilStable` :
  - ne conclut pas tant que le bouton « Stop » est affiché (sauf texte figé
    depuis 60 s) ;
  - exige 25 s de stabilité si un appel ou un bloc de code est encore ouvert ;
  - suit le budget du job envoyé par le proxy (`jobTimeoutMs`).
  Par ailleurs, `content.js` attend la fin de la réponse précédente avant de
  mémoriser « l'ancienne réponse ». Le résultat signale `partial` si
  l'extension a abandonné.
- **Cause 4 — délais trop courts.** `NEMAPI_JOB_TIMEOUT` passe de 240 à 900 s.
  Qwen Code abandonne une connexion sans en-têtes après 120 s, puis sans chunk
  après 240 s. Une requête en streaming qui n'a pas de réponse après 8 s
  reçoit donc tout de suite les en-têtes SSE, puis un chunk vide toutes les
  15 s. Une erreur survenue après les en-têtes est envoyée dans le flux
  (`data: {"error": …}`), et le SDK OpenAI la remonte comme une erreur.
- **Tests :** `tests/test_continuation.py` couvre la détection, le recollage
  (chevauchement, redémarrage, rien de répété), la file de jobs (suite en tête,
  même discussion, annulation en cascade, échec), un agent virtuel qui écrit un
  gros fichier à travers une limite de sortie de 700 caractères (avec et sans
  contexte incrémental, fichier identique à l'octet près) et le keep-alive SSE.
  `test_extension_dom.py` vérifie dans Chromium l'attente « occupé » et
  « appel ouvert ».

## 10. v5.0.0 — Intégration agents IA et nouvelle interface (2026-10-18)

- **Nouvel onglet « Intégration agents IA »** (`/integrations`, menu ☰).
  Une carte par agent. Pour l'instant il n'y a que Qwen Code. Chaque
  intégration est **désactivée par défaut** et enregistrée dans
  `config.json → integrations`.
- **Intégration désactivée = NemApi classique (v4.0).** Le réglage
  `fresh_chat` est utilisé et tout le prompt est tapé à chaque requête. Les
  requêtes annexes vont au navigateur. Il n'y a ni suite automatique ni
  keep-alive.
- **Intégration activée.** Elle ne s'applique qu'aux requêtes de l'agent, et
  ses quatre fonctions peuvent être activées une à une :
  - contexte incrémental ;
  - requêtes annexes en local ;
  - suite automatique des réponses coupées ;
  - connexion maintenue (keep-alive SSE).
- **Détection à chaque requête**, dans cet ordre :
  1. l'en-tête `X-NemApi-Agent` (une autre valeur force le mode classique) ;
  2. le `User-Agent` `QwenCode/…` (envoyé par `DefaultOpenAICompatibleProvider.buildHeaders()` de qwen-code) ;
  3. le prompt système « You are Qwen Code ».
  Le chemin de détection utilisé et le mode appliqué à chaque client sont
  visibles sur la page.
- **Ce qui reste actif pour tous :**
  - le parseur d'appels d'outils ;
  - le contrat ` ````tool ` ;
  - la robustesse de l'extension ;
  - le garde-fou `find_truncated_call`, qui interdit d'exécuter un appel
    coupé. Sans la suite automatique, la réponse partielle est rendue en
    texte.
- **Configuration.** Le réglage « Contexte incrémental » est retiré.
  `POST /settings` ne renvoie plus que `stream_enabled`, `auto_config`,
  `fresh_chat` et `premium_md`. Les anciennes clés `incremental_context` et
  `side_queries_local` sont ignorées et ne sont pas migrées.
- **Nouvelle API :**
  - `GET /agent-integrations` ;
  - `POST /agent-integrations/{id}` avec `{enabled?, features?}`. Un id
    inconnu renvoie 404. Le changement d'état du contexte incrémental
    réinitialise la session ;
  - `/health` et `/status` gagnent `app_version` ;
  - `/status` gagne aussi `integrations`.
- **Interface web refaite :**
  - un seul `admin.css` (thèmes sombre et clair, mobile) ;
  - un `admin.js` commun qui fournit le menu, la barre du haut, les
    notifications, une fenêtre de confirmation à la place de
    `alert`/`confirm`, une requête `fetch` avec délai, le bandeau
    « proxy injoignable » et les badges du menu ;
  - des rafraîchissements qui ne réécrivent le DOM que s'il a changé, ce qui
    évite le clignotement et la perte du focus ;
  - le chat de test gère les erreurs envoyées dans le flux SSE.
- **Tests :**
  - `tests/test_integrations.py` (9 tests) ;
  - `tests/test_web_ui.py` réécrit (toutes les pages, aucune boîte native,
    mobile, thème, intégration, chat, analytics, clés) ;
  - les outils `virtual_agent.configure()`, `demo_incremental.py` et
    `chat.py` sont adaptés.
  Résultat : 159 tests passent.
