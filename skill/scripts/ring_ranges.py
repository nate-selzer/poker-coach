"""6-max 100bb preflop charts, generated from one hand score.

Every range is "the top X% of hands" by a single score (raw equity vs a
random hand, plus bonuses for suitedness and connectedness), so charts stay
internally consistent. These are approximations, not solver output; the
percentages below are typical raise-first-in / defend frequencies.

Preflop tree: open 2.5 | vs open: fold, call, 3-bet to 9 |
opener vs 3-bet: fold, call, jam | 3-bettor vs jam: fold, call.
Players other than the opener fold to a 3-bet.
"""
import json
import os

from cards import RANK_VAL, all_classes, class_combos

SEATS = ["UTG", "HJ", "CO", "BTN", "SB", "BB"]
POSTFLOP_ORDER = ["SB", "BB", "UTG", "HJ", "CO", "BTN"]

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_EQ = json.load(open(os.path.join(_DATA, "class_equity.json")))
# Equity vs a tight (top ~12%) range: ranks hands for 3-bets, jams and calling jams,
# where equity vs a random hand overrates small pairs relative to A-K.
_EQ_TIGHT = json.load(open(os.path.join(_DATA, "class_equity_tight.json")))


def score(cls):
    s = _EQ[cls]
    if len(cls) == 3:
        gap = RANK_VAL[cls[0]] - RANK_VAL[cls[1]]
        if cls[2] == "s":
            s += 2.5
        s += {1: 1.5, 2: 1.0, 3: 0.5}.get(gap, 0.0)
    else:
        s += 1.5  # pairs: set value
    return s


def score_tight(cls):
    s = _EQ_TIGHT[cls]
    if len(cls) == 3 and cls[2] == "s":
        s += 1.5
    if len(cls) == 2 and RANK_VAL[cls[0]] <= 7:
        s -= 4.0  # small pairs set-mine by calling rather than 3-betting
    return s


def score_multiway(cls):
    """Calling over a caller: favor hands that make strong hands, not dominated offsuit cards."""
    s = score(cls)
    if len(cls) == 2:
        return s + 2.0
    gap = RANK_VAL[cls[0]] - RANK_VAL[cls[1]]
    if cls[2] == "s":
        return s + 1.5
    return s - (1.0 if gap <= 2 else 3.0)


_ORDER = sorted(all_classes(), key=score, reverse=True)
_ORDER_MW = sorted(all_classes(), key=score_multiway, reverse=True)
_ORDER_TIGHT = sorted(all_classes(), key=score_tight, reverse=True)
_COMBOS = {c: len(class_combos(c)) for c in _ORDER}


def band(lo_pct, hi_pct, order=None):
    """{class: weight} for hands ranked between lo% and hi% of all combos."""
    out, cum = {}, 0.0
    lo, hi = lo_pct / 100 * 1326, hi_pct / 100 * 1326
    for c in order or _ORDER:
        start, end = cum, cum + _COMBOS[c]
        overlap = max(0.0, min(end, hi) - max(start, lo))
        if overlap > 0:
            out[c] = round(overlap / _COMBOS[c], 3)
        cum = end
        if cum >= hi:
            break
    return out


RFI = {"UTG": 16, "HJ": 21, "CO": 28, "BTN": 45, "SB": 40}
# (3-bet %, call %) for a responder facing an open, by opener.
VS_OPEN_IP = {"UTG": (3.5, 6), "HJ": (4.5, 7), "CO": (6, 9), "BTN": (0, 0), "SB": (0, 0)}
VS_OPEN_SB = {"UTG": (4.5, 2), "HJ": (5.5, 2), "CO": (7.5, 2), "BTN": (11, 2)}
VS_OPEN_BB = {"UTG": (4, 22), "HJ": (5, 27), "CO": (7, 35), "BTN": (10, 48), "SB": (13, 47)}
JAM_PCT = 2.6       # opener's 4-bet jam range vs a 3-bet (about QQ+, AK)
CALL_JAM_PCT = 2.2  # 3-bettor's call vs that jam


def _vs_open_pcts(opener, responder):
    if responder == "BB":
        return VS_OPEN_BB[opener]
    if responder == "SB":
        return VS_OPEN_SB.get(opener, (0, 0))
    return VS_OPEN_IP[opener]


SQUEEZE_PER_CALLER = 1.5  # extra 3-bet % per caller: callers' ranges are capped
OVERCALL_PER_CALLER = 0.2  # call range grows ~20% per caller (better pot odds)


def strategy(node, seat, cls, opener=None, callers=0):
    """{action: prob} for a hand class.

    node: 'open' (no raise yet), 'vs_open', 'vs_3bet' (opener), 'vs_jam' (3-bettor).
    """
    if node == "open":
        if seat == "BB":
            return {"check": 1.0}
        r = band(0, RFI[seat]).get(cls, 0.0)
        return {"fold": 1 - r, "raise": r}
    if node == "vs_open":
        tb, call = _vs_open_pcts(opener, seat)
        order = _ORDER
        if callers:
            tb += SQUEEZE_PER_CALLER * callers
            call *= 1 + OVERCALL_PER_CALLER * callers
            order = _ORDER_MW
        r = band(0, tb, _ORDER_TIGHT).get(cls, 0.0)
        c = 0.0 if r >= 1 else min(1 - r, band(0, tb + call, order).get(cls, 0.0))
        return {"fold": max(0.0, 1 - r - c), "call": c, "raise": r}
    if node == "vs_3bet":
        opened = band(0, RFI[seat]).get(cls, 0.0)
        j = min(opened, band(0, JAM_PCT, _ORDER_TIGHT).get(cls, 0.0))
        c = min(opened - j, band(0, RFI[seat] * 0.4, _ORDER_TIGHT).get(cls, 0.0) - j)
        c = max(0.0, c)
        return {"fold": max(0.0, 1 - j - c), "call": c, "allin": j}
    if node == "vs_jam":
        c = band(0, CALL_JAM_PCT, _ORDER_TIGHT).get(cls, 0.0)
        return {"fold": 1 - c, "call": c}
    raise ValueError(node)


def range_for(node, seat, action, opener=None, callers=0):
    """{combo: weight} of hands that take `action` at `node`."""
    out = {}
    for cls in _ORDER:
        w = strategy(node, seat, cls, opener, callers).get(action, 0.0)
        if w > 0.005:
            for combo in class_combos(cls):
                out[combo] = w
    return out


def class_range_for(node, seat, action, opener=None, callers=0):
    """{class: weight} version, for flop solves (keeps suit isomorphism)."""
    return {cls: w for cls in _ORDER
            if (w := strategy(node, seat, cls, opener, callers).get(action, 0.0)) > 0.005}
