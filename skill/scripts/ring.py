"""4-max and 6-max modes: preflop from position charts, heads-up postflop from the
solver, multiway postflop from a rules-based villain with approximate grades.

coach.py delegates new/act/next/status here when the table is set to 6.
The 'SB'/'BB' keys inside solver ranges mean IP/OOP (solver.py's labels).
"""
import copy
import hashlib
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ring_ranges as RR  # noqa: E402
import solver as S  # noqa: E402
import table as T  # noqa: E402
from cards import DECK, RANK_VAL, describe, eval_hand, hand_class  # noqa: E402

import coach as C  # noqa: E402  (shared persistence, logging, formatting)

STATE = os.path.join(C.DATA_DIR, "ring_state.json")


def table_size():
    return C.load(C.META, {}).get("table", 6)


def seats_for(n):
    """4-max is 6-max without UTG and HJ; the CO faces the same three players behind."""
    return RR.SEATS[-n:]


def meta_key(n):
    return "ring" if n == 6 else f"ring{n}"  # 'ring' kept for existing 6-max history
STACK = 100.0
OPEN_TO, THREEBET_TO = 2.5, 9.0
MULTI_BET = [33, 75]
RAISE_LIMIT = 2
fmt = C.fmt


# ---------- helpers ----------

def pot(st):
    return sum(st["contrib"].values())


def behind(st, seat):
    return STACK - st["contrib"][seat]


def who(st, seat):
    return f"{seat} ({'you' if seat == st['hero'] else 'villain'})"


def put(st, seat, amount):
    amount = max(0.0, min(amount, behind(st, seat)))
    st["contrib"][seat] += amount
    st["street_contrib"][seat] += amount


def live(st):
    return [s for s in RR.POSTFLOP_ORDER if s in st["in_hand"]]


# ---------- preflop ----------

def pf_node(st, seat):
    p = st["pf"]
    if p["jammer"]:
        return "vs_jam"
    if p["threebettor"]:
        return "vs_3bet"
    if p["opener"]:
        return "vs_open"
    return "open"


def pf_options(st, seat):
    node = pf_node(st, seat)
    to_call = max(st["contrib"].values()) - st["contrib"][seat]
    if node == "open":
        if seat == "BB":
            return node, [{"id": "check", "key": "check", "label": "check (everyone folded)"}]
        return node, [{"id": "fold", "key": "fold", "label": "fold"},
                      {"id": "raise", "key": "raise", "label": f"raise to {fmt(OPEN_TO)}"}]
    if node == "vs_open":
        return node, [{"id": "fold", "key": "fold", "label": "fold"},
                      {"id": "call", "key": "call", "label": f"call {fmt(to_call)}"},
                      {"id": "raise", "key": "raise", "label": f"3-bet to {fmt(THREEBET_TO)}"}]
    if node == "vs_3bet":
        return node, [{"id": "fold", "key": "fold", "label": "fold"},
                      {"id": "call", "key": "call", "label": f"call {fmt(to_call)}"},
                      {"id": "allin", "key": "allin", "label": f"all-in ({fmt(behind(st, seat))})"}]
    return node, [{"id": "fold", "key": "fold", "label": "fold"},
                  {"id": "call", "key": "call", "label": f"call {fmt(to_call)} (all-in)"}]


def n_callers(st):
    """Players who have flat-called the open so far."""
    return sum(1 for r in st["pf"]["range"].values() if r[0] == "vs_open" and r[2] == "call")


def pf_strategy(st, seat):
    node = pf_node(st, seat)
    return RR.strategy(node, seat, hand_class(*st["cards"][seat]), st["pf"]["opener"], n_callers(st))


def apply_preflop(st, seat, a):
    p = st["pf"]
    node = pf_node(st, seat)
    if a == "fold":
        st["in_hand"].remove(seat)
        st["history"].append(f"{who(st, seat)} folds")
    elif node == "open" and a == "raise":
        put(st, seat, OPEN_TO - st["contrib"][seat])
        p["opener"] = seat
        p["range"][seat] = ["open", seat, "raise", None]
        st["history"].append(f"{who(st, seat)} raises to {fmt(OPEN_TO)}")
    elif node == "open":  # BB checks after everyone folded
        st["history"].append(f"{who(st, seat)} wins the blinds")
    elif node == "vs_open" and a == "call":
        put(st, seat, OPEN_TO - st["contrib"][seat])
        p["range"][seat] = ["vs_open", seat, "call", p["opener"], n_callers(st)]
        st["history"].append(f"{who(st, seat)} calls")
    elif node == "vs_open":  # 3-bet
        put(st, seat, THREEBET_TO - st["contrib"][seat])
        p["threebettor"] = seat
        p["range"][seat] = ["vs_open", seat, "raise", p["opener"], n_callers(st)]
        st["history"].append(f"{who(st, seat)} 3-bets to {fmt(THREEBET_TO)}")
        for s in list(st["in_hand"]):  # everyone but the opener drops out
            if s not in (seat, p["opener"]):
                st["in_hand"].remove(s)
                st["history"].append(f"{who(st, s)} folds")
        st["pf_queue"] = [p["opener"]]
        return
    elif node == "vs_3bet" and a == "call":
        put(st, seat, THREEBET_TO - st["contrib"][seat])
        p["range"][seat] = ["vs_3bet", seat, "call", None]
        st["history"].append(f"{who(st, seat)} calls")
    elif node == "vs_3bet":  # jam
        put(st, seat, STACK)
        p["jammer"] = seat
        st["history"].append(f"{who(st, seat)} moves all-in")
        st["pf_queue"] = [p["threebettor"]]
        return
    else:  # vs_jam call
        put(st, seat, STACK)
        st["history"].append(f"{who(st, seat)} calls all-in")


