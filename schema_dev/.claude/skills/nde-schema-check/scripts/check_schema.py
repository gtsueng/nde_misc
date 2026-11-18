"""
check_schema.py - Validate an NDE schema JSON-LD file (e.g. NDE_schema_v2.json).

Checks, in order:
  1. Syntax      - unclosed / mismatched brackets and braces, unterminated strings,
                   unnecessary (trailing, leading, double) commas, missing commas/colons,
                   duplicate keys. Uses a tolerant parser so ALL syntax problems are
                   reported at once, with exact line numbers, instead of stopping at the
                   first one like json.load does.
  2. $validation - every "$ref": "#/definitions/X" resolves to a definition in that class,
                   required/recommended/optional entries exist in "properties", the block is
                   valid JSON Schema (draft-07).
  3. Inheritance - rdfs:subClassOf targets exist, no cycles, each $validation property is
                   allowed on the class (schema:domainIncludes of the class or an ancestor),
                   and parent/child $validation conflicts that biothings' merge will produce.
  4. biothings_schema - runs biothings_schema.Schema on the (in-memory repaired) file,
                   collecting every error instead of stopping at the first, and maps each
                   one back to a line with an explanation of the cause.

The input file is never modified. Output: a Markdown report (and a console summary).

Usage:
    python check_schema.py [SCHEMA_FILE] [--out REPORT.md] [--skip-biothings]
"""

import argparse
import copy
import datetime
import json
import os
import re
import sys
import traceback

# --------------------------------------------------------------------------------------
# Issue collection
# --------------------------------------------------------------------------------------

SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}


class Issue:
    def __init__(self, severity, check, line, message, explanation="", fix="", col=None, record=None):
        self.severity = severity
        self.check = check
        self.line = line
        self.col = col
        self.message = message
        self.explanation = explanation
        self.fix = fix
        self.record = record


class Report:
    def __init__(self, lines):
        self.lines = lines
        self.issues = []
        self.notes = []

    def add(self, *args, **kwargs):
        self.issues.append(Issue(*args, **kwargs))

    def snippet(self, line, context=1):
        if not line or line < 1 or line > len(self.lines):
            return ""
        out = []
        for n in range(max(1, line - context), min(len(self.lines), line + context) + 1):
            marker = ">>" if n == line else "  "
            out.append(f"{marker} {n:>6} | {self.lines[n - 1].rstrip()}")
        return "\n".join(out)


# --------------------------------------------------------------------------------------
# 1. Tolerant tokenizer + parser
# --------------------------------------------------------------------------------------

class Tok:
    __slots__ = ("kind", "value", "line", "col", "pos", "end")

    def __init__(self, kind, value, line, col, pos, end):
        self.kind, self.value, self.line, self.col, self.pos, self.end = kind, value, line, col, pos, end


LITERAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
BAREWORD_RE = re.compile(r"[^\s{}\[\]:,\"]+")


def tokenize(text, report):
    """Returns tokens plus a list of text edits [(pos, delete_len, insert_str)] for repair."""
    toks, edits = [], []
    i, line, line_start, n = 0, 1, 0, len(text)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            line_start = i + 1
            i += 1
            continue
        if c in " \t\r﻿":
            i += 1
            continue
        col = i - line_start + 1
        if c in "{}[]:,":
            toks.append(Tok(c, c, line, col, i, i + 1))
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            closed = False
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                if ch == '"':
                    closed = True
                    break
                if ch == "\n":
                    break
                buf.append(ch)
                j += 1
            if closed:
                raw = text[i:j + 1]
                try:
                    val = json.loads(raw)
                except Exception:
                    val = "".join(buf)
                    report.add("ERROR", "syntax", line,
                               f"Invalid escape sequence or control character in string {raw[:60]!r}",
                               "JSON strings may only contain the escapes \\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX and no raw tab characters.",
                               "Fix or remove the backslash / tab in this string.", col=col)
                toks.append(Tok("str", val, line, col, i, j + 1))
                i = j + 1
            else:
                # unterminated string: close it at the end of the line (before a trailing comma)
                content = "".join(buf)
                stripped = content.rstrip()
                trailing_comma = stripped.endswith(",")
                val = stripped[:-1] if trailing_comma else stripped
                report.add("ERROR", "syntax", line,
                           f"Unterminated string starting at column {col}: {text[i:j][:70]!r}",
                           "The string has no closing double quote before the end of the line. "
                           "JSON strings cannot span lines, so the parser swallows the rest of the line"
                           + (" (including the comma meant to follow it)" if trailing_comma else "")
                           + " and everything after is misread.",
                           "Add the missing closing \" " + ("before the trailing comma." if trailing_comma else "at the end of the value."),
                           col=col)
                insert_at = i + 1 + len(content.rstrip()) - (1 if trailing_comma else 0)
                edits.append((insert_at, 0, '"'))
                toks.append(Tok("str", val, line, col, i, j))
                if trailing_comma:
                    toks.append(Tok(",", ",", line, col + len(stripped), insert_at, insert_at + 1))
                i = j
            continue
        m = LITERAL_RE.match(text, i)
        if m and (m.end() == n or not re.match(r"[A-Za-z0-9_]", text[m.end()])):
            toks.append(Tok("lit", json.loads(m.group(0)), line, col, i, m.end()))
            i = m.end()
            continue
        m = BAREWORD_RE.match(text, i)
        word = m.group(0) if m else c
        report.add("ERROR", "syntax", line, f"Unexpected text {word[:40]!r} at column {col}",
                   "Only strings (in double quotes), numbers, true, false, null, and the characters { } [ ] : , are allowed in JSON. "
                   "Single quotes, comments, and unquoted words are not.",
                   "Quote the value with double quotes or remove the stray text.", col=col)
        edits.append((i, len(word), ""))
        i += len(word)
    toks.append(Tok("eof", None, line, 1, n, n))
    return toks, edits


