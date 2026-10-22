# AGENTS.md — NemApi

Notes de travail pour les agents et le développeur : **ce qu'il faut savoir
avant de toucher au code**, et pourquoi le code est comme il est.

> Le `README.md` est la documentation d'usage (installation, configuration, API).
> Ce fichier est le companion technique : invariants, pièges, et décisions
> prises. Les deux se complètent, ils ne se remplacent pas.

---

## 1. Le projet en une minute

NemApi est une **passerelle locale compatible OpenAI** qui pilote les
interfaces web des LLM (DeepSeek, Qwen, Claude, Gemini, ChatGPT, Kimi, Z.ai)
via une extension Chromium. Aucune clé API fournisseur : l'extension tape dans
le chat web et relit la réponse dans le DOM.

Deux moitiés, aucune dépendance entre elles au-delà du HTTP :

```
extension/  ──(HTTP /job, /result)──▶  proxy/
  (Chromium)                             (FastAPI)
```

Le client (Qwen Code, Cline, curl…) ne voit qu'un endpoint OpenAI. Le proxy
met en file, l'extension exécute.

**Langue :** le code, les logs, les identifiants et les commentaires sont en
anglais. La documentation et les échanges sont en français.

---

## 2. Le trajet d'une requête

```
POST /v1/chat/completions
  │
  ├─ prepare()          résout le provider depuis le nom du modèle,
  │                     construit le prompt, détecte l'agent de code,
  │                     extrait les tool_calls et les images
  │
  ├─ jobs.create()      file FIFO PAR PROVIDER (1 job en vol par provider,
  │                     parallèle entre providers), dédup 2,5 s,
  │                     Future asyncio
  │
  ├─ extension          GET /job?wait=20 (long-poll) → injecte base.js +
  │                     providers/<x>.js → tape dans la page → extrait
  │                     la réponse → POST /result
  │
  └─ postprocess()      parse les tool_calls, renvoie du JSON OpenAI ou du SSE
```

