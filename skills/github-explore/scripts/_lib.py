"""Shared helpers for github-explore skill scripts.

All scripts in this directory import from _lib. Keep the surface small.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------- stdout encoding ----------
# On Windows, PowerShell defaults to the OEM/ANSI code page (e.g. CP936/GBK),
# so UTF-8 output (★, ✓, →, etc.) renders as ?. Reconfigure stdout/stderr to
# UTF-8 so the agent and humans see the same bytes the script emits. Fail
# silently on Python < 3.7 or non-reconfigurable streams (CI logs etc.).
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---------- gh invocation ----------

def run_gh(args: Sequence[str], timeout: int = 90) -> subprocess.CompletedProcess:
    """Run a gh command; returns the CompletedProcess (callers check
    returncode). Dies only when the binary is missing or the call times out.

    Decodes stdout/stderr as UTF-8 (with replacement on bad bytes) so a
    non-UTF-8 host locale (e.g. CP936/GBK on Chinese Windows) doesn't raise
    UnicodeDecodeError inside subprocess's reader thread and silently turn
    real results into an empty list.
    """
    try:
        return subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        die("`gh` CLI not found on PATH. Install from https://cli.github.com/")
    except subprocess.TimeoutExpired:
        die(f"gh command timed out after {timeout}s: gh {' '.join(args[:3])}...")


def gh_json(args: Sequence[str], timeout: int = 90) -> Any:
    """Run gh with --json and parse output. Returns None if empty."""
    result = run_gh(args, timeout=timeout)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        die(f"gh command failed: gh {' '.join(args[:5])}...\n  {msg}")
    out = (result.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        die(f"Failed to parse gh JSON: {e}\nFirst 200 chars: {out[:200]}")


def gh_text(args: Sequence[str], timeout: int = 90) -> str:
    """Run gh and return stdout text."""
    result = run_gh(args, timeout=timeout)
    if result.returncode != 0:
        msg = (result.stderr or result.stdout or "").strip()
        die(f"gh command failed: gh {' '.join(args[:5])}...\n  {msg}")
    return result.stdout


def ensure_auth() -> None:
    """Verify gh is authenticated; exit with a clear message if not."""
    result = run_gh(["auth", "status"], timeout=10)
    if result.returncode != 0:
        die(
            "Not authenticated with GitHub. Run `gh auth login` first, "
            "or set GH_TOKEN environment variable."
        )


# ---------- output ----------

def info(msg: str) -> None:
    """Progress message to stderr so stdout stays pipeable."""
    print(f"… {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    print(f"⚠  {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


# ---------- output schema (contract) ----------

def print_schema(schema_filename: str, script_label: str) -> None:
    """Print a script's output JSON schema (field contract) and exit 0.

    The schema files live in scripts/schemas/ and are the single source of
    truth for output structure. --schema lets the agent read the contract
    instead of guessing field names (fullName vs full_name, nested keys, ...).
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "schemas", schema_filename)
    if not os.path.exists(path):
        die(f"schema file not found: {path}")
    print(f"# {script_label} output schema (see scripts/schemas/{schema_filename})")
    with open(path, encoding="utf-8") as fh:
        print(fh.read())
    sys.exit(0)


def detect_format(args_format: Optional[str]) -> str:
    """Pick output format: explicit, else markdown.

    Default is markdown (was: table) so callers automatically get the layered
    summary + disk-written full report. table is still selectable via
    --format table; json via --format json.

    Note: we don't auto-switch to json when piped, because the typical caller
    is a terminal that captures stdout via PIPE (sys.stdout.isatty()==False).
    Silently switching them to JSON would skip the disk-write path and the
    user would never see the report landed.
    """
    if args_format:
        return args_format
    return "markdown"


def format_table(rows: List[Dict[str, Any]], columns: List[Tuple[str, str, int]]) -> str:
    """Render a fixed-width table.

    columns: list of (key, header, max_width). max_width=0 means no truncation.
    """
    if not rows:
        return "(no results)"

    widths: Dict[str, int] = {}
    for key, header, max_w in columns:
        cell_width = max(
            len(header),
            *(len(_cell(row.get(key))) for row in rows),
        )
        widths[key] = min(cell_width, max_w) if max_w else cell_width

    def _render_cell(row: Dict[str, Any], key: str, w: int) -> str:
        text = _cell(row.get(key))
        if w and len(text) > w:
            text = text[: max(0, w - 1)] + "…"
        return text.ljust(w)

    header_line = "  ".join(header.ljust(widths[key]) for key, header, _ in columns)
    separator = "  ".join("-" * widths[key] for key, _, _ in columns)
    body = "\n".join(
        "  ".join(_render_cell(row, key, widths[key]) for key, _, _ in columns)
        for row in rows
    )
    return f"{header_line}\n{separator}\n{body}"


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v).replace("\n", " ")