class TolerantParser:
    """Recursive-descent JSON parser that records problems and keeps going."""

    def __init__(self, toks, text, report):
        self.toks, self.text, self.report = toks, text, report
        self.i = 0
        self.stack = []          # open containers: (char, tok)
        self.lines = {}          # path tuple -> line of the key (or value for array items)
        self.edits = []          # (pos, delete_len, insert_str)
        self.unclosed = []       # opener tokens left unclosed

    def peek(self):
        return self.toks[self.i]

    def next(self):
        t = self.toks[self.i]
        if t.kind != "eof":
            self.i += 1
        return t

    def _comma_issue(self, tok, kind):
        msgs = {
            "trailing": ("Unnecessary trailing comma before '{close}'",
                         "JSON does not allow a comma after the last item of an object or array. "
                         "Python's json module and most JSON tools reject the whole file because of it."),
            "double": ("Unnecessary extra comma (two commas in a row)",
                       "Two consecutive commas leave an empty item, which is not valid JSON."),
            "leading": ("Unnecessary comma at the start of an object/array",
                        "A comma directly after '{' or '[' has no item before it, which is not valid JSON."),
        }
        title, expl = msgs[kind]
        nxt = self.peek()
        self.report.add("ERROR", "comma", tok.line, title.format(close=nxt.value),
                        expl, f"Delete the comma at line {tok.line}, column {tok.col}.", col=tok.col)
        self.edits.append((tok.pos, 1, ""))

    def _missing_comma(self, prev_tok, tok):
        self.report.add("ERROR", "comma", tok.line,
                        f"Missing comma before {self._desc(tok)} (previous item ends at line {prev_tok.line})",
                        "Items in an object or array must be separated by commas.",
                        f"Add a comma at the end of line {prev_tok.line}.", col=tok.col)
        self.edits.append((prev_tok.end, 0, ","))

    @staticmethod
    def _desc(tok):
        if tok.kind == "str":
            return f'"{str(tok.value)[:40]}"'
        if tok.kind == "eof":
            return "end of file"
        return f"'{tok.value}'"

    def _enclosing_has(self, ch):
        return any(c == ch for c, _ in self.stack[:-1])

    def parse(self):
        val = self.parse_value(())
        t = self.peek()
        while t.kind != "eof":
            if t.kind in "}]":
                self.report.add("ERROR", "brackets", t.line,
                                f"Extra closing '{t.value}' after the end of the top-level object",
                                "Every bracket was already closed, so this one has no matching opener. "
                                "Usually an earlier block was closed one level too soon (an extra '}' or ']' somewhere above).",
                                "Find the extra closer above (see indentation hints) and remove it.", col=t.col)
                self.edits.append((t.pos, 1, ""))
            else:
                self.report.add("ERROR", "syntax", t.line,
                                f"Unexpected {self._desc(t)} after the end of the top-level object",
                                "The top-level object was closed before this point, so the rest of the file is outside it. "
                                "This usually means an extra '}' was added somewhere above.",
                                "Look for an extra closing brace above this line.", col=t.col)
            self.next()
            t = self.peek()
        return val

    def parse_value(self, path, key_tok=None):
        t = self.peek()
        if t.kind == "{":
            return self.parse_container(path, "{")
        if t.kind == "[":
            return self.parse_container(path, "[")
        if t.kind in ("str", "lit"):
            self.next()
            return t.value
        self.report.add("ERROR", "syntax", t.line,
                        f"Missing value: found {self._desc(t)} where a value was expected",
                        "A key was followed by ':' but no value, or an array has an empty slot.",
                        "Add the missing value or remove the dangling key.", col=t.col)
        return None

    def parse_container(self, path, open_ch):
        close_ch = "}" if open_ch == "{" else "]"
        open_tok = self.next()
        self.stack.append((open_ch, open_tok))
        result = {} if open_ch == "{" else []
        key_lines = {}
        state = "start"   # start | after_comma | after_item
        prev_tok = open_tok
        idx = 0
        while True:
            t = self.peek()
            if t.kind == "eof":
                self.unclosed.append(open_tok)
                self.stack.pop()
                return result
            if t.kind == close_ch:
                self.next()
                self.stack.pop()
                return result
            if t.kind in "}]":   # wrong closer
                other_open = "{" if t.kind == "}" else "["
                if self._enclosing_has(other_open):
                    # this container was never closed; let the enclosing one consume the closer
                    self.unclosed.append(open_tok)
                    self.stack.pop()
                    return result
                self.report.add("ERROR", "brackets", t.line,
                                f"Mismatched '{t.value}': the innermost open bracket is '{open_ch}' from line {open_tok.line}",
                                f"A '{t.value}' can only close a '{other_open}', but there is no open '{other_open}' here. "
                                "It is either a typo for '" + close_ch + "' or an extra closer.",
                                f"Replace it with '{close_ch}' or delete it.", col=t.col)
                self.edits.append((t.pos, 1, ""))
                self.next()
                continue
            if t.kind == ",":
                self.next()
                if state == "start":
                    self._comma_issue(t, "leading")
                elif state == "after_comma":
                    self._comma_issue(t, "double")
                else:
                    nxt = self.peek()
                    if nxt.kind in "}]":
                        self._comma_issue(t, "trailing")
                    state = "after_comma"
                prev_tok = t
                continue
            if t.kind == ":":
                self.report.add("ERROR", "syntax", t.line, "Unexpected ':'",
                                "A colon may only follow an object key.", "Remove the stray ':'.", col=t.col)
                self.edits.append((t.pos, 1, ""))
                self.next()
                continue
            # an item starts here
            if state == "after_item":
                self._missing_comma(self.toks[self.i - 1], t)
            if open_ch == "{":
                if t.kind != "str":
                    self.report.add("ERROR", "syntax", t.line,
                                    f"Object key must be a double-quoted string, found {self._desc(t)}",
                                    "Inside '{...}' every entry must look like \"key\": value.",
                                    "Quote the key, or check whether a '[' was intended instead of '{'.", col=t.col)
                    self.parse_value(path + ("?",))
                    state = "after_item"
                    continue
                key_tok = self.next()
                key = key_tok.value
                if self.peek().kind == ":":
                    self.next()
                else:
                    self.report.add("ERROR", "syntax", key_tok.line, f"Missing ':' after key \"{key}\"",
                                    "Every object key must be followed by a colon.",
                                    f"Add ':' after \"{key}\".", col=key_tok.col)
                    self.edits.append((key_tok.end, 0, ":"))
                if key in key_lines:
                    self.report.add("ERROR", "duplicate-key", key_tok.line,
                                    f"Duplicate key \"{key}\" (first defined at line {key_lines[key]})",
                                    "JSON parsers silently keep only the LAST occurrence, so the earlier definition is lost.",
                                    "Merge the two entries or delete one of them.", col=key_tok.col)
                key_lines[key] = key_tok.line
                self.lines[path + (key,)] = key_tok.line
                result[key] = self.parse_value(path + (key,))
            else:
                self.lines[path + (idx,)] = t.line
                result.append(self.parse_value(path + (idx,)))
                idx += 1
            state = "after_item"


def indent_of(s):
    return len(s) - len(s.lstrip(" \t"))


def bracket_location_hints(lines, opener_tok):
    """For an unclosed opener, find where its closer was probably supposed to go:
    the first later line whose indentation is <= the opener line's indentation."""
    base = indent_of(lines[opener_tok.line - 1])
    for n in range(opener_tok.line + 1, len(lines) + 1):
        s = lines[n - 1]
        if not s.strip():
            continue
        if indent_of(s) <= base:
            return n
    return None


MATCH = {"{": "}", "[": "]"}


def _line_end_pos(text, line_no):
    """Position just after the last non-whitespace char of line `line_no` (1-based)."""
    start = 0
    for _ in range(line_no - 1):
        start = text.index("\n", start) + 1
    end = text.find("\n", start)
    end = len(text) if end == -1 else end
    seg = text[start:end].rstrip()
    return start + len(seg)


def _prev_nonblank(lines, line_no):
    k = line_no - 1
    while k > 0 and not lines[k - 1].strip():
        k -= 1
    return max(k, 1)


