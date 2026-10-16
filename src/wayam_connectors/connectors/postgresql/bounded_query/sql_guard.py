"""Static guard for bounded read queries.

The guard is a defence in depth layer: the real protection is a read-only database
role plus `SET TRANSACTION READ ONLY`, a statement timeout and a row cap applied by
the adapter. The guard rejects obvious write attempts, multiple statements and
side-effecting constructs *before* they reach the database, so a product cannot
rely on error messages from the source to learn its structure.
"""

from __future__ import annotations

import re

from wayam_connectors.contracts.errors import PolicyError, ValidationError

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|truncate|drop|alter|create|grant|revoke|vacuum|analyze|analyse|cluster|reindex|"
    r"copy|call|do|execute|prepare|deallocate|listen|notify|unlisten|lock|refresh|set|reset|show|discard|load|"
    r"security|checkpoint|comment|import|begin|commit|rollback|savepoint|release|abort|start|end|declare|fetch|move|close|"
    r"into|returning|pg_sleep|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_terminate_backend|pg_cancel_backend|"
    r"dblink|lo_import|lo_export|nextval|setval|currval)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(with|select|values|table)\b", re.IGNORECASE)
_MAX_LEN = 20_000


def strip_sql_comments_and_strings(sql: str) -> str:
    """Return SQL with string literals, quoted identifiers and comments blanked so keyword checks are not fooled."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                if j == -1:
                    raise ValidationError("unterminated dollar-quoted string")
                out.append("''")
                i = j + len(tag)
                continue
        if ch in ("'", '"'):
            q = ch
            j = i + 1
            while j < n:
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValidationError("unterminated quoted literal")
            out.append("''" if q == "'" else '"x"')
            i = j + 1
            continue
        if ch == "E" and i + 1 < n and sql[i + 1] == "'":  # E'' escape strings: treat like normal, backslashes inside
            j = i + 2
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise ValidationError("unterminated escape string")
            out.append("''")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def guard_read_only(sql: str) -> str:
    """Validate a query is a single read statement. Returns the trimmed SQL (trailing semicolon removed)."""
    if not sql or not sql.strip():
        raise ValidationError("empty query")
    if len(sql) > _MAX_LEN:
        raise PolicyError("query too long", details={"max_chars": _MAX_LEN})
    if "\x00" in sql:
        raise ValidationError("NUL byte in query")
    cleaned = strip_sql_comments_and_strings(sql)
    body = cleaned.strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if ";" in body:
        raise PolicyError("multiple statements are not allowed")
    if not _ALLOWED_START.match(body):
        raise PolicyError("only SELECT/WITH/VALUES/TABLE queries are allowed")
    m = _FORBIDDEN_KEYWORDS.search(body)
    if m:
        raise PolicyError("query contains a forbidden keyword", details={"keyword": m.group(0).lower()})
    trimmed = sql.strip()
    return trimmed[:-1].rstrip() if trimmed.endswith(";") else trimmed


def wrap_with_limit(sql: str, max_rows: int) -> str:
    """Wrap the query so the database itself never returns more than max_rows+1 rows (the +1 detects truncation)."""
    if max_rows < 1:
        raise ValidationError("max_rows must be >= 1")
    return f"SELECT * FROM ({sql}) AS wayam_bounded LIMIT {int(max_rows) + 1}"
