#!/usr/bin/env python3
"""Guard rails for the handoff baton: validate, redact, commit, archive, brief.

Stdlib only. The `brain` bash wrapper shells out to this module so that the
rules for a valid baton live in exactly one place, and so that every write to
``handoff/current.md`` goes through the same path:

    redact secrets -> validate against the handoff contract -> lock ->
    (optional) detect a concurrent writer -> back up -> atomic replace

Subcommands (all print human-readable text; exit 0 on success, 1 on refusal,
2 on usage error):

    commit  --draft FILE --dest FILE [--expect-sha HEX]
    archive --brain-dir DIR --slug SLUG [--date YYYY-MM-DD]
    slug    FILE                     suggested archive slug from the H1
    brief   FILE                     staleness / status summary for `brain resume`
    sha     FILE                     sha256 of FILE, or empty if missing
    validate-store DIR [--fix]       the `brain validate` report
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

try:  # POSIX advisory locking; absent on Windows, where we degrade to no lock.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
# Token-shaped patterns only: each needs a long run of token characters, so
# prose such as "Bearer token", "Bearer <CLOUDFLARE_API_TOKEN>" or a mention of
# the `sk-` prefix is left alone. Order matters: PEM blocks first.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem-block", re.compile(r"-----BEGIN [A-Z0-9 ]+-----(?:[\s\S]*?-----END [A-Z0-9 ]+-----)?")),
    ("bearer-token", re.compile(r"(?i)(?<=\bBearer )[A-Za-z0-9._~+/-]{20,}=*")),
    ("openai-anthropic-key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("google-oauth-token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("azure-account-key", re.compile(r"(?<=AccountKey=)[A-Za-z0-9+/]{20,}=*")),
)


def find_secrets(text: str) -> list[tuple[str, int]]:
    """Return (kind, line_number) for every secret-shaped match."""
    found: list[tuple[str, int]] = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            found.append((kind, text.count("\n", 0, match.start()) + 1))
    return sorted(found, key=lambda item: item[1])


def redact(text: str) -> tuple[str, list[str]]:
    """Replace secret-shaped values with ``[REDACTED:<kind>]``.

    Returns the new text and the kinds that were redacted. Idempotent: a
    redaction marker never matches any pattern.
    """
    kinds: list[str] = []
    for kind, pattern in SECRET_PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{kind}]", text)
        kinds.extend([kind] * count)
    return text, kinds


# ---------------------------------------------------------------------------
# Handoff contract (mirrors schemas/handoff.md plus cardinality/non-empty)
# ---------------------------------------------------------------------------
STATUS_VALUES = ("in-progress", "blocked", "done")
# tag -> exactly one (True) or at least one (False)
REQUIRED_TAGS = {"status": True, "agent": True, "scope": True, "next": True, "blocker": True, "done": False}


def split_frontmatter(content: str) -> tuple[str, str] | None:
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def observations(body: str, tag: str) -> list[str]:
    # [ \t]* rather than \s*: \s would cross the newline after an empty
    # observation and capture the next line as its value, so "- [next] " followed
    # by "- [file] x" used to pass as a non-empty [next].
    return [m.strip() for m in re.findall(rf"^-[ \t]*\[{re.escape(tag)}\][ \t]*(.*)$", body, re.MULTILINE)]


def frontmatter_value(fm: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", fm, re.MULTILINE)
    return match.group(1).strip() if match else ""


def status_hint(value: str) -> str | None:
    """Suggest the fix for `[status] done — archived ...` style prose."""
    head = re.match(r"(in-progress|blocked|done)\b\W*(.*)$", value, re.IGNORECASE)
    if head and head.group(1).lower() in STATUS_VALUES:
        rest = head.group(2).strip()
        extra = f" and move the prose to its own line: '- [note] {rest}'" if rest else ""
        return f"write '- [status] {head.group(1).lower()}'{extra}"
    return None


def validate_note(content: str, rel_path: str) -> tuple[list[str], list[str]]:
    """Validate one note. Returns (errors, hints)."""
    errors: list[str] = []
    hints: list[str] = []
    parts = split_frontmatter(content)
    if not content.startswith("---"):
        return ["missing YAML frontmatter opening '---'"], hints
    if parts is None:
        return ["unclosed YAML frontmatter"], hints
    fm, raw_body = parts
    body = raw_body.strip()

    title = frontmatter_value(fm, "title")
    if not title:
        return ["frontmatter missing non-empty 'title'"], hints
    note_type = frontmatter_value(fm, "type")
    if not note_type:
        return ["frontmatter missing non-empty 'type'"], hints
    if not re.search(r"^#\s+(.+)$", body, re.MULTILINE):
        return ["body missing Level-1 heading '# <Title>'"], hints

    if note_type == "handoff":
        if rel_path == "handoff/current.md" and title != "current":
            errors.append(f"expected frontmatter title 'current', got '{title}'")
        if "## Observations" not in body:
            errors.append("missing '## Observations' section")
        else:
            for tag, single in REQUIRED_TAGS.items():
                values = observations(body, tag)
                if not values:
                    errors.append(f"missing mandatory observation tag '- [{tag}]'")
                    continue
                if single and len(values) > 1:
                    errors.append(
                        f"[{tag}] appears {len(values)} times; exactly one is allowed "
                        f"(conflicting values: {', '.join(v or '<empty>' for v in values)})"
                    )
                    hints.append(f"keep only the latest '- [{tag}]' line")
                if any(not v for v in values):
                    errors.append(f"[{tag}] present but empty")
            for value in observations(body, "status"):
                if value not in STATUS_VALUES:
                    errors.append(
                        f"invalid [status] '{value}' (must be exactly one of: "
                        f"{', '.join(sorted(STATUS_VALUES))})"
                    )
                    hint = status_hint(value)
                    if hint:
                        hints.append(hint)

    for kind, line in find_secrets(content):
        errors.append(f"possible secret ({kind}) on line {line}; refer to secrets by name only")
        hints.append("run `brain validate --fix` to redact, then rotate the exposed credential")
    return errors, hints


def fix_note(content: str) -> str:
    """Apply the mechanical, meaning-preserving fixes only.

    * ``- [status] done — archived 2026-08-27`` becomes ``- [status] done``
      followed by ``- [note] archived 2026-08-27``.
    * secret-shaped values are redacted.
    """
    def _status(match: re.Match[str]) -> str:
        value = match.group(2).strip()
        if value in STATUS_VALUES:
            return match.group(0)
        head = re.match(r"(in-progress|blocked|done)\b\W*(.*)$", value, re.IGNORECASE)
        if not head:
            return match.group(0)
        line = f"{match.group(1)}[status] {head.group(1).lower()}"
        rest = head.group(2).strip()
        return f"{line}\n{match.group(1)}[note] {rest}" if rest else line

    content = re.sub(r"^(-[ \t]*)\[status\][ \t]*(.*)$", _status, content, flags=re.MULTILINE)
    content, _ = redact(content)
    return content


def validate_store(brain_dir: Path, fix: bool = False) -> int:
    if not brain_dir.is_dir():
        print(f"Error: Brain directory not found at {brain_dir}", file=sys.stderr)
        return 1
    md_files = sorted(brain_dir.rglob("*.md"))
    if not md_files:
        print(f"No markdown files found in {brain_dir}")
        return 0
    print(f"Validating {len(md_files)} markdown notes in {brain_dir}...\n")
    errors = valid = fixed = 0
    for path in md_files:
        rel = path.relative_to(brain_dir).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[FAIL] {rel}: unable to read file ({exc})")
            errors += 1
            continue
        problems, hints = validate_note(content, rel)
        if problems and fix:
            repaired = fix_note(content)
            if repaired != content:
                atomic_write(path, repaired)
                fixed += 1
                print(f"[FIXED] {rel}")
                problems, hints = validate_note(repaired, rel)
        if problems:
            print(f"[FAIL] {rel}: " + "; ".join(problems))
            for hint in dict.fromkeys(hints):
                print(f"       hint: {hint}")
            errors += 1
            continue
        print(f"[OK]   {rel}")
        valid += 1
    print(f"\nValidation complete: {valid}/{len(md_files)} notes valid.")
    if fixed:
        print(f"{fixed} note(s) repaired by --fix.")
    if errors:
        print(f"{errors} error(s) detected.", file=sys.stderr)
        if not fix:
            print("Tip: `brain validate --fix` repairs prose after [status] and redacts secrets.", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Atomic, locked writes
# ---------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def handoff_lock(handoff_dir: Path, timeout: float = 30.0):
    """Serialise writers of handoff/ across processes (advisory flock)."""
    handoff_dir.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover
        yield
        return
    lock_path = handoff_dir / ".handoff.lock"
    with open(lock_path, "a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"another agent holds {lock_path} (waited {timeout:.0f}s)")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Refused(Exception):
    """A write was refused; the message says why and how to fix it."""


def commit_baton(draft_text: str, dest: Path, expect_sha: str | None = None) -> list[str]:
    """Redact, validate and atomically install a new handoff baton.

    ``expect_sha`` is the sha256 of ``dest`` when the caller read it (amend
    mode); an empty string means "dest did not exist". If ``dest`` changed in
    the meantime another agent checkpointed concurrently and the write is
    refused rather than silently discarding their baton.

    Returns warnings (e.g. what was redacted). Raises Refused.
    """
    if not draft_text.strip():
        raise Refused(
            "checkpoint refused: the baton is empty (stdin was empty or not a terminal). "
            "Pipe a complete note, or pass --task/--done/--next flags."
        )
    text, redacted = redact(draft_text)
    warnings = [f"redacted a {kind} before writing; refer to secrets by name only" for kind in redacted]
    problems, hints = validate_note(text, "handoff/current.md")
    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        detail += "".join(f"\n  hint: {h}" for h in dict.fromkeys(hints))
        raise Refused(f"checkpoint refused: the baton does not satisfy schemas/handoff.md\n{detail}")
    try:
        with handoff_lock(dest.parent):
            if expect_sha is not None and sha256_of(dest) != expect_sha:
                raise Refused(
                    "checkpoint refused: handoff/current changed while you were editing it "
                    "(another agent checkpointed). Re-run `brain resume`, then checkpoint again."
                )
            if dest.exists():
                atomic_write(dest.with_name(dest.name + ".bak"), dest.read_text(encoding="utf-8"))
            atomic_write(dest, text)
    except TimeoutError as exc:
        raise Refused(f"checkpoint refused: {exc}") from exc
    return warnings


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def slugify(title: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > limit:
        slug = slug[:limit].rsplit("-", 1)[0] or slug[:limit]
    return slug.strip("-") or "handoff"


def h1_title(content: str) -> str:
    match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def rewrite_for_archive(content: str, slug: str, date_str: str) -> str:
    permalink = f"handoff/{date_str}-{slug}"

    def _mark_done(body: str) -> str:
        prior = observations(body, "status")
        body = re.sub(r"^-[ \t]*\[status\][ \t]+.*$", "- [status] done", body, flags=re.MULTILINE, count=1)
        if prior and prior[0] not in ("done",):
            # Never silently claim a state that was not reached: keep a record
            # of what the status was when the baton was retired.
            body = body.replace(
                "- [status] done", f"- [status] done\n- [archived] {date_str}; status was {prior[0]}", 1
            )
        return body

    parts = split_frontmatter(content)
    if parts is None:
        fm = f"\ntitle: {slug}\ntype: handoff\npermalink: {permalink}\ntags:\n- handoff\n- done\n"
        return f"---{fm}---\n{_mark_done(content)}"
    fm, body = parts
    if re.search(r"^title:.*$", fm, flags=re.MULTILINE):
        fm = re.sub(r"^title:.*$", f"title: {slug}", fm, flags=re.MULTILINE, count=1)
    else:
        fm = f"title: {slug}\n" + fm
    if re.search(r"^permalink:.*$", fm, flags=re.MULTILINE):
        fm = re.sub(r"^permalink:.*$", f"permalink: {permalink}", fm, flags=re.MULTILINE, count=1)
    else:
        fm = fm.rstrip() + f"\npermalink: {permalink}\n"
    return f"---{fm}---{_mark_done(body)}"


def archive_baton(brain_dir: Path, slug: str, today: date | None = None) -> Path:
    """Move handoff/current.md to handoff/<date>-<slug>.md; never overwrite."""
    if not SLUG_RE.fullmatch(slug or ""):
        raise Refused(
            f"invalid slug '{slug}': use lowercase-kebab-case, e.g. '{slugify(slug or 'handoff')}'"
        )
    handoff_dir = brain_dir / "handoff"
    src = handoff_dir / "current.md"
    date_str = (today or date.today()).isoformat()
    dest = handoff_dir / f"{date_str}-{slug}.md"
    try:
        with handoff_lock(handoff_dir):
            if not src.exists():
                raise Refused("no handoff/current to archive")
            if dest.exists():
                raise Refused(f"already exists: {dest} (pick another slug, e.g. '{slug}-2')")
            content = rewrite_for_archive(src.read_text(encoding="utf-8"), slug, date_str)
            fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=handoff_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp, 0o644)
                # link() fails if dest exists, so even a writer that ignores the
                # lock can never be overwritten.
                os.link(tmp, dest)
            except FileExistsError as exc:
                raise Refused(f"already exists: {dest}") from exc
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
            src.unlink()
    except TimeoutError as exc:
        raise Refused(str(exc)) from exc
    return dest


# ---------------------------------------------------------------------------
# Resume briefing
# ---------------------------------------------------------------------------
def humanize_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def brief(path: Path, now: float | None = None, stale_hours: float = 24.0) -> list[str]:
    content = path.read_text(encoding="utf-8")
    body = (split_frontmatter(content) or ("", content))[1]
    status = (observations(body, "status") or ["?"])[0] or "?"
    agent = (observations(body, "agent") or ["unknown agent"])[0] or "unknown agent"
    nxt = (observations(body, "next") or [""])[0]
    mtime = path.stat().st_mtime
    age = (now if now is not None else time.time()) - mtime
    stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    lines = [
        f"[brain] handoff/current: status {status}, last touched {humanize_age(age)} ago "
        f"by {agent} ({stamp})",
    ]
    if nxt:
        lines.append(f"[brain] next: {nxt}")
    if status == "done":
        lines.append(
            "[brain] WARNING: this task is done - archive it before starting new work: "
            f"brain archive {slugify(h1_title(content))}"
        )
    elif age > stale_hours * 3600:
        lines.append(
            f"[brain] WARNING: baton is {humanize_age(age)} old - confirm it is still the live task"
        )
    problems, _ = validate_note(content, "handoff/current.md")
    if problems:
        lines.append(f"[brain] WARNING: baton fails validation ({problems[0]}); run `brain validate`")
    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="handoff_guard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--draft", required=True)
    p_commit.add_argument("--dest", required=True)
    p_commit.add_argument("--expect-sha", default=None)
    p_archive = sub.add_parser("archive")
    p_archive.add_argument("--brain-dir", required=True)
    p_archive.add_argument("--slug", required=True)
    p_archive.add_argument("--date", default=None)
    for name in ("slug", "brief", "sha"):
        sub.add_parser(name).add_argument("file")
    p_validate = sub.add_parser("validate-store")
    p_validate.add_argument("dir")
    p_validate.add_argument("--fix", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "commit":
            draft = Path(args.draft).read_text(encoding="utf-8")
            for warning in commit_baton(draft, Path(args.dest), args.expect_sha):
                print(f"brain: warning: {warning}", file=sys.stderr)
            return 0
        if args.cmd == "archive":
            when = date.fromisoformat(args.date) if args.date else None
            dest = archive_baton(Path(args.brain_dir), args.slug, when)
            print(dest)
            return 0
        if args.cmd == "slug":
            print(slugify(h1_title(Path(args.file).read_text(encoding="utf-8"))))
            return 0
        if args.cmd == "brief":
            print("\n".join(brief(Path(args.file))))
            return 0
        if args.cmd == "sha":
            print(sha256_of(Path(args.file)))
            return 0
        if args.cmd == "validate-store":
            return validate_store(Path(args.dir), fix=args.fix)
    except Refused as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 1
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
