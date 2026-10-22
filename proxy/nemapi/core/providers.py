"""Provider catalogue, model aliases, pricing and model → provider resolution."""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .errors import APIError

# Canonical model per provider (the web UI decides the real model).
PROVIDERS: Dict[str, Dict] = {
    "deepseek": {"models": ["deepseek-chat"], "display_name": "DeepSeek"},
    "qwen": {"models": ["qwen-chat"], "display_name": "Qwen"},
    "claude": {"models": ["claude-chat"], "display_name": "Claude"},
    "gemini": {"models": ["gemini-chat"], "display_name": "Gemini"},
    "chatgpt": {"models": ["gpt-chat"], "display_name": "ChatGPT"},
    "kimi": {"models": ["kimi-chat"], "display_name": "Kimi"},
    "zai": {"models": ["glm-chat"], "display_name": "Z.ai"},
}

# Aliases accepted from clients (Qwen Code, Cursor, Continue, Aider…).
MODEL_ALIASES: Dict[str, str] = {
    # DeepSeek
    "deepseek-chat": "deepseek", "deepseek-coder": "deepseek", "deepseek-v3": "deepseek",
    "deepseek-r1": "deepseek", "chat": "deepseek",
    # Qwen
    "qwen-chat": "qwen", "qwen-plus": "qwen", "qwen2.5-plus": "qwen", "qwen2.5-coder": "qwen",
    "qwen3-coder": "qwen", "qwen3-coder-plus": "qwen", "qwen-max": "qwen", "plus": "qwen",
    # Claude
    "claude-chat": "claude", "claude-sonnet": "claude", "claude-3-sonnet": "claude",
    "claude-3-haiku": "claude", "claude-3.5-sonnet": "claude", "claude-4-sonnet": "claude",
    "sonnet": "claude", "haiku": "claude", "opus": "claude",
    # Gemini
    "gemini-chat": "gemini", "gemini-2.5-flash": "gemini", "gemini-2.0-flash": "gemini",
    "gemini-pro": "gemini", "gemini-flash": "gemini", "flash": "gemini",
    # ChatGPT
    "gpt-chat": "chatgpt", "chatgpt": "chatgpt", "gpt-4": "chatgpt", "gpt-4o": "chatgpt",
    "gpt-4.1": "chatgpt", "gpt-5": "chatgpt", "gpt-3.5-turbo": "chatgpt", "o1": "chatgpt",
    "o3": "chatgpt",
    # Kimi
    "kimi-chat": "kimi", "kimi": "kimi", "moonshot": "kimi", "kimi-k2": "kimi", "kimi-k3": "kimi",
    # Z.ai / Zhipu
    "glm-chat": "zai", "zai-chat": "zai", "zai": "zai", "z.ai": "zai", "glm": "zai",
    "glm-4": "zai", "glm-5": "zai", "chatglm": "zai",
}

# Keyword fallback for unknown model ids (e.g. "claude-opus-4-1", "gpt-4o-mini").
# Order matters: the first matching rule wins.
_KEYWORD_RULES = [
    (re.compile(r"deepseek"), "deepseek"),
    (re.compile(r"qwen|qwq|qianwen|tongyi"), "qwen"),
    (re.compile(r"claude|sonnet|opus|haiku|anthropic"), "claude"),
    (re.compile(r"gemini|gemma|bard"), "gemini"),
    (re.compile(r"kimi|moonshot"), "kimi"),
    (re.compile(r"glm|zhipu|z\.ai|zai|bigmodel"), "zai"),
    (re.compile(r"chatgpt|openai|gpt|^o[1-9](\b|-|$)"), "chatgpt"),
]

