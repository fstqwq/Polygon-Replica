"""Detect the runnable top-level class in a single-file Java source."""

import re
from pathlib import Path


_JAVA_CLASS_DECL_RE = re.compile(
    r"\b(?P<public>public\s+)?"
    r"(?:(?:abstract|final|static|strictfp|sealed|non-sealed)\s+)*"
    r"class\s+(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\b"
)
_JAVA_MAIN_METHOD_RE = re.compile(
    r"\b(?:(?:public|protected|private|static|final|synchronized|strictfp|native)\s+)*"
    r"void\s+main\s*\(",
    re.MULTILINE,
)


def _strip_java_noncode(source_text: str) -> str:
    text = str(source_text or "")
    out: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        current = text[index]
        next_char = text[index + 1] if (index + 1) < size else ""
        if current == "/" and next_char == "/":
            out.append(" ")
            out.append(" ")
            index += 2
            while index < size and text[index] not in "\r\n":
                out.append(" ")
                index += 1
            continue
        if current == "/" and next_char == "*":
            out.append(" ")
            out.append(" ")
            index += 2
            while index < size:
                char = text[index]
                tail = text[index + 1] if (index + 1) < size else ""
                if char == "*" and tail == "/":
                    out.append(" ")
                    out.append(" ")
                    index += 2
                    break
                out.append("\n" if char == "\n" else " ")
                index += 1
            continue
        if current in {'"', "'"}:
            quote = current
            out.append(" ")
            index += 1
            escaped = False
            while index < size:
                char = text[index]
                if char == "\n":
                    out.append("\n")
                    index += 1
                    break
                out.append(" ")
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    index += 1
                    break
                index += 1
            continue
        out.append(current)
        index += 1
    return "".join(out)


def _java_top_level_classes(source_text: str) -> list[tuple[str, bool, str]]:
    stripped = _strip_java_noncode(source_text)
    values: list[tuple[str, bool, str]] = []
    index = 0
    size = len(stripped)
    brace_depth = 0
    while index < size:
        char = stripped[index]
        if char == "{":
            brace_depth += 1
            index += 1
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            index += 1
            continue
        if brace_depth != 0:
            index += 1
            continue
        match = _JAVA_CLASS_DECL_RE.match(stripped, index)
        if match is None:
            index += 1
            continue
        class_name = str(match.group("name") or "")
        if not class_name:
            index = match.end()
            continue
        body_open = stripped.find("{", match.end())
        if body_open < 0:
            index = match.end()
            continue
        nested_depth = 1
        body_index = body_open + 1
        while body_index < size and nested_depth > 0:
            token = stripped[body_index]
            if token == "{":
                nested_depth += 1
            elif token == "}":
                nested_depth -= 1
            body_index += 1
        if nested_depth != 0:
            index = body_index
            continue
        class_body = stripped[body_open + 1 : body_index - 1]
        values.append((class_name, bool(match.group("public")), class_body))
        index = body_index
    return values


def _java_class_has_main(class_body: str) -> bool:
    stripped = _strip_java_noncode(class_body)
    index = 0
    size = len(stripped)
    brace_depth = 0
    while index < size:
        char = stripped[index]
        if char == "{":
            brace_depth += 1
            index += 1
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            index += 1
            continue
        if brace_depth != 0:
            index += 1
            continue
        match = _JAVA_MAIN_METHOD_RE.search(stripped, index)
        if match is None:
            return False
        method_start = match.start()
        if method_start < index:
            index += 1
            continue
        open_paren = stripped.find("(", match.end() - 1)
        if open_paren < 0:
            return False
        nested_depth = 1
        close_index = open_paren + 1
        while close_index < size and nested_depth > 0:
            token = stripped[close_index]
            if token == "(":
                nested_depth += 1
            elif token == ")":
                nested_depth -= 1
            close_index += 1
        if nested_depth != 0:
            return False
        params = stripped[open_paren + 1 : close_index - 1]
        modifiers = stripped[max(index, method_start - 128) : method_start]
        modifier_tokens = set(
            re.findall(r"[A-Za-z_$][A-Za-z0-9_$]*", modifiers)
        )
        if "static" in modifier_tokens and "String" in params:
            return True
        index = close_index
    return False


def detect_java_entry_point(source_name: str, source_bytes: bytes) -> str:
    """Return the Java class name used as the single-file submission entry."""

    safe_source_name = Path(source_name).name
    source_text = source_bytes.decode("utf-8", errors="replace")
    top_level_classes = _java_top_level_classes(source_text)
    if not top_level_classes:
        raise RuntimeError(
            "java entry point detection failed for "
            f"{safe_source_name}: no top-level classes found"
        )
    public_classes = [
        name for name, is_public, _body in top_level_classes if is_public
    ]
    if len(public_classes) == 1:
        return public_classes[0]
    runnable_classes = [
        name
        for name, _is_public, body in top_level_classes
        if _java_class_has_main(body)
    ]
    if len(runnable_classes) == 1:
        return runnable_classes[0]
    if len(runnable_classes) > 1:
        raise RuntimeError(
            "java entry point detection failed for "
            f"{safe_source_name}: multiple runnable classes found "
            f"({', '.join(runnable_classes)})"
        )
    raise RuntimeError(
        "java entry point detection failed for "
        f"{safe_source_name}: no runnable main class found"
    )
