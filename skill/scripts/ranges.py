"""Simplified heads-up 100bb preflop charts.

These are hand-written approximations of HU no-limp GTO play, not solver
output. The preflop tree is fixed:

  SB (button):  fold | raise to 2.5
  BB vs open:   fold | call | 3-bet to 10
  SB vs 3-bet:  fold | call | jam (100)
  BB vs jam:    fold | call
"""
from cards import RANKS, RANK_VAL, all_classes, class_combos


def _expand(token):
    token = token.strip()
    if not token:
        return []
    plus = token.endswith("+")
    if plus:
        token = token[:-1]
    if "-" in token:
        a, b = token.split("-")
        suffix = a[2:] if len(a) > 2 else ""
        if a[0] == a[1]:
            lo, hi = sorted([RANK_VAL[a[0]], RANK_VAL[b[0]]])
            return [RANKS[v - 2] * 2 for v in range(lo, hi + 1)]
        lo, hi = sorted([RANK_VAL[a[1]], RANK_VAL[b[1]]])
        return _with_suffix([a[0] + RANKS[v - 2] for v in range(lo, hi + 1)], suffix)
    r1, r2, suffix = token[0], token[1], token[2:]
    if r1 == r2:
        if plus:
            return [RANKS[v - 2] * 2 for v in range(RANK_VAL[r1], 15)]
        return [r1 + r2]
    if plus:
        return _with_suffix([r1 + RANKS[v - 2] for v in range(RANK_VAL[r2], RANK_VAL[r1])], suffix)
    return _with_suffix([r1 + r2], suffix)


def _with_suffix(bases, suffix):
    if suffix:
        return [b + suffix for b in bases]
    return [b + s for b in bases for s in ("s", "o")]


def chart(spec):
    """'22+, A2s+, KQo:0.5' -> {class: weight}. Later tokens override earlier ones."""
    out = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        w = 1.0
        if ":" in part:
            part, ws = part.split(":")
            w = float(ws)
        for cls in _expand(part):
            out[cls] = w
    return out


SUITED_ALL = "J2s+, T2s+, 92s+, 82s+, 72s+, 62s+, 52s+, 42s+, 32s"

SB_OPEN = chart(
    "22+, A2+, K2+, Q2+, J5+, T6+, 96+, 86+, 75+, 65, 54, " + SUITED_ALL + ", "
    "J3o, J4o, T4o, T5o, 95o, J2o:0.5, 85o:0.5, 74o:0.5, 64o:0.5, 53o:0.5, 43o:0.5"
)
BB_DEFEND = chart(
    "22+, A2+, K2+, Q2+, " + SUITED_ALL
)
# Offsuit defends (non-3-bet hands) come from raw equity vs SB_OPEN times an
# assumed out-of-position realization factor (0.88 for hands within two ranks,
# 0.80 otherwise): realized >= 31% calls, 29-31% mixes, below folds.
BB_DEFEND.update(chart(
    "KJo:1.0, QJo:1.0, ATo:1.0, KTo:1.0, QTo:1.0, JTo:1.0, J9o:1.0, T9o:1.0, A8o:1.0, K8o:1.0, Q8o:1.0, J8o:1.0, T8o:1.0, 98o:1.0, A7o:1.0, K7o:1.0, Q7o:1.0, J7o:1.0, T7o:1.0, 97o:1.0, 87o:1.0, A6o:1.0, K6o:1.0, Q6o:1.0, J6o:1.0, T6o:1.0, 96o:1.0, 86o:1.0, 76o:1.0, A5o:1.0, K5o:1.0, Q5o:1.0, J5o:1.0, T5o:1.0, 95o:1.0, 85o:1.0, 75o:1.0, 65o:1.0, A4o:1.0, K4o:1.0, Q4o:1.0, J4o:1.0, T4o:1.0, 94o:0.5, 84o:0.5, 74o:0.0, 64o:1.0, 54o:1.0, A3o:1.0, K3o:1.0, Q3o:1.0, J3o:1.0, T3o:1.0, 93o:0.5, 83o:0.0, 73o:0.0, 63o:0.0, 53o:1.0, 43o:0.5, A2o:1.0, K2o:1.0, Q2o:1.0, J2o:1.0, T2o:0.5, 92o:0.0, 82o:0.0, 72o:0.0, 62o:0.0, 52o:0.0, 42o:0.0, 32o:0.0"
))
BB_3BET = chart(
    "99+, AJs+, AQo+, KQs, 88:0.5, ATs:0.5, KJs:0.5, AJo:0.5, KQo:0.5, "
    "A5s, A4s, A3s:0.5, A2s:0.5, K9s:0.5, K8s:0.3, Q9s:0.4, J9s:0.4, T8s:0.4, "
    "97s:0.4, 86s:0.4, 75s:0.4, 64s:0.4, 54s:0.4, A9o:0.3, K9o:0.2, Q9o:0.2"
)
SB_CONT_VS_3BET = chart(
    "22+, A2s+, A8o+, A7o:0.5, K5s+, KTo+, K9o:0.5, Q8s+, QJo, QTo:0.5, "
    "J8s+, JTo:0.5, T8s+, 97s+, 86s+, 75s+, 64s+, 54s"
)
SB_JAM = chart("QQ+, AK, JJ:0.5, AQs:0.5, A5s:0.5, A4s:0.5")
BB_CALL_JAM = chart("QQ+, AK, JJ:0.75, TT:0.4, AQs:0.5")

NODES = {
    "sb_open": ("SB", ["fold", "raise"]),
    "bb_vs_open": ("BB", ["fold", "call", "raise"]),
    "sb_vs_3bet": ("SB", ["fold", "call", "allin"]),
    "bb_vs_jam": ("BB", ["fold", "call"]),
}


def preflop_strategy(node, cls):
    """{action: probability} for a hand class at a preflop node."""
    if node == "sb_open":
        r = SB_OPEN.get(cls, 0.0)
        return {"fold": 1 - r, "raise": r}
    if node == "bb_vs_open":
        r = BB_3BET.get(cls, 0.0)
        c = max(0.0, BB_DEFEND.get(cls, 0.0) - r)
        return {"fold": max(0.0, 1 - r - c), "call": c, "raise": r}
    if node == "sb_vs_3bet":
        j = SB_JAM.get(cls, 0.0)
        c = max(0.0, SB_CONT_VS_3BET.get(cls, 0.0) - j)
        return {"fold": max(0.0, 1 - j - c), "call": c, "allin": j}
    if node == "bb_vs_jam":
        c = BB_CALL_JAM.get(cls, 0.0)
        return {"fold": 1 - c, "call": c}
    raise ValueError(node)


def class_weights_for(node, action):
    """{class: weight} of the range that took `action` at `node`."""
    return {cls: preflop_strategy(node, cls)[action] for cls in all_classes()}


def postflop_class_ranges(line):
    """Start-of-flop {class: weight} ranges for both seats."""
    if line == "srp":
        sb = class_weights_for("sb_open", "raise")
        bb = class_weights_for("bb_vs_open", "call")
    elif line == "3bp":
        bb = class_weights_for("bb_vs_open", "raise")
        sb = class_weights_for("sb_vs_3bet", "call")
    else:
        raise ValueError(line)
    return {"SB": sb, "BB": bb}


def postflop_ranges(line):
    """Start-of-flop {combo: weight} ranges for both seats."""
    return {seat: _to_combos(w) for seat, w in postflop_class_ranges(line).items()}


def _to_combos(class_weights):
    out = {}
    for cls, w in class_weights.items():
        if w > 0.005:
            for combo in class_combos(cls):
                out[combo] = w
    return out