def bracket_pass(text, lines):
    """Find the FIRST bracket problem, using indentation to locate it precisely.
    In a pretty-printed file a multi-line block's closer sits at the same indentation as the
    line that opened it; where that stops being true is where a bracket went missing or extra.
    Returns (issues, edits) or None if the brackets are balanced."""
    toks, _ = tokenize(text, Report(lines))
    stack, popped = [], []
    ind = lambda ln: indent_of(lines[ln - 1])

    def first_on_line(t):
        return lines[t.line - 1][: t.col - 1].strip() == ""

    for idx, t in enumerate(toks):
        if t.kind in "{[":
            stack.append(t)
            continue
        if t.kind not in "}]":
            continue
        if not stack:
            return ([Issue("ERROR", "brackets", t.line, f"Extra closing '{t.value}' with no matching opener",
                           "Every bracket before this point is already closed.", "Delete it.", col=t.col)],
                    [(t.pos, 1, "")])
        top = stack[-1]
        want = MATCH[top.kind]
        multiline_first = first_on_line(t) and top.line != t.line
        if t.kind == want and (not multiline_first or ind(top.line) == ind(t.line)):
            popped.append((stack.pop(), t, len(stack)))
            continue
        ic = ind(t.line)
        if multiline_first and ic < ind(top.line):
            # (a) closer is shallower than the open block and lines up with an outer opener
            #     -> everything above that opener was left unclosed
            for k in range(len(stack) - 2, -1, -1):
                o = stack[k]
                if ind(o.line) == ic and MATCH[o.kind] == t.kind:
                    issues, edits = [], []
                    for u in reversed(stack[k + 1:]):
                        cu = MATCH[u.kind]
                        hint = bracket_location_hints(lines, u) or t.line
                        hint = min(hint, t.line)
                        ins_line = _prev_nonblank(lines, hint)
                        issues.append(Issue(
                            "ERROR", "brackets", u.line,
                            f"Unclosed '{u.kind}' opened at line {u.line} (column {u.col}); its '{cu}' is missing",
                            f"The block opened here is never closed. Indentation returns to this block's level at line {hint}, "
                            f"so the '{cu}' most likely belongs at the end of line {ins_line}. "
                            f"(Detected when the '{t.value}' at line {t.line} lined up with the opener at line {o.line} instead.)",
                            f"Add '{cu}' after line {ins_line}.", col=u.col))
                        edits.append((_line_end_pos(text, ins_line), 0, cu))
                    return issues, edits
        if multiline_first:
            # (b) closer is deeper than the open block -> an earlier closer closed this block's opener too soon
            if ic > ind(top.line):
                for o, c, depth in reversed(popped):
                    if o.line <= top.line:
                        break
                    # o must have been a direct child of the block that is open now
                    if depth == len(stack) and ind(o.line) == ic and MATCH[o.kind] == t.kind:
                        between = [x for x in toks if c.pos < x.pos < t.pos and x.kind in ("str", "lit")]
                        early_is_extra = any(ind(x.line) > ic for x in between)
                        extra = c if early_is_extra else t
                        other = t if early_is_extra else c
                        return ([Issue("ERROR", "brackets", extra.line,
                                       f"Extra '{extra.value}' at line {extra.line}, column {extra.col}",
                                       f"The '{o.kind}' opened at line {o.line} is closed twice: at line {c.line} and again at line {t.line}. "
                                       + ("Content after the first closer is still indented inside the block, so the first closer is the extra one."
                                          if early_is_extra else "Nothing belongs to the block after the first closer, so the second one is extra.")
                                       + f" The correct closer is at line {other.line}.",
                                       "Delete the extra closer.", col=extra.col)],
                                [(extra.pos, 1, "")])
        if t.kind == want:  # cosmetic indentation difference only
            popped.append((stack.pop(), t, len(stack)))
            continue
        # wrong kind of closer
        if ind(top.line) == ic or not multiline_first:
            return ([Issue("ERROR", "brackets", t.line,
                           f"Mismatched '{t.value}': it should be '{want}' to close the '{top.kind}' opened at line {top.line}",
                           f"'{t.value}' can only close '{'{' if t.kind == '}' else '['}'.",
                           f"Change it to '{want}'.", col=t.col)],
                    [(t.pos, 1, want)])
        return ([Issue("ERROR", "brackets", t.line,
                       f"Unexpected '{t.value}': the innermost open bracket is '{top.kind}' from line {top.line}",
                       "This closer does not match any open bracket at its indentation level.",
                       "Check the brackets between these two lines.", col=t.col)],
                [(t.pos, 1, "")])
    if stack:
        issues, edits = [], []
        for u in reversed(stack):
            cu = MATCH[u.kind]
            hint = bracket_location_hints(lines, u)
            ins_line = _prev_nonblank(lines, hint) if hint else len(lines)
            issues.append(Issue("ERROR", "brackets", u.line,
                                f"Unclosed '{u.kind}' opened at line {u.line} (column {u.col}); its '{cu}' is missing",
                                "The file ends while this block is still open."
                                + (f" Indentation suggests it should close at the end of line {ins_line}." if hint else ""),
                                f"Add '{cu}' after line {ins_line}.", col=u.col))
            edits.append((_line_end_pos(text, ins_line), 0, cu))
        return issues, edits
    return None


def check_syntax(text, report):
    """Returns (data, path_lines, repaired_text). All repairs are intra-line, so line numbers
    in the repaired text match the original file."""
    lines = report.lines
    toks, tok_edits = tokenize(text, report)
    text1 = apply_edits(text, tok_edits)
    seen_fixes = set()
    for _ in range(200):
        res = bracket_pass(text1, lines)
        if res is None:
            break
        issues, edits = res
        sig = tuple((i.line, i.col, i.message) for i in issues)
        if sig in seen_fixes:   # repair did not help; let the tolerant parser report the rest
            break
        seen_fixes.add(sig)
        report.issues.extend(issues)
        text1 = apply_edits(text1, edits)
    toks, _ = tokenize(text1, Report(lines))
    parser = TolerantParser(toks, text1, report)
    data = parser.parse()
    for op in parser.unclosed:
        close_ch = "}" if op.kind == "{" else "]"
        hint = bracket_location_hints(lines, op)
        where = (f" Based on indentation, the closing '{close_ch}' most likely belongs just before line {hint}."
                 if hint else "")
        report.add("ERROR", "brackets", op.line,
                   f"Unclosed '{op.kind}' opened at line {op.line}, column {op.col}",
                   f"This '{op.kind}' never gets its matching '{close_ch}'." + where,
                   f"Add the missing '{close_ch}'" + (f" before line {hint}." if hint else "."), col=op.col)
        if hint:
            # insert closer at the end of the previous non-blank line
            k = hint - 1
            while k > 0 and not lines[k - 1].strip():
                k -= 1
            parser.edits.append((_line_end_pos(text1, k), 0, close_ch))
    return data, parser.lines, apply_edits(text1, parser.edits)


def apply_edits(text, edits):
    for pos, dl, ins in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:pos] + ins + text[pos + dl:]
    return text


# --------------------------------------------------------------------------------------
# helpers for semantic checks
# --------------------------------------------------------------------------------------

def local_name(curie):
    if not isinstance(curie, str):
        return str(curie)
    return re.split(r"[:/#]", curie)[-1]


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def ids_of(v):
    out = []
    for x in as_list(v):
        if isinstance(x, dict) and "@id" in x:
            out.append(x["@id"])
        elif isinstance(x, str):
            out.append(x)
    return out


class Locator:
    def __init__(self, path_lines, graph):
        self.pl = path_lines
        self.graph = graph

    def line(self, *path):
        path = tuple(path)
        while path:
            if path in self.pl:
                return self.pl[path]
            path = path[:-1]
        return None

    def rec_index(self, rec_id):
        for i, r in enumerate(self.graph):
            if isinstance(r, dict) and r.get("@id") == rec_id:
                return i
        return None

    def rec_line(self, rec_id, *sub):
        i = self.rec_index(rec_id)
        if i is None:
            return None
        return self.line("@graph", i, *sub) or self.line("@graph", i)


def walk(obj, path=()):
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, path + (i,))


