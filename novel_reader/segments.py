import re
from bisect import bisect_left
from dataclasses import dataclass


MIN_TARGET = 30
MAX_TARGET = 60
DEFAULT_TARGET = 45
# Allow an intact turn or clause some headroom before splitting it.
LONG_TURN = 84
LOOKAHEAD = 2 * LONG_TURN
PUNCTUATION = re.compile(
    r"""(?:(?:[。！？!?；;、…]|—{2,}|\.{2,}|(?<!\d)[,:，：]|[,:，：](?!\d)|\.(?=\s|$|[”’」』)"]|[\u3400-\u9fff]))"""
    r"""[”’」』）》】)\]"']*)+"""
)
STRONG = frozenset("。！？!?；;.…")
QUOTES = {"“": "”", "「": "」", "『": "』", "‘": "’", '"': '"'}
CLOSERS = set(QUOTES.values())
SPEAKER_LABEL = re.compile(r"[\u3400-\u9fff]{1,8}[：:]")
PADDING = " \t\u3000\u200b\ufeff"


class SegmentError(ValueError):
    pass


@dataclass(frozen=True)
class Passage:
    doc: int
    start: int
    end: int
    text: str
    more: bool = False

    def prefix(self, stop):
        return Passage(self.doc, self.start, self.start + stop, self.text[:stop])

    @property
    def next_position(self):
        return self.doc, self.end


def spoken_count(text):
    return sum(c.isalnum() for c in text)


def next_passage(book, position):
    """Read a bounded window inside a single EPUB paragraph, keeping source offsets."""
    doc, offset = position
    for index in range(doc, len(book.docs)):
        text, _ = book.document(index)
        start = offset if index == doc else 0
        while start < len(text):
            while start < len(text) and text[start].isspace():
                start += 1
            stop, count = start, 0
            while stop < len(text) and text[stop] != "\n":
                if text[stop].isalnum():
                    if count >= LOOKAHEAD:
                        break
                    count += 1
                stop += 1
            if count:
                return Passage(index, start, stop, text[start:stop],
                               stop < len(text) and text[stop] != "\n")
            start = stop + 1
    return None


def turn_boundary(text):
    """Find the first change of speaker, including dialogue with a reporting clause."""
    stack = []
    dialogue = False
    for i, c in enumerate(text):
        if not stack and (i == 0 or text[i - 1] in STRONG) and SPEAKER_LABEL.match(text, i):
            if i:
                return i, dialogue
            dialogue = True
        if stack and c == stack[-1]:
            stack.pop()
            if not stack and (dialogue or (i and text[i - 1] in STRONG)):
                return i + 1, True
        elif c in QUOTES:
            if not stack:
                lead = text[:i].rstrip(PADDING)
                dialogue = not lead or lead[-1] in "：:"
                if dialogue and lead:
                    # Keep the speaker tag after the preceding sentence with its quote.
                    prev = max((m.end() for m in PUNCTUATION.finditer(lead[:-1])
                                if STRONG.intersection(m.group())), default=0)
                    if prev:
                        return prev, False
                elif lead and lead[-1] in STRONG:
                    return len(lead), False
            stack.append(QUOTES[c])
        elif c in CLOSERS and not stack:
            # Starting in the middle of a long speech or after seeking.
            return i + 1, True
    return len(text), dialogue


def choose_cut(text, more=False, target=DEFAULT_TARGET, *, predict=None):
    """Prefer a whole turn, then a sentence, clause, prosodic break or word."""
    line = re.search(r"(?<=\S)[^\S\n]*\n", text)
    limit = line.end() if line else len(text)
    end, dialogue = turn_boundary(text[:limit])
    complete = end < len(text) or not more
    piece = text[:end]
    counts = [0]
    for c in piece:
        counts.append(counts[-1] + c.isalnum())
    total = counts[-1]
    if not total:
        return end, False

    if complete and total <= LONG_TURN and (dialogue or total <= target + 12):
        return end, False

    def score(offset):
        length = counts[offset]
        tail = total - length
        # Avoid a tiny opening or stranding the final few syllables.
        return (abs(length - target) + 3 * max(0, 17 - length)
                + (3 * max(0, 14 - tail) if complete and tail else 0))

    choices = []
    for match in PUNCTUATION.finditer(piece):
        offset = match.end()
        if not 0 < counts[offset] <= LONG_TURN:
            continue
        if (piece[offset:].lstrip(PADDING)[:1] in QUOTES
                and match.group().rstrip().endswith(("：", ":"))):
            continue
        if counts[offset] == total and not complete:
            continue
        strong = STRONG.intersection(match.group())
        if not strong and counts[offset] < 14 and total > LONG_TURN:
            continue
        penalty = 0 if strong else 22
        choices.append((score(offset) + penalty, offset))
    if complete and total <= LONG_TURN:
        choices.append((score(end), end))
        # Do not break a single reasonable sentence at an internal comma.
        if total <= MAX_TARGET + 6 and not any(STRONG.intersection(m.group())
                for m in PUNCTUATION.finditer(piece) if counts[m.end()] < total):
            return end, False
    if choices:
        return min(choices)[1], False

    if predict is not None:
        breaks = [(score(i) + 4 * (3 - rank), i) for i, rank in predict(piece).items()
                  if 0 < i < end and rank and 0 < counts[i] <= LONG_TURN]
        if breaks:
            return min(breaks)[1], True

    # No usable punctuation. Prefer a word boundary even when WeTTS is off.
    import jieba
    breaks = [(score(stop), stop) for _, _, stop in jieba.tokenize(piece, HMM=False)
              if 0 < counts[stop] <= LONG_TURN and stop < end
              and not (piece[stop - 1].isascii() and piece[stop - 1].isalnum()
                       and piece[stop].isascii() and piece[stop].isalnum())]
    if breaks:
        return min(breaks)[1], True
    return bisect_left(counts, target), True