def advance_preflop(st):
    """Run preflop until the hero must act or preflop ends."""
    while st["street"] == "preflop" and not st["over"]:
        if len(st["in_hand"]) == 1:
            return finish(st, [st["in_hand"][0]])
        if not st["pf_queue"]:
            return end_preflop(st)
        seat = st["pf_queue"][0]
        if seat not in st["in_hand"]:
            st["pf_queue"].pop(0)
            continue
        if hero_out(st):
            return
        if seat == st["hero"]:
            st["to_act"] = seat
            return
        strat = pf_strategy(st, seat)
        keys = list(strat)
        p = st["pf"]
        # Seeded by the situation, so a simulated line reproduces the real one.
        rng = random.Random(f"{st.get('seed')}|{seat}|{pf_node(st, seat)}|{p['opener']}|{p['threebettor']}")
        a = rng.choices(keys, weights=[max(strat[k], 0) for k in keys])[0]
        st["pf_queue"].pop(0)
        apply_preflop(st, seat, a)


def end_preflop(st):
    players = live(st)
    if len(players) == 1:
        return finish(st, players)
    if any(behind(st, s) <= 1e-6 for s in players):
        return showdown(st)
    start_street(st, "flop")


# ---------- postflop common ----------

def start_street(st, street):
    n = {"flop": 3, "turn": 4, "river": 5}[street]
    st["street"] = street
    st["board"] = st["board_full"][:n]
    st["street_contrib"] = {s: 0.0 for s in st["contrib"]}
    st["history"].append(f"--- {street.upper()}: {' '.join(st['board'])} (pot {fmt(pot(st))}, {len(st['in_hand'])} players)")
    if street == "flop":
        st["mode"] = "hu" if len(st["in_hand"]) == 2 else "mw"
    elif st["mode"] == "mw" and len(st["in_hand"]) == 2:
        mw_to_hu(st)  # a multiway pot down to two players: hand it to the solver
    if st["mode"] == "hu":
        hu_start_street(st, street)
    else:
        mw_start_street(st, street)


def street_over(st):
    players = live(st)
    if len(players) == 1:
        return finish(st, players)
    if st["street"] == "river" or sum(behind(st, s) > 1e-6 for s in players) <= 1:
        return showdown(st)
    start_street(st, {"flop": "turn", "turn": "river"}[st["street"]])


def showdown(st):
    st["board"] = st["board_full"][:]
    st["history"].append(f"--- SHOWDOWN: board {' '.join(st['board'])}")
    players = live(st)
    ranks = {s: eval_hand(st["cards"][s] + st["board"]) for s in players}
    best = max(ranks.values())
    st["revealed"] = players
    finish(st, [s for s in players if ranks[s] == best])


def finish(st, winners):
    st["over"], st["to_act"] = True, None
    # return an uncalled bet to a lone winner
    if len(winners) == 1:
        w = winners[0]
        others = [st["contrib"][s] for s in st["contrib"] if s != w]
        st["contrib"][w] = min(st["contrib"][w], max(others))
    total = pot(st)
    share = total / len(winners)
    st["history"].append(f"{', '.join(who(st, w) for w in winners)} {'split' if len(winners) > 1 else 'wins'} {fmt(total)}")
    st["hero_net"] = (share if st["hero"] in winners else 0.0) - st["contrib"][st["hero"]]
    if st.get("spec"):
        return
    meta = C.load(C.META, {"hands": 0, "net": 0.0})
    ring = meta.setdefault(meta_key(len(st["seats"])), {"hands": 0, "net": 0.0})
    ring["net"] += st["hero_net"]
    C.Mx.check_due(meta)
    C.save(C.META, meta)


# ---------- heads-up postflop (solver) ----------

def hu_seats(st):
    a, b = live(st)  # postflop order: first acts first = OOP
    return {"OOP": a, "IP": b}


def hu_key(st, seat):
    return "BB" if seat == hu_seats(st)["OOP"] else "SB"


def mw_to_hu(st):
    """Seed solver ranges from the multiway ranges narrowed so far.

    Villains' ranges are exact under the rules-based policy they play by; the
    hero's range is narrowed as if the hero followed that same policy, which
    is an approximation.
    """
    hs = hu_seats(st)
    st["mode"] = "hu"
    st["hu_ranges"] = {"SB": dict(st["mw_ranges"][hs["IP"]]), "BB": dict(st["mw_ranges"][hs["OOP"]])}
    st["history"].append("(down to two players: the solver takes over from here)")


