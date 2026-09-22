"""Cline free-tier routing enforcement.

Cline can reach several providers and most of them bill. Two facts make this a
runtime concern rather than a one-time setup step:

1. Cline resolves its *effective* provider from ``~/.cline/data/globalState.json``
   (keys ``actModeApiProvider`` / ``planModeApiProvider``), **not** from
   ``settings/providers.json``. Fixing only the latter looks applied while the paid
   provider is still billed.
2. That state is mutable. Any interactive Cline session rewrites it. Observed on
   2026-09-22: state had drifted back to the paid ``cline`` provider on
   ``moonshotai/kimi-k3``, with OpenRouter holding ``anthropic/claude-fable-5``
   at $10/M in and $50/M out.

Only the Gemini provider is a verified free route. OpenRouter's ``:free`` slugs
return "This model is unavailable for free", and the ``cline`` provider spends
Cline Credits, which are $0.00 on this account.

Enforcement is therefore applied immediately before every invocation, and the
model is always sent explicitly so Cline can never fall back to a persisted choice.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

#: The one provider verified to serve requests at no cost on this host.
FREE_PROVIDER = "gemini"

#: Gemini ids that are free-tier. Deliberately an explicit allowlist rather than a
#: prefix match, so a future paid Gemini tier cannot qualify by naming convention.
FREE_MODELS: tuple[str, ...] = (
    "gemini-3.6-flash",
    "gemini-3.6-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
)

DEFAULT_FREE_MODEL = FREE_MODELS[0]

#: Escape hatch. Set to "0" to permit paid providers and models again.
ENV_ENFORCE = "BRAIN_CLINE_ENFORCE_FREE"


def enforcement_enabled() -> bool:
    return os.environ.get(ENV_ENFORCE, "1") != "0"


def is_free_model(model: str | None) -> bool:
    return bool(model) and model in FREE_MODELS


def coerce_model(requested: str | None) -> str:
    """The model to actually send, forced onto the free tier.

    ``auto`` is coerced rather than passed through: it delegates the choice back to
    Cline's persisted state, which is the paid-drift path this guards against. A
    concrete id is always returned for the same reason.
    """
    if not enforcement_enabled():
        return requested or DEFAULT_FREE_MODEL
    if is_free_model(requested):
        return requested  # type: ignore[return-value]
    return DEFAULT_FREE_MODEL


def free_models_only(models: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Filter a model list down to the free allowlist, preserving order.

    Never returns empty: an empty catalogue would make the agent look unavailable
    instead of cheap.
    """
    if not enforcement_enabled():
        return tuple(models)
    kept = tuple(m for m in models if is_free_model(m))
    return kept or (DEFAULT_FREE_MODEL,)


def _config_root(config_dir: str | Path | None = None) -> Path:
    if config_dir:
        return Path(config_dir).expanduser()
    return Path(os.environ.get("BRAIN_CLINE_CONFIG_DIR", Path.home() / ".cline"))


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def pin_free_route(
    config_dir: str | Path | None = None,
    data_dir: str | Path | None = None,
) -> list[str]:
    """Force every Cline mode and provider record onto the free route.

    Rewrites only provider selection and model ids. Credentials are never read,
    logged, or modified. Returns a list of human-readable changes, empty when the
    state was already compliant, so a caller can log drift without re-deriving it.
    """
    if not enforcement_enabled():
        return []

    root = _config_root(config_dir)
    base = Path(data_dir).expanduser() if data_dir else root / "data"
    global_state_path = base / "globalState.json"
    providers_path = base / "settings" / "providers.json"

    changes: list[str] = []
    model = DEFAULT_FREE_MODEL

    state = _load_json(global_state_path)
    if state:
        desired = {
            "actModeApiProvider": FREE_PROVIDER,
            "planModeApiProvider": FREE_PROVIDER,
            "actModeGeminiModelId": model,
            "planModeGeminiModelId": model,
        }
        for key, value in desired.items():
            if state.get(key) != value:
                changes.append(f"globalState.{key}: {state.get(key)!r} -> {value!r}")
                state[key] = value
        # Clear model ids parked on the billing providers so a manual provider
        # switch cannot inherit a paid default, and drop cached paid pricing info.
        for key in list(state):
            if key.endswith("ModelId") and key not in desired and state.get(key):
                changes.append(f"globalState.{key}: cleared {state[key]!r}")
                state[key] = ""
            elif key.endswith("ModelInfo") and isinstance(state.get(key), dict):
                info = state[key]
                if info.get("inputPrice") or info.get("outputPrice"):
                    changes.append(f"globalState.{key}: cleared paid model info")
                    state[key] = {}
        if changes:
            _write_json_atomic(global_state_path, state)

    providers = _load_json(providers_path)
    if providers:
        provider_changes: list[str] = []
        if providers.get("lastUsedProvider") != FREE_PROVIDER:
            provider_changes.append(
                f"providers.lastUsedProvider: {providers.get('lastUsedProvider')!r} -> {FREE_PROVIDER!r}"
            )
            providers["lastUsedProvider"] = FREE_PROVIDER

        records = providers.get("providers")
        if isinstance(records, dict):
            free_blob = records.get(FREE_PROVIDER)
            if isinstance(free_blob, dict):
                settings = free_blob.setdefault("settings", {})
                if settings.get("model") != model:
                    provider_changes.append(
                        f"providers.{FREE_PROVIDER}.model: {settings.get('model')!r} -> {model!r}"
                    )
                    settings["model"] = model
            for name, blob in records.items():
                if name == FREE_PROVIDER or not isinstance(blob, dict):
                    continue
                settings = blob.get("settings")
                if isinstance(settings, dict) and settings.get("model"):
                    provider_changes.append(f"providers.{name}.model: cleared {settings['model']!r}")
                    settings["model"] = ""

        if provider_changes:
            _write_json_atomic(providers_path, providers)
            changes.extend(provider_changes)

    return changes
