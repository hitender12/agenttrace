"""A tiny YAML-subset reader for eval case files (stdlib only).

Supports exactly the subset AgentTrace case files need:

- mappings by indentation (``key: value``), nested mappings
- sequences with ``- item`` (including ``- key: value`` maps)
- inline lists (``[a, b]``) and inline empty maps/lists (``{}``, ``[]``)
- plain scalars, single/double-quoted strings, integers, floats,
  booleans (``true``/``false``), ``null``/``~``
- comments (``# ...``) and a single leading document marker (``---``)

Anything else — tabs, anchors/aliases, tags, flow maps, multi-document
files, duplicate keys, nesting deeper than 100 levels — raises
:class:`YamlSubsetError` with a clear message instead of a traceback.
This is intentionally *not* a full YAML parser: case files are small
and hand-written, and a loud, precise error beats silently misreading
a case.
"""
from __future__ import annotations


class YamlSubsetError(ValueError):
    """Raised when a case file uses YAML outside the supported subset."""


# Maximum nesting depth (mappings/sequences/inline lists). A case file
# ~2000 levels deep used to recurse until Python raised RecursionError;
# the guard turns it into a plain YamlSubsetError instead.
MAX_DEPTH = 100


def _scalar(token: str, line_no: int, depth: int = 0):
    if depth > MAX_DEPTH:
        raise YamlSubsetError(
            f"line {line_no}: nesting too deep (max {MAX_DEPTH} levels)")
    token = token.strip()
    if token in ("", "~", "null", "Null", "NULL"):
        return None
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        quote = token[0]
        body = token[1:-1]
        if quote == "'":
            return body.replace("''", "'")
        return body.replace('\\"', '"').replace("\\\\", "\\")
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, line_no, depth + 1) for part in _split_inline(inner)]
    if token.startswith("{"):
        raise YamlSubsetError(
            f"line {line_no}: flow mappings ({{...}}) are not supported; "
            "write the mapping on separate lines")
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    if token[0] in "&*!|>@`%":
        raise YamlSubsetError(
            f"line {line_no}: YAML feature {token[0]!r} is not supported "
            "in case files")
    return token


def _split_inline(text: str) -> list[str]:
    parts, depth, current, quote = [], 0, "", None
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current += ch
        elif ch == "[":
            depth += 1
            current += ch
        elif ch == "]":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def _strip_comment(line: str) -> str:
    # A '#' starts a comment only when preceded by whitespace (or at start).
    in_single, in_double = False, False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1] in (" ", "\t"):
                return line[:i].rstrip()
    return line.rstrip()


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_yaml_subset(text: str):
    """Parse the YAML subset described in the module docstring."""
    raw_lines = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw.split("#", 1)[0]:
            raise YamlSubsetError(
                f"line {line_no}: tabs are not allowed for indentation")
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if line.strip() == "---":
            if raw_lines:
                raise YamlSubsetError(
                    f"line {line_no}: multi-document files are not supported")
            continue
        if line.strip() == "...":
            raise YamlSubsetError(
                f"line {line_no}: document-end marker is not supported")
        raw_lines.append((line_no, line))
    if not raw_lines:
        raise YamlSubsetError("empty case file")
    pos = 0

    def parse_block(indent: int, depth: int = 0):
        nonlocal pos
        if depth > MAX_DEPTH:
            line_no = raw_lines[pos][0] if pos < len(raw_lines) else 0
            raise YamlSubsetError(
                f"line {line_no}: nesting too deep (max {MAX_DEPTH} levels)")
        if pos >= len(raw_lines):
            return None
        line_no, line = raw_lines[pos]
        if _indent_of(line) != indent:
            raise YamlSubsetError(
                f"line {line_no}: unexpected indentation "
                f"(expected {indent} spaces)")
        if line.strip().startswith("- ") or line.strip() == "-":
            return parse_sequence(indent, depth)
        return parse_mapping(indent, depth)

    def parse_sequence(indent: int, depth: int = 0):
        nonlocal pos
        items = []
        while pos < len(raw_lines):
            line_no, line = raw_lines[pos]
            if _indent_of(line) != indent or not line.strip().startswith("-"):
                break
            stripped = line.strip()
            after_dash = stripped[1:].lstrip()
            # Column where the item's content starts ('- ' is 2 chars).
            item_indent = indent + (len(stripped) - len(after_dash))
            if not after_dash:
                pos += 1
                if pos < len(raw_lines) and _indent_of(raw_lines[pos][1]) > indent:
                    items.append(parse_block(_indent_of(raw_lines[pos][1]),
                                             depth + 1))
                else:
                    items.append(None)
                continue
            if ":" in after_dash and not after_dash.startswith(("[", "{", "'", '"')):
                # '- key: value' — a mapping starting on the dash line.
                key, _, rest = after_dash.partition(":")
                key = key.strip()
                if not key or " " in key:
                    raise YamlSubsetError(
                        f"line {line_no}: bad mapping key {key!r}")
                mapping = {key: _scalar(rest, line_no) if rest.strip() else None}
                pos += 1
                if rest.strip() == "":
                    if pos < len(raw_lines) and _indent_of(raw_lines[pos][1]) > indent:
                        mapping[key] = parse_block(_indent_of(raw_lines[pos][1]),
                                                   depth + 1)
                # Further keys of the same mapping sit at item_indent.
                # Duplicate keys are an error here, exactly as in plain
                # mappings — last-wins would silently drop a case.
                while pos < len(raw_lines):
                    n2, l2 = raw_lines[pos]
                    if _indent_of(l2) != item_indent or l2.strip().startswith("-"):
                        break
                    k2, _, v2 = l2.strip().partition(":")
                    k2 = k2.strip()
                    if not k2 or ":" not in l2:
                        break
                    if k2 in mapping:
                        raise YamlSubsetError(
                            f"line {n2}: duplicate key {k2!r}")
                    mapping[k2] = _scalar(v2, n2) if v2.strip() else None
                    pos += 1
                    if v2.strip() == "" and pos < len(raw_lines) \
                            and _indent_of(raw_lines[pos][1]) > item_indent:
                        mapping[k2] = parse_block(_indent_of(raw_lines[pos][1]),
                                                  depth + 1)
                items.append(mapping)
                continue
            items.append(_scalar(after_dash, line_no))
            pos += 1
        return items

    def parse_mapping(indent: int, depth: int = 0):
        nonlocal pos
        mapping: dict = {}
        while pos < len(raw_lines):
            line_no, line = raw_lines[pos]
            if _indent_of(line) != indent:
                break
            stripped = line.strip()
            if stripped.startswith("-"):
                break
            if ":" not in stripped:
                raise YamlSubsetError(
                    f"line {line_no}: expected 'key: value', got {stripped!r}")
            key, _, rest = stripped.partition(":")
            key = key.strip()
            if not key or key in mapping:
                raise YamlSubsetError(
                    f"line {line_no}: duplicate or empty key {key!r}")
            pos += 1
            if rest.strip():
                mapping[key] = _scalar(rest, line_no)
            elif pos < len(raw_lines) and _indent_of(raw_lines[pos][1]) > indent:
                mapping[key] = parse_block(_indent_of(raw_lines[pos][1]),
                                           depth + 1)
            else:
                mapping[key] = None
        return mapping

    result = parse_block(_indent_of(raw_lines[0][1]))
    if pos != len(raw_lines):
        line_no, line = raw_lines[pos]
        raise YamlSubsetError(
            f"line {line_no}: unexpected content {line.strip()!r}")
    return result