def hu_flop_args(st):
    hs = hu_seats(st)
    cls = {s: RR.class_range_for(*st["pf"]["range"][s]) for s in hs.values()}
    ranges = {"SB": cls[hs["IP"]], "BB": cls[hs["OOP"]]}
    floor = [hand_class(*st["cards"][s]) for s in hs.values()]
    stack = min(behind(st, s) for s in hs.values())
    return [st["board_full"][:3], pot(st), stack, ranges, floor]


def presolve_path(args):
    key = hashlib.sha1(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]
    return os.path.join(C.SOLVES, f"ringpre_{key}.json")


def spawn_presolve(args):
    out = presolve_path(args)
    if os.path.exists(out) or C.pid_alive(out + ".pid"):
        return
    os.makedirs(C.SOLVES, exist_ok=True)
    with open(out + ".args.json", "w") as f:
        json.dump(args, f)
    log = open(os.path.join(C.DATA_DIR, "presolve.log"), "a")
    subprocess.Popen(["nice", "-n", "10", sys.executable, os.path.abspath(__file__), "presolve", out],
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())


def run_presolve(out):
    with open(out + ".pid", "w") as f:
        f.write(str(os.getpid()))
    try:
        with open(out + ".args.json") as f:
            args = json.load(f)
        S.solve(*args, out)
    finally:
        os.remove(out + ".pid")


def speculate(st, first=None):
    """Play a preflop line on a copy (hero: `first`, then the chart's top action);
    presolve the heads-up flop it reaches."""
    sim = copy.deepcopy(st)
    sim["spec"] = True
    if first is not None:
        apply_preflop(sim, sim["hero"], first)
        if sim["pf_queue"] and sim["pf_queue"][0] == sim["hero"]:
            sim["pf_queue"].pop(0)
    advance_preflop(sim)
    for _ in range(10):
        if sim["over"] or sim["street"] != "preflop":
            break
        if sim["to_act"] != sim["hero"]:
            break
        strat = pf_strategy(sim, sim["hero"])
        a = max(strat, key=strat.get)
        apply_preflop(sim, sim["hero"], a)
        if sim["pf_queue"] and sim["pf_queue"][0] == sim["hero"]:
            sim["pf_queue"].pop(0)
        advance_preflop(sim)
    if sim.get("spec_flop"):
        spawn_presolve(sim["spec_flop"])


def hu_start_street(st, street):
    hs = hu_seats(st)
    if street == "flop" and st.get("spec"):
        st["spec_flop"] = hu_flop_args(st)  # simulation only: record, don't solve
        st["street"] = "spec-done"
        return
    if street == "flop":
        args = hu_flop_args(st)
        pre = presolve_path(args)
        waited = False
        while not os.path.exists(pre) and C.pid_alive(pre + ".pid"):
            if not waited:
                print("(waiting for the background flop solve...)", file=sys.stderr, flush=True)
                waited = True
            time.sleep(1)
        if os.path.exists(pre):
            st["hu_ranges"] = {"SB": RR.range_for(*st["pf"]["range"][hs["IP"]]),
                               "BB": RR.range_for(*st["pf"]["range"][hs["OOP"]])}
            st["hu_prev"] = st["hu_ranges"]
            st["hu_actions"] = []
            st["solve_file"] = pre
            root = hu_root(st)
            st["to_act"] = hs["IP"] if root["player"] == 0 else hs["OOP"]
            return
    if street == "flop":
        cls = {s: RR.class_range_for(*st["pf"]["range"][s]) for s in hs.values()}
        st["hu_ranges"] = {"SB": RR.range_for(*st["pf"]["range"][hs["IP"]]),
                           "BB": RR.range_for(*st["pf"]["range"][hs["OOP"]])}
        solve_ranges = {"SB": cls[hs["IP"]], "BB": cls[hs["OOP"]]}
        floor = [hand_class(*st["cards"][s]) for s in hs.values()]
    else:
        solve_ranges = st["hu_ranges"]
        floor = ["".join(st["cards"][s]) for s in hs.values()]
    st["hu_prev"] = st["hu_ranges"]
    st["hu_actions"] = []
    os.makedirs(C.SOLVES, exist_ok=True)
    out = os.path.join(C.SOLVES, f"ring{st['hand_no']}_{street}.json")
    print(f"(solving the {street}...)", file=sys.stderr, flush=True)
    stack = min(behind(st, s) for s in hs.values())
    root = S.solve(st["board"], pot(st), stack, solve_ranges, floor, out)
    st["solve_file"] = out
    st["to_act"] = hs["IP"] if root["player"] == 0 else hs["OOP"]


def hu_root(st):
    with open(st["solve_file"]) as f:
        return json.load(f)


