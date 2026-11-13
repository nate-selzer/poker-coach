"""Drill deals: random hands filtered to reach a spot type the student misses.

Each drill picks a deal whose flop gives the hero a hand of the drilled kind,
and whose preflop cards make the line a single-raised pot without anyone
mixing: the hero holds a hand the chart plays purely (open, or call in the BB)
and the villain holds a hand the chart continues with 100%. The `edges` drill
is preflop-only and deals the hero a hand at the edge of their chart.

The villain's strategy is unchanged; only which hands get dealt is filtered,
so the villain never holds a hand it would mix with preflop.
"""
import random

import ranges as R
from cards import DECK, RANK_VAL, all_classes, describe, eval_hand, hand_class

DRILLS = {
    "strong": "strong made hands on the flop (top pair good kicker, overpairs, two pair, sets)",
    "medium": "medium pairs on the flop (second/bottom pair, weak-kicker top pair, underpairs)",
    "air": "no pair and no real draw on the flop",
    "draws": "flush draws and open-enders with no pair",
    "edges": "preflop hands at the edge of your opening/defending chart",
}
MODES = {
    "mix": "rotate through all of them",
    "adaptive": "pick types in proportion to where you make the most mistakes and lose the most EV",
}
ROTATION = ["strong", "medium", "air", "draws", "edges"]


def category(dec):
    """The drill type a logged decision belongs to, or None."""
    if dec.get("drill"):
        return dec["drill"]
    if dec["street"] == "preflop":
        return "edges"
    return classify(dec["hole"], dec["board"][:3])


def adaptive_weights(decisions):
    """{drill: (weight, n, mistakes, ev_lost)} favouring where the student loses most.

    Weight is (smoothed mistake rate + EV lost per decision) squared, so a type with
    few samples starts near the average and every type keeps a floor.
    """
    stats = {k: [0, 0.0, 0.0] for k in ROTATION}
    for d in decisions:
        if d.get("verdict") not in ("GOOD", "OK", "MISTAKE"):
            continue  # multiway approximate grades don't say much
        k = category(d)
        if k not in stats:
            continue
        st = stats[k]
        st[0] += 1
        st[1] += {"MISTAKE": 1.0, "OK": 0.3}.get(d["verdict"], 0.0)
        st[2] += d.get("ev_loss_bb") or 0.0
    out = {}
    for k, (n, m, ev) in stats.items():
        w = (m + 1) / (n + 4) + ev / (n + 4)
        out[k] = (max(w, 0.05) ** 2, n, m, ev)  # squared so real gaps stand out
    return out


def resolve(drill, hand_no, decisions=()):
    """The concrete drill for a hand; 'mix' rotates, 'adaptive' samples by leak weight."""
    if drill == "mix":
        return ROTATION[hand_no % len(ROTATION)]
    if drill == "adaptive":
        w = adaptive_weights(decisions)
        return random.choices(ROTATION, weights=[w[k][0] for k in ROTATION])[0]
    return drill


def classify(hole, flop):
    """'strong' | 'medium' | 'air' | 'draws' | None for the hero's flop hand."""
    rank = eval_hand(list(hole) + list(flop))
    desc = describe(hole, flop)
    hv = sorted((RANK_VAL[c[0]] for c in hole), reverse=True)
    bv = sorted((RANK_VAL[c[0]] for c in flop), reverse=True)
    own_pair = hv[0] == hv[1] or any(v in bv for v in hv)
    big_draw = "flush draw" in desc or "open-ended" in desc
    if rank[0] >= 3:
        return "strong" if own_pair else None  # trips on a trips board isn't the hero's hand
    if rank[0] == 2:
        return "strong" if all(v in bv for v in hv) else "medium"
    if rank[0] == 1 and own_pair:
        if "overpair" in desc:
            return "strong"
        if "top pair" in desc:
            kicker = hv[1] if hv[0] == bv[0] else hv[0]
            return "strong" if kicker >= 10 else "medium"
        return None if big_draw else "medium"
    if not own_pair:
        if big_draw:
            return "draws"
        if "gutshot" not in desc:
            return "air"
    return None


def _edge_classes(seat):
    """Hand classes at the chart's edge for the hero's first preflop decision."""
    classes = all_classes()
    if seat == "SB":
        freq = {c: R.SB_OPEN.get(c, 0.0) for c in classes}
    else:
        freq = {c: 1 - R.preflop_strategy("bb_vs_open", c)["fold"] for c in classes}
    mixed = [c for c in classes if 0 < freq[c] < 1]
    # Chart order runs strong to weak within each rank, so the last pure
    # continues and first pure folds per top card sit next to the cutoff.
    edge = set(mixed)
    by_top = {}
    for c in classes:
        by_top.setdefault(c[0] + (c[2:] or "p"), []).append(c)
    for group in by_top.values():
        group.sort(key=lambda c: -RANK_VAL[c[1]])
        for a, b in zip(group, group[1:]):
            if (freq[a] > 0) != (freq[b] > 0):
                edge.update((a, b))
    return sorted(edge)


def _pure(node, cls, action):
    return R.preflop_strategy(node, cls)[action] >= 0.999


def deal(drill, hero, rng=None, tries=200000):
    """A {"cards", "board_full"} deal for the drill, with the hero in `hero`."""
    rng = rng or random.Random()
    villain = "BB" if hero == "SB" else "SB"
    edges = set(_edge_classes(hero)) if drill == "edges" else None
    for _ in range(tries):
        deck = DECK[:]
        rng.shuffle(deck)
        cards = {"SB": deck[0:2], "BB": deck[2:4]}
        hcls, vcls = hand_class(*cards[hero]), hand_class(*cards[villain])
        if drill == "edges":
            if hcls not in edges:
                continue
            if hero == "BB" and not _pure("sb_open", vcls, "raise"):
                continue  # make sure the BB faces an open
        else:
            if hero == "SB":
                ok = _pure("sb_open", hcls, "raise") and _pure("bb_vs_open", vcls, "call")
            else:
                ok = _pure("sb_open", vcls, "raise") and _pure("bb_vs_open", hcls, "call")
            if not ok or classify(cards[hero], deck[4:7]) != drill:
                continue
        return {"cards": cards, "board_full": deck[4:9]}
    raise RuntimeError(f"no {drill} deal found")
