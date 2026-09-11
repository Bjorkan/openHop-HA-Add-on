#!/usr/bin/env python3
"""Small, dependency-free helpers for the Home Assistant app bootstrap."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_PR_BARE_NUMBER = re.compile(r"^(\d+)$")
_PR_HASHED_NUMBER = re.compile(r"^#(\d+)$")
_PR_PREFIXED_NUMBER = re.compile(r"(?i)^pr[\s#\-_]*#?[\s#\-_]*(\d+)$")
_PR_REF = re.compile(r"(?i)^(?:refs/)?pull/(\d+)(?:/(head|merge))?$")
_OPTIONS_KEY = "branch_or_pr"
_OPTIONS_ENV_OVERRIDE = "OPENHOP_ADDON_SOURCE_REF"
_OPTIONS_DEFAULT = "main"
_OPTIONS_DEFAULT_PATH = Path("/data/options.json")
_DIST_INFO_GLOB = "openhop_repeater-*.dist-info/direct_url.json"
_ALLOWED_REPOSITORY_URLS = {
    "https://github.com/openhop-dev/openhop_repeater",
    "https://github.com/openhop-dev/openhop_repeater.git",
}
_PIP_INSTALL_PREFIX = (
    "openhop_repeater[hardware] @ "
    "git+https://github.com/openhop-dev/openhop_repeater.git@"
)


def _is_valid_pull_number(number: str) -> bool:
    """A GitHub pull number is a positive integer without leading zeros."""
    return bool(number) and number.isdigit() and not number.startswith("0")


def pr_number_to_ref(number: str) -> str:
    """Map a bare PR number such as ``42`` to the GitHub pull-request ref."""
    return f"refs/pull/{number}/head"


def pr_ref_from_number(number: str) -> str | None:
    """Return the pull-request ref for valid numbers, else ``None``."""
    if _is_valid_pull_number(number):
        return pr_number_to_ref(number)
    return None


def normalize_source_ref(raw: object) -> str:
    """Normalize a user-supplied branch or pull-request value to a git ref.

    Accepted inputs:
      - branch names such as ``main`` or ``dev``;
      - bare PR numbers (int or str) such as ``42`` or ``"42"``;
      - PR spellings such as ``#42``, ``pr 42``, ``PR-42``, ``PR #42``;
      - full pull refs such as ``pull/42/head`` or ``refs/pull/42/head``.
    """
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return pr_number_to_ref(str(raw)) if _is_valid_pull_number(str(raw)) else ""
    if isinstance(raw, float):
        if not raw.is_integer():
            return ""
        return normalize_source_ref(int(raw))
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value:
        return ""

    bare = _PR_BARE_NUMBER.fullmatch(value)
    if bare:
        return (
            pr_number_to_ref(bare.group(1))
            if _is_valid_pull_number(bare.group(1))
            else ""
        )

    hashed = _PR_HASHED_NUMBER.fullmatch(value)
    if hashed:
        return (
            pr_number_to_ref(hashed.group(1))
            if _is_valid_pull_number(hashed.group(1))
            else ""
        )

    prefixed = _PR_PREFIXED_NUMBER.fullmatch(value)
    if prefixed:
        number = prefixed.group(1)
        return pr_number_to_ref(number) if _is_valid_pull_number(number) else ""

    pull = _PR_REF.fullmatch(value)
    if pull:
        number = pull.group(1)
        if _is_valid_pull_number(number):
            qualifier = (pull.group(2) or "head").lower()
            if qualifier == "merge":
                return f"refs/pull/{number}/merge"
            return f"refs/pull/{number}/head"
        return ""

    if is_valid_git_ref(value):
        return value
    return ""


def looks_like_pr(value: str) -> bool:
    """Return True when a normalized ref addresses a pull request."""
    return value.startswith("refs/pull/")


def read_desired_ref(options_path: Path | str | None = None) -> str:
    """Read the requested branch/PR from Home Assistant app options.

    Lenient wrapper around :func:`resolve_desired_ref`: a missing options
    file, a missing ``branch_or_pr`` key, or an empty value selects the
    default branch. Invalid explicit values also fall back to the default
    here; bootstraps that must refuse to start should use
    :func:`resolve_desired_ref` and honor its error.
    """
    ref, _ = resolve_desired_ref(options_path)
    return ref


def resolve_desired_ref(
    options_path: Path | str | None = None,
) -> tuple[str, str | None]:
    """Resolve the requested branch/PR, returning ``(ref, error)``.

    ``error`` is ``None`` on success. A missing options file, a missing
    ``branch_or_pr`` key, or an empty value selects the default branch. An
    explicitly configured value that cannot be normalized is an error: the
    caller should refuse to start rather than silently run other code.
    ``OPENHOP_ADDON_SOURCE_REF`` overrides the options file so tests and
    local runs can inject a value.
    """
    override_env = (os.environ.get(_OPTIONS_ENV_OVERRIDE) or "").strip()
    if override_env:
        normalized = normalize_source_ref(override_env)
        if not normalized:
            return (
                _OPTIONS_DEFAULT,
                f"invalid {_OPTIONS_ENV_OVERRIDE} value {override_env!r}: "
                "use a branch name (for example 'main') or a pull-request "
                "number (for example '42')",
            )
        return (normalized, None)

    if options_path is None:
        options_file = _OPTIONS_DEFAULT_PATH
    else:
        options_file = Path(options_path)
    try:
        data = json.loads(options_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (_OPTIONS_DEFAULT, None)
    if not isinstance(data, dict):
        return (_OPTIONS_DEFAULT, None)
    raw = data.get(_OPTIONS_KEY)
    if raw is None:
        return (_OPTIONS_DEFAULT, None)
    if isinstance(raw, str) and not raw.strip():
        return (_OPTIONS_DEFAULT, None)
    normalized = normalize_source_ref(raw)
    if not normalized:
        return (
            _OPTIONS_DEFAULT,
            f"invalid '{_OPTIONS_KEY}' app option {raw!r} in {options_file}: "
            "use a branch name (for example 'main') or a pull-request "
            "number (for example '42')",
        )
    return (normalized, None)


def is_valid_git_ref(ref: str) -> bool:
    """Validate a branch-like Git ref without invoking a shell or Git."""
    if not ref or len(ref.encode("utf-8")) > 255:
        return False
    if ref == "@" or ref.startswith("-"):
        return False
    if not _SAFE_REF.fullmatch(ref):
        return False
    if ".." in ref or "@{" in ref or "//" in ref:
        return False
    if ref.startswith(("/", ".")) or ref.endswith(("/", ".", ".lock")):
        return False

    for component in ref.split("/"):
        if (
            not component
            or component.startswith(".")
            or component.endswith((".", ".lock"))
        ):
            return False
    return True


def is_valid_source_ref(ref: str) -> bool:
    """Validate a normalized branch ref or pull-request ref."""
    if normalize_source_ref(ref) != ref:
        return False
    if looks_like_pr(ref):
        return True
    return is_valid_git_ref(ref)


def _candidate_direct_urls(site_packages: Path) -> Iterable[Path]:
    candidates: list[tuple[int, Path]] = []
    for path in site_packages.glob(_DIST_INFO_GLOB):
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified, path))
    ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
    return (path for _, path in ordered)


def installed_ref(site_packages: Path) -> str:
    """Return the requested VCS revision for the newest installed distribution."""
    for direct_url_path in _candidate_direct_urls(site_packages):
        try:
            data = json.loads(direct_url_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        url = str(data.get("url", "")).lower().rstrip("/")
        if url not in _ALLOWED_REPOSITORY_URLS:
            continue

        vcs_info = data.get("vcs_info")
        if not isinstance(vcs_info, dict) or vcs_info.get("vcs") != "git":
            continue

        revision = vcs_info.get("requested_revision")
        if (
            isinstance(revision, str)
            and revision
            and (is_valid_git_ref(revision) or looks_like_pr(revision))
            and normalize_source_ref(revision) == revision
        ):
            return revision
    return ""


def are_safe_pip_args(args: list[str]) -> bool:
    """Allow only version checks and the branch-update commands the app issues."""
    if args in (["--version"], ["-V"]):
        return True

    allowed_prefixes = (
        ["install", "--upgrade", "--no-cache-dir"],
        ["install", "--upgrade", "--force-reinstall", "--no-cache-dir"],
    )
    matching_prefix = next(
        (prefix for prefix in allowed_prefixes if args[:-1] == prefix),
        None,
    )
    if matching_prefix is None or len(args) != len(matching_prefix) + 1:
        return False

    install_spec = args[-1]
    if not install_spec.startswith(_PIP_INSTALL_PREFIX):
        return False
    return is_valid_source_ref(install_spec[len(_PIP_INSTALL_PREFIX) :])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("ref")

    validate_source = subparsers.add_parser("validate-source")
    validate_source.add_argument("ref")

    normalize = subparsers.add_parser("normalize")
    normalize.add_argument("value")

    desired = subparsers.add_parser("desired-ref")
    desired.add_argument(
        "--options",
        dest="options",
        type=Path,
        default=_OPTIONS_DEFAULT_PATH,
    )
    desired.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when the configured value is invalid",
    )

    installed = subparsers.add_parser("installed-ref")
    installed.add_argument("site_packages", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # pip options are intentionally opaque here. argparse otherwise tries to
    # interpret flags such as --version before REMAINDER can capture them.
    if raw_args[:1] == ["validate-pip-args"]:
        return 0 if are_safe_pip_args(raw_args[1:]) else 1

    args = _build_parser().parse_args(raw_args)
    if args.command == "validate":
        return 0 if is_valid_git_ref(args.ref) else 1
    if args.command == "validate-source":
        return 0 if is_valid_source_ref(args.ref) else 1
    if args.command == "normalize":
        normalized = normalize_source_ref(args.value)
        if not normalized:
            return 1
        print(normalized)
        return 0
    if args.command == "desired-ref":
        ref, error = resolve_desired_ref(args.options)
        if error is not None:
            print(error, file=sys.stderr)
            if args.strict:
                return 1
        print(ref)
        return 0
    if args.command == "installed-ref":
        print(installed_ref(args.site_packages))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