def hu_options(st, node):
    seat = st["to_act"]
    p, stack = pot(st), behind(st, seat)
    other = [s for s in live(st) if s != seat][0]
    raises = [a for a in node["strategy"]["actions"] if a.startswith("RAISE") and S.amount_bb(a) < stack - 1e-6]
    opts = []
    for a in node["strategy"]["actions"]:
        if a in ("CHECK", "FOLD"):
            opts.append({"id": a.lower(), "key": a, "label": a.lower()})
        elif a == "CALL":
            opts.append({"id": "call", "key": a, "label": f"call {fmt(st['contrib'][other] - st['contrib'][seat])}"})
        else:
            amt = S.amount_bb(a)
            if amt >= stack - 1e-6:
                opts.append({"id": "allin", "key": a, "label": f"all-in ({fmt(stack)})"})
            elif a.startswith("BET"):
                pct = round(100 * amt / p)
                opts.append({"id": f"bet{pct}", "key": a, "label": f"bet {fmt(amt)} ({pct}% pot)"})
            else:
                to = st["street_contrib"][seat] + amt
                rid = "raise" if len(raises) == 1 else f"raise{fmt(to)}"
                opts.append({"id": rid, "key": a, "label": f"raise to {fmt(to)}"})
    return opts


def hu_apply(st, root, seat, a):
    node = S.node_at(root, st["hu_actions"])
    other = [s for s in live(st) if s != seat][0]
    if a == "CHECK":
        st["history"].append(f"{who(st, seat)} checks")
    elif a == "FOLD":
        st["history"].append(f"{who(st, seat)} folds")
        st["in_hand"].remove(seat)
    elif a == "CALL":
        put(st, seat, st["contrib"][other] - st["contrib"][seat])
        st["history"].append(f"{who(st, seat)} calls")
    else:
        put(st, seat, S.amount_bb(a))
        verb = "bets" if a.startswith("BET") else "raises to"
        st["history"].append(f"{who(st, seat)} {verb}{' all-in' if behind(st, seat) <= 1e-6 else ''} {fmt(st['street_contrib'][seat])}")
    st["hu_actions"].append(a)
    if a == "FOLD":
        return finish(st, [other])
    child = node.get("childrens", {}).get(a)
    if child is not None and child.get("node_type") == "action_node":
        hs = hu_seats(st)
        st["to_act"] = hs["IP"] if child["player"] == 0 else hs["OOP"]
        return
    st["hu_ranges"] = S.narrow(root, st["hu_actions"], st["hu_ranges"])
    for k in ("SB", "BB"):
        if sum(st["hu_ranges"][k].values()) < 1.0:
            st["hu_ranges"][k] = st["hu_prev"][k]
    street_over(st)


def hu_strategy(st, seat, node):
    combo = "".join(st["cards"][seat])
    strat = S.combo_strategy(node, combo)
    return strat if strat is not None else S.range_strategy(node, st["hu_ranges"][hu_key(st, seat)])


# ---------- multiway postflop (rules-based villain) ----------

def tier(hole, board):
    """'monster' | 'strong' | 'medium' | 'draw' | 'weak' for a hand on this board."""
    rank = eval_hand(list(hole) + list(board))
    board_rank = eval_hand(list(board)) if len(board) >= 5 else None
    desc = describe(hole, board)
    hv = sorted((RANK_VAL[c[0]] for c in hole), reverse=True)
    bv = sorted((RANK_VAL[c[0]] for c in board), reverse=True)
    uses_hole = board_rank is None or rank > board_rank
    if rank[0] >= 3 and uses_hole and (rank[0] >= 4 or any(v in bv for v in hv) or hv[0] == hv[1]):
        return "monster"
    if rank[0] == 2 and all(v in bv for v in hv):
        return "monster"
    if "overpair" in desc or ("top pair" in desc and (hv[1] if hv[0] == bv[0] else hv[0]) >= 10):
        return "strong"
    if rank[0] in (1, 2) and any(v in bv for v in hv) or (hv[0] == hv[1] and rank[0] >= 1):
        return "medium"
    if len(board) < 5 and ("flush draw" in desc or "open-ended" in desc):
        return "draw"
    return "weak"


def mw_policy(t, to_call, pot_now, river):
    """Villain's {action-kind: prob}; kinds are check/bet33/bet75/fold/call/raise."""
    if to_call <= 0:
        table = {
            "monster": {"bet75": 0.6, "bet33": 0.2, "check": 0.2},
            "strong": {"bet75": 0.4, "bet33": 0.4, "check": 0.2},
            "medium": {"bet33": 0.25, "check": 0.75},
            "draw": {"bet75": 0.3, "bet33": 0.2, "check": 0.5},
            "weak": {"bet33": 0.12, "check": 0.88},
        }
        return table["weak" if (t == "draw" and river) else t]
    price = to_call / (pot_now + to_call)
    if t == "monster":
        return {"raise": 0.45, "call": 0.55}
    if t == "strong":
        return {"call": 0.85, "raise": 0.1, "fold": 0.05} if price <= 0.4 else {"call": 0.7, "fold": 0.3}
    if t == "medium":
        return {"call": 0.6, "fold": 0.4} if price <= 0.3 else {"call": 0.25, "fold": 0.75}
    if t == "draw" and not river:
        return {"call": 0.8, "raise": 0.1, "fold": 0.1} if price <= 0.35 else {"call": 0.4, "fold": 0.6}
    return {"fold": 0.9, "call": 0.05, "raise": 0.05}