# ---------- time helpers ----------

def parse_since(s: str) -> str:
    """Convert human duration to a GitHub-search date qualifier.

    Examples: '30d' -> '>2025-07-11', '6m' -> '>2025-02-10', '1y' -> '>2024-08-10'
    """
    if not s or len(s) < 2:
        raise ValueError(f"Invalid duration: {s!r}. Use e.g. 30d, 6m, 1y.")

    unit = s[-1].lower()
    try:
        n = int(s[:-1])
    except ValueError as e:
        raise ValueError(f"Invalid duration: {s!r}") from e

    now = datetime.now()
    if unit == "d":
        dt = now - timedelta(days=n)
    elif unit == "w":
        dt = now - timedelta(weeks=n)
    elif unit == "m":
        dt = now - timedelta(days=n * 30)
    elif unit == "y":
        dt = now - timedelta(days=n * 365)
    else:
        raise ValueError(f"Unknown unit {unit!r} in {s!r}. Use d/w/m/y.")
    return dt.strftime("%Y-%m-%d")


def humanize_date(iso: Optional[str]) -> str:
    """YYYY-MM-DD slice of an ISO timestamp; '' if None."""
    if not iso:
        return ""
    return iso[:10]


# ---------- repo filtering helpers ----------

def is_low_quality(repo: Dict[str, Any], min_stars: int = 5) -> bool:
    """Heuristic to drop demo/empty/archived repos from discovery output.

    Accepts BOTH `stargazerCount` (gh repo view, singular) and
    `stargazersCount` (gh search repos, plural) for compatibility.
    """
    stars = repo.get("stargazersCount") or repo.get("stargazerCount") or 0
    if repo.get("isArchived"):
        return True
    if repo.get("isFork") and stars < 50:
        return True
    if stars < min_stars:
        return True
    desc = (repo.get("description") or "").strip()
    if not desc and stars < 100:
        return True
    return False


