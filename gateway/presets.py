"""Ready-to-use pool templates for the dashboard's 'Quick add provider' flow.

Model IDs are examples only — providers retire and rename models often. The UI
tells the user to confirm the current IDs in each provider's docs.
"""

from __future__ import annotations

PRESETS = [
    {
        "id": "openai",
        "name": "OpenAI",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-6-luna", "gpt-6.2-sol"],
        "key_hint": "sk-... (api.openai.com)",
        "note": "Free tier ($100/month in allowed geographies) may be available — check your dashboard.",
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "provider": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-5-5"],
        "key_hint": "sk-ant-... (console.anthropic.com)",
        "note": "API is credit-based; no free tier. Never use claude.ai session tokens here.",
    },
    {
        "id": "gemini",
        "name": "Google Gemini (OpenAI-compatible)",
        "provider": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-2.5-flash"],
        "key_hint": "AI Studio API key",
        "note": "Gemini API has a free tier with real API keys — verify limits and model IDs in AI Studio.",
    },
    {
        "id": "groq",
        "name": "Groq",
        "provider": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "models": ["llama-3.3-70b-versatile"],
        "key_hint": "gsk_... (console.groq.com)",
        "note": "Free API tier available. Model IDs change often — check the console.",
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "provider": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openrouter/auto"],
        "key_hint": "sk-or-... (openrouter.ai)",
        "note": "Aggregates many providers, including free models. One account per person.",
    },
    {
        "id": "cerebras",
        "name": "Cerebras",
        "provider": "openai",
        "base_url": "https://api.cerebras.ai/v1",
        "models": ["llama-3.3-70b"],
        "key_hint": "csk-... (cloud.cerebras.ai)",
        "note": "Fast inference with a free tier — check current limits.",
    },
    {
        "id": "ollama",
        "name": "Local / Ollama",
        "provider": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": ["qwen2.5-coder"],
        "key_hint": "ollama (any placeholder)",
        "note": "Completely free and unlimited. Run `ollama serve` and pull a model first.",
    },
]


def get_presets() -> list[dict]:
    return [p.copy() for p in PRESETS]