def mw_start_street(st, street):
    if street == "flop":
        st["mw_ranges"] = {s: RR.range_for(*st["pf"]["range"][s]) for s in st["in_hand"]}
    st["mw"] = {"acted": [], "raises": 0}
    mw_next(st, None)


def mw_next(st, after):
    """Set to_act to the next player who must act, or end the street."""
    order = [s for s in live(st) if behind(st, s) > 1e-6]
    top = max(st["street_contrib"][s] for s in live(st))
    pending = [s for s in order if s not in st["mw"]["acted"] or st["street_contrib"][s] < top - 1e-9]
    if not pending or (len(order) <= 1 and all(st["street_contrib"][s] >= top - 1e-9 for s in order)):
        st["to_act"] = None
        return street_over(st)
    if after is None:
        st["to_act"] = pending[0]
        return
    ring = live(st)
    i = ring.index(after) if after in ring else -1
    for k in range(1, len(ring) + 1):
        s = ring[(i + k) % len(ring)]
        if s in pending:
            st["to_act"] = s
            return


def mw_options(st, seat):
    p = pot(st)
    top = max(st["street_contrib"][s] for s in live(st))
    to_call = top - st["street_contrib"][seat]
    stack = behind(st, seat)
    if to_call <= 1e-9:
        opts = [{"id": "check", "key": "check", "label": "check"}]
        for pct in MULTI_BET:
            amt = round(p * pct / 100, 1)
            if amt < stack:
                opts.append({"id": f"bet{pct}", "key": f"bet{pct}", "label": f"bet {fmt(amt)} ({pct}% pot)"})
        opts.append({"id": "allin", "key": "allin", "label": f"all-in ({fmt(stack)})"})
        return opts, 0.0
    opts = [{"id": "fold", "key": "fold", "label": "fold"},
            {"id": "call", "key": "call", "label": f"call {fmt(min(to_call, stack))}"}]
    raise_to = top * 3
    if st["mw"]["raises"] < RAISE_LIMIT and raise_to - st["street_contrib"][seat] < stack:
        opts.append({"id": "raise", "key": "raise", "label": f"raise to {fmt(raise_to)}"})
    if stack > to_call:
        opts.append({"id": "allin", "key": "allin", "label": f"all-in ({fmt(stack)})"})
    return opts, to_call


def mw_apply(st, seat, kind):
    p = pot(st)
    top = max(st["street_contrib"][s] for s in live(st))
    if kind == "check":
        st["history"].append(f"{who(st, seat)} checks")
    elif kind == "fold":
        st["in_hand"].remove(seat)
        st["history"].append(f"{who(st, seat)} folds")
    elif kind == "call":
        put(st, seat, top - st["street_contrib"][seat])
        st["history"].append(f"{who(st, seat)} calls")
    elif kind.startswith("bet"):
        put(st, seat, round(p * int(kind[3:]) / 100, 1))
        st["history"].append(f"{who(st, seat)} bets {fmt(st['street_contrib'][seat])}")
        st["mw"]["acted"] = []
    elif kind == "raise":
        put(st, seat, top * 3 - st["street_contrib"][seat])
        st["mw"]["raises"] += 1
        st["history"].append(f"{who(st, seat)} raises to {fmt(st['street_contrib'][seat])}")
        st["mw"]["acted"] = []
    elif kind == "allin":
        put(st, seat, behind(st, seat))
        st["history"].append(f"{who(st, seat)} moves all-in ({fmt(st['street_contrib'][seat])})")
        if st["street_contrib"][seat] > top:
            st["mw"]["acted"] = []
    if seat in st["in_hand"]:
        st["mw"]["acted"].append(seat)
    mw_next(st, seat)


def mw_villain_kind(st, seat):
    opts, to_call = mw_options(st, seat)
    river = st["street"] == "river"
    pol = mw_policy(tier(st["cards"][seat], st["board"]), to_call, pot(st), river)
    valid = {o["key"] for o in opts}
    pol = {k: v for k, v in pol.items() if k in valid}
    if not pol:
        pol = {"call" if "call" in valid else "check": 1.0}
    kind = random.choices(list(pol), weights=list(pol.values()))[0]
    return kind


def mw_narrow(st, seat, kind):
    """Update a villain's range by how the rules-based policy would act."""
    _, to_call = mw_options(st, seat)
    river = st["street"] == "river"
    new = {}
    dead = set(st["board"])
    for combo, w in st["mw_ranges"][seat].items():
        a, b = combo[:2], combo[2:]
        if a in dead or b in dead:
            continue
        pol = mw_policy(tier([a, b], st["board"]), to_call, pot(st), river)
        p = pol.get(kind, 0.0) if kind != "allin" else pol.get("raise", 0.0) + pol.get("bet75", 0.0)
        if p > 0:
            new[combo] = w * p
    if sum(new.values()) >= 1.0:
        st["mw_ranges"][seat] = new