# --------------------------------------------------------------------------------------
# 1b. Stray whitespace in names
# --------------------------------------------------------------------------------------

NAME_LISTS = ("required", "recommended", "optional")
NAME_VALUE_KEYS = ("@id", "rdfs:label", "$ref")


def check_whitespace(data, loc, report):
    """Leading/trailing (or internal) whitespace in property names, definition names,
    marginality-list entries, @id / rdfs:label / $ref values, and any other key.
    These are invisible in an editor but make the name a different string, so e.g.
    "givenName " in "recommended" silently matches no property."""

    def where(path):
        # class @id + JSON path inside it, for the message
        if len(path) >= 2 and path[0] == "@graph" and isinstance(path[1], int):
            g = data.get("@graph", [])
            rid = g[path[1]].get("@id") if path[1] < len(g) and isinstance(g[path[1]], dict) else None
            rest = "/".join(str(p) for p in path[2:])
            return f"{rid or 'record #' + str(path[1])}" + (f" → {rest}" if rest else "")
        return "/".join(str(p) for p in path) or "top level"

    def flag(name, path, kind, siblings=None):
        stripped = name.strip()
        if name != stripped:
            parts = []
            if name[:1].isspace():
                parts.append("leading")
            if name[-1:].isspace():
                parts.append("trailing")
            extra = ""
            if siblings is not None:
                extra = (f" It therefore matches no key in the sibling \"properties\" (\"{stripped}\" does exist), so the "
                         "requirement level silently has no effect." if stripped in siblings else "")
            report.add("ERROR", "whitespace", loc.line(*path),
                       f"{' and '.join(parts).capitalize()} whitespace in {kind} {json.dumps(name)} ({where(path[:-1])})",
                       "Whitespace is part of the name, so this is a different string from "
                       f"\"{stripped}\" and won't match it anywhere it is referenced (marginality lists, $refs, "
                       "domainIncludes, the data being validated)." + extra,
                       f"Change it to \"{stripped}\".")
        elif any(ch.isspace() for ch in name) and kind in ("property name", "definition name", "marginality-list entry"):
            report.add("WARNING", "whitespace", loc.line(*path),
                       f"Whitespace inside {kind} {json.dumps(name)} ({where(path[:-1])})",
                       "Property and definition names are normally camelCase / lowercase without spaces.",
                       "Remove the space(s) if unintended.")

    for path, node in walk(data):
        if isinstance(node, dict):
            parent = path[-1] if path else None
            for k, v in node.items():
                kind = ("property name" if parent == "properties" else
                        "definition name" if parent == "definitions" else "key")
                if kind == "key" and k == k.strip():
                    pass  # only flag ordinary keys for leading/trailing whitespace
                else:
                    flag(k, path + (k,), kind)
                if k in NAME_VALUE_KEYS and isinstance(v, str):
                    flag(v, path + (k,), f"{k} value")
            # marginality lists next to "properties"
            props = node.get("properties") if isinstance(node.get("properties"), dict) else None
            for lst in NAME_LISTS:
                for j, name in enumerate(as_list(node.get(lst))):
                    if isinstance(name, str):
                        flag(name, path + (lst, j), "marginality-list entry", props)


# --------------------------------------------------------------------------------------
# 2. $validation checks
# --------------------------------------------------------------------------------------

def class_ancestors(cid, classes):
    """In-file ancestor chain (nearest first), stops on external parents or cycles."""
    out, seen, todo = [], {cid}, [cid]
    while todo:
        cur = todo.pop(0)
        rec = classes.get(cur)
        if not rec:
            continue
        for p in ids_of(rec.get("rdfs:subClassOf")):
            if p not in seen:
                seen.add(p)
                out.append(p)
                todo.append(p)
    return out


def check_validation_blocks(graph, classes, loc, report):
    from jsonschema import Draft7Validator

    for gi, rec in enumerate(graph):
        if not isinstance(rec, dict) or "rdfs:Class" not in as_list(rec.get("@type")):
            continue
        cid = rec.get("@id")
        val = rec.get("$validation")
        base = ("@graph", gi, "$validation")
        if val is None:
            report.add("INFO", "$validation", loc.line("@graph", gi),
                       f"Class {cid} has no $validation block",
                       "Without $validation the class inherits its parent's rules only through biothings' merge and "
                       "the NDE portal has no property list for it.", record=cid)
            continue
        if not isinstance(val, dict):
            report.add("ERROR", "$validation", loc.line(*base), f"$validation of {cid} is not an object", record=cid)
            continue
        if "properties" not in val:
            report.add("ERROR", "$validation", loc.line(*base), f"$validation of {cid} has no \"properties\"",
                       "biothings_schema requires a \"properties\" field in every $validation block.", record=cid)
        for k in ("$schema", "type"):
            if k not in val:
                report.add("WARNING", "$validation", loc.line(*base), f"$validation of {cid} is missing \"{k}\"",
                           "Every class's $validation block normally declares "
                           + ("\"$schema\": \"http://json-schema.org/draft-07/schema#\"." if k == "$schema" else "\"type\": \"object\"."),
                           record=cid)

        own_defs = val.get("definitions", {}) if isinstance(val.get("definitions"), dict) else {}
        anc_defs = {}
        for a in class_ancestors(cid, classes):
            av = classes[a].get("$validation", {}) if isinstance(classes.get(a), dict) else {}
            for dname in (av.get("definitions") or {}):
                anc_defs.setdefault(dname, a)

        used = set()
        for path, node in walk(val, base):
            if isinstance(node, dict) and isinstance(node.get("$ref"), str):
                ref = node["$ref"]
                ln = loc.line(*path, "$ref")
                m = re.match(r"^#/definitions/(.+)$", ref)
                if not m:
                    if ref.startswith("#"):
                        report.add("ERROR", "$validation", ln, f"Unsupported $ref \"{ref}\" in {cid}",
                                   "Local refs must have the form \"#/definitions/<name>\".", record=cid)
                    continue
                dname = m.group(1)
                used.add(dname)
                prop = path[len(base) + 1] if len(path) > len(base) + 1 and path[len(base)] == "properties" else None
                where = f"property \"{prop}\"" if prop else "/".join(str(p) for p in path[len(base):])
                if dname in own_defs:
                    continue
                if dname in anc_defs:
                    report.add("WARNING", "$validation", ln,
                               f"{cid}: {where} refers to #/definitions/{dname}, which is only defined in parent class {anc_defs[dname]}",
                               "biothings_schema merges parent definitions into the child, so this resolves there, but any tool that "
                               "reads this class's $validation on its own (jsonschema, the DDE/NDE UI) will fail to resolve the $ref.",
                               f"Copy the \"{dname}\" definition into {cid}'s $validation.definitions.", record=cid)
                else:
                    similar = [d for d in list(own_defs) + list(anc_defs) if d.lower() == dname.lower()]
                    report.add("ERROR", "$validation", ln,
                               f"{cid}: {where} refers to #/definitions/{dname}, which does not exist in this class"
                               + (" or any parent class" if anc_defs or class_ancestors(cid, classes) else ""),
                               "The $ref points to a missing definition, so the JSON schema cannot be resolved."
                               + (f" A definition with different capitalization exists: {similar}." if similar else ""),
                               f"Add \"{dname}\" under $validation.definitions of {cid}, or fix the $ref name.", record=cid)
        for dname in own_defs:
            if dname not in used:
                report.add("INFO", "$validation", loc.line(*base, "definitions", dname),
                           f"{cid}: definition \"{dname}\" is never referenced", record=cid)

        # required / recommended / optional must name existing properties (at every nesting level)
        for path, node in walk(val, base):
            if not (isinstance(node, dict) and isinstance(node.get("properties"), dict)):
                continue
            props = node["properties"]
            for lst in ("required", "recommended", "optional"):
                for j, name in enumerate(as_list(node.get(lst))):
                    if isinstance(name, str) and name != name.strip() and name.strip() in props:
                        continue  # reported by the whitespace check
                    if name not in props:
                        report.add("ERROR" if lst == "required" else "WARNING", "$validation",
                                   loc.line(*path, lst, j) or loc.line(*path, lst),
                                   f"{cid}: \"{name}\" is listed in \"{lst}\" but is not in the sibling \"properties\""
                                   f" ({'/'.join(str(p) for p in path[len(base):]) or 'top level'})",
                                   "A name listed here must match a key of the \"properties\" object next to it." +
                                   (" A missing required property makes every record fail validation." if lst == "required" else ""),
                                   "Fix the spelling or add the property.", record=cid)
            seen = {}
            for lst in ("required", "recommended", "optional"):
                for name in as_list(node.get(lst)):
                    if name in seen and seen[name] != lst:
                        report.add("WARNING", "$validation", loc.line(*path, lst),
                                   f"{cid}: \"{name}\" is listed in both \"{seen[name]}\" and \"{lst}\"", record=cid)
                    seen.setdefault(name, lst)

        # meta-validate against draft-07
        try:
            for err in Draft7Validator(Draft7Validator.META_SCHEMA).iter_errors(val):
                p = base + tuple(err.absolute_path)
                report.add("ERROR", "$validation", loc.line(*p),
                           f"{cid}: invalid JSON Schema at $validation/{'/'.join(str(x) for x in err.absolute_path)}: {err.message}",
                           "The $validation block must itself be a valid draft-07 JSON Schema "
                           "(biothings runs the same check: error_type invalid_validation_schema).",
                           "Fix the keyword/value named in the message (common causes: \"type\": \"text\" instead of \"string\", "
                           "a list where an object is expected).", record=cid)
        except Exception as e:
            report.add("WARNING", "$validation", loc.line(*base), f"Could not meta-validate {cid}: {e}", record=cid)


