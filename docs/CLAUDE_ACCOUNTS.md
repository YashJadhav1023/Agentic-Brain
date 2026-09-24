# Claude Code Accounts & the pxpipe Token Proxy

This document covers two connected features:

1. **Multiple Claude Code accounts**, each isolated, each usable by the swarm —
   on a Pro/Max **subscription**, an OAuth token, or an API key.
2. **pxpipe**, an optional local proxy that cuts the input tokens those accounts
   spend by rendering bulky context as images.

---

## 1. Why subscription support works the way it does

**The brain never holds your Claude subscription credential.**

A Claude Pro/Max seat is authenticated by an OAuth credential that the official
`claude` CLI obtains, stores, refreshes and revokes. This project deliberately
does not reimplement that flow, because minting a subscription token from outside
the official client requires presenting Anthropic's first-party OAuth client id —
i.e. impersonating Claude Code. For a project other people run, that would put
*their* accounts at risk of suspension, not just ours.

So the division of responsibility is:

| Concern | Owner |
| --- | --- |
| Logging in, token refresh, revocation | the official `claude` CLI |
| Where each account's credential lives | `CLAUDE_CONFIG_DIR`, one directory per account |
| Reading login **metadata** (plan, expiry) for health | this project |
| Reading, copying, storing or transmitting the token | **nobody** — it is never touched |

`agents/claude/auth.py:inspect_profile()` parses the credential file to answer
"is this account logged in, on what plan, until when". It never binds, returns or
logs the two token fields, and `ProfileStatus.to_dict()` is asserted clean by
`tests/unit/test_claude_auth.py::test_status_never_carries_token_material`.

### The three authentication modes

| `--auth-mode` | What it is | Secret held by this project |
| --- | --- | --- |
| `subscription` (default) | A Pro/Max seat logged in with the official CLI | **none** |
| `oauth_token` | A long-lived token you minted with `claude setup-token` | yes, in the CredentialManager |
| `api_key` | A pay-per-token `sk-ant-…` key | yes, in the CredentialManager |

For the two modes that do involve a secret, it is taken at a hidden prompt,
handed straight to the CredentialManager, referenced by a `secret://` URI, and
never written to `config/providers.json`.

---

## 2. Adding accounts

### The account you already have

If you have used `claude` on this machine, you are already logged in and the
first account needs no setup — it points at `~/.claude`:

```bash
brain claude list
# ACCOUNT                AUTH           PLAN        PXPIPE  STATUS
# claude-account-1       subscription   max         on      READY
```

### Adding another subscription account

Each account gets its own profile directory, so two accounts never share a
session, a credential, or a rate-limit budget — the same isolation model
Antigravity accounts get from `--app_data_dir`.

```bash
brain claude add --name account-2 --use-pxpipe
```

That creates `~/.mission-control/claude/account-2` with mode `0700`, registers
the account, and prints the one interactive command you must run yourself:

```bash
CLAUDE_CONFIG_DIR="/home/you/.mission-control/claude/account-2" claude /login
```

Log in with the second Google/Anthropic identity in the browser window that
opens. Then verify:

```bash
brain claude status claude-account-2
```

`brain claude login <agent-id>` reprints that command at any time.

### Adding an API-key or OAuth-token account

```bash
brain claude add --name ci-key --auth-mode api_key          # hidden prompt for the key
brain claude add --name bot --auth-mode oauth_token         # hidden prompt for the token
```

Leaving the prompt blank falls back to `ANTHROPIC_API_KEY` /
`CLAUDE_CODE_OAUTH_TOKEN` from the environment, which is the usual choice in CI.

### Removing an account

```bash
brain claude remove claude-account-2
```

This removes the account from configuration and **leaves its profile directory in
place**, because deleting it would revoke a live session. To actually revoke,
run the `claude /logout` command the command prints.

---

## 3. What the swarm does with these accounts

Each enabled account is registered as a first-class execution resource, so it
appears in `brain agents`, `brain health`, the router's candidate set, and
Mission Control, with `authentication_type: subscription` distinguishing the
accounts no secret is held for.

Execution is `claude -p --output-format json`, with `--model`, `--fallback-model`
and `--resume` for continuation.

### Two parsing rules that exist because the CLI lies

Verified against `claude` 2.1.280:

1. **Exit code 0 is not success.** A rate-limited run exits 0 and reports the
   failure only in the JSON body.
2. **`subtype` is not success either.** The same run reported
   `subtype: "success"` while `is_error` was `true`.

`is_error` is therefore the only field that decides, and
`api_error_status` is mapped to a `failure_class` the failover engine can act on:

| Condition | `failure_class` | Failover behaviour |
| --- | --- | --- |
| HTTP 429/529, or a quota phrase in the message | `rate_limit` | move the task to a sibling account |
| HTTP 401/403, or "please run /login" | `auth` | stop; a human must log in |
| anything else | `error` | stop |

