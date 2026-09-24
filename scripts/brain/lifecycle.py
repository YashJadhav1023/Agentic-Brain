"""Safe, dependency-free lifecycle planning primitives for brain history.

This module deliberately performs no filesystem, note-store, or network mutation.
It only validates lifecycle metadata and returns immutable plans for a caller to
review before separately executing an archive or (in the future) a purge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from hashlib import sha256
import re
from typing import Final, Union


ARCHIVE_SOURCE: Final = "handoff/current.md"
ARCHIVE_DIRECTORY: Final = "handoff"
SHA256_LENGTH: Final = 64
_SLUG_PATTERN: Final = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SHA256_PATTERN: Final = re.compile(r"[0-9a-f]{64}")


class LifecycleError(ValueError):
    """Base exception for invalid lifecycle planning input."""


class SlugValidationError(LifecycleError):
    """Raised when a lifecycle slug is not a strict, portable identifier."""


class ArchivePlanValidationError(LifecycleError):
    """Raised when an archive plan is not canonical or internally consistent."""


class PurgeConfirmationRequiredError(LifecycleError):
    """Raised until a caller explicitly confirms a future purge plan."""


class LifecycleState(str, Enum):
    """States available to history records and plans."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"
    QUARANTINED = "quarantined"
    PURGED = "purged"


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """Immutable lifecycle metadata; it never changes a note or its location."""

    slug: str
    state: LifecycleState

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not isinstance(self.state, LifecycleState):
            raise LifecycleError("state must be a LifecycleState")


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    """A fully validated, non-mutating instruction to archive the live baton."""

    slug: str
    archive_date: date
    source: str
    destination: str
    source_sha256: str
    state: LifecycleState = LifecycleState.ARCHIVED

    def __post_init__(self) -> None:
        validate_archive_plan(self)

    def to_dict(self) -> dict[str, str | bool]:
        """Return serializable review output; no archive action is performed."""
        return {
            "action": "archive",
            "mutates_storage": False,
            "slug": self.slug,
            "state": self.state.value,
            "source": self.source,
            "destination": self.destination,
            "source_sha256": self.source_sha256,
            "archive_date": self.archive_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PurgePlan:
    """A future purge instruction only; this module cannot execute it."""

    target: str
    source_sha256: str
    state: LifecycleState = LifecycleState.PURGED
    confirmed: bool = True

    def __post_init__(self) -> None:
        require_explicit_purge_confirmation(self.confirmed)
        _validate_relative_markdown_path(self.target, field="target")
        _validate_sha256(self.source_sha256)
        if self.state is not LifecycleState.PURGED:
            raise LifecycleError("purge plans must have the purged state")

    def to_dict(self) -> dict[str, str | bool]:
        """Return serializable review output; purging remains out of scope."""
        return {
            "action": "purge",
            "mutates_storage": False,
            "target": self.target,
            "source_sha256": self.source_sha256,
            "state": self.state.value,
            "confirmed": self.confirmed,
        }


def validate_slug(slug: str) -> str:
    """Validate and return a canonical lowercase kebab-case archive slug.

    Slugs are intentionally limited to 63 ASCII characters so they are safe in
    file names and URLs. Leading/trailing hyphens, repeated hyphens, whitespace,
    path separators, uppercase letters, and Unicode lookalikes are rejected.
    """
    if not isinstance(slug, str):
        raise SlugValidationError("slug must be a string")
    if not 1 <= len(slug) <= 63 or _SLUG_PATTERN.fullmatch(slug) is None:
        raise SlugValidationError(
            "slug must be 1-63 lowercase ASCII letters/digits separated by single hyphens"
        )
    return slug


def source_hash(source_content: Union[str, bytes]) -> str:
    """Return the SHA-256 hash of supplied content without reading any file."""
    if isinstance(source_content, str):
        payload = source_content.encode("utf-8")
    elif isinstance(source_content, bytes):
        payload = source_content
    else:
        raise TypeError("source_content must be str or bytes")
    return sha256(payload).hexdigest()


def canonical_archive_destination(slug: str, archive_date: date) -> str:
    """Return the sole valid archive destination for a slug and calendar date."""
    validate_slug(slug)
    if not isinstance(archive_date, date):
        raise ArchivePlanValidationError("archive_date must be a datetime.date")
    return f"{ARCHIVE_DIRECTORY}/{archive_date.isoformat()}-{slug}.md"


def build_archive_plan(
    slug: str,
    source_content: Union[str, bytes],
    archive_date: date,
) -> ArchivePlan:
    """Build, validate, and return a non-mutating plan to archive current.md."""
    return ArchivePlan(
        slug=validate_slug(slug),
        archive_date=archive_date,
        source=ARCHIVE_SOURCE,
        destination=canonical_archive_destination(slug, archive_date),
        source_sha256=source_hash(source_content),
    )


def validate_archive_plan(plan: ArchivePlan) -> ArchivePlan:
    """Ensure an archive plan has canonical paths, state, date, and digest."""
    if not isinstance(plan, ArchivePlan):
        raise ArchivePlanValidationError("plan must be an ArchivePlan")
    validate_slug(plan.slug)
    if not isinstance(plan.archive_date, date):
        raise ArchivePlanValidationError("archive_date must be a datetime.date")
    if plan.state is not LifecycleState.ARCHIVED:
        raise ArchivePlanValidationError("archive plans must have the archived state")
    if plan.source != ARCHIVE_SOURCE:
        raise ArchivePlanValidationError(f"archive source must be {ARCHIVE_SOURCE!r}")
    expected_destination = canonical_archive_destination(plan.slug, plan.archive_date)
    if plan.destination != expected_destination:
        raise ArchivePlanValidationError(
            f"destination must be the canonical path {expected_destination!r}"
        )
    _validate_sha256(plan.source_sha256)
    return plan


def require_explicit_purge_confirmation(confirmed: bool) -> None:
    """Require literal ``True`` before a future purge plan can be represented."""
    if confirmed is not True:
        raise PurgeConfirmationRequiredError(
            "future purge planning requires explicit confirmed=True"
        )


def build_purge_plan(
    target: str,
    source_content: Union[str, bytes],
    *,
    confirmed: bool = False,
) -> PurgePlan:
    """Build a confirmed, review-only purge plan; no deletion is ever performed."""
    require_explicit_purge_confirmation(confirmed)
    return PurgePlan(target=target, source_sha256=source_hash(source_content))


def _validate_relative_markdown_path(path: str, *, field: str) -> None:
    if not isinstance(path, str) or not path or "\\" in path:
        raise LifecycleError(f"{field} must be a non-empty POSIX-style relative path")
    components = path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise LifecycleError(f"{field} must not contain empty, '.' or '..' components")
    if not path.endswith(".md"):
        raise LifecycleError(f"{field} must name a Markdown note")


def _validate_sha256(digest: str) -> None:
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise LifecycleError(f"source_sha256 must be {SHA256_LENGTH} lowercase hexadecimal characters")