# --------------------------------------------------------------------------------------
# 3. Inheritance checks
# --------------------------------------------------------------------------------------

def check_inheritance(data, graph, classes, props_by_label, loc, report):
    ctx = data.get("@context", {}) if isinstance(data.get("@context"), dict) else {}
    infile_prefix = None
    if classes:
        infile_prefix = next(iter(classes)).split(":")[0]

    # record-level basics
    ids = {}
    for gi, rec in enumerate(graph):
        if not isinstance(rec, dict):
            continue
        rid = rec.get("@id")
        ln = loc.line("@graph", gi)
        for f in ("@id", "@type", "rdfs:label", "rdfs:comment"):
            if f not in rec:
                report.add("ERROR" if f != "rdfs:comment" else "WARNING", "record", ln,
                           f"Record {rid or '#'+str(gi)} is missing \"{f}\"",
                           "biothings_schema requires @id, @type, rdfs:comment and rdfs:label on every @graph record.", record=rid)
        if rid in ids:
            report.add("ERROR", "record", ln, f"Duplicate @id {rid} (also at line {ids[rid]})",
                       "Two records with the same @id overwrite each other.", record=rid)
        ids[rid] = ln
        if rid and rec.get("rdfs:label") and local_name(rid) != rec["rdfs:label"]:
            report.add("ERROR", "record", loc.line("@graph", gi, "rdfs:label") or ln,
                       f"@id {rid} does not match rdfs:label \"{rec['rdfs:label']}\"",
                       "biothings requires the local part of @id to equal rdfs:label exactly (error_type unmatched_label).",
                       "Make the label and the @id identical.", record=rid)
        if isinstance(rid, str) and ":" in rid and not rid.startswith("http"):
            pref = rid.split(":")[0]
            if pref not in ctx:
                report.add("ERROR", "record", ln, f"Prefix \"{pref}\" of {rid} is not declared in @context", record=rid)

    # subClassOf targets and cycles
    for cid, rec in classes.items():
        parents = ids_of(rec.get("rdfs:subClassOf"))
        ln = loc.rec_line(cid, "rdfs:subClassOf")
        if not parents:
            report.add("WARNING", "inheritance", ln, f"Class {cid} has no rdfs:subClassOf",
                       "biothings requires every class to have a path to schema:Thing (error_type no_path_to_root).", record=cid)
        for p in parents:
            pref = p.split(":")[0] if ":" in p else None
            if pref and pref not in ctx and not p.startswith("http"):
                report.add("ERROR", "inheritance", ln, f"{cid}: parent {p} uses prefix \"{pref}\" not declared in @context",
                           record=cid)
            if pref == infile_prefix and p not in classes:
                report.add("ERROR", "inheritance", ln, f"{cid}: parent class {p} is not defined in this file",
                           f"Classes in the \"{infile_prefix}\" namespace must be defined in this schema.",
                           "Fix the parent @id or add the parent class.", record=cid)
        # cycle
        seen, cur = [cid], cid
        stack = list(parents)
        visited = set()
        while stack:
            p = stack.pop()
            if p == cid:
                report.add("ERROR", "inheritance", ln, f"Cycle in rdfs:subClassOf involving {cid}", record=cid)
                break
            if p in visited or p not in classes:
                continue
            visited.add(p)
            stack.extend(ids_of(classes[p].get("rdfs:subClassOf")))

    # property records
    for label, recs in props_by_label.items():
        for gi, prec in recs:
            ln = loc.line("@graph", gi)
            dom = ids_of(prec.get("schema:domainIncludes"))
            if not dom:
                report.add("ERROR", "inheritance", ln, f"Property {prec.get('@id')} has no schema:domainIncludes",
                           "biothings looks up schema:domainIncludes for every property used in a $validation block; a property "
                           "record without it makes biothings crash with KeyError 'http://schema.org/domainIncludes'.",
                           "Add schema:domainIncludes listing the class(es) that use this property.", record=prec.get("@id"))
            for d in dom:
                if d.split(":")[0] == infile_prefix and d not in classes:
                    report.add("ERROR", "inheritance", loc.line("@graph", gi, "schema:domainIncludes") or ln,
                               f"Property {prec.get('@id')}: domainIncludes {d} is not a class in this file",
                               record=prec.get("@id"))
            if not prec.get("schema:rangeIncludes"):
                report.add("WARNING", "inheritance", ln, f"Property {prec.get('@id')} has no schema:rangeIncludes",
                           record=prec.get("@id"))

    # $validation properties must be allowed on the class or an ancestor
    for cid, rec in classes.items():
        val = rec.get("$validation")
        if not isinstance(val, dict) or not isinstance(val.get("properties"), dict):
            continue
        anc = [cid] + class_ancestors(cid, classes)
        external_anc = [a for a in anc if a not in classes]
        for pname in val["properties"]:
            recs = props_by_label.get(pname)
            if not recs:
                continue  # defined in a base schema (schema.org, niaid, bioschemas) -> biothings checks it
            doms = set()
            for _, prec in recs:
                doms.update(ids_of(prec.get("schema:domainIncludes")))
            if doms & set(anc):
                continue
            ln = loc.rec_line(cid, "$validation", "properties", pname)
            msg = (f"{cid}: property \"{pname}\" is in $validation, but its schema:domainIncludes ({', '.join(sorted(doms)) or 'none'}) "
                   f"does not include {cid} or any of its in-file ancestors ({', '.join(anc[1:]) or 'none'})")
            report.add("WARNING" if external_anc else "ERROR", "inheritance", ln, msg,
                       "biothings only allows a property in a class's $validation if the property's domainIncludes contains the "
                       "class or one of its parent classes (error: 'field ... is not defined in this class or any of its parent classes')."
                       + (f" One of the domain classes may still be an ancestor through the external parent(s) {external_anc}; "
                          "the biothings section below is authoritative." if external_anc else ""),
                       f"Add {{\"@id\": \"{cid}\"}} to the schema:domainIncludes of the \"{pname}\" property record.", record=cid)

    # parent/child $validation merge conflicts
    for cid, rec in classes.items():
        cval = rec.get("$validation")
        if not isinstance(cval, dict):
            continue
        cprops = cval.get("properties") or {}
        for parent in ids_of(rec.get("rdfs:subClassOf")):
            prec = classes.get(parent)
            if not prec or not isinstance(prec.get("$validation"), dict):
                continue
            pval = prec["$validation"]
            pprops = pval.get("properties") or {}
            missing = [p for p in pprops if p not in cprops]
            if missing:
                report.add("INFO", "inheritance", loc.rec_line(cid, "$validation", "properties"),
                           f"{cid} does not restate {len(missing)} propert{'y' if len(missing)==1 else 'ies'} from parent {parent}: "
                           + ", ".join(missing[:15]) + (" ..." if len(missing) > 15 else ""),
                           "biothings merges these in from the parent, so they are still inherited. Listed in case any were meant to "
                           "be overridden or removed (a child cannot remove an inherited property).", record=cid)
            for pname, pdef in pprops.items():
                cdef = cprops.get(pname)
                if not (isinstance(cdef, dict) and isinstance(pdef, dict)):
                    continue
                for key in ("anyOf", "oneOf", "enum", "items"):
                    if key in cdef and key in pdef and cdef[key] != pdef[key] and isinstance(cdef[key], list):
                        report.add("WARNING", "inheritance", loc.rec_line(cid, "$validation", "properties", pname, key),
                                   f"{cid}.{pname}: \"{key}\" differs from parent {parent} (line {loc.rec_line(parent, '$validation', 'properties', pname, key)})",
                                   f"biothings' merge UNIONS list values, so the effective \"{key}\" for {cid} will contain the parent's "
                                   "options as well as the child's. A child cannot narrow an inherited list this way.",
                                   "Make the lists identical, or move the narrower definition to a class that does not inherit it.", record=cid)
                tc, tp = cdef.get("type"), pdef.get("type")
                if tc and tp and tc != tp and not ("anyOf" in cdef or "anyOf" in pdef):
                    report.add("WARNING", "inheritance", loc.rec_line(cid, "$validation", "properties", pname, "type"),
                               f"{cid}.{pname}: type {tc!r} conflicts with parent {parent} type {tp!r}",
                               "The child's scalar value wins in the biothings merge, but data valid for the parent may be invalid here.",
                               record=cid)
            # requirement-level conflicts
            levels = ("required", "recommended", "optional")
            plevel = {n: l for l in levels for n in as_list(pval.get(l))}
            clevel = {n: l for l in levels for n in as_list(cval.get(l))}
            for n, pl in plevel.items():
                cl = clevel.get(n)
                if cl and cl != pl:
                    eff = " + ".join(sorted({cl, pl}))
                    report.add("WARNING", "inheritance", loc.rec_line(cid, "$validation", cl),
                               f"{cid}: \"{n}\" is \"{cl}\" here but \"{pl}\" in parent {parent}",
                               f"The merge unions these lists, so \"{n}\" ends up in both ({eff})"
                               + ("; it stays required no matter what the child says." if "required" in (cl, pl) else "."),
                               record=cid)


