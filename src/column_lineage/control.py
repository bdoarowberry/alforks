"""Lexical control-flow context (IF/ELSE/WHILE/TRY/CATCH) for indexed statements.

The source index splits procedure bodies into statements but drops the control
structure around them. This module recovers it from the masked source so a step
can say which runtime branch it belongs to. It never raises: on input it cannot
follow it stops early, and statements after that point have no context.
"""
from __future__ import annotations

import re

from table_lineage.index import DEFINITION, GO

WORD = re.compile(r"[A-Za-z_@#][\w$#@]*")
STATEMENT_START = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE", "SET", "DECLARE", "RETURN",
    "IF", "WHILE", "BEGIN", "PRINT", "RAISERROR", "THROW", "TRUNCATE", "WITH", "BREAK", "CONTINUE",
    "OPEN", "FETCH", "CLOSE", "DEALLOCATE", "COMMIT", "ROLLBACK", "SAVE", "GOTO", "WAITFOR", "CREATE",
    "DROP", "ALTER", "USE", "END", "ELSE",
}


def _collapse(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def header_end(masked: str, start: int, limit: int) -> int | None:
    """Offset just after the AS that ends a CREATE PROCEDURE header."""
    depth = 0
    i = start
    while i < limit:
        char = masked[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and char in "aA" and masked[i:i + 2].upper() == "AS" and \
                (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in "_$#@")) and \
                (i + 2 >= len(masked) or not (masked[i + 2].isalnum() or masked[i + 2] in "_$#")):
            return i + 2
        elif depth == 0 and char == ";":
            return None
        i += 1
    return None


class _Parser:
    def __init__(self, text: str, masked: str, statement_ends: dict[int, int]):
        self.text = text
        self.masked = masked
        self.ends = statement_ends
        self.regions: list[tuple[int, int, str]] = []
        self.arms: dict[tuple[int, int], tuple[int, str]] = {}   # region -> (owning IF/WHILE/TRY offset, arm)
        self.visited: set[int] = set()   # indexed statement starts the parser walked through
        self.steps = 0

    def word_at(self, i: int) -> tuple[str, int] | None:
        match = WORD.match(self.masked, i)
        if not match:
            return None
        if i > 0 and (self.masked[i - 1].isalnum() or self.masked[i - 1] in "_$#@"):
            return None
        return match.group().upper(), match.end()

    def skip_ws(self, i: int, end: int) -> int:
        while i < end and (self.masked[i].isspace() or self.masked[i] == ";"):
            i += 1
        return i

    def condition_end(self, i: int, end: int) -> int:
        """A condition ends at the first top-level statement keyword."""
        depth = 0
        case_depth = 0
        j = i
        while j < end:
            char = self.masked[j]
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif depth == 0 and (char.isalpha() or char == "_"):
                word = self.word_at(j)
                if word:
                    name, after = word
                    if name == "CASE":
                        case_depth += 1
                    elif name == "END" and case_depth:
                        case_depth -= 1
                    elif not case_depth and name in STATEMENT_START and j > i:
                        return j
                    j = after
                    continue
            j += 1
        return end

    def simple_end(self, i: int, end: int) -> int:
        known = self.ends.get(i)
        if known is not None and known > i:
            self.visited.add(i)
            return min(known, end)
        # Unindexed statement (PRINT, TRUNCATE, cursor verbs...): to ';' or end of line.
        newline = self.masked.find("\n", i, end)
        semicolon = self.masked.find(";", i, end)
        candidates = [p for p in (newline, semicolon) if p >= 0]
        return (min(candidates) + 1) if candidates else end

    def statement(self, i: int, end: int) -> int:
        self.steps += 1
        if self.steps > 200000:
            raise RecursionError("control-flow scan budget exceeded")
        i = self.skip_ws(i, end)
        if i >= end:
            return end
        word = self.word_at(i)
        if word is None:
            return self.simple_end(i, end)
        name, after = word
        if name in ("IF", "WHILE"):
            cond_end = self.condition_end(after, end)
            condition = _collapse(self.text[after:cond_end])
            label = f"{name} {condition}"
            body_end = self.statement(cond_end, end)
            self.regions.append((cond_end, body_end, label))
            self.arms[(cond_end, body_end)] = (i, "then" if name == "IF" else "loop")
            if name == "IF":
                j = self.skip_ws(body_end, end)
                nxt = self.word_at(j)
                if nxt and nxt[0] == "ELSE":
                    else_end = self.statement(nxt[1], end)
                    self.regions.append((nxt[1], else_end, f"ELSE (of IF {condition})"))
                    self.arms[(nxt[1], else_end)] = (i, "else")
                    return else_end
            return body_end
        if name == "BEGIN":
            j = self.skip_ws(after, end)
            nxt = self.word_at(j)
            if nxt and nxt[0] in ("TRAN", "TRANSACTION", "DISTRIBUTED"):
                return self.simple_end(i, end)
            if nxt and nxt[0] == "TRY":
                body_end = self.block(nxt[1], end, "TRY")
                self.regions.append((nxt[1], body_end, "TRY"))
                k = self.skip_ws(body_end, end)
                begin = self.word_at(k)
                if begin and begin[0] == "BEGIN":
                    catch = self.word_at(self.skip_ws(begin[1], end))
                    if catch and catch[0] == "CATCH":
                        catch_end = self.block(catch[1], end, "CATCH")
                        self.regions.append((catch[1], catch_end, "CATCH"))
                        self.arms[(catch[1], catch_end)] = (i, "catch")
                        return catch_end
                return body_end
            return self.block(after, end, None)
        return self.simple_end(i, end)

    def block(self, i: int, end: int, kind: str | None) -> int:
        """Parse statements until the matching END [TRY|CATCH]; return offset after it."""
        while i < end:
            i = self.skip_ws(i, end)
            if i >= end:
                return end
            word = self.word_at(i)
            if word and word[0] == "END":
                after = word[1]
                if kind:
                    nxt = self.word_at(self.skip_ws(after, end))
                    if nxt and nxt[0] == kind:
                        return nxt[1]
                return after
            nxt = self.statement(i, end)
            i = nxt if nxt > i else i + 1
        return end


def _parse(text: str, masked: str, statements) -> _Parser:
    ends = {s.start: s.end for s in statements}
    procedures = [m for m in DEFINITION.finditer(masked) if m.group("kind").lower().startswith("proc")]
    batches = [0] + [m.end() for m in GO.finditer(masked)]
    limits = sorted(set(batches + [m.start() for m in procedures] + [len(masked)]))
    parser = _Parser(text, masked, ends)
    for start in limits[:-1]:
        stop = next(limit for limit in limits if limit > start)
        body = start
        proc = next((m for m in procedures if m.start() == start), None)
        if proc:
            found = header_end(masked, proc.end(), stop)
            if found is None:
                continue
            body = found
        try:
            i = body
            while i < stop:
                nxt = parser.statement(i, stop)
                i = nxt if nxt > i else i + 1
        except RecursionError:
            continue
    return parser


def control_contexts(text: str, masked: str, statements) -> dict[int, tuple[str, ...]]:
    """Map each statement start offset to its enclosing control labels, outermost first."""
    parser = _parse(text, masked, statements)
    result: dict[int, tuple[str, ...]] = {}
    for statement in statements:
        enclosing = [r for r in parser.regions if r[0] <= statement.start < r[1]]
        enclosing.sort(key=lambda r: (r[0], -r[1]))
        result[statement.start] = tuple(label for _, _, label in enclosing)
    return result


def control_regions(text: str, masked: str, statements) -> dict[int, tuple[tuple[int, int, str, int, str], ...]]:
    """Like control_contexts, with each region's span and its arm: (start, end, label, owner, arm).

    Only statements the parser actually reached are included: a missing start means the control
    flow around it could not be followed. ``owner`` is the offset of the IF/WHILE/BEGIN TRY that opens
    the region; ``arm`` is ``then`` or
    ``else`` for IF, ``loop`` for WHILE, ``try`` / ``catch`` for TRY ... CATCH. Two regions with the same
    owner and different arms of an IF are mutually exclusive.
    """
    parser = _parse(text, masked, statements)
    result: dict[int, tuple[tuple[int, int, str, int, str], ...]] = {}
    for statement in statements:
        if statement.start not in parser.visited:
            continue
        enclosing = [r for r in parser.regions if r[0] <= statement.start < r[1]]
        enclosing.sort(key=lambda r: (r[0], -r[1]))
        result[statement.start] = tuple((s, e, label) + parser.arms.get((s, e), (s, "try" if label == "TRY" else "block"))
                                        for s, e, label in enclosing)
    return result