def multi_equity(hero, villain_ranges, board, samples=2500, rng=None):
    rng = rng or random.Random()
    lists = []
    for weights in villain_ranges:
        combos = [(c[:2], c[2:]) for c, w in weights.items() if w > 0]
        ws = [w for w in weights.values() if w > 0]
        if not combos:
            return None
        lists.append((combos, ws))
    dead0 = set(hero) | set(board)
    score = n = 0
    for _ in range(samples):
        dead = set(dead0)
        hands = []
        ok = True
        for combos, ws in lists:
            for _try in range(20):
                a, b = rng.choices(combos, weights=ws)[0]
                if a not in dead and b not in dead:
                    break
            else:
                ok = False
                break
            dead |= {a, b}
            hands.append([a, b])
        if not ok:
            continue
        deck = [c for c in DECK if c not in dead]
        run = list(board) + rng.sample(deck, 5 - len(board))
        hr = eval_hand(list(hero) + run)
        vr = [eval_hand(h + run) for h in hands]
        best = max(vr)
        if hr > best:
            score += 1
        elif hr == best:
            score += 1 / (1 + sum(v == hr for v in vr))
        n += 1
    return score / n if n else None


# ---------- driving the hand ----------

def current(st):
    """(seat, options, context) for the player to act."""
    seat = st["to_act"]
    if st["street"] == "preflop":
        node, opts = pf_options(st, seat)
        return seat, opts, ("pf", node)
    if st["mode"] == "hu":
        root = hu_root(st)
        node = S.node_at(root, st["hu_actions"])
        return seat, hu_options(st, node), ("hu", root, node)
    opts, to_call = mw_options(st, seat)
    return seat, opts, ("mw", to_call)


def apply(st, seat, opt, ctx, advance=True):
    if ctx[0] == "pf":
        apply_preflop(st, seat, opt["key"])
        if st["pf_queue"] and st["pf_queue"][0] == seat:
            st["pf_queue"].pop(0)
        return advance_preflop(st) if advance else None
    if ctx[0] == "hu":
        return hu_apply(st, ctx[1], seat, opt["key"])
    mw_narrow(st, seat, opt["key"])
    return mw_apply(st, seat, opt["key"])


def hero_out(st):
    """End the hand once the hero has folded; the rest isn't worth simulating."""
    if not st["over"] and st["hero"] not in st["in_hand"]:
        st["over"], st["to_act"] = True, None
        st["hero_net"] = -st["contrib"][st["hero"]] + 0.0  # avoid -0.0
        st["history"].append("(you folded; rest of hand not played)")
        if st.get("spec"):
            return True
        meta = C.load(C.META, {"hands": 0, "net": 0.0})
        meta.setdefault(meta_key(len(st["seats"])), {"hands": 0, "net": 0.0})["net"] += st["hero_net"]
        C.Mx.check_due(meta)
        C.save(C.META, meta)
        return True
    return False


def villain_turns(st):
    if hero_out(st):
        return
    while not st["over"] and st["to_act"] and st["to_act"] != st["hero"]:
        seat, opts, ctx = current(st)
        if ctx[0] == "pf":
            return advance_preflop(st)  # preflop villains act inside advance_preflop
        if ctx[0] == "hu":
            strat = hu_strategy(st, seat, ctx[2])
            keys = [o["key"] for o in opts]
            w = [max(strat.get(k, 0.0), 0.0) for k in keys]
            key = random.choices(keys, weights=w if sum(w) > 0 else None)[0]
            hu_apply(st, ctx[1], seat, key)
        else:
            kind = mw_villain_kind(st, seat)
            mw_narrow(st, seat, kind)
            mw_apply(st, seat, kind)