# --------------------------------------------------------------------------------------
# 4. biothings_schema
# --------------------------------------------------------------------------------------

BT_EXPLAIN = {
    "invalid_class_label": ("The class label must start with an uppercase letter.", "Capitalize the first letter of rdfs:label and @id."),
    "invalid_property_label": ("Property labels must start with a lowercase letter (camelCase).", "Lowercase the first letter of rdfs:label and @id."),
    "unmatched_label": ("The local part of @id must equal rdfs:label.", "Make @id and rdfs:label identical."),
    "dup_label": ("Two records share an rdfs:label.", "Rename or remove the duplicate record."),
    "undefined_domainincludes_class": ("schema:domainIncludes names a class that is not defined in this file or the base schemas.",
                                       "Fix the class @id or define the class."),
    "undefined_rangeincludes": ("schema:rangeIncludes names a class/type that is not defined in this file or the base schemas.",
                                "Fix the @id (e.g. schema:Text, schema:URL) or define the class."),
    "missing_domainincludes": ("The property has no schema:domainIncludes.", "Add the classes that use this property."),
    "missing_rangeincludes": ("The property has no schema:rangeIncludes.", "Add the expected value type(s)."),
    "invalid_class": ("The class record does not match biothings' class JSON schema (required keys / value shapes).",
                      "See the detailed message; usually a missing rdfs:comment/rdfs:label or a malformed rdfs:subClassOf."),
    "invalid_property": ("The property record does not match biothings' property JSON schema.",
                         "See the detailed message; usually a missing or malformed domainIncludes/rangeIncludes."),
    "invalid_validation_schema": ("Problem in the class's $validation block.",
                                  "If 'field X ... not defined in this class or any of its parent classes': the property X's "
                                  "schema:domainIncludes does not contain this class or any ancestor (in this file or in "
                                  "schema.org/bioschemas/niaid) - add the class to its domainIncludes, or remove/rename X. "
                                  "Otherwise the $validation is not valid JSON Schema - see the message."),
    "no_path_to_root": ("The class has no rdfs:subClassOf chain up to schema:Thing.",
                        "Check rdfs:subClassOf; the parent must exist in this file or in a loaded base schema."),
    "non_class_or_property_@type": ("@type must be rdfs:Class or rdf:Property.", "Fix @type."),
}


def explain_exception(exc, tb_text, data, loc):
    """Translate an uncaught biothings exception into a cause."""
    msg = str(exc)
    if isinstance(exc, KeyError) and "domainIncludes" in msg:
        # find which validation property matches a record with no domainIncludes
        graph = data.get("@graph", [])
        labels_wo_dom = {r.get("rdfs:label"): i for i, r in enumerate(graph)
                         if isinstance(r, dict) and "schema:domainIncludes" not in r}
        used = []
        for i, r in enumerate(graph):
            v = r.get("$validation") if isinstance(r, dict) else None
            if isinstance(v, dict):
                for p in (v.get("properties") or {}):
                    if p in labels_wo_dom:
                        used.append((p, labels_wo_dom[p], r.get("@id")))
        detail = "; ".join(f"\"{p}\" (record at line {loc.line('@graph', gi)}) used by {c}" for p, gi, c in used)
        return ("A record whose rdfs:label matches a property name used in a $validation block has no schema:domainIncludes. "
                "biothings looks up every record (class OR property, in this file AND in the base schemas) with that label "
                "and indexes ['http://schema.org/domainIncludes'] without a default. "
                + (f"Culprits: {detail}." if detail else
                   "No in-file culprit found, so it is a base-schema record (e.g. a schema.org class whose name equals the property)."))
    if isinstance(exc, KeyError) and "rdfs:label" in msg:
        return "A @graph record has no rdfs:label."
    if isinstance(exc, KeyError) and "@type" in msg:
        return "A @graph record has no @type."
    if "RecursionError" in type(exc).__name__ or "maximum recursion" in msg:
        return "A $ref chain refers to itself (a definition that $refs itself, directly or indirectly), which biothings' expand_ref cannot expand."
    if "ConnectionError" in type(exc).__name__ or "HTTPSConnection" in msg or "Max retries" in msg:
        return "biothings could not download the base schemas (schema.org from GitHub, bioschemas/niaid from the DDE). Check the network connection."
    return "See the traceback; the last frame inside biothings_schema shows which check failed."


