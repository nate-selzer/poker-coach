"""Cards, hand classes, a 7-card evaluator and equity calculation."""
import random
from collections import Counter
from itertools import combinations

RANKS = "23456789TJQKA"
SUITS = "cdhs"
DECK = [r + s for r in RANKS for s in SUITS]
RANK_VAL = {r: i + 2 for i, r in enumerate(RANKS)}

CATEGORY_NAMES = [
    "high card", "pair", "two pair", "trips", "straight",
    "flush", "full house", "quads", "straight flush",
]


def hand_class(c1, c2):
    """'AhKd' -> 'AKo', 'AhKh' -> 'AKs', 'AhAd' -> 'AA'."""
    r1, r2 = c1[0], c2[0]
    if RANK_VAL[r1] < RANK_VAL[r2]:
        r1, r2 = r2, r1
    if r1 == r2:
        return r1 + r2
    return r1 + r2 + ("s" if c1[1] == c2[1] else "o")


def all_classes():
    out = []
    for i in range(12, -1, -1):
        for j in range(12, -1, -1):
            hi, lo = RANKS[max(i, j)], RANKS[min(i, j)]
            if i == j:
                out.append(hi + lo)
            elif i > j:
                out.append(hi + lo + "s")
            else:
                out.append(hi + lo + "o")
    return list(dict.fromkeys(out))


def class_combos(cls):
    """All 2-card combos for a hand class, as 'AhKd' strings."""
    r1, r2 = cls[0], cls[1]
    out = []
    if r1 == r2:
        for a, b in combinations(SUITS, 2):
            out.append(r1 + a + r2 + b)
    elif cls[2] == "s":
        for s in SUITS:
            out.append(r1 + s + r2 + s)
    else:
        for a in SUITS:
            for b in SUITS:
                if a != b:
                    out.append(r1 + a + r2 + b)
    return [canon(c) for c in out]


def canon(combo):
    """One spelling per combo: higher card first ('6c8d' -> '8d6c')."""
    a, b = combo[:2], combo[2:]
    if (RANK_VAL[a[0]], SUITS.index(a[1])) < (RANK_VAL[b[0]], SUITS.index(b[1])):
        a, b = b, a
    return a + b


def split(combo):
    return combo[:2], combo[2:]


def _straight_high(rank_set):
    vals = set(rank_set)
    if 14 in vals:
        vals.add(1)
    for top in range(14, 4, -1):
        if all(v in vals for v in range(top - 4, top + 1)):
            return top
    return 0


def eval_hand(cards):
    """Best 5-card hand rank of 5-7 cards, as a comparable tuple."""
    vals = [RANK_VAL[c[0]] for c in cards]
    suits = Counter(c[1] for c in cards)
    flush_suit = next((s for s, n in suits.items() if n >= 5), None)
    if flush_suit:
        fvals = sorted((RANK_VAL[c[0]] for c in cards if c[1] == flush_suit), reverse=True)
        sf = _straight_high(fvals)
        if sf:
            return (8, sf)
    counts = Counter(vals)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    if groups[0][1] == 4:
        quad = groups[0][0]
        kick = max(v for v in vals if v != quad)
        return (7, quad, kick)
    trips = sorted((v for v, n in counts.items() if n >= 3), reverse=True)
    pairs = sorted((v for v, n in counts.items() if n == 2), reverse=True)
    if trips and (len(trips) > 1 or pairs):
        t = trips[0]
        p = max([x for x in trips[1:]] + pairs)
        return (6, t, p)
    if flush_suit:
        return (5,) + tuple(fvals[:5])
    st = _straight_high(vals)
    if st:
        return (4, st)
    desc = sorted(vals, reverse=True)
    if trips:
        t = trips[0]
        kick = [v for v in desc if v != t][:2]
        return (3, t) + tuple(kick)
    if len(pairs) >= 2:
        p1, p2 = pairs[0], pairs[1]
        kick = [v for v in desc if v != p1 and v != p2][:1]
        return (2, p1, p2) + tuple(kick)
    if pairs:
        p = pairs[0]
        kick = [v for v in desc if v != p][:3]
        return (1, p) + tuple(kick)
    return (0,) + tuple(desc[:5])


def describe(hole, board):
    """Short made-hand + draw description, e.g. 'pair (top pair) + flush draw'."""
    cards = list(hole) + list(board)
    if len(cards) < 5:
        return ""
    rank = eval_hand(cards)
    name = CATEGORY_NAMES[rank[0]]
    board_vals = sorted({RANK_VAL[c[0]] for c in board}, reverse=True)
    hole_vals = [RANK_VAL[c[0]] for c in hole]
    if rank[0] == 1:
        p = rank[1]
        if p not in hole_vals:
            name = "board pair (no pair of your own)"
        elif hole_vals[0] == hole_vals[1] == p and p > board_vals[0]:
            name += " (overpair)"
        elif p not in board_vals:
            name += " (underpair)"
        elif p == board_vals[0]:
            name += " (top pair)"
        elif len(board_vals) > 1 and p == board_vals[1]:
            name += " (second pair)"
        else:
            name += " (weak pair)"
    draws = []
    if len(board) < 5 and rank[0] < 4:
        suits = Counter(c[1] for c in cards)
        for s, n in suits.items():
            if n == 4 and any(c[1] == s for c in hole):
                draws.append("flush draw")
        vals = {RANK_VAL[c[0]] for c in cards}
        if 14 in vals:
            vals.add(1)
        outs = 0
        for v in range(1, 15):
            if v not in vals and _straight_high(vals | {v}):
                outs += 1
        if outs >= 2:
            draws.append("open-ended straight draw")
        elif outs == 1:
            draws.append("gutshot")
    return name + "".join(" + " + d for d in draws)


def equity(hero, villain_weights, board, samples=4000, rng=None):
    """Hero's showdown equity vs a weighted villain range ({combo: weight})."""
    rng = rng or random.Random()
    dead = set(hero) | set(board)
    combos, weights = [], []
    for combo, w in villain_weights.items():
        a, b = split(combo)
        if w > 0 and a not in dead and b not in dead:
            combos.append((a, b))
            weights.append(w)
    if not combos:
        return None
    need = 5 - len(board)
    if need == 0:
        hero_rank = eval_hand(list(hero) + list(board))
        total = score = 0.0
        for (a, b), w in zip(combos, weights):
            vr = eval_hand([a, b] + list(board))
            score += w * (1.0 if hero_rank > vr else 0.5 if hero_rank == vr else 0.0)
            total += w
        return score / total
    score = 0.0
    picks = rng.choices(combos, weights=weights, k=samples)
    for a, b in picks:
        deck = [c for c in DECK if c not in dead and c != a and c != b]
        run = list(board) + rng.sample(deck, need)
        hr = eval_hand(list(hero) + run)
        vr = eval_hand([a, b] + run)
        score += 1.0 if hr > vr else 0.5 if hr == vr else 0.0
    return score / samples
