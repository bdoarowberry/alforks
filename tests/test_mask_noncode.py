"""mask_noncode must stay byte-identical to the original character-by-character version."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from table_lineage.index import mask_noncode

ROOT = Path(__file__).parents[1]


def reference(text: str) -> str:
    result = list(text)
    i = 0
    while i < len(text):
        start = i
        if text.startswith('--', i):
            end = text.find('\n', i)
            i = len(text) if end < 0 else end
        elif text.startswith('/*', i):
            i += 2
            depth = 1
            while i < len(text) and depth:
                if text.startswith('/*', i):
                    depth += 1
                    i += 2
                elif text.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif text[i] == "'":
            i += 1
            while i < len(text):
                if text[i] == "'":
                    if i + 1 < len(text) and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif text[i] in '["':
            close = ']' if text[i] == '[' else '"'
            i += 1
            while i < len(text):
                if text[i] == close:
                    if i + 1 < len(text) and text[i + 1] == close:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        else:
            i += 1
            continue
        for pos in range(start, i):
            if text[pos] not in '\r\n':
                result[pos] = ' '
    return ''.join(result)


def test_matches_reference_on_fixtures():
    for path in sorted((ROOT / "fixtures").rglob("*.sql")):
        text = path.read_text(encoding="utf-8")
        assert mask_noncode(text) == reference(text), path


def test_matches_reference_on_random_input():
    rng = random.Random(7)
    alphabet = ["-", "--", "/*", "*/", "*", "/", "'", "''", "[", "]", "]]", '"', '""', "\n", "\r\n", "a", " ", "x;"]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert mask_noncode(text) == reference(text), repr(text)