def make_loader():
    """biothings_schema's BaseSchemaLoader only lists the first 20 DDE registry entries
    (?size=20). The registry now has more, and "niaid" falls outside the first 20, so it is
    silently never loaded and every niaid-derived class fails with no_path_to_root.
    This subclass lists the whole registry."""
    from biothings_schema.dataload import BaseSchemaLoader, load_json_or_yaml
    from biothings_schema.settings import DDE_SCHEMA_BASE_URL

    class FullRegistryLoader(BaseSchemaLoader):
        _reg = None

        @property
        def registered_dde_schemas(self):
            if FullRegistryLoader._reg is None:
                data = load_json_or_yaml(DDE_SCHEMA_BASE_URL + "?field=_id&size=100")
                FullRegistryLoader._reg = [h["namespace"] for h in data["hits"]]
            return FullRegistryLoader._reg

    return FullRegistryLoader()


def check_context_prefixes(data, own_ns, loader, loc, report):
    """Compare each @context prefix IRI with the IRI the registered DDE schema of the same
    name uses for itself. Returns {prefix: correct_iri} for mismatches."""
    ctx = data.get("@context", {})
    fixes = {}
    try:
        registered = loader.registered_dde_schemas
    except Exception as e:
        report.notes.append(f"Could not read the DDE registry ({e}); @context prefix check skipped.")
        return fixes
    for pref, iri in ctx.items():
        if pref == own_ns or pref not in registered or not isinstance(iri, str):
            continue
        try:
            src = loader.load_dde_schemas(pref)
        except Exception:
            continue
        reg_iri = (src.get("@context") or {}).get(pref)
        if reg_iri and reg_iri != iri:
            fixes[pref] = reg_iri
            report.add("WARNING", "inheritance", loc.line("@context", pref),
                       f"@context prefix \"{pref}\" is \"{iri}\" but the registered {pref} schema in DDE uses \"{reg_iri}\"",
                       f"Classes and properties are compared by full IRI, so {pref}:X here and {pref}:X in the registered schema are "
                       f"different things. Every class whose rdfs:subClassOf is a {pref}: class then has no path to schema:Thing, and "
                       "biothings rejects every property in its $validation. In testing, this was masked when the previously "
                       f"published {own_ns} schema was also loaded as a base (DDE's default), so it may not show up on DDE until "
                       "the old version is replaced.",
                       f"Change the \"{pref}\" entry in @context to \"{reg_iri}\".")
    return fixes


def run_biothings(data, loc, report, repaired, own_ns, include_published=False):
    try:
        from biothings_schema import Schema
        from biothings_schema.settings import COMMON_NAMESPACES
    except ImportError as e:
        report.notes.append(f"biothings_schema is not importable ({e}); run with the nde env python.")
        return
    if repaired:
        report.notes.append("biothings_schema was run on an in-memory copy with the syntax errors above auto-repaired; "
                            "fix them in the file and re-run to be sure.")
    loader = make_loader()
    data = copy.deepcopy(data)
    fixes = check_context_prefixes(data, own_ns, loader, loc, report)
    if fixes:
        data["@context"].update(fixes)
        report.notes.append("biothings_schema was run with the mismatched @context prefixes corrected in memory "
                            f"({', '.join(fixes)}), so the errors it reports are not just the knock-on effect of that.")
    base = [n for n in data.get("@context", {}) if n not in COMMON_NAMESPACES]
    if not include_published and own_ns in base:
        base.remove(own_ns)
        report.notes.append(f"biothings_schema was run WITHOUT the currently published \"{own_ns}\" schema from DDE as a base "
                            "(DDE's default includes it, which can hide problems because the old version's property/class "
                            "definitions are accepted too). Use --include-published to reproduce DDE's behaviour.")
    if "schema" in base:
        base[base.index("schema")] = "schema.org"
    sc = None
    try:
        sc = Schema(data, base_schema=base, base_schema_loader=loader,
                    validator_options={"raise_on_validation_error": False})
    except Exception as e:
        tb = traceback.format_exc()
        report.add("ERROR", "biothings", None, f"biothings_schema crashed while loading: {type(e).__name__}: {e}",
                   explain_exception(e, tb, data, loc), "")
        report.notes.append("biothings traceback (load):\n" + tb)
        return
    errs = [e.to_dict() for e in sc.validator.validation_errors]
    # group knock-on errors: when a class has no path to root, every field in it fails too
    rootless = {e.get("record_id") for e in errs if e.get("error_type") == "no_path_to_root"}
    grouped = {}
    for d in errs:
        if (d.get("error_type") == "invalid_validation_schema" and d.get("field")
                and d.get("record_id") in rootless):
            grouped.setdefault(d["record_id"], []).append(d["field"])
            d["_skip"] = True
    for d in errs:
        if d.get("_skip"):
            continue
        if d.get("error_type") == "no_path_to_root" and d.get("record_id") in grouped:
            fl = grouped[d["record_id"]]
            d["long_message"] = (f"Knock-on effect: biothings also rejected all {len(fl)} properties in this class's $validation "
                                 f"({', '.join(fl[:10])}{' ...' if len(fl) > 10 else ''}) because it cannot find any ancestors. "
                                 "Fix the subClassOf chain first and re-run.")
    for err in [d for d in errs if not d.get("_skip")]:
        d = err
        etype = d.get("error_type", "")
        rid = d.get("record_id")
        field = d.get("field")
        # record_id may be a full URI or a bare name
        cand = None
        for r in data.get("@graph", []):
            if isinstance(r, dict) and rid and (r.get("@id") == rid or local_name(r.get("@id")) == local_name(rid)):
                cand = r.get("@id")
                break
        ln = None
        if cand:
            ln = loc.rec_line(cand, "$validation", "properties", field) if field else loc.rec_line(cand)
        expl, fix = BT_EXPLAIN.get(etype, ("", ""))
        long = d.get("long_message", "")
        if long and long != d["message"]:
            expl = (expl + " Detail: " + long.split("\n\n")[0][:600]).strip()
        if etype == "invalid_validation_schema" and field and cand:
            for _, prec in [x for x in [(None, r) for r in data.get("@graph", [])]
                            if isinstance(x[1], dict) and x[1].get("rdfs:label") == field
                            and "rdf:Property" in as_list(x[1].get("@type"))]:
                doms = ", ".join(ids_of(prec.get("schema:domainIncludes"))) or "none"
                expl += (f" In this file, {prec.get('@id')} has schema:domainIncludes = {doms}; none of these is {cand} "
                         "or one of its ancestors.")
                fix = f"Add {{\"@id\": \"{cand}\"}} (or one of its parents) to schema:domainIncludes of {prec.get('@id')}."
        report.add("WARNING" if d.get("warning") else "ERROR", "biothings", ln,
                   f"[{etype or 'error'}] {d['message']}" + (f" (record {cand or rid})" if (cand or rid) else ""),
                   expl, fix, record=cand or rid)
    try:
        _ = sc.validation
    except Exception as e:
        tb = traceback.format_exc()
        report.add("ERROR", "biothings", None, f"Schema.validation failed: {type(e).__name__}: {e}",
                   explain_exception(e, tb, data, loc))
        report.notes.append("biothings traceback (Schema.validation):\n" + tb)