def grade(st, seat, opt, opts, ctx):
    hero = st["hero"]
    n = len(st["seats"])
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M"), "mode": f"{n}max", "hand": f"{n}max-{st['hand_no']}",
           "street": st["street"], "seat": hero, "hole": st["cards"][hero], "board": st["board"],
           "history": list(st["history"]), "options": [o["id"] for o in opts], "chosen": opt["id"], "note": None}
    notes = ["COACH NOTES (for the coach; don't reveal before the student explains):"]
    others = [s for s in live(st) if s != hero]
    to_call = max(st["contrib"][s] for s in live(st)) - st["contrib"][hero]
    if ctx[0] in ("pf", "hu"):
        strat = pf_strategy(st, hero) if ctx[0] == "pf" else hu_strategy(st, hero, ctx[2])
        p = strat.get(opt["key"], 0.0)
        best = max(strat, key=strat.get)
        verdict = "GOOD" if p >= C.GOOD else "OK (low-frequency mix)" if p >= C.OK else "MISTAKE"
        if p >= C.GOOD and opt["key"] != best and strat[best] - p > 0.4:
            verdict = "OK (solver prefers another line)"
        label = {o["key"]: o["id"] for o in opts}
        shown = C.fmt_strategy(strat, opts)
        src = "chart" if ctx[0] == "pf" else "solver"
        head = [f"GRADE: {verdict}", f"You chose: {opt['label']} — {src} plays it {p * 100:.0f}% with this hand",
                f"{src.capitalize()} with your hand: {shown}"]
        rec.update(strategy={label.get(k, k): round(v, 3) for k, v in strat.items()}, chosen_freq=round(p, 3), verdict=verdict)
        if ctx[0] == "hu":
            vr = S.narrow(ctx[1], st["hu_actions"], st["hu_ranges"])[hu_key(st, others[0])]
            bd, n = C.range_breakdown(vr, st["board"])
            notes.append(f"  Villain range (~{n:.0f} combos): {bd}")
            eq = multi_equity(st["cards"][hero], [vr], st["board"], samples=2500)
        else:  # preflop: equity vs the players who have already shown a range
            vranges = [RR.range_for(*st["pf"]["range"][s]) for s in others if s in st["pf"]["range"]]
            eq = multi_equity(st["cards"][hero], vranges, [], samples=2500) if vranges else None
        if eq is not None:
            notes.append(f"  Your equity vs {'villain' if len(others) == 1 else f'{len(others)} villains'}: {eq * 100:.0f}%")
        if ctx[0] == "hu":
            evs = S.combo_evs(ctx[2], "".join(st["cards"][hero]))
            loss = S.ev_loss(evs, opt["key"])
            if evs:
                notes.append("  Solver EVs (bb): " + " | ".join(f"{label.get(k, k)} {v:+.1f}" for k, v in evs.items()))
        else:
            loss = C.price_ev_loss(strat, opt["key"], eq, pot(st), to_call, realize=C.realization(st, hero))
    else:  # multiway: approximate
        vranges = [st["mw_ranges"][s] for s in others]
        eq = multi_equity(st["cards"][hero], vranges, st["board"], samples=2500)
        need = ctx[1] / (pot(st) + ctx[1]) if ctx[1] > 0 else None
        kind = opt["key"]
        if need is not None and kind in ("call", "fold") and eq is not None:
            margin = 0.05
            if kind == "call":
                verdict = "GOOD" if eq >= need else "OK (close)" if eq >= need - margin else "MISTAKE"
            else:
                verdict = "GOOD" if eq < need else "OK (close)" if eq < need + margin else "MISTAKE"
            call_ev = eq * (pot(st) + ctx[1]) - ctx[1]
            loss = round(max(0.0, call_ev if kind == "fold" else -call_ev), 2)
            head = [f"GRADE: {verdict} (approximate: multiway, equity vs price, no solver)",
                    f"You chose: {opt['label']} — your equity {eq * 100:.0f}% vs {need * 100:.0f}% needed"]
        else:
            verdict = "APPROX"
            loss = None
            head = ["GRADE: APPROX — multiway bet/check/raise; no solver. Coach judges with the notes below.",
                    f"You chose: {opt['label']}"]
        rec.update(strategy={}, chosen_freq=None, verdict=verdict)
        for s in others:
            bd, n = C.range_breakdown(st["mw_ranges"][s], st["board"])
            notes.append(f"  {s} range (~{n:.0f} combos): {bd}")
        if eq is not None:
            notes.append(f"  Your equity vs all {len(others)} villains: {eq * 100:.0f}%")
        if need is not None:
            notes.append(f"  Pot odds: need {need * 100:.0f}%")
        notes.append(f"  Your hand: {describe(st['cards'][hero], st['board'])} (tier: {tier(st['cards'][hero], st['board'])})")
    if to_call > 0 and ctx[0] != "mw":
        notes.append(f"  Pot odds: call {fmt(to_call)} into {fmt(pot(st))} -> need {100 * to_call / (pot(st) + to_call):.0f}%")
    notes.append(f"  Pot {fmt(pot(st))}, players in hand: {', '.join(live(st))}")
    rec["ev_loss_bb"] = loss
    new_verdict = C.close_in_ev(verdict, loss, pot(st))
    if new_verdict != verdict:
        head[0] = f"GRADE: {new_verdict}"
        verdict = rec["verdict"] = new_verdict
    head[0] += C.ev_tag(loss)
    rec["coach_notes"] = notes[1:]
    with open(C.NOTES, "w") as f:
        f.write("\n".join(notes) + "\n")
    return verdict, "\n".join(head), rec