def dedupe_repos(repos: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Dedupe by fullName, preserving order."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in repos:
        key = r.get("fullName") or r.get("name")
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ---------- search with retry ----------

def _classify_search_error(err: str) -> str:
    """Short, human-readable reason for a failed gh search call."""
    low = err.lower()
    if "rate limit" in low or "403" in err or "429" in err:
        return "rate limit"
    if "invalid search query" in low or "422" in err:
        return "invalid query"
    if "not found" in low or "404" in err:
        return "not found"
    return "gh failed"


def gh_search_with_retry(
    search_type: str,
    query: str,
    extra: Sequence[str],
    max_attempts: int = 3,
    timeout: int = 60,
) -> Tuple[List[Any], str]:
    """Run `gh search <search_type> <query tokens> <extra>` with rate-limit retry.

    Returns (parsed_list_or_empty, reason). reason is '' when the command
    succeeded (even with an empty result set); otherwise a short description
    of what went wrong ('rate limit', 'invalid query', 'timeout', ...).
    Callers should warn() on a non-empty reason and treat an empty list as
    "no usable results" -- NOT as a certain "no matches". Never sys.exits
    (except when the gh binary itself is missing).

    The query is passed as SEPARATE positional arguments (one per whitespace
    token), never as a single quoted argument: gh CLI rewrites a single-arg
    multi-qualifier query into one quoted value for the first qualifier, which
    makes GitHub silently drop every qualifier after it (cli/cli#13678 —
    e.g. `language:` and `stars:>=` vanish after `pushed:>`). Splitting the
    tokens sidesteps the quoting entirely; quoted phrases inside the query
    (e.g. `"exact phrase"`) survive because gh joins the args back with single
    spaces. Scripts should STILL post-filter their results with
    filter_repos()/filter_issues() as defense in depth (GitHub's index can
    leak archived repos even with `archived:false`).
    """
    delay = 2.0
    last_err = ""
    for attempt in range(max_attempts):
        try:
            proc = subprocess.run(
                ["gh", "search", search_type, *query.split(), *extra],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError:
            die("`gh` CLI not found on PATH. Install from https://cli.github.com/")
        except subprocess.TimeoutExpired:
            return [], "timeout"
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            if not out:
                return [], ""
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError as e:
                return [], f"bad JSON: {e}"
            return (parsed if isinstance(parsed, list) else []), ""
        err = (proc.stderr or proc.stdout or "").strip()
        last_err = err
        if "rate limit" in err.lower() or "403" in err or "429" in err:
            warn(
                f"rate limit hit (attempt {attempt + 1}/{max_attempts}), "
                f"sleeping {delay:.0f}s"
            )
            time.sleep(delay)
            delay *= 2
            continue
        # Not a rate limit: the query was likely rejected by GitHub.
        return [], _classify_search_error(err)
    warn(f"giving up after {max_attempts} attempts: {last_err[:120]}")
    return [], "rate limit"


# ---------- defensive post-filtering ----------
#
# gh CLI (and the GitHub search index) do not reliably honor qualifiers:
#   * gh wraps multi-token queries in quotes, so GitHub silently drops every
#     qualifier after the first (language:, topic:, min-stars, ... vanish).
#   * even a plain `archived:false` can leak archived repos (search index lag).
# The qualifiers are still sent (best effort, harmless when honored), and the
# returned JSON fields are then re-checked here so script-level filters hold
# regardless of gh version or GitHub quirks.

def filter_repos(
    repos: Iterable[Dict[str, Any]],
    *,
    include_forks: bool = False,
    include_archived: bool = False,
    min_stars: Optional[int] = None,
    max_stars: Optional[int] = None,
    language: Optional[str] = None,
    pushed_since: Optional[str] = None,
    created_since: Optional[str] = None,
    owner: Optional[str] = None,
    org: Optional[str] = None,
    license_spdx: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Re-apply repo filter flags on the returned JSON fields (post-filter).

    Accepts both `stargazersCount` (gh search repos, plural) and
    `stargazerCount` (gh repo view, singular) star fields.
    """
    out: List[Dict[str, Any]] = []
    for r in repos:
        if not include_archived and r.get("isArchived"):
            continue
        if not include_forks and r.get("isFork"):
            continue
        stars = r.get("stargazersCount") or r.get("stargazerCount") or 0
        if min_stars is not None and stars < min_stars:
            continue
        if max_stars is not None and stars > max_stars:
            continue
        lang = (r.get("language") or "").strip()
        if language and lang.lower() != language.lower():
            continue
        if pushed_since:
            pushed = (r.get("pushedAt") or "")
            if pushed[:10] and pushed[:10] < parse_since(pushed_since):
                continue
        if created_since:
            created = (r.get("createdAt") or "")
            if created[:10] and created[:10] < parse_since(created_since):
                continue
        fn = (r.get("fullName") or "").lower()
        if owner and not fn.startswith(owner.lower() + "/"):
            continue
        if org and not fn.startswith(org.lower() + "/"):
            continue
        if license_spdx:
            lic = r.get("license")
            spdx = ""
            if isinstance(lic, dict):
                spdx = lic.get("spdxId") or lic.get("key") or ""
            elif isinstance(lic, str):
                spdx = lic
            if spdx.lower() != license_spdx.lower():
                continue
        out.append(r)
    return out


def filter_issues(
    issues: Iterable[Dict[str, Any]],
    *,
    state: Optional[str] = None,
    since: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Re-apply issue/PR filter flags on returned JSON fields (post-filter).

    Same rationale as filter_repos: gh's quoting can drop `is:open`,
    `label:`, `updated:>` etc. from the query, so we re-check here.
    """
    out: List[Dict[str, Any]] = []
    for i in issues:
        st = (i.get("state") or "").lower()
        if state and state != "all" and st != state.lower():
            continue
        if since:
            updated = (i.get("updatedAt") or "")
            if updated[:10] and updated[:10] < parse_since(since):
                continue
        if labels:
            names = {(lab.get("name") or "") for lab in (i.get("labels") or [])}
            if not all(lab in names for lab in labels):
                continue
        out.append(i)
    return out