# --------------------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------------------

def render(report, schema_path, status):
    issues = sorted(report.issues, key=lambda x: (SEVERITY_ORDER[x.severity], x.line or 10**9))
    counts = {s: sum(1 for i in issues if i.severity == s) for s in SEVERITY_ORDER}
    sections = [
        ("syntax", "1. Syntax: brackets, braces and strings", ("brackets", "syntax", "duplicate-key")),
        ("comma", "2. Unnecessary / missing commas", ("comma",)),
        ("whitespace", "3. Stray whitespace in names", ("whitespace",)),
        ("$validation", "4. $validation definitions", ("$validation",)),
        ("inheritance", "5. Records and property inheritance", ("record", "inheritance")),
        ("biothings", "6. biothings_schema errors", ("biothings",)),
    ]
    out = [f"# Schema check report: `{os.path.basename(schema_path)}`", "",
           f"- File: `{schema_path}`",
           f"- Checked: {datetime.datetime.now():%Y-%m-%d %H:%M}",
           f"- Result: **{counts['ERROR']} errors, {counts['WARNING']} warnings, {counts['INFO']} info**",
           f"- {status}", ""]
    for n in report.notes:
        if "traceback" not in n:
            out.append(f"> {n}")
            out.append("")
    for key, title, checks in sections:
        sec = [i for i in issues if i.check in checks]
        out.append(f"## {title} ({len(sec)})")
        out.append("")
        if not sec:
            out.append("No problems found.")
            out.append("")
            continue
        for i in sec:
            loc_s = f"line {i.line}" + (f", col {i.col}" if i.col else "") if i.line else "line unknown"
            out.append(f"### {i.severity} · {loc_s}")
            out.append(f"**{i.message}**")
            out.append("")
            if i.explanation:
                out.append(f"*Why:* {i.explanation}")
                out.append("")
            if i.fix:
                out.append(f"*Fix:* {i.fix}")
                out.append("")
            snip = report.snippet(i.line)
            if snip:
                out.append("```")
                out.append(snip)
                out.append("```")
                out.append("")
    tbs = [n for n in report.notes if "traceback" in n]
    if tbs:
        out.append("## Appendix: biothings tracebacks")
        for t in tbs:
            out.append("```")
            out.append(t)
            out.append("```")
    return "\n".join(out), counts


def main():
    try:  # Windows consoles (cp1252) can't print characters like →
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    default_schema = os.path.abspath(os.path.join(here, "..", "..", "..", "..", "NDE_schema_v2.json"))
    ap.add_argument("schema", nargs="?", default=default_schema)
    ap.add_argument("--out", help="Markdown report path (default: <schema>_check_report.md next to the schema)")
    ap.add_argument("--skip-biothings", action="store_true", help="Skip the biothings_schema step (no network needed)")
    ap.add_argument("--include-published", action="store_true",
                    help="Also load the currently published version of this schema's namespace from DDE as a base "
                         "(DDE's own behaviour; can mask problems in the new version)")
    args = ap.parse_args()

    path = os.path.abspath(args.schema)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    report = Report(lines)

    # 1. syntax
    data, path_lines, fixed = check_syntax(text, report)
    strict_ok = True
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        strict_ok = False
        if not any(i.check in ("syntax", "comma", "brackets") for i in report.issues):
            report.add("ERROR", "syntax", e.lineno, f"json.load error: {e.msg}", col=e.colno)

    repaired = False
    if not strict_ok:
        try:
            data = json.loads(fixed)
            repaired = True
            status = ("File is NOT valid JSON. All syntax errors could be auto-repaired in memory, so the remaining checks "
                      "ran on the repaired copy (the file itself was not changed).")
        except json.JSONDecodeError as e:
            status = (f"File is NOT valid JSON and could not be fully repaired automatically (still failing at line {e.lineno} "
                      f"of the repaired copy). Semantic checks ran on a best-effort parse and may be incomplete.")
            if not isinstance(data, dict):
                data = {}
    else:
        status = "File is valid JSON."
        brk = [i for i in report.issues if i.check == "brackets"]
        if brk:
            status = ("File parses as valid JSON, BUT its braces do not match its indentation: an extra closer and a "
                      "missing closer cancel out, so the content between them sits at the wrong nesting level "
                      "(semantic checks below ran on the file as it parses, i.e. with the misplaced content).")
            for i in brk:
                i.explanation = ("The file still parses because this is paired with a compensating bracket problem nearby "
                                 "(see the neighbouring brackets issue). Everything between the two is at the wrong nesting "
                                 "level, e.g. properties that end up outside \"properties\" and are silently ignored by JSON "
                                 "Schema. " + i.explanation.replace("The block opened here is never closed. ", ""))

    graph = [r for r in data.get("@graph", []) if isinstance(r, dict)] if isinstance(data, dict) else []
    loc = Locator(path_lines, data.get("@graph", []) if isinstance(data, dict) else [])
    classes = {r["@id"]: r for r in graph if "rdfs:Class" in as_list(r.get("@type")) and "@id" in r}
    props_by_label = {}
    for gi, r in enumerate(data.get("@graph", []) if isinstance(data, dict) else []):
        if isinstance(r, dict) and "rdf:Property" in as_list(r.get("@type")):
            props_by_label.setdefault(r.get("rdfs:label"), []).append((gi, r))

    if isinstance(data, dict):
        check_whitespace(data, loc, report)
    if graph:
        check_validation_blocks(data["@graph"], classes, loc, report)
        check_inheritance(data, data["@graph"], classes, props_by_label, loc, report)
        if not args.skip_biothings:
            own_ns = next(iter(classes)).split(":")[0] if classes else None
            run_biothings(data, loc, report, repaired, own_ns, args.include_published)
        else:
            report.notes.append("biothings_schema step skipped (--skip-biothings).")
    else:
        report.notes.append("No @graph records could be parsed; semantic checks skipped.")

    md, counts = render(report, path, status)
    out = args.out or os.path.splitext(path)[0] + "_check_report.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)

    print(status)
    print(f"{counts['ERROR']} errors, {counts['WARNING']} warnings, {counts['INFO']} info")
    shown = [i for i in sorted(report.issues, key=lambda x: (SEVERITY_ORDER[x.severity], x.line or 10**9))
             if i.severity != "INFO"]
    for i in shown[:60]:
        print(f"  {i.severity:7} line {str(i.line or '?'):>6}  [{i.check}] {i.message}")
    if len(shown) > 60:
        print(f"  ... {len(shown) - 60} more in the report")
    print(f"Report written to {out}")
    return 1 if counts["ERROR"] else 0


if __name__ == "__main__":
    sys.exit(main())
