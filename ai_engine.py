"""AI Rewriter engine (Gemini SDK or any OpenAI-compatible provider).

Responsibilities:
  - Strip Telegram noise (invite links, @handles, old hashtags).
  - Rewrite & format the caption specifically for a Facebook audience.
  - Apply free-form admin edit instructions (used by the Messenger approval flow).

Backends (auto-selected at call time):
  - OpenAI-compatible HTTP (`/chat/completions`) when AI_API_KEY or
    AI_BASE_URL is set: works with OpenRouter, Groq, DeepSeek and any other
    provider that speaks the standard API.
  - Gemini SDK: `google.genai` (current, supported) with transparent fallback
    to the deprecated `google.generativeai`.
"""
import re

import requests

from config import get_setting

HTTP_DEFAULT_BASE = "https://openrouter.ai/api/v1/chat/completions"
HTTP_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def _setting(key, default="", cfg=None):
    """Read a setting from the tenant config (dict) when supplied, otherwise
    from the legacy .env globals (guest path / tests)."""
    if cfg is not None:
        return cfg.get(key) or default
    return get_setting(key, default)


def _load_backend():
    """Return a small SDK backend exposing `configure(key)` and
    `generate(client, model, prompt)`, or None when neither SDK is installed."""
    try:
        from google import genai as modern

        def configure(api_key):
            return modern.Client(api_key=api_key)

        def generate(client, model, prompt, cfg=None):
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text

        return {"name": "google.genai", "configure": configure, "generate": generate}
    except Exception:  # module missing -> try legacy SDK
        pass

    try:
        import google.generativeai as legacy

        def configure(api_key):
            legacy.configure(api_key=api_key)
            return legacy.GenerativeModel

        def generate(model_class, name, prompt, cfg=None):
            model = model_class(name)
            response = model.generate_content(prompt)
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                raise RuntimeError(f"Gemini blocked prompt: {response.prompt_feedback.block_reason}")
            return response.text

        return {"name": "google.generativeai", "configure": configure, "generate": generate}
    except Exception:
        return None


def _http_backend():
    """OpenAI-compatible chat backend (OpenRouter, Groq, DeepSeek, ...).

    Reads AI_BASE_URL live (defaults to OpenRouter), so a single endpoint works
    for providers that expose the standard /chat/completions API.
    """

    def configure(api_key):
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    def generate(headers, model, prompt, cfg=None):
        base_url = _setting("AI_BASE_URL", HTTP_DEFAULT_BASE, cfg)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _BASE_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        response = requests.post(base_url, json=payload, headers=headers, timeout=45)
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError(f"AI provider returned non-JSON (HTTP {response.status_code})")
        if isinstance(data, dict) and data.get("choices"):
            return data["choices"][0]["message"]["content"]
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"].get("message", str(data["error"])))
        raise RuntimeError(f"Unexpected AI provider response (HTTP {response.status_code})")

    return {"name": "openai-compatible", "configure": configure, "generate": generate}


def _model(cfg=None):
    """Backend + credentials for the current settings. Never caches the choice,
    so settings changes made through the dashboard apply immediately.

    cfg is a tenant config dict (.env-style keys). When None the legacy .env
    globals are used (guest path / tests).

    Preference:
      1. OpenAI-compatible HTTP when AI_API_KEY or AI_BASE_URL is set.
      2. Gemini SDK (google.genai / google.generativeai) otherwise.
    """
    prefer_http = bool(
        _setting("AI_API_KEY", "", cfg) or _setting("AI_BASE_URL", "", cfg)
    )
    backend = _http_backend() if prefer_http else (_load_backend() or _http_backend())
    api_key = _setting("AI_API_KEY", "", cfg) or _setting("GEMINI_API_KEY", "", cfg)
    if not api_key:
        raise RuntimeError("AI_API_KEY / GEMINI_API_KEY is not configured")
    model = (
        _setting("AI_MODEL", "", cfg)
        or _setting("GEMINI_MODEL", "", cfg)
        or HTTP_DEFAULT_MODEL
    )
    return backend["name"], backend, backend["configure"](api_key), model


_BASE_PROMPT = (
    "You are a professional social-media copywriter who repurposes Telegram "
    "channel posts for a Facebook Page.\n\n"
    "Rules:\n"
    "1. Remove any Telegram invite links (t.me/...), @usernames/handles, and "
    "legacy hashtags from the raw text.\n"
    "2. Rewrite the text with an engaging, human, slightly friendly tone that "
    "reads naturally for Facebook. Keep every factual detail intact.\n"
    "3. Use clean spacing, short paragraphs, and at most a few well-placed, "
    "relevant emojis (do not stuff emojis).\n"
    "4. Preserve the core message, numbers, names, prices, links (except "
    "Telegram links), and any call-to-action.\n"
    "5. End the caption with exactly 3-5 relevant hashtags on a new line.\n"
    "6. If there is nothing meaningful to rewrite, return the cleaned original "
    "text verbatim.\n\n"
    "Output ONLY the final caption. No preamble, no quotes, no commentary."
)


def _generate(prompt, cfg=None):
    _name, backend, client, model = _model(cfg)
    return backend["generate"](client, model, prompt, cfg)


def _strip_noise(text):
    """Remove Telegram links, handles and old hashtags from raw text."""
    if not text:
        return ""
    text = re.sub(r"https?://t\.me/[^\s]+", "", text)
    text = re.sub(r"(?<![\w])t\.me/[^\s]+", "", text)
    text = re.sub(r"@[A-Za-z0-9_]{3,}", "", text)
    text = re.sub(r"(^|\s)#[\w\u4e00-\u9fff]+", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ping(cfg=None):
    """Connectivity check for the configured AI backend.

    Returns a (ok, detail) tuple; never raises.
    """
    try:
        name, _backend, _client, model = _model(cfg)
        response = _generate("Reply with the single word: ok", cfg)
        ok = bool(response and str(response).strip())
        detail = f"{model} ({name})" if ok else "AI returned an empty response"
        return ok, detail
    except Exception as exc:
        return False, str(exc)


def rewrite_for_facebook(raw_text, cfg=None):
    """Clean + AI rewrite. Raises on AI failure; callers decide fallback."""
    cleaned = _strip_noise(raw_text)
    if not cleaned:
        return raw_text or ""

    prompt = f"{_BASE_PROMPT}\n\n--- RAW POST ---\n{cleaned}"
    return (_generate(prompt, cfg) or cleaned).strip()


def rewrite_with_instruction(current_caption, instruction, cfg=None):
    """Apply a free-form admin edit instruction to an already rewritten caption."""
    if not instruction or not instruction.strip():
        raise RuntimeError("Empty edit instruction")

    prompt = (
        _BASE_PROMPT
        + "\n\nAdditionally, apply this requested change from the page admin "
          "to the CURRENT CAPTION (keep the house style and end with 3-5 "
          f"hashtags).\n\nREQUESTED CHANGE:\n{instruction.strip()}\n\n"
          f"--- CURRENT CAPTION ---\n{current_caption}"
    )
    return (_generate(prompt, cfg) or current_caption).strip()