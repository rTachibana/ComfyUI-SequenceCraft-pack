"""
Dynamic prompts parser.

Supported syntax
----------------
{A|B|C}
    Randomly pick one option.

{2::A|B}
    Weighted pick — A is twice as likely as B.

{A|{B|C}}
    Nesting is supported.

__name__
    Replace with a random line from wildcards/name.txt (also .json/.yaml).

__dir/name__
    Wildcard in a subdirectory.

# comment
    Lines (or line portions) starting with # are treated as comments and
    stripped from the output.  Inside a variant group, options that reduce
    to empty after comment stripping are excluded from the draw.

${var=!value}
    Declare variable ``var`` with IMMEDIATE evaluation.
    The value is resolved once at declaration time; every reference to
    ``${var}`` returns that same result.
    Example: ${h=!{red|blue}} → always the same colour throughout the prompt.

${var=value}
    Declare variable ``var`` with DEFERRED evaluation.
    The value is re-evaluated fresh on every ``${var}`` reference.
    Example: ${h={red|blue}} → may produce different colours at each use.

${var?=!value}
    Declare variable ``var`` only if it is NOT already set.
    Useful for providing defaults in reusable templates.

${@{ name: value … | name: value … }}
    SET: pick ONE option and declare all of its ``name: value`` lines at once
    (same line format as the preset slot), so related parts stay together:
        ${@{
        bottom: pleated skirt
        wind: skirt fluttering in the wind
        |
        bottom: denim shorts
        wind: hair blowing in the wind
        }}
    Values are resolved immediately (like ``=!``). ``2::`` at the start of an
    option weights it.  Options are separated by a ``|`` outside ``{…}``.

${if var~text: then | else}
    CONDITION: output ``then`` if the variable's value matches, else ``else``
    (the ``| else`` part is optional).  Operators (case-insensitive):
    ``~`` contains, ``!~`` does not contain, ``=`` equals, ``!=`` differs.
    An undeclared variable counts as "".

${var}
    Read variable ``var`` (error if not declared).

${var:default}
    Read variable ``var``, or use ``default`` if it is not declared.

Notes
-----
- Variable declarations produce no output (they are replaced by "").
- Variable and variant/wildcard syntax can be freely mixed.
- Variables are resolved before {A|B} expansion so that ``${var=!{A|B}}``
  works correctly: the variant is expanded once inside the declaration.
"""

from __future__ import annotations

import json
import re
import random
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_VARIANT_PASSES = 200
_MAX_RESOLVE_ROUNDS = 20

_VARIANT_RE  = re.compile(r"(?<!\$)\{([^{}]*)\}")
_WILDCARD_RE = re.compile(r"__(!?)([a-zA-Z0-9_/\\.-]+)__")

# Matches the *inner* content of ${…} as a declaration.
# Groups: name, preserve ('?' or ''), immediate ('!' or ''), value
_VAR_DECL_INNER_RE = re.compile(
    r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_-]*)(?P<preserve>\??)=(?P<immediate>!?)(?P<value>.*)",
    re.DOTALL,
)

# Validates a bare variable name (used when parsing accesses).
_VAR_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")


# ---------------------------------------------------------------------------
# Wildcard file loading
# ---------------------------------------------------------------------------

def _load_txt(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _load_json(text: str) -> list[str]:
    data = json.loads(text)
    if isinstance(data, list):
        return [str(v) for v in data if str(v).strip()]
    return []


def _load_yaml(text: str) -> list[str]:
    if not _HAS_YAML:
        return []
    data = _yaml.safe_load(text)
    if isinstance(data, list):
        return [str(v) for v in data if str(v).strip()]
    return []


def load_wildcard_file(path: Path) -> list[str]:
    """Load wildcard values from a file. Returns an empty list on any error."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    ext = path.suffix.lower()
    if ext == ".txt":
        return _load_txt(text)
    if ext == ".json":
        return _load_json(text)
    if ext in (".yaml", ".yml"):
        return _load_yaml(text)
    return []


WILDCARD_EXTS = (".txt", ".yaml", ".yml", ".json")
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9_.-]+(?:[/\\][a-zA-Z0-9_.-]+)*$")


def list_wildcards(wildcards_dir: Path) -> list[str]:
    """Names (``dir/name`` without extension) of all wildcard files."""
    names: set[str] = set()
    if wildcards_dir.is_dir():
        for p in wildcards_dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in WILDCARD_EXTS:
                names.add(p.relative_to(wildcards_dir).with_suffix("").as_posix())
    return sorted(names, key=str.lower)


def safe_wildcard_name(name: str) -> bool:
    """True if *name* stays inside the wildcards folder (no ``..``, no absolute path)."""
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
        return False
    return all(part not in ("", ".", "..") for part in name.replace("\\", "/").split("/"))


def find_wildcard(name: str, wildcards_dir: Path) -> list[str]:
    """
    Find and load values for a wildcard name.
    ``name`` may contain '/' or '\\' as subdirectory separators.
    Returns an empty list if nothing is found (or the name would leave the folder).
    """
    if not safe_wildcard_name(name):
        return []
    parts = name.replace("\\", "/").split("/")
    rel = Path(*parts)
    for ext in (".txt", ".yaml", ".yml", ".json"):
        candidate = wildcards_dir / (str(rel) + ext)
        if candidate.is_file():
            values = load_wildcard_file(candidate)
            if values:
                return values
    return []


# ---------------------------------------------------------------------------
# Syntax validation
# ---------------------------------------------------------------------------

def _src_location(text: str, pos: int) -> str:
    """Return a human-readable 'line N, col N: …snippet…' string for *pos*."""
    line = text[:pos].count("\n") + 1
    col  = pos - text[:pos].rfind("\n")
    s = max(0, pos - 18)
    e = min(len(text), pos + 18)
    snippet = text[s:e].replace("\n", "↵")
    if s > 0:
        snippet = "…" + snippet
    if e < len(text):
        snippet = snippet + "…"
    return f"line {line}, col {col}: {snippet!r}"


def _check_braces(text: str) -> list[str]:
    """Report unmatched ``{`` / ``}`` in variant syntax (skips ``${…}`` blocks)."""
    errors: list[str] = []
    stack: list[int] = []
    i = 0
    while i < len(text):
        if text[i] == "$" and i + 1 < len(text) and text[i + 1] == "{":
            end = _find_variable_end(text, i)
            if end == -1:
                errors.append(f"Unclosed '${{' at {_src_location(text, i)}")
                i += 2
            else:
                i = end + 1
            continue
        if text[i] == "{":
            stack.append(i)
        elif text[i] == "}":
            if stack:
                stack.pop()
            else:
                errors.append(f"Unexpected '}}' at {_src_location(text, i)}")
        i += 1
    for pos in stack:
        errors.append(f"Unclosed '{{' at {_src_location(text, pos)}")
    return errors


def _check_wildcards(text: str) -> list[str]:
    """Report ``__`` markers that are not part of a valid ``__name__`` wildcard."""
    valid_positions: set[int] = set()
    for m in _WILDCARD_RE.finditer(text):
        valid_positions.add(m.start())       # opening __
        valid_positions.add(m.end() - 2)    # closing __
    errors: list[str] = []
    for m in re.finditer(r"__", text):
        if m.start() not in valid_positions:
            errors.append(f"Unclosed or malformed wildcard at {_src_location(text, m.start())}")
    return errors


def validate_syntax(text: str) -> list[str]:
    """
    Return a list of syntax error messages for *text*.
    An empty list means the text is valid.

    Comments (``#`` to end of line) are stripped before checking so that
    ``}`` or ``__`` appearing after ``#`` are not counted as closing tokens.
    """
    stripped = _strip_line_comments(text)
    return _check_braces(stripped) + _check_wildcards(stripped)


def diagnose(text: str, wildcards_dir: Path | None = None) -> list[dict[str, Any]]:
    """
    Like :func:`validate_syntax`, but structured for editors:
    ``[{"code", "line", "col", "name"?}]`` sorted by position.

    Codes: ``unclosed_var`` (``${`` without ``}``), ``bad_set`` (``${@{…}}`` with
    no ``name: value`` line), ``bad_if`` (``${if …}`` not in ``if name~text: …`` form), ``unclosed_brace``,
    ``unexpected_brace``, ``bad_wildcard`` (stray ``__``) and, when
    *wildcards_dir* is given, ``unknown_wildcard`` (no such file).
    Comments are stripped first; line/column still match the original text.
    """
    stripped = _strip_line_comments(text)
    found: list[tuple[int, dict[str, Any]]] = []

    def at(pos: int, code: str, **extra: Any) -> None:
        line = stripped[:pos].count("\n") + 1
        col = pos - stripped[:pos].rfind("\n")
        found.append((pos, {"code": code, "line": line, "col": col, **extra}))

    stack: list[int] = []
    i = 0
    while i < len(stripped):
        if stripped[i] == "$" and i + 1 < len(stripped) and stripped[i + 1] == "{":
            end = _find_variable_end(stripped, i)
            if end == -1:
                at(i, "unclosed_var")
                i += 2
            else:
                inner = stripped[i + 2:end]
                if inner.startswith("@") and (not inner.startswith("@{") or not _set_options(inner[2:-1])):
                    at(i, "bad_set")
                elif re.match(r"if\s", inner) and not _IF_RE.match(inner):
                    at(i, "bad_if")
                i = end + 1
            continue
        if stripped[i] == "{":
            stack.append(i)
        elif stripped[i] == "}":
            if stack:
                stack.pop()
            else:
                at(i, "unexpected_brace")
        i += 1
    for pos in stack:
        at(pos, "unclosed_brace")

    valid: set[int] = set()
    for m in _WILDCARD_RE.finditer(stripped):
        valid.update((m.start(), m.end() - 2))
        if wildcards_dir is not None and not find_wildcard(m.group(2), wildcards_dir):
            at(m.start(), "unknown_wildcard", name=m.group(2))
    for m in re.finditer(r"__", stripped):
        if m.start() not in valid:
            at(m.start(), "bad_wildcard")

    return [d for _, d in sorted(found, key=lambda x: x[0])]


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------

def _strip_line_comments(text: str) -> str:
    """Remove ``#``-to-end-of-line comments from every line in *text*."""
    lines = []
    for line in text.splitlines():
        comment_pos = line.find("#")
        lines.append(line[:comment_pos] if comment_pos != -1 else line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Variant resolution  {A|B|C}
# ---------------------------------------------------------------------------

def _weighted_choice(raw_options: list[str], rng: random.Random) -> str:
    """
    Pick one option, honouring an ``N::text`` weight prefix.
    e.g. ``["2::cat", "dog"]`` → cat has 2× the probability of dog.

    Options that are empty (or become empty after comment stripping) are
    excluded from the draw.
    """
    items: list[str] = []
    weights: list[float] = []
    for opt in raw_options:
        clean = _strip_line_comments(opt).strip()
        if not clean:
            continue
        if "::" in clean:
            prefix, _, body = clean.partition("::")
            try:
                items.append(body)
                weights.append(float(prefix))
                continue
            except ValueError:
                pass
        items.append(clean)
        weights.append(1.0)
    if not items:
        return ""
    return rng.choices(items, weights=weights, k=1)[0]


def resolve_variants(
    text: str, rng: random.Random, wildcards_dir: Path, context: dict[str, Any]
) -> str:
    """Replace all {A|B|C} groups with a random choice (innermost first)."""
    for _ in range(_MAX_VARIANT_PASSES):
        m = _VARIANT_RE.search(text)
        if not m:
            break

        inner = m.group(1)
        parts = inner.split("$$")
        is_unique = False
        count = 1
        sep = ", "
        options_str = inner

        if len(parts) >= 2:
            prefix = parts[0].strip()
            is_valid_prefix = False
            if prefix.startswith("!") and prefix[1:].isdigit():
                is_unique = True
                count = int(prefix[1:])
                is_valid_prefix = True
            elif prefix.isdigit():
                count = int(prefix)
                is_valid_prefix = True

            if is_valid_prefix:
                if len(parts) == 2:
                    options_str = parts[1]
                elif len(parts) >= 3:
                    sep = parts[1]
                    options_str = "$$".join(parts[2:])

        options = [o.strip() for o in options_str.split("|")]
        chosen_list = []

        if is_unique:
            seen = set()
            attempts = 0
            max_attempts = max(count * 50, 1000)
            while len(chosen_list) < count and attempts < max_attempts:
                attempts += 1
                raw_opt = _weighted_choice(options, rng)
                if not raw_opt:
                    break
                resolved = _resolve_expression(raw_opt, rng, wildcards_dir, context)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    chosen_list.append(resolved)
        else:
            for _ in range(count):
                raw_opt = _weighted_choice(options, rng)
                if not raw_opt:
                    break
                chosen_list.append(raw_opt)

        chosen_str = sep.join(chosen_list)
        text = text[: m.start()] + chosen_str + text[m.end() :]
    return text


# ---------------------------------------------------------------------------
# Wildcard resolution  __name__
# ---------------------------------------------------------------------------

def resolve_wildcards(
    text: str, rng: random.Random, wildcards_dir: Path, global_seen: set[str] = None
) -> tuple[str, bool]:
    """
    Replace ``__name__`` or ``__!name__`` tokens with a random value from the matching file.
    Returns ``(resolved_text, any_replaced)``.
    Unknown wildcards are left unchanged.
    """
    replaced = False

    def _replace(m: re.Match) -> str:
        nonlocal replaced
        is_unique = bool(m.group(1))
        wildcard_name = m.group(2)
        values = find_wildcard(wildcard_name, wildcards_dir)

        if values:
            replaced = True
            if is_unique and global_seen is not None:
                available = [v for v in values if v not in global_seen]
                if not available:
                    available = values
                chosen = rng.choice(available)
                global_seen.add(chosen)
                return chosen
            else:
                return rng.choice(values)
        return m.group(0)

    return _WILDCARD_RE.sub(_replace, text), replaced


# ---------------------------------------------------------------------------
# Variable resolution  ${var=value}  /  ${var}
# ---------------------------------------------------------------------------

def _find_variable_end(text: str, dollar_pos: int) -> int:
    """
    Given the index of the ``$`` in ``${…}``, return the index of the
    matching closing ``}``, counting nested braces.
    Returns -1 if no match is found.
    """
    if dollar_pos + 1 >= len(text) or text[dollar_pos + 1] != "{":
        return -1
    depth = 1
    i = dollar_pos + 2
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i - 1 if depth == 0 else -1


def _resolve_expression(
    expr: str,
    rng: random.Random,
    wildcards_dir: Path,
    context: dict[str, Any],
) -> str:
    """
    Fully resolve *expr*: variables, then variants, then wildcards.
    Called recursively for immediate variable values and deferred accesses.
    """
    text = expr
    global_seen = context.setdefault("__global_seen__", set())
    for _ in range(_MAX_RESOLVE_ROUNDS):
        prev = text
        text = resolve_variables(text, rng, wildcards_dir, context)
        text = resolve_variants(text, rng, wildcards_dir, context)
        text, _ = resolve_wildcards(text, rng, wildcards_dir, global_seen)
        if text == prev:
            break
    return text


_SET_LINE_RE = re.compile(r"^\s*(?P<name>[a-zA-Z_][a-zA-Z0-9_-]*)\s*:\s*(?P<value>.*?)\s*$")
_IF_RE = re.compile(
    r"^if\s+(?P<name>[a-zA-Z_][a-zA-Z0-9_-]*)\s*(?P<op>!~|~|!=|=)\s*(?P<value>[^:]*?)\s*:(?P<body>.*)$",
    re.DOTALL,
)
_SET_WEIGHT_RE = re.compile(r"^\s*(?P<w>\d+(?:\.\d+)?)::")


def _split_top(text: str, sep: str = "|") -> list[str]:
    """Split on *sep* only outside ``{…}`` (so nested variants stay whole)."""
    parts, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _set_options(body: str) -> list[tuple[float, list[tuple[str, str]]]]:
    """Parse the body of ``${@{…}}`` into ``[(weight, [(name, value), …]), …]``."""
    options = []
    for raw in _split_top(body):
        weight = 1.0
        m = _SET_WEIGHT_RE.match(raw)
        if m:
            weight = float(m.group("w"))
            raw = raw[m.end():]
        pairs = []
        for line in raw.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            lm = _SET_LINE_RE.match(line)
            if lm:
                pairs.append((lm.group("name"), lm.group("value")))
        if pairs:
            options.append((weight, pairs))
    return options


def _plain_value(name: str, rng: random.Random, wildcards_dir: Path, context: dict[str, Any]) -> str:
    value = context.get(name, "")
    if isinstance(value, tuple) and value[0] == "deferred":
        value = _resolve_expression(value[1], rng, wildcards_dir, context)
    return str(value)


def _condition_holds(op: str, actual: str, expected: str) -> bool:
    actual, expected = actual.lower(), expected.strip().lower()
    if op == "~":
        return expected in actual
    if op == "!~":
        return expected not in actual
    if op == "=":
        return actual.strip() == expected
    return actual.strip() != expected   # "!="


def resolve_variables(
    text: str,
    rng: random.Random,
    wildcards_dir: Path,
    context: dict[str, Any],
) -> str:
    """
    Process all ``${…}`` tokens in *text* left-to-right, updating *context*.

    - Declarations (``${var=value}``) are removed from the output.
    - Accesses (``${var}`` / ``${var:default}``) are replaced by their value.
    - Unknown variable accesses are left unchanged.
    """
    result: list[str] = []
    pos = 0

    while pos < len(text):
        idx = text.find("${", pos)
        if idx == -1:
            result.append(text[pos:])
            break

        result.append(text[pos:idx])

        end = _find_variable_end(text, idx)
        if end == -1:
            result.append("$")
            pos = idx + 1
            continue

        inner = text[idx + 2 : end]
        pos = end + 1

        # ${@{ a: x\n b: y | a: z\n b: w }} — pick one set, declare all its variables at once
        if inner.startswith("@{") and inner.endswith("}"):
            options = _set_options(inner[2:-1])
            if options:
                pairs = rng.choices([o[1] for o in options], weights=[o[0] for o in options], k=1)[0]
                for name, value_tpl in pairs:
                    context[name] = _resolve_expression(value_tpl, rng, wildcards_dir, context)
            continue

        # ${if name~text: then | else} — output depends on a variable's value
        m = _IF_RE.match(inner)
        if m:
            then_else = _split_top(m.group("body"))
            then_tpl = then_else[0]
            else_tpl = "|".join(then_else[1:]) if len(then_else) > 1 else ""
            actual = _plain_value(m.group("name"), rng, wildcards_dir, context)
            chosen = then_tpl if _condition_holds(m.group("op"), actual, m.group("value")) else else_tpl
            result.append(_resolve_expression(chosen.strip(), rng, wildcards_dir, context))
            continue

        m = _VAR_DECL_INNER_RE.match(inner)
        if m:
            name      = m.group("name")
            preserve  = bool(m.group("preserve"))
            immediate = bool(m.group("immediate"))
            value_tpl = m.group("value")

            if preserve and name in context:
                continue

            if immediate:
                context[name] = _resolve_expression(value_tpl, rng, wildcards_dir, context)
            else:
                context[name] = ("deferred", value_tpl)

            continue

        if ":" in inner:
            colon = inner.index(":")
            name = inner[:colon]
            default_tpl: str | None = inner[colon + 1 :]
        else:
            name = inner
            default_tpl = None

        if not _VAR_NAME_RE.match(name):
            result.append("${" + inner + "}")
            continue

        if name in context:
            value = context[name]
            if isinstance(value, tuple) and value[0] == "deferred":
                result.append(_resolve_expression(value[1], rng, wildcards_dir, context))
            else:
                result.append(str(value))
        elif default_tpl is not None:
            result.append(_resolve_expression(default_tpl, rng, wildcards_dir, context))
        else:
            result.append("${" + inner + "}")

    return "".join(result)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate(text: str, seed: int, wildcards_dir: Path) -> str:
    """
    Fully resolve *text* using the given *seed* and *wildcards_dir*.

    Resolution order per iteration:
    1. Variables  (${var=…} declarations, ${var} accesses)
    2. Variants   ({A|B|C})
    3. Wildcards  (__name__)
    """
    rng = random.Random(seed)
    context: dict[str, Any] = {}
    global_seen = context.setdefault("__global_seen__", set())

    for _ in range(_MAX_RESOLVE_ROUNDS):
        prev = text
        text = resolve_variables(text, rng, wildcards_dir, context)
        text = resolve_variants(text, rng, wildcards_dir, context)
        text, _ = resolve_wildcards(text, rng, wildcards_dir, global_seen)
        if text == prev:
            break

    return _strip_line_comments(text)