def render(st):
    hero = st["hero"]
    n = len(st["seats"])
    out = [f"{n}-max hand #{st['hand_no']} — you are {hero}, 100bb deep, blinds 0.5/1"]
    out.append(f"Your cards: {' '.join(st['cards'][hero])}")
    if st["board"]:
        out.append(f"Board: {' '.join(st['board'])}")
        out.append(f"Your hand: {describe(st['cards'][hero], st['board'])}")
    out.append("Action:")
    out += [f"  {h}" for h in st["history"]]
    if st["over"]:
        for s in st.get("revealed") or []:
            if s != hero:
                out.append(f"{s} showed: {' '.join(st['cards'][s])} ({describe(st['cards'][s], st['board'])})")
        out.append(f"HAND OVER — your result: {'+' if st['hero_net'] >= 0 else ''}{fmt(st['hero_net'])}bb")
        ring = C.load(C.META, {}).get(meta_key(n), {"hands": 0, "net": 0})
        out.append(f"{n}-max session: {ring['hands']} hands, {'+' if ring['net'] >= 0 else ''}{fmt(ring['net'])}bb.")
        return "\n".join(out)
    if st.get("pending_review"):
        out.append("PAUSED for review. Run `next` to continue.")
        return "\n".join(out)
    players = live(st) if st["street"] != "preflop" else [s for s in st["seats"] if s in st["in_hand"]]
    out.append(f"Pot: {fmt(pot(st))} | in hand: {', '.join(players)} | you have {fmt(behind(st, hero))} behind")
    _, opts, ctx = current(st)
    if ctx[0] == "mw":
        out.append("(multiway pot: no solver; grades are approximate)")
    out.append("YOUR OPTIONS: " + " | ".join(f"[{o['id']}] {o['label']}" for o in opts))
    out.append(T.line(f"{n}-max hand #{st['hand_no']} · {st['street']}", fmt(pot(st)), st["seats"], hero, st["history"],
                      st["board"], st["cards"][hero], fmt(behind(st, hero)),
                      describe(st["cards"][hero], st["board"]) if st["board"] else ""))
    return "\n".join(out)


# ---------- commands ----------

def cmd_new(args):
    st = C.load(STATE, None)
    if st and not st["over"] and "--force" not in args:
        print("A hand is in progress. Use `new --force` to abandon it.\n")
        print(render(st))
        return
    meta = C.load(C.META, {"hands": 0, "net": 0.0})
    seats = seats_for(table_size())
    ring = meta.setdefault(meta_key(len(seats)), {"hands": 0, "net": 0.0})
    ring["hands"] += 1
    C.save(C.META, meta)
    n = ring["hands"]
    deck = DECK[:]
    random.shuffle(deck)
    cards = {s: deck[2 * i:2 * i + 2] for i, s in enumerate(seats)}
    k = 2 * len(seats)
    st = {
        "hand_no": n, "seats": seats, "hero": seats[(n - 1) % len(seats)], "cards": cards,
        "board_full": deck[k:k + 5], "board": [],
        "street": "preflop", "contrib": {s: 0.0 for s in seats}, "street_contrib": {s: 0.0 for s in seats},
        "in_hand": list(seats), "pf": {"opener": None, "threebettor": None, "jammer": None, "range": {}},
        "pf_queue": list(seats), "history": ["SB posts 0.5, BB posts 1"], "over": False, "to_act": None,
        "pending_review": False, "mode": None, "seed": random.getrandbits(32),
    }
    put(st, "SB", 0.5)
    put(st, "BB", 1.0)
    advance_preflop(st)
    villain_turns(st)
    C.save(STATE, st)
    if not st["over"] and st["street"] == "preflop" and st["to_act"] == st["hero"]:
        _, opts = pf_options(st, st["hero"])
        for o in opts:  # presolve every line the hero might continue with
            if o["key"] != "fold":
                speculate(st, o["key"])
    print(render(st))


def cmd_act(args):
    st = C.load(STATE, None)
    if not st or st["over"]:
        print("No hand in progress. Run `new`.")
        return
    if st.get("pending_review"):
        print("Still paused on the last decision. Run `next` first.")
        return
    want = " ".join(args).strip().lower()
    seat, opts, ctx = current(st)
    if seat != st["hero"]:
        print("It's not your turn.")
        return
    opt = next((o for o in opts if o["id"] == want), None)
    if opt is None:
        print(f"Unknown option '{want}'. Options: " + ", ".join(o["id"] for o in opts))
        return
    verdict, report, rec = grade(st, seat, opt, opts, ctx)
    pause = verdict in ("MISTAKE", "APPROX")
    # On a paused preflop review, don't run the rest of preflop (and the flop
    # solve) now: start that solve in the background and finish on `next`.
    apply(st, seat, opt, ctx, advance=not (pause and ctx[0] == "pf"))
    C.append_jsonl(C.LOG, rec)
    print(report + "\n", flush=True)
    if pause and not st["over"]:
        st["pending_review"] = True
        C.save(STATE, st)
        if ctx[0] == "pf":
            speculate(st)
        print("PAUSED for review. Run `next` when the discussion is done.")
        return
    villain_turns(st)
    C.save(STATE, st)
    print(render(st))


def cmd_next(args):
    st = C.load(STATE, None)
    if not st:
        print("No hand. Run `new`.")
        return
    st["pending_review"] = False
    if st["street"] == "preflop":
        advance_preflop(st)
    villain_turns(st)
    C.save(STATE, st)
    print(render(st))


def cmd_status(args):
    st = C.load(STATE, None)
    print(render(st) if st else "No hand yet. Run `new`.")


COMMANDS = {"new": cmd_new, "act": cmd_act, "next": cmd_next, "status": cmd_status}


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "presolve":
    run_presolve(sys.argv[2])
