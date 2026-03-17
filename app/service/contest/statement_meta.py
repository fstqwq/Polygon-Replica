from __future__ import annotations


def _skip_tex_space_and_comments(text: str, index: int) -> int:
    length = len(text)
    pos = max(0, index)
    while pos < length:
        ch = text[pos]
        if ch in " \t\r\n":
            pos += 1
            continue
        if ch == "%":
            newline = text.find("\n", pos)
            if newline < 0:
                return length
            pos = newline + 1
            continue
        break
    return pos


def _read_tex_braced_group(text: str, index: int) -> tuple[str, int] | None:
    pos = _skip_tex_space_and_comments(text, index)
    if pos >= len(text) or text[pos] != "{":
        return None
    pos += 1
    depth = 1
    chars: list[str] = []
    while pos < len(text):
        ch = text[pos]
        if ch == "\\":
            if pos + 1 < len(text):
                chars.append(ch)
                pos += 1
                chars.append(text[pos])
                pos += 1
                continue
        if ch == "{":
            depth += 1
            chars.append(ch)
            pos += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return ("".join(chars).strip(), pos + 1)
            chars.append(ch)
            pos += 1
            continue
        chars.append(ch)
        pos += 1
    return None


def infer_contest_header_fields(statements_tex: str) -> dict[str, str]:
    text = str(statements_tex or "")
    marker = r"\contest"
    start = text.find(marker)
    if start < 0:
        return {"title": "", "location": "", "date": ""}
    pos = start + len(marker)
    groups: list[str] = []
    for _ in range(3):
        parsed = _read_tex_braced_group(text, pos)
        if parsed is None:
            return {"title": "", "location": "", "date": ""}
        value, pos = parsed
        groups.append(value)
    return {
        "title": groups[0],
        "location": groups[1],
        "date": groups[2],
    }
