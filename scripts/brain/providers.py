#!/usr/bin/env python3
"""Provider accounts for the shared brain — bring-your-own-key workers.

Why this exists
---------------
Until now a swarm task could only run by shelling out to an agent CLI
(``cline``, ``agy``, ``kiro-cli``). On a machine where none of those are
installed the swarm had no workers at all, so the project only worked for
whoever happened to have the same tools.

This module adds the other half, modelled on OmniRoute: a user registers a
provider account (an API key) and that account becomes a usable worker
immediately, with **no CLI and no IDE required**. Detection is automatic — keys
already present in the environment or in another agent's config are found and
offered rather than asked for again.

Design rules
------------
* Stdlib only. This runs on whatever Python the host has; adding a dependency
  to call an HTTP endpoint is not worth it.
* Keys are never returned by any listing function, never logged, and never
  written to the brain store. ``~/.config/brain/providers.json`` is 0600 and is
  the only place this module writes a key.
* Every provider here speaks the OpenAI chat-completions shape, which is what
  makes one small client cover all of them.
* Free-tier and terms-of-service metadata is carried per provider so the UI can
  be honest about what a key actually costs.

Terms of service
----------------
Antigravity and Kiro free tiers explicitly prohibit access through third-party
proxies. They are therefore **not** in this catalog: those agents are driven
through their own CLI, which is the sanctioned path. Only providers that offer a
real developer API are registered here.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("BRAIN_CONFIG_DIR", Path.home() / ".config" / "brain"))
ACCOUNTS_FILE = Path(os.environ.get("BRAIN_PROVIDERS_FILE", CONFIG_DIR / "providers.json"))
HTTP_TIMEOUT = float(os.environ.get("BRAIN_PROVIDER_TIMEOUT", "120"))

# Free-tier classes, following OmniRoute's honesty split: a one-time signup
# credit is not a recurring budget and must not be advertised as one.
RECURRING = "recurring"      # documented monthly/daily grant that resets
UNCAPPED = "uncapped"        # permanently free, rate limited, no published cap
SIGNUP = "signup-credit"     # one-time credit; does not recur
PAID = "paid"                # no free tier


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    base_url: str                      # OpenAI-compatible /chat/completions root
    env_vars: tuple[str, ...]          # env vars checked during auto-detection
    default_model: str
    free_type: str = PAID
    free_note: str = ""
    # USD per 1M tokens. 0.0 means the default model is free on the free tier.
    usd_per_mtok_in: float = 0.0
    usd_per_mtok_out: float = 0.0
    models_path: str = "/models"       # used to validate a key cheaply
    tos_note: str = ""


# Ordered best-free-first so auto-selection prefers a genuinely free account.
# Figures are documented free-tier allowances and move over time; they are
# labels for the UI, never billing truth.
CATALOG: tuple[Provider, ...] = (
    Provider(
        id="gemini",
        label="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_model="gemini-3.6-flash",
        free_type=RECURRING,
        free_note="Flash family free tier; Pro tiers are paid",
        tos_note="free tier framed for development use",
    ),
    Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        env_vars=("GROQ_API_KEY",),
        default_model="llama-3.3-70b-versatile",
        free_type=RECURRING,
        free_note="documented recurring free tier, per-model RPD caps",
    ),
    Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        env_vars=("CEREBRAS_API_KEY",),
        default_model="gpt-oss-120b",
        free_type=RECURRING,
        free_note="free tier with daily token cap",
    ),
    Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        env_vars=("MISTRAL_API_KEY",),
        default_model="mistral-small-latest",
        free_type=RECURRING,
        free_note="free Experiment tier, rate limited",
    ),
    Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        env_vars=("OPENROUTER_API_KEY",),
        default_model="deepseek/deepseek-chat-v3.1",
        free_type=UNCAPPED,
        free_note="':free' model slugs exist but many have been retired; verify per model",
    ),
    Provider(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        env_vars=("DEEPSEEK_API_KEY",),
        default_model="deepseek-chat",
        free_type=SIGNUP,
        free_note="one-time signup credit, then pay as you go",
        usd_per_mtok_in=0.28,
        usd_per_mtok_out=0.42,
    ),
    Provider(
        id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        env_vars=("TOGETHER_API_KEY",),
        default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
        free_type=SIGNUP,
        free_note="signup credit; some permanently free endpoints",
    ),
    Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        env_vars=("OPENAI_API_KEY",),
        default_model="gpt-4o-mini",
        free_type=PAID,
        usd_per_mtok_in=0.15,
        usd_per_mtok_out=0.60,
    ),
    Provider(
        id="anthropic",
        label="Anthropic",
        base_url="https://api.anthropic.com/v1",
        env_vars=("ANTHROPIC_API_KEY",),
        default_model="claude-haiku-4-5",
        free_type=PAID,
        usd_per_mtok_in=1.00,
        usd_per_mtok_out=5.00,
        models_path="/models",
    ),
    Provider(
        id="ollama",
        label="Ollama (local)",
        base_url=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434") + "/v1",
        env_vars=(),
        default_model="llama3.2",
        free_type=UNCAPPED,
        free_note="runs on this machine; no key, no billing, no network egress",
    ),
)

BY_ID = {p.id: p for p in CATALOG}


# ---------------------------------------------------------------------------
# Stored accounts
# ---------------------------------------------------------------------------
def _read_store() -> dict:
    try:
        if ACCOUNTS_FILE.exists():
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}


def _write_store(data: dict) -> None:
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    os.replace(tmp, ACCOUNTS_FILE)
    ACCOUNTS_FILE.chmod(0o600)


def _legacy_gemini_key_file() -> str | None:
    """The brain already shipped a single Gemini key file; keep honouring it."""
    p = Path(os.environ.get("BRAIN_ANTIGRAVITY_API_KEY_FILE",
                            CONFIG_DIR / "antigravity-api.key"))
    try:
        if p.exists():
            v = p.read_text(encoding="utf-8").strip()
            return v or None
    except Exception:
        pass
    return None


def _keys_from_other_agents() -> dict[str, tuple[str, str]]:
    """Harvest keys another installed agent already holds.

    Returns ``{provider_id: (key, source)}``. This is what lets a new user skip
    re-entering a key they already configured somewhere else on the machine.
    """
    found: dict[str, tuple[str, str]] = {}
    cline_providers = Path.home() / ".cline" / "data" / "settings" / "providers.json"
    try:
        if cline_providers.exists():
            data = json.loads(cline_providers.read_text(encoding="utf-8"))
            for name, cfg in (data.get("providers") or {}).items():
                key = ((cfg or {}).get("settings") or {}).get("apiKey")
                if key and name in BY_ID and name not in found:
                    found[name] = (key, "cline config")
    except Exception:
        pass
    return found


def resolve_key(provider_id: str) -> tuple[str | None, str]:
    """Return ``(key, source)`` for a provider without ever logging the value.

    Precedence: explicitly stored account, then environment, then a key another
    agent already has, then the legacy single-key file.
    """
    p = BY_ID.get(provider_id)
    if p is None:
        return None, "unknown provider"
    if not p.env_vars and p.id == "ollama":
        return "", "local, no key required"

    stored = (_read_store().get("accounts") or {}).get(provider_id) or {}
    if stored.get("api_key"):
        return stored["api_key"], "brain providers.json"
    for var in p.env_vars:
        v = os.environ.get(var)
        if v:
            return v, f"environment ${var}"
    harvested = _keys_from_other_agents().get(provider_id)
    if harvested:
        return harvested
    if provider_id == "gemini":
        legacy = _legacy_gemini_key_file()
        if legacy:
            return legacy, "brain antigravity-api.key"
    return None, "not configured"


def add_account(provider_id: str, api_key: str, model: str | None = None) -> dict:
    """Register or replace an account. The key is written 0600 and never echoed."""
    if provider_id not in BY_ID:
        return {"status": "error", "error": f"unknown provider {provider_id!r}",
                "known": sorted(BY_ID)}
    if not api_key or not api_key.strip():
        return {"status": "error", "error": "empty api key"}
    store = _read_store()
    store.setdefault("accounts", {})[provider_id] = {
        "api_key": api_key.strip(),
        "model": model or BY_ID[provider_id].default_model,
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_store(store)
    return {"status": "added", "provider": provider_id,
            "model": store["accounts"][provider_id]["model"],
            "stored_at": str(ACCOUNTS_FILE), "mode": "0600"}


def remove_account(provider_id: str) -> dict:
    store = _read_store()
    if provider_id in (store.get("accounts") or {}):
        del store["accounts"][provider_id]
        _write_store(store)
        return {"status": "removed", "provider": provider_id}
    return {"status": "absent", "provider": provider_id}


def list_accounts() -> list[dict]:
    """Describe every provider and whether it is usable. Never includes a key."""
    out = []
    store_models = {k: v.get("model") for k, v in (_read_store().get("accounts") or {}).items()}
    for p in CATALOG:
        key, source = resolve_key(p.id)
        out.append({
            "provider": p.id,
            "label": p.label,
            "configured": key is not None,
            "key_source": source,
            "model": store_models.get(p.id) or p.default_model,
            "free_type": p.free_type,
            "free_note": p.free_note,
            "tos_note": p.tos_note,
            "usd_per_mtok_in": p.usd_per_mtok_in,
            "usd_per_mtok_out": p.usd_per_mtok_out,
            "is_free_default": p.usd_per_mtok_in == 0.0 and p.usd_per_mtok_out == 0.0,
            "base_url": p.base_url,
        })
    out.sort(key=lambda r: (not r["configured"], not r["is_free_default"], r["provider"]))
    return out


def configured_accounts() -> list[dict]:
    return [a for a in list_accounts() if a["configured"]]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


def _auth_headers(p: Provider, key: str) -> dict[str, str]:
    if p.id == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _request(url: str, headers: dict, payload: dict | None, timeout: float) -> tuple[int, dict | None, str]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    for k, v in headers.items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw), ""
            except Exception:
                return r.status, None, raw[:300].decode(errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            body = json.loads(raw)
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else str(body.get("error") or "")
            return e.code, body, str(msg or "")[:300]
        except Exception:
            return e.code, None, raw[:300].decode(errors="replace")
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


def test_account(provider_id: str, timeout: float = 30.0) -> dict:
    """Validate a key with the cheapest call the provider offers.

    Listing models costs no tokens, so this is a free health check.
    """
    p = BY_ID.get(provider_id)
    if p is None:
        return {"provider": provider_id, "ok": False, "error": "unknown provider"}
    key, source = resolve_key(provider_id)
    if key is None:
        return {"provider": provider_id, "ok": False, "error": "no key configured",
                "key_source": source}
    started = time.perf_counter()
    code, body, err = _request(p.base_url + p.models_path, _auth_headers(p, key), None, timeout)
    latency = round((time.perf_counter() - started) * 1000)
    models: list[str] = []
    if isinstance(body, dict):
        for item in (body.get("data") or body.get("models") or []):
            mid = item.get("id") if isinstance(item, dict) else None
            if mid:
                models.append(mid)
    ok = code == 200
    return {
        "provider": provider_id, "label": p.label, "ok": ok, "http": code,
        "latency_ms": latency, "model_count": len(models),
        "models": models[:40], "key_source": source,
        "error": "" if ok else (err or f"http {code}"),
        "cost": "free (model listing consumes no tokens)",
    }


def estimate_cost(provider_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    p = BY_ID.get(provider_id)
    if p is None:
        return 0.0
    return round(
        (prompt_tokens / 1_000_000.0) * p.usd_per_mtok_in
        + (completion_tokens / 1_000_000.0) * p.usd_per_mtok_out,
        6,
    )


def chat(provider_id: str, prompt: str, model: str | None = None,
         system: str | None = None, timeout: float | None = None) -> dict:
    """Run one completion and return the text plus a full token/cost account.

    This is the direct-API worker path: it needs only a key, so it works on a
    machine with no agent CLI installed at all.
    """
    p = BY_ID.get(provider_id)
    if p is None:
        return {"ok": False, "error": f"unknown provider {provider_id!r}"}
    key, source = resolve_key(provider_id)
    if key is None:
        return {"ok": False, "error": f"no key configured for {provider_id}",
                "key_source": source}
    stored = (_read_store().get("accounts") or {}).get(provider_id) or {}
    model = model or stored.get("model") or p.default_model

    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": messages}
    if p.id == "anthropic":
        payload = {"model": model, "max_tokens": 4096,
                   "messages": [{"role": "user", "content": prompt}]}
        url = p.base_url + "/messages"
    else:
        url = p.base_url + "/chat/completions"

    started = time.perf_counter()
    code, body, err = _request(url, _auth_headers(p, key), payload,
                               timeout or HTTP_TIMEOUT)
    elapsed = round(time.perf_counter() - started, 3)

    if code != 200 or not isinstance(body, dict):
        return {"ok": False, "provider": provider_id, "model": model, "http": code,
                "error": err or f"http {code}", "seconds": elapsed,
                "is_capacity_error": code in (429, 529) or "quota" in (err or "").lower()
                                     or "RESOURCE_EXHAUSTED" in (err or "")}

    if p.id == "anthropic":
        text = "".join(b.get("text", "") for b in body.get("content", [])
                       if isinstance(b, dict))
        usage = body.get("usage") or {}
        pt = int(usage.get("input_tokens") or 0)
        ct = int(usage.get("output_tokens") or 0)
    else:
        choices = body.get("choices") or []
        text = ((choices[0] or {}).get("message") or {}).get("content", "") if choices else ""
        usage = body.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)

    return {
        "ok": True, "provider": provider_id, "model": model, "text": text or "",
        "seconds": elapsed, "key_source": source,
        "usage": {
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct or int(usage.get("total_tokens") or 0),
        },
        "cost_usd": estimate_cost(provider_id, pt, ct),
        "billing": "free tier" if p.usd_per_mtok_in == 0.0 and p.usd_per_mtok_out == 0.0
                   else "metered",
    }


def is_capacity_error(result: dict) -> bool:
    """Whether a failed call was a quota/rate wall rather than a real fault.

    A free tier is a small tier. Exhausting one provider's daily request budget
    is routine, not an error in the task, so it must trigger failover to another
    account instead of failing the work.
    """
    if result.get("ok"):
        return False
    if result.get("is_capacity_error"):
        return True
    if int(result.get("http") or 0) in (429, 503, 529):
        return True
    text = (result.get("error") or "").lower()
    return any(sig in text for sig in (
        "quota", "rate limit", "rate-limit", "ratelimit", "resource_exhausted",
        "too many requests", "overloaded", "capacity", "insufficient balance",
        "exceeded your current",
    ))


def should_try_next_account(result: dict) -> bool:
    """Whether a failure is the account's problem rather than the request's.

    Failover must continue for anything provider-specific — a quota wall, an
    unreachable endpoint, a rejected key — because another account may serve the
    same request perfectly. It must stop only for a fault that would repeat
    identically everywhere (a malformed request), so a genuine bug is not hidden
    behind a sweep through every account.
    """
    if result.get("ok"):
        return False
    if is_capacity_error(result):
        return True
    http = int(result.get("http") or 0)
    # http == 0 means the request never completed: DNS failure, connection
    # refused, TLS error, timeout. A local Ollama that is not running lands here.
    if http == 0:
        return True
    # 401/403 = this key is bad or lacks access; 404 = this provider does not
    # serve the requested model. Both are per-account, so keep going.
    return http in (401, 403, 404, 500, 502, 504)


def chat_with_failover(prompt: str, model: str | None = None,
                       system: str | None = None, timeout: float | None = None,
                       preferred: str | None = None) -> dict:
    """Run a completion against the first account that answers.

    Free tiers are small and exhaust routinely — a single Gemini key hits its
    daily request cap quickly. Stacking every configured free account and failing
    over on a quota wall is what makes a bring-your-own-key setup actually
    dependable, and it is the reason this returns an ``attempts`` trail: the
    account that served the work must be verifiable, not assumed.
    """
    order: list[str] = []
    if preferred:
        order.append(preferred)
    # Free accounts first, then metered, so failover never silently starts
    # spending money before every free option is exhausted.
    for account in list_accounts():
        if account["configured"] and account["is_free_default"] and account["provider"] not in order:
            order.append(account["provider"])
    for account in list_accounts():
        if account["configured"] and account["provider"] not in order:
            order.append(account["provider"])

    if not order:
        return {"ok": False, "error": "no provider account configured",
                "attempts": []}

    attempts: list[dict] = []
    for provider_id in order:
        result = chat(provider_id, prompt, model=model if provider_id == preferred else None,
                      system=system, timeout=timeout)
        attempts.append({
            "provider": provider_id,
            "ok": bool(result.get("ok")),
            "http": result.get("http"),
            "error": (result.get("error") or "")[:160],
            "capacity": is_capacity_error(result),
        })
        if result.get("ok"):
            result["attempts"] = attempts
            result["failed_over"] = len(attempts) > 1
            return result
        if not should_try_next_account(result):
            # A diagnosable fault (malformed request) will fail the same way on
            # every account, so stop rather than burning the pool.
            result["attempts"] = attempts
            return result

    return {"ok": False, "attempts": attempts,
            "error": "every configured provider account is at its quota or unavailable",
            "is_capacity_error": True}


def best_free_account() -> str | None:
    """Pick the cheapest configured account, preferring a genuinely free one."""
    for a in list_accounts():
        if a["configured"] and a["is_free_default"]:
            return a["provider"]
    for a in list_accounts():
        if a["configured"]:
            return a["provider"]
    return None


def discover() -> dict:
    """Full account picture for onboarding and the dashboard. No key values."""
    accounts = list_accounts()
    configured = [a for a in accounts if a["configured"]]
    return {
        "accounts": accounts,
        "configured_count": len(configured),
        "free_count": sum(1 for a in configured if a["is_free_default"]),
        "catalog_size": len(CATALOG),
        "store": str(ACCOUNTS_FILE),
        "best_free": best_free_account(),
        "worker_available_without_cli": bool(configured),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Brain provider accounts")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list")
    sub.add_parser("discover")
    t = sub.add_parser("test"); t.add_argument("provider", nargs="?")
    a = sub.add_parser("add"); a.add_argument("provider"); a.add_argument("--model")
    r = sub.add_parser("remove"); r.add_argument("provider")
    c = sub.add_parser("chat"); c.add_argument("provider"); c.add_argument("prompt"); c.add_argument("--model")
    args = ap.parse_args()

    if args.cmd in (None, "list"):
        for row in list_accounts():
            mark = "OK " if row["configured"] else "-- "
            free = "FREE" if row["is_free_default"] else "PAID"
            print(f"{mark}{row['provider']:<12} {free:<5} {row['free_type']:<14} "
                  f"{row['key_source']:<28} {row['model']}")
    elif args.cmd == "discover":
        print(json.dumps(discover(), indent=2))
    elif args.cmd == "test":
        targets = [args.provider] if args.provider else [a["provider"] for a in configured_accounts()]
        for t_ in targets:
            res = test_account(t_)
            print(f"{'OK ' if res['ok'] else 'ERR'} {t_:<12} http={res.get('http')} "
                  f"{res.get('latency_ms')}ms models={res.get('model_count')} {res.get('error','')}")
    elif args.cmd == "add":
        import getpass
        key = os.environ.get("BRAIN_PROVIDER_KEY") or getpass.getpass(
            f"API key for {args.provider} (not echoed): ")
        print(json.dumps(add_account(args.provider, key, args.model), indent=2))
    elif args.cmd == "remove":
        print(json.dumps(remove_account(args.provider), indent=2))
    elif args.cmd == "chat":
        res = chat(args.provider, args.prompt, args.model)
        if res.get("ok"):
            print(res["text"])
            u = res["usage"]
            print(f"\n-- {res['provider']}/{res['model']}  "
                  f"in={u['prompt_tokens']} out={u['completion_tokens']} "
                  f"total={u['total_tokens']}  ${res['cost_usd']:.6f} ({res['billing']})")
        else:
            print("error:", res.get("error"))