This is why multiple accounts are worth adding: when one hits its weekly limit,
the queue keeps moving instead of stalling.

---

## 4. pxpipe: cutting the tokens each account spends

[pxpipe](https://github.com/teamchong/pxpipe) (MIT) is a loopback proxy that
rewrites the bulky parts of an Anthropic request as dense PNG pages before it
leaves the machine. An image's token cost is fixed by its pixel dimensions, not
by how much text is inside it, so token-dense context gets much cheaper.

It compresses three things, each behind a profitability gate: the static system
prompt and tool-doc slab, large `tool_result` bodies, and older history turns.
Recent turns, your own messages and the model's response are never touched.

Measured on a real request from this integration: **116,185 characters of request
context became 30,958 characters plus 16 image pages.**

### Using it

```bash
brain pxpipe status      # is it installed, is it running, where from
brain pxpipe start       # start it on 127.0.0.1:47821
brain pxpipe savings     # measured token savings from its event log
brain pxpipe stop
```

Then enable it per account (`--use-pxpipe` on `add`, or `"use_pxpipe": true` in
`config/providers.json`). Accounts without that flag talk to Anthropic directly.

Configuration lives in the `integrations.pxpipe` block of
`config/providers.json`: port, install location, event log, and the model scope.

### It is opt-in because it is lossy

Imaged content is read through the model's vision channel, which is not OCR.
When pixels underdetermine a glyph, the language prior fills the gap — so a
misread is a **plausible wrong value, not an error**. Exact identifiers (hashes,
IDs, secrets) inside imaged content are not byte-safe. Recent turns stay text,
which is what makes coding sessions tolerable: the agent re-reads files before
editing.

Do not route a task whose correctness depends on transcribing exact strings out
of old history through pxpipe.

### Model scope, and a known upstream bug

The default scope is `claude-fable-5,claude-sonnet-5,claude-opus-5,gemini`.
pxpipe has no measured dense profile for sonnet/opus, so it renders them with its
safer "legible" geometry rather than risking misreads
(`resolveClaudeProfile` in `src/core/claude-model-profiles.ts`).

**Caveat:** upstream issue
[#216](https://github.com/teamchong/pxpipe/issues/216) reports that legible
profile bailing out of history collapse on very long `claude-opus-5` sessions,
which then sends raw history as text and can 400. If you hit that, drop
`claude-opus-5` from the `models` string.

### Security properties

* Bound to `127.0.0.1`, never `0.0.0.0` — a non-loopback bind would expose an
  unauthenticated forwarder that relays whatever credential it is handed.
* Holds no credential of its own. The agent's CLI supplies auth and pxpipe
  forwards it unchanged; it classifies credentials by shape only and refuses to
  send an Anthropic credential to a non-Anthropic upstream.
* `brain pxpipe stop` only stops a proxy the brain started, never one you
  started yourself.
* The `npx` fallback is version-pinned, so a proxy cannot silently change
  behaviour under a running swarm.

### How savings are measured, and what is excluded

pxpipe fires a free `count_tokens` probe on the original uncompressed body in
parallel with each real request, so both sides of the comparison come from the
same request at the same moment.

`brain pxpipe savings` counts a request only when **both** halves exist:

* `unmeasured_requests` — no baseline probe succeeded. Excluded.
* `unbilled_requests` — the request failed upstream (e.g. a 429) so nothing was
  billed. **Excluded**, because a real baseline against zero billed tokens would
  report a fictitious 100% saving. This was a real bug in this integration; it is
  now covered by
  `tests/unit/test_pxpipe_manager.py::test_failed_request_cannot_inflate_savings`.

---

## 5. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `NEEDS LOGIN` in `brain claude list` | that profile has no live credential | run the command from `brain claude login <id>` |
| `You've hit your weekly limit` | subscription quota | add another account; `failure_class: rate_limit` lets the swarm fail over |
| `credential file is readable by other local users` | profile permissions loosened | `chmod 600 <config_dir>/.credentials.json` |
| `brain pxpipe start` fails | not installed | clone pxpipe and set `repo_dir`, or install it; check `runtime/pxpipe-<port>.log` |
| pxpipe running but `compressed: false`, `reason: unsupported_model` | the model id is outside the scope | add it to `integrations.pxpipe.models` |
| Savings show 0 with rows present | all rows unmeasured or unbilled | check `brain pxpipe savings --json` for which bucket they fell into |

## 6. Attribution

pxpipe is © its authors and MIT-licensed; upstream is
[teamchong/pxpipe](https://github.com/teamchong/pxpipe). This project drives it
as an optional external process and vendors none of its code. Its maintainer has
stated in [issue #107](https://github.com/teamchong/pxpipe/issues/107) that he
cannot keep up with issues and PRs, so treat the pinned version as the contract
and re-verify before bumping it.