Trois entrées typiques : `/v1/chat/completions` (le client),
`GET /job` + `POST /result` (l'extension), et l'interface web
(`proxy/web/`) qui pilote la configuration.

---

## 3. Invariants à ne pas casser

1. **Un appel d'outils coupé n'est JAMAIS exécuté.** Si la réponse s'arrête
   au milieu d'un appel, le proxy le détecte (`utils/continuation.py`), le
   marque « non exécuté » et **relance une continuation dans la même
   discussion**. Le résultat est recousu. C'est la garantie la plus
   importante du projet : un agent qui croit avoir écrit un fichier alors
   que le fichier est tronqué est pire qu'un échec franc.
2. **La discussion web est persistante par défaut.** NemApi ne rouvre plus
   l'URL du chat à chaque requête : `fresh_chat` est `False` par défaut, pour
   tous les clients (le mode « Chat test » de l'interface comme les agents).
   L'intégration agent (contexte incrémental, fonctions agent) reste
   **opt-in**, par fournisseur et par agent : désactivée, NemApi retrouve le
   comportement de la v4.0 *sauf* le rafraîchissement d'URL, qui n'a plus
   lieu. Le réglage global « Fresh-chat » peut réactiver l'ancien
   comportement s'il est explicitement activé.
3. **Un seul job en vol par provider.** Le proxy sérialise par provider ;
   l'extension refuse un second job pour le même provider. Ne jamais
   contourner cette règle : deux onglets sur le même chat web s'entrelacent.
4. **Le base64 d'une image n'est jamais tapé en texte.** Il passe par
   l'`input[type=file]`. Le prompt ne contient que le texte.
5. **Les URL d'image distantes sont refusées** (400 `invalid_image`) :
   l'extension ne doit jamais aller chercher une URL arbitraire avec les
   cookies du fournisseur.

---

## 4. Sous-systèmes sensibles

### 4.1 Le composer du navigateur

**C'est le point le plus fragile de tout le projet.** Écrire dans un
champ contrôlé est un acte silencieux : si l'application rejette l'écriture,
rien ne le signale — le texte semble écrit, puis disparaît au rendu suivant.

**Chaque provider a son moteur d'édition, et un seul chemin d'écriture ne
convient qu'à un seul d'entre eux :**

| Provider | Composer | Moteur | Chemin d'écriture |
|---|---|---|---|
| DeepSeek | `div[contenteditable]` | Lexical | `execCommand("insertText")` |
| Qwen | `textarea#chat-input` | React/textarea | setter du prototype + insertion native |
| Claude | `div.ProseMirror` | ProseMirror | `execCommand("insertText")` |
| ChatGPT | `div#prompt-textarea` | ProseMirror | `execCommand("insertText")` |
| Gemini | `div.ql-editor` | **Quill** | **`quill.setText()` via le pont** |
| Kimi | `div.chat-input-editor` | Lexical | `execCommand("insertText")` |

`base.js:setTextareaValue` distingue ces cas ; `base.js:sendWithProof`
essaie chaque surface candidate jusqu'à ce qu'une prenne le texte.

Règles qui découlent des bugs déjà payés :

- **Écrire dans le bon type d'élément.** Un `contenteditable` et un
  `textarea` n'acceptent pas les mêmes opérations : appeler le setter de
  `HTMLTextAreaElement.prototype` sur un `div` lève `Illegal invocation`
  (avalée en silence par le `try/catch` → le texte n'arrive nulle part).
- **Pour un `contenteditable`, seul `document.execCommand("insertText")`
  fonctionne.** C'est une vraie insertion navigateur, qui émet le couple
  `beforeinput`/`input` que l'éditeur attend. Affecter `textContent` suffit
  pour que l'écran affiche le texte, pas pour que l'application le prenne.
- **Pour un `textarea` piloté par React, passer par le setter du prototype**,
  pas par `el.value = x` : l'affectation directe met aussi à jour le
  *value tracker* de React, qui en déduit « pas de changement » et ignore
  l'événement.
- **Pour Quill, écrire le MODÈLE, pas le DOM.** Quill v1 attache
  l'instance au conteneur : `container.__quill = this` (`core/quill.js`).
  `quill.setText(text, "user")` met à jour le modèle et émet l'événement que
  l'application écoute. La source `"user"` (celle d'un vrai collage) est
  indispensable : une application qui ne réagit qu'aux saisies utilisateur
  ignore une écriture `"api"`.
- **L'instance Quill est invisible depuis le monde isolé.** `__quill` est
  un *expando* : il appartient au realm JS qui l'a posé, et Chrome donne un
  realm distinct au monde isolé et au monde page. L'écriture Quill passe
  donc par le pont (`nemapi:set-quill` dans `page_bridge.js`), qui répond
  par un attribut DOM.
- **La preuve que l'application a pris le texte n'est pas la valeur DOM.**
  C'est l'**activation du bouton Envoyer** : l'application ne l'active que
  si le composer contient réellement le texte. `base.js:sendWithProof`
  réécrit le prompt tant que ce signal n'est pas là.
- **`findSend` ne doit pas filtrer les boutons désactivés.** C'est
  l'appelant qui attend l'activation ; filtrer le bouton désactivé revient
  à ne jamais le trouver.
- **`findComposerRoot` doit partir AU-DESSUS du champ.** Un champ dont la
  classe contient « input » (`message-input-textarea`, `chat-input-editor`)
  passe le filtre `[class*='input']` et désigne à tort le composer : le
  bouton Envoyer est un **frère** du champ, pas un descendant. C'est ainsi
  que Qwen et Kimi n'envoyaient qu'`Enter`, sans jamais cliquer.
- **Le caret doit être en fin de champ.** Un `el.click()` en début de
  sélection détruit la sélection : le texte s'insère alors au début du champ
  au lieu de le remplacer.
- **Vider le composer avant d'écrire.** Un éditeur riche **insère au
  caret** : sans vidage, le prompt s'ajoute au brouillon restant (Kimi
  restaure même un brouillon depuis `localStorage`) et c'est le tout qui
  part. `selectAll` + `delete` est la voie normale ; elle échoue sur
  certaines pages (Kimi `/agent` sélectionne mais ne supprime pas), d'où le
  repli DOM.
- **La vérification du composer est une ÉGALITÉ, pas un « contient ».**
  Un brouillon + notre texte « contient » notre texte, mais le message
  envoyé serait la concaténation des deux.
- **Le composer peut vivre dans un shadow root.** Le contenu d'un custom
  element est invisible à `document.querySelectorAll`. `inputCandidatesFrom`
  ne cherche dans les shadow roots ouverts qu'**après** un échec de la
  recherche directe : le chemin normal ne coûte rien.
- **Un `.ql-clipboard` est aussi `contenteditable`** (Gemini) : ne jamais
  l'écrire. Les sélecteurs du composer l'excluent explicitement.
- **L'état du bouton Envoyer n'est pas le même partout** : attribut
  `disabled` (Qwen, Claude, ChatGPT), classe `disabled` sur un `<div>`
  (Kimi — le contrôle n'est même pas un `<button>`), `aria-disabled` +
  `mat-mdc-button-disabled` (Gemini). Chaque provider passe sa propre règle
  à `sendWithProof`.
- **Le bouton Stop de ChatGPT clignote** entre deux lots de tokens : le
  signal fiable est la **barre d'actions du tour** (copier, j'aime…), qui
  n'apparaît qu'à la fin du message. Sur Gemini, à l'inverse, le bouton
  d'envoi **devient** le bouton « Stop response » au moment de l'envoi :
  c'est le signal « c'est parti ».

Les sélecteurs ci-dessus ne sont pas devinés : ils viennent de projets
open-source qui pilotent réellement ces sites. En cas de doute, vérifier la
structure vivante avant d'inventer un sélecteur.

**Sources vérifiées** (structure DOM relevée sur les sites réels) :

| Provider | Source | Ce qu'elle apporte |
|---|---|---|
| DeepSeek | `nezumi0627/deepseek-web-harness` (`server/browser.js`) | `composer()` (textarea vs contenteditable), `sendPrompt()` (fill vs insertText), `.ds-assistant-message-main-content` |
| Qwen | `rakaarwaky/qwen-web-arwaky`, `devcrax/qwen-web` | `textarea.message-input-textarea`, `.message-input-right-button-send button`, `.qwen-markdown` |
| Claude | `timothy22000/claude-autocontinue` (`extension/content.js`) | `div[contenteditable="true"][data-placeholder]`, `.ProseMirror`, `execCommand('insertText')` = seule voie fiable |
| Gemini | `Vineetpandey0/Context-Sync` (`injectors/gemini.js`) | `<rich-textarea class="ql-container ql-bubble"> > div.ql-editor`, `button[aria-label="Send message"]`, `model-response` |
| ChatGPT | `Octo-Lex/ChatGPT-Web2API` (`chatgpt_dom.py`) | `div[role="textbox"]#prompt-textarea`, le `textarea#prompt-textarea` est un fallback **caché**, `data-testid="stop-button"` |
| Kimi | `ops120/kimi-brain` (`references/site-map.md`) | `div.chat-input-editor[data-lexical-editor="true"]`, `div.send-button-container`, 2 blocs markdown dont le 1er = la pensée |
| Quill | `slab/quill` v1.3.7 (`core/quill.js`) | `container.__quill = this`, `setText(text, source)`, `modify()` refuse `source:USER` en mode strict |

### 4.2 La détection de fin de réponse

`base.js:waitUntilStable` applique deux règles :

- **La stabilité du texte est la source de vérité**, pas le drapeau
  « en train de générer » du site. Un site dont l'indicateur reste affiché
  (ou dont un faux positif s'y glisse) ajoutait autrefois 60 s à *chaque*
  requête : la réponse était là, le proxy ne la renvoyait pas.
- **Une réponse « à l'appel ouvert » n'est un problème que si le site génère
  encore.** Si la page est idle, la réponse est prise après `stableMs` : c'est
  le proxy qui détecte un appel coupé et le reprend (§3.1). Attendre 25 s
  « pour être sûr » était la première source de latence du projet.

Détection de « en train de générer » : **stricte et locale**. Un motif
`[class*='loading']` ou `[class*='stop']` appliqué au document entier trouve
des éléments sans rapport (squelette, image paresseuse) et garde la page
« occupée » indéfiniment. Chercher le bouton Stop par **préfixe de label**,
et les spinners **dans** le composer ou dans la dernière réponse.

**Coût de l'extraction :** le polling doit rester **bon marché**. La
conversion markdown complète (clone DOM + `innerText` + regex) ne doit pas
tourner à chaque tick : elle coûte en O(longueur de la réponse). Polling sur
une lecture brute (`readCheapText`), conversion **une fois**, à la fin.

### 4.3 Multimodal — les images (v5.1, étendu aux 7 fournisseurs en v5.2)

**État :** le bug historique est corrigé et validé sur le vrai
chat.deepseek.com (vérifié par l'utilisateur le 2026-10-21). En v5.2, le
même mécanisme est étendu aux 6 autres fournisseurs.

Le bug historique : *« l'image s'uploade bien, mais le texte qui l'accompagne
n'est jamais saisi, et le bouton Envoyer n'est jamais pressé ».* Deux causes
racines distinctes, toutes deux **silencieuses** :

1. **On écrivait dans le mauvais élément** (cf. §4.1) : le setter de
   `HTMLTextAreaElement` était appelé sur un `div[contenteditable]`.
2. **L'attente de fin d'upload ne s'exécutait pas** : elle devinait le nom de
   classe de l'aperçu (`ds-animated-size-item`), qui ne correspond pas au
   build courant. Le code concluait « pas d'upload », tapait le prompt
   **pendant** l'upload, et le re-render final de l'application effaçait le
   texte. Le bouton Envoyer restait désactivé → rien n'était envoyé.

**Le bon témoin de l'upload est le `input[type=file]` rempli par le
navigateur** — pas le nom d'une classe CSS, pas un aperçu `blob:`. Un
aperçu peut être un `canvas` ou déjà une URL CDN.

**Le bon moyen de savoir que le prompt est parti : le bouton Envoyer
s'active** et le texte survit. D'où la boucle de réécriture : tant que le
bouton n'est pas actif, le prompt est réécrit.

**Mécanisme générique (`base.js:attachFilesToPage`)** — c'est lui qu'il faut
utiliser pour tout nouveau fournisseur :

1. `findFileInput(selectors)` : le `<input type=file>` de la page. Il est
   presque toujours **caché** (`display:none`) : la visibilité n'est donc
   pas un critère. Sélecteurs du provider d'abord, `input[type=file]` en
   dernier recours, puis les **shadow roots ouverts** (le contenu d'un
   custom element est invisible à `document.querySelectorAll`).
   - Un `[capture]` est **exclu** : c'est l'entrée « appareil photo », pas la
     galerie. ChatGPT en a quatre (photo / image+vidéo / images / tout
     fichier) et la première dans le DOM est celle de la photo : un
     `input[type=file]` nu atterrit dessus et l'upload est perdu sans un mot.
   - Un `[disabled]` est exclu aussi.
2. Si aucun input n'est monté, le bouton d'attache est cliqué
   (`openAttach`) — **uniquement si le bouton ouvre un MENU**, jamais le
   sélecteur natif : une boîte de dialogue native bloque la page.
3. `attachFiles(input, files)` : le navigateur remplit `input.files` via un
   `DataTransfer`, puis les événements `input` + `change` sont émis.
4. `waitUploadDone(input)` : attente **bornée** (25 s) de la fin de
   l'upload (l'input garde les fichiers, plus de spinner dans la zone du
   composer). Jamais d'attente « DOM calme » : une page qui met à jour un
   compteur n'est jamais calme, et l'attente a coûté 90 s pour rien.

**Un transport ne vaut QUE s'il est prouvé.** Remplir `input.files` depuis
JS n'est PAS une preuve : c'est nous qui l'avons rempli. Une application
qui ignore les événements dispatchés (le cas de ChatGLM) laisse l'image
nulle part, et la requête enverrait un prompt dont l'image manque en
silence — un échec franc vaut mieux. `waitUploadFinished` exige donc une
preuve (aperçu, nom de fichier sur la pastille, input vidé par l'app) **et**
l'absence de spinner avant de laisser passer le prompt.

Contraintes du format (à respecter) :

| Limite | Valeur |
|---|---|
| Taille max par image | 20 Mo |
| Nombre max d'images par requête | 8 |
| Format d'entrée | data URL base64 uniquement |
| Portage des images | **uniquement le dernier message `user`** |

Le portage de l'image est une décision d'architecture : la discussion web
contient déjà les images des tours précédents, les renvoyer alourdirait le
prompt sans gain.

**Piège connu :** ne jamais attendre « que le DOM devienne calme » pour
savoir qu'un upload est fini. Une page qui met à jour un compteur ou un
spinner ne devient jamais calme ; cette attente a coûté 90 s pour rien. La
preuve par le bouton Envoyer est instantanée.

#### 4.3.1 Le prompt doit attendre la fin de l'upload

`base.js:waitUploadFinished` est l'étape qui manquait à l'extension v5.2.

Une application qui **re-render son composer à la fin du transfert** efface
le prompt tapé pendant l'upload (l'image reste, le texte disparaît) et le
bouton Envoyer reste désactivé : la requête pend jusqu'au watchdog. Le
prompt doit donc attendre que l'upload soit **terminé**, pas seulement
*démarré*.

Le mécanisme :

1. **La page a-t-elle pris le fichier ?** Un aperçu apparaît (image
   `blob:`/`data:`), ou le nom du fichier s'affiche sur la pastille, ou
   l'input a été vidé par l'application (elle a consommé le fichier).
2. **Le transfert est-il terminé ?** Plus de spinner, et la zone
   d'attachement ne bouge plus.

Borné (30 s) et jamais « attends que le DOM se calme ». Un site qui
n'affiche ni aperçu ni spinner est lâché au bout de `quietMs` (8 s) : la
boucle de réécriture du prompt reste le filet de sécurité.

**Différence avec DeepSeek** : DeepSeek a son propre `attachFiles`
(validation manuelle sur le vrai site), il utilise le même
`waitUploadFinished` mais avec `probe: attachmentShown` — son aperçu peut
être un `canvas` ou déjà une URL CDN, la preuve est donc le DOM autour du
champ, pas l'image.

**Le test de régression** :
`tests/test_providers_attachments.py::test_the_prompt_is_typed_after_the_upload_not_during_it`
rejoue une fausse app qui prend le fichier **asynchrone** (2,5 s) et
re-render son composer à la fin ; elle note que le composer contenait du
texte à ce moment-là. Sans l'attente, ce marqueur est à `1` : le prompt a
été tapé pendant l'upload.

#### 4.3.2 Les sélecteurs vérifiés en ligne (2026-10)

Les sélecteurs ci-dessous ne sont pas devinés : ils viennent de projets
open-source qui pilotent réellement ces sites, et de captures du DOM réel.

| Provider | Input de fichier | Bouton d'attache | Menu | Source |
|---|---|---|---|---|
| DeepSeek | `input[type=file]` à côté du bouton d'attache | — | — | `nezumi0627/deepseek-web-harness` |
| ChatGPT | `form input[type=file][accept='image/*'][multiple]:not([capture])`, `input[type=file][data-testid='file-upload-input']` | `button[data-testid="composer-plus-btn"]` | « Add photos & files » | `yatuk/tamga` (capture 2026-10-11), `lidge-ai/cli-jaw`, `pine2D/polyask` |
| Claude | `input[data-testid='file-upload']`, `input#chat-input-file-upload-onpage` | `button[aria-label="Add files, connectors, and more"]` | « Add files or photos » | `yatuk/tamga`, `pine2D/polyask`, `GautamVhavle/CatGPT-Gateway` |
| Gemini | `images-files-uploader input.hidden-file-input[type=file]` (**monté à l'ouverture du menu**) | `button[aria-label="Upload & tools"]`, `button[aria-label="Open upload file menu"]` | `button[data-test-id="local-images-files-uploader-button"]` | `jerrychen751/LangQueue`, `lumingya/universal-web-api` |
| Qwen | `input#filesUpload[type=file][multiple]` (**persistant**, dans `.message-input-container`) | `.mode-select-open` | « Upload attachment » / « 上传附件 » | `yatuk/tamga` (capture 2026-10-18), `devcrax/qwen-web`, `rakaarwaky/qwen-web-arwaky` |
| Kimi | `.chat-editor-action input.hidden-input[type=file]` | `.toolkit-trigger-btn` | inconnu (confiance faible) | `yatuk/tamga`, `pine2D/polyask` |
| Z.ai (`chat.z.ai`) | `.messageInputContainer input[type=file]` | `button#upload-file-button` | inconnu | `yatuk/tamga`, `srbhptl39/MCP-SuperAssistant` |
| Z.ai (`chatglm.cn`) | `.upload-demo input.el-upload__input[type=file]` | — | — | `pine2D/polyask` |

Trois déductions à ne pas oublier :

- **`#filesUpload` n'est PAS mort** sur Qwen (il est persistant) : la
  croyance inverse circulait dans le code.
- **Les `aria-label` sont TRADUITS** sur le site réel (« Dosya ekle »,
  « 上传附件 »). Les ancres `data-testid` / `id` sont les seules stables :
  `button[data-testid="composer-plus-btn"]` pour ChatGPT,
  `.toolkit-trigger-btn` pour Kimi, `button#upload-file-button` pour Z.ai.
- **`chatglm.cn` et `chat.z.ai` n'ont pas le même front-end.** Sur
  `chatglm.cn`, l'input est un upload Element-Plus qui **ignore** les
  événements `input`/`change` dispatchés : le chemin JS ne peut pas y
  aboutir. C'est pourquoi `zai.js` distingue les deux sites par
  `location.hostname`, et pourquoi l'échec doit être **explicite** plutôt
  qu'un prompt envoyé sans son image.

Le transport « drop » est le plus fiable sur Gemini (`rich-textarea`,
`.input-area`) et sur Qwen (`#dropzone-container`, `.message-input-container`) ;
il est inutilisé ailleurs. Le transport « paste » (synthétique, sur le
composer — **pas** sur `document.body`) reste le repli des sites sans input.

### 4.4 Les intégrations d'agents

`services/integrations.py` décrit un **profil par agent** :

| Champ | Rôle |
|---|---|
| `user_agents` | préfixes de `User-Agent` (Qwen Code : `QwenCode/`) |
| `prompt_markers` | fragments du prompt système de l'agent |
| `features` | les fonctions de l'intégration, chacune activable seule |
| `header` | la valeur de `X-NemApi-Agent` que l'agent doit envoyer |
| `native_tools_only` | l'agent n'envoie jamais de tableau `tools` (Nemesis) |
| `snippet` | la configuration à copier, `{base}` = l'URL de base de NemApi |

La détection est faite **à chaque requête**, dans cet ordre : en-tête
`X-NemApi-Agent` (prioritaire, une autre valeur force le mode classique) →
`User-Agent` → prompt système → **discussion attribuée**. Elle ne
s'applique qu'aux requêtes de cet agent : les autres clients (Chat test,
scripts) restent en mode classique.

**Le corps de la requête est strictement OpenAI** (`schemas.py`) : `model`,
`messages`, `tools`/`functions`, `stream`. Ni `fresh_chat` ni `provider` —
un client qui les envoie est **ignoré, pas obéi**. Le fournisseur est déduit
du `model`. C'est un invariant : une variable de comportement dans le corps
lierait chaque client à un dialecte NemApi et casserait le « un protocole
pour tout le monde ».

**Qui ouvre une nouvelle discussion ?** Jamais le client. Le proxy décide :
`fresh_chat` vaut `False` par défaut (persistance de la discussion, pour tous
les clients) ; une intégration qui n'a ni `persistent_chat` ni
`incremental_context` laisse le réglage global décider, et le réglage global
« Fresh-chat » peut forcer l'ouverture d'une nouvelle URL s'il est activé.

**Qwen Code** — les trois points bloquants sont trois fonctions séparées,
chacune activable seule :

| Fonction | Rôle |
|---|---|
| `persistent_chat` | plus de nouvelle URL : la discussion ne se rouvre jamais |
| `incremental_context` | le contexte est géré dans la discussion (§4.6) |
| `tool_calls` | le tableau `tools` OpenAI est transmis au modèle avec son contrat, la réponse revient en `tool_calls` |

plus `side_queries_local`, `continuation` et `keepalive`.

**Nemesis** ([NEMESIS-AGENT](https://github.com/teteekoue/NEMESIS-AGENT)) :
`persistent_chat` (la requête n'ouvre jamais une nouvelle discussion) et
`native_tool_calls` (les appels d'outils de l'agent deviennent des
`tool_calls` OpenAI, et le contrat d'outils de NemApi n'est **pas** injecté :
l'agent a le sien).

Quatre détails qui ne se devinent pas :

- **Nemesis fusionne son prompt système dans le premier message
  `user`** (« `[SYSTEM INSTRUCTIONS]` … »), pas dans un message `system` :
  la détection des marqueurs doit donc regarder le **premier message quel
  que soit son rôle**.
- **Nemesis n'envoie pas de liste d'outils** : le parseur ne peut donc pas
  vérifier le nom d'un appel. `parse_tool_calls(..., generic=True)` (fonction
  « native_tool_calls ») accepte alors un bloc JSON dont la forme est
  explicite (`tool` / `action` / `tool_name` + un objet d'arguments). La
  barre est haute exprès : une réponse contient souvent un extrait JSON, et
  un extrait ne doit jamais être avalé comme un appel.
- **Nemesis n'envoie son prompt système qu'une fois.** Le marqueur ne
  permet donc de le reconnaître qu'au **premier** tour ; ensuite il reviendrait
  au mode classique et rouvrirait une discussion vide à chaque requête.
  D'où les deux parades : l'**en-tête** `X-NemApi-Agent: nemesis` (recommandé,
  patch client dans `docs/nemesis-nemapi.patch`) et la **discussion attribuée**.
- **La discussion attribuée** (`config.discussion_owners`) : un agent qui
  ne rouvre jamais de discussion en garde la propriété. Les requêtes
  suivantes, sans signal, lui sont attribuées — sauf si elles portent un
  tableau `tools` (`native_tools_only`), ce qui trahirait un autre client.

Pas de contexte incrémental pour Nemesis : il n'envoie que le message
courant, son contexte est de son côté.

### 4.5 Le parseur d'appels d'outils

`utils/tools_format.py` (~900 l.) est le point de concentration de
complexité : il doit comprendre le format ````tool` (fence ```` + ligne
`<tool>{json}</tool>`), le DSML de DeepSeek, le XML de Qwen3-Coder, les
`<invoke>`, et le JSON tolérant.

Règle non négociable : **le texte restant ne doit jamais contenir de
résidus d'appel d'outils** (ni fence, ni `<tool>`, ni tag DSML). L'agent
reçoit du prose + des `tool_calls` structurés, jamais les deux mélangés.

Un appel dont le nom est inconnu **n'est pas exécuté** et reste visible dans
le texte.

### 4.6 Le contexte incrémental

`utils/history.py` + `services/sessions.py` : en mode agent, seul ce que la
discussion ne contient **pas encore** est tapé dans la page web. La dédup
est **positionnelle** (comparaison de l'historique et de la vue), pas par
similarité de texte : un message utilisateur identique légitime (« continue »
tapé deux fois) doit être renvoyé deux fois, une reprise identique ne doit
l'être pas.

### 4.7 L'interface web et les tarifs de référence

`proxy/web/` : HTML + CSS + JS, **sans build ni dépendance**. C'est un
choix : le proxy est local, il doit démarrer sans `npm install` et sans
réseau.

- **`admin.css`** est le design system : jetons (couleur, espace, rayon,
  ombre, mouvement) d'abord, puis le shell, puis les composants. Un
  composant qui n'utilise pas les jetons est une dette.
- **`admin.js`** est le shell commun (menu, barre du haut, `api()`, `poll()`,
  toasts, modales) plus quelques helpers de rendu (`areaChart()`).
- **Toute icône SVG porte la classe `ico`**, qui porte la taille par
  défaut. Un SVG sans largeur occupe son conteneur : c'est le bug qui a
  affiché des icônes de 200 px sur le tableau de bord.
- Le thème clair et le thème sombre sont **définis ensemble** (jamais l'un
  après l'autre) : le contraste se vérifie dans les deux.
- `prefers-reduced-motion` coupe les animations.

**Les tarifs de référence** (`core/providers.py:TOKEN_PRICING`) sont les
prix API publics vérifiés sur les pages tarifaires officielles
(`PRICING_SOURCES`). Ils servent à estimer le « coût équivalent » de
l'usage — le pont local, lui, est gratuit. Un tarif qui n'est pas vérifié
est une faute : la date de vérification (`PRICING_CHECKED_AT`) est affichée
dans la page Analytics, elle doit être mise à jour avec les prix.

---

## 5. Le port

Le port **officiel** du projet est **8090** (`http://127.0.0.1:8090/v1`).
La source de vérité est `proxy/nemapi/core/settings.py:DEFAULT_PORT` — tout
le reste en dérive.

> Historique : le port a longtemps été.codé en dur à 8090 dans l'extension et
> les outils, alors que le proxy tournait sur 8081 (8090 occupé). Les outils
> (`proxy/tools/*.py`) et le `--port` de `run.py` **dérivent maintenant** de
> `settings.py` : ils ne peuvent plus diverger.

Si le port change un jour, voici **tous** les emplacements à mettre à jour
ensemble — une seule source de vérité, mais l'extension est compilée à part :

| Fichier | Contenu |
|---|---|
| `proxy/nemapi/core/settings.py` | `DEFAULT_PORT` — **la source de vérité** |
| `extension/background.js` | `const PROXY = "http://127.0.0.1:<port>"` (ligne 4) et l'URL de l'admin (ligne 541) |
| `extension/manifest.json` | `host_permissions` — `http://127.0.0.1:<port>/*` et `http://localhost:<port>/*` |
| `proxy/web/admin.html` | endpoint affiché dans le tableau de bord |
| `proxy/nemapi/services/integrations.py` | config Qwen Code proposée à l'utilisateur |
| `README.md` | toutes les URLs |

Après changement : **recharger l'extension** dans `chrome://extensions` (les
`host_permissions` sont figées à l'installation) et redémarrer le proxy.

---

## 6. Tests

```bash
cd proxy
venv/bin/python -m pytest -q          # suite complète (~10 min)
venv/bin/python -m pytest -q --durations=10
venv/bin/python -m pytest tests/test_extension_dom.py -q   # extension + Playwright
venv/bin/python -m pytest tests/test_providers_attachments.py -q   # upload d'image, 7 fournisseurs
```

État actuel : **268 tests passés, 0 échec, 0 ignoré**.

Les tests de l'extension tournent dans un **vrai Chromium** (Playwright) via
CDP, dans deux mondes : `base.js` dans un monde isolé, `page_bridge.js`
dans le monde page. C'est ce harnais qui permet de reproduire fidèlement
les bugs d'écriture (§4.1, §4.3) — un mock ne reproduirait pas le wipe au
re-render.

`tests/test_providers_attachments.py` rejoue l'upload d'image sur les 7
fournisseurs : la fausse app garde son propre état, n'accepte le fichier que
si le **navigateur** a rempli son `input[type=file]`, et n'envoie le message
que si le texte ET l'image sont présents. Trois scénarios par fournisseur :

| Test | Ce qu'il vérifie |
|---|---|
| `test_the_image_reaches_the_app_and_the_text_is_sent_with_it` | le fichier atteint l'app **par le bon input** (une page peut en avoir plusieurs), le texte est tapé, le message porte les deux |
| `test_the_prompt_is_typed_after_the_upload_not_during_it` | le prompt n'est **pas** tapé pendant l'upload (le re-render le wipes) — régression §4.3.1 |
| `test_a_page_that_ignores_the_file_events_does_not_report_a_success` | une app qui ignore les événements ne doit **pas** faire croire que l'image est partie |

Le DOM de chaque scénario est celui du vrai site (§4.3.2) : un sélecteur
qui dérive est attrapé ici. `tests/test_nemesis_integration.py` vérifie le
profil NEMESIS (détection, discussion unique, appels d'outils natifs,
absence de contexte incrémental) **et le trajet du vrai client** : le
premier tour est reconnu par le prompt système, les suivants par la
discussion attribuée, une requête qui porte un tableau `tools` n'hérite
pas du mode de l'agent.

`tests/test_web_ui.py` ouvre les six pages dans un vrai Chromium : aucune
erreur JavaScript, **aucune icône SVG sans la classe `ico`** (un SVG sans
largeur occupe son conteneur), les prix de référence affichés avec leur
date de vérification, et la carte de chaque agent qui explique comment
NemApi le reconnaît.

Dépendances de test nécessaires (sinon les tests sont *ignorés*, pas échoués) :

```bash
venv/bin/python -m pip install playwright "markdown-it-py[linkify]"
venv/bin/python -m playwright install chromium
venv/bin/python -m playwright install-deps chromium   # Linux: bibliothèques système
```

**Un test ignoré est une porte de sortie, pas un succès.** Si un test est
sauté, l'installer plutôt que de livrer tel quel. En particulier, sans
`install-deps`, Chromium ne démarre pas (`libglib-2.0.so.0` manquant) et
**43 tests sont ignorés** au lieu d'échouer — c'est ce qui avait masqué
l'implémentation incomplète de l'upload d'image.

Les tests de version (`test_integrations.py`, `test_web_ui.py`) doivent
référencer `nemapi.__version__` et **jamais** une version en dur : un bump
de version cassait le build à chaque fois.

---

## 7. Conventions

- **Python** : annotations de type, `from __future__ import annotations`,
  docstrings en anglais, une fonction = une responsabilité.
- **JavaScript** : ES2020, pas de build, pas de dépendances, `var`/`let`/`const`
  plutôt que `var` seul, tout accès DOM protégé par `try/catch` (la page peut
  re-rendre entre deux instructions).
- **Commentaires** : expliquer le *pourquoi* (un piège, une contrainte de
  l'API du site), jamais le *quoi*. Chaque commentaire existe parce qu'un bug
  concret a été payé.
- **Commits** : préfixe de version (`v5.1.0: …`) et description du
  **comportement** observé, pas de la liste de fichiers.

## 8. Pièges connus

- `extension/manifest.json` déclare `minimum_chrome_version: 103` mais utilise
  `match_origin_as_fallback` (**Chrome 119+**). Sur 103–118 la clé est
  ignorée, sans casse ici (les `matches` sont exacts) — mais c'est à garder
  en tête si les `matches` évoluent.
- `proxy/data/` (config, stats, sessions) est **non versionné** : c'est l'état
  de la machine, pas du code. Vider `sessions.json` remet le contexte à zéro.
- Un `MutationObserver` sur toute la page est coûteux : observer un
  sous-arbre, jamais `document`.
- `api.github.com` et `grep.app` sont **rate-limités** depuis cette machine ;
  `lite.duckduckgo.com/lite/?q=` et `raw.githubusercontent.com` fonctionnent.