# Public API list prices, USD per 1K tokens (verified 2026-10-22 on the
# official pricing pages). These are the prices an API would bill; the web
# bridge itself is free — the numbers only put the usage in perspective.
#
# Each entry:
#   prompt      — price per 1K input tokens
#   completion  — price per 1K output tokens
#   cached      — price per 1K input tokens when the provider serves them
#                from its context cache (None = no separate cache price)
#   label       — the reference model the price refers to
#   note        — what makes the price special (promo, off-peak, tier…)
TOKEN_PRICING: Dict[str, Dict] = {
    "deepseek": {
        "prompt": 0.00066, "completion": 0.00198, "cached": 0.000022,
        "label": "DeepSeek V4-Pro",
        "note": "tarif hors pointe (la pointe, 01h–04h et 06h–10h UTC, coûte le double)",
    },
    "qwen": {
        "prompt": 0.00040, "completion": 0.00160, "cached": 0.00004,
        "label": "Qwen3.7-Plus",
        "note": "prix international ≤ 256K tokens ; −20 % en promotion ; cache à 10 %",
    },
    "claude": {
        "prompt": 0.00200, "completion": 0.01000, "cached": 0.00010,
        "label": "Claude Sonnet 5.5",
        "note": "lecture du cache à 5 % ; écriture du cache facturée en plus",
    },
    "gemini": {
        "prompt": 0.00075, "completion": 0.00375, "cached": 0.000075,
        "label": "Gemini 3.8 Flash",
        "note": "prix d'introduction jusqu'au 31/12/2026 (1,50 $ / 7,50 $ ensuite)",
    },
    "chatgpt": {
        "prompt": 0.00200, "completion": 0.01000, "cached": 0.00010,
        "label": "GPT-6.1 Sol",
        "note": "entrée en cache à 5 % ; écriture du cache facturée en plus",
    },
    "kimi": {
        "prompt": 0.00300, "completion": 0.01500, "cached": 0.00030,
        "label": "Kimi K3",
        "note": "l'écriture du cache coûte le prix d'une entrée complète",
    },
    "zai": {
        "prompt": 0.00140, "completion": 0.00440, "cached": 0.00026,
        "label": "GLM-5.3",
        "note": "prix international en USD (la plateforme Chine est en CNY)",
    },
}

# Where the numbers come from (official pricing pages, checked 2026-10-22).
PRICING_SOURCES: Dict[str, str] = {
    "deepseek": "api-docs.deepseek.com/quick_start/pricing",
    "qwen": "alibabacloud.com/help/en/model-studio/model-pricing",
    "claude": "anthropic.com/pricing",
    "gemini": "ai.google.dev/pricing",
    "chatgpt": "platform.openai.com/docs/pricing",
    "kimi": "platform.kimi.ai/docs/pricing/chat",
    "zai": "docs.z.ai/guides/overview/pricing",
}

# When the prices above were checked against the official pages.
PRICING_CHECKED_AT = "2026-10-22"

MODEL_TO_PROVIDER: Dict[str, str] = {}
for _pid, _info in PROVIDERS.items():
    for _m in _info["models"]:
        MODEL_TO_PROVIDER[_m] = _pid
        MODEL_TO_PROVIDER[f"{_pid}/{_m}"] = _pid
for _alias, _pid in MODEL_ALIASES.items():
    MODEL_TO_PROVIDER[_alias] = _pid
    MODEL_TO_PROVIDER[f"{_pid}/{_alias}"] = _pid


def canonical_model(provider: str) -> str:
    return PROVIDERS[provider]["models"][0]


def guess_provider(model: str) -> Optional[str]:
    """Resolve a model id to a provider id, or None if unknown."""
    if not model:
        return None
    raw = model.strip()
    low = raw.lower()
    hit = MODEL_TO_PROVIDER.get(raw) or MODEL_TO_PROVIDER.get(low)
    if hit:
        return hit
    if "/" in low:
        head, tail = low.split("/", 1)[0].strip(), low.rsplit("/", 1)[-1].strip()
        if head in PROVIDERS:
            return head
        hit = MODEL_TO_PROVIDER.get(tail)
        if hit:
            return hit
        low = tail
    for rule, pid in _KEYWORD_RULES:
        if rule.search(low):
            return pid
    return None


def resolve_provider(model: str, explicit_provider: str = "") -> str:
    """Return the provider id for a request or raise APIError(400).

    The provider comes from the model, like every OpenAI-compatible gateway:
    ``deepseek-chat`` → deepseek, ``qwen-plus`` → qwen. ``explicit_provider``
    is only used internally (the admin tools), never read from a request.
    """
    explicit = (explicit_provider or "").lower().strip()
    if explicit:
        if explicit not in PROVIDERS:
            raise APIError(
                400,
                f"Unknown provider '{explicit}'. Available: {', '.join(PROVIDERS)}.",
            )
        return explicit
    if not model:
        raise APIError(
            400,
            "The 'model' field is required. Examples: 'qwen-chat', 'qwen-plus', "
            "'deepseek-chat', 'claude-sonnet'.",
        )
    provider = guess_provider(model)
    if not provider:
        examples = [f"{p}/{canonical_model(p)}" for p in PROVIDERS] + ["qwen-plus", "claude-sonnet"]
        raise APIError(400, f"Unknown model '{model}'. Try one of: {', '.join(examples)}")
    return provider


def providers_listing() -> List[Dict]:
    return [
        {"id": pid, "models": info["models"], "display_name": info.get("display_name", pid)}
        for pid, info in PROVIDERS.items()
    ]


def calculate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = TOKEN_PRICING.get(provider)
    if not pricing:
        return 0.0
    return (prompt_tokens / 1000) * pricing["prompt"] + (completion_tokens / 1000) * pricing["completion"]
