#!/usr/bin/env python3
"""Heads-up NLHE trainer: you play a solver-driven villain and every decision
you make is graded against the solver (postflop) or the preflop charts.

Commands:
  new [--force]       deal a new hand (abandons an unfinished one with --force)
  status              reprint the current spot
  act <option-id>     make your decision; prints the grade and coach notes,
                      then plays on unless the decision was a MISTAKE
  next                continue the hand after a paused MISTAKE review
  notes               coach notes (equity, ranges) for the last graded decision;
                      only run after the student has explained their play
  note <text>         attach a note (your reasoning + the coach's verdict)
                      to the last graded decision
  stats [--mixing]    session summary and recent mistakes; --mixing: solver-frequency
                      calibration report (due every 200 hands)
  drill <type|mix|adaptive|off> deal hands that reach a chosen spot type
  table <2|4|6>        heads-up (default), 4-max or 6-max; new/act/next/status follow it
  export [dir]        save progress to <dir>/poker-coach-save.tar.gz
  import <file>       restore progress from a save made by export
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import drills as Dr  # noqa: E402
import mixing as Mx  # noqa: E402
import ranges as R  # noqa: E402
import solver as S  # noqa: E402
from cards import DECK, RANK_VAL, describe, equity, eval_hand, hand_class  # noqa: E402

DATA_DIR = os.path.expanduser(os.environ.get("POKER_COACH_DIR", "~/workspace/poker-coach"))
STATE = os.path.join(DATA_DIR, "state.json")
META = os.path.join(DATA_DIR, "meta.json")
LOG = os.path.join(DATA_DIR, "decisions.jsonl")
HANDS = os.path.join(DATA_DIR, "hands.jsonl")
SOLVES = os.path.join(DATA_DIR, "solves")
NOTES = os.path.join(DATA_DIR, "last_notes.txt")  # coach notes for the last graded decision
DEALS = os.path.join(DATA_DIR, "deals.json")  # pre-dealt upcoming hands; never show these

STACK = 100.0
GOOD, OK = 0.25, 0.08
OTHER = {"SB": "BB", "BB": "SB"}


# ---------- persistence ----------

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def append_jsonl(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def load_root(st):
    with open(st["solve_file"]) as f:
        return json.load(f)


# ---------- helpers ----------

def fmt(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")


def pot(st):
    return st["contrib"]["SB"] + st["contrib"]["BB"]


def behind(st, seat):
    return STACK - st["contrib"][seat]


def street_pot_start(st):
    return st["street_start_pot"]


def say(st, seat):
    return f"{seat} ({'you' if seat == st['hero'] else 'villain'})"


def fmt_strategy(strat, options):
    label = {o["key"]: o["id"] for o in options}
    items = sorted(strat.items(), key=lambda kv: -kv[1])
    return " | ".join(f"{label.get(k, k)} {p * 100:.0f}%" for k, p in items if p >= 0.005) or "(none)"


# ---------- options ----------

def preflop_options(st):
    node = st["pf_node"]
    seat = R.NODES[node][0]
    labels = {
        "sb_open": {"fold": "fold", "raise": "raise to 2.5"},
        "bb_vs_open": {"fold": "fold", "call": "call 1.5", "raise": "3-bet to 10"},
        "sb_vs_3bet": {"fold": "fold", "call": "call 7.5", "allin": "all-in (100)"},
        "bb_vs_jam": {"fold": "fold", "call": "call 90 (all-in)"},
    }[node]
    return seat, [{"id": a, "key": a, "label": labels[a]} for a in R.NODES[node][1]]


def postflop_options(st, node):
    seat = S.PLAYER_TO_SEAT[node["player"]]
    p = pot(st)
    stack = behind(st, seat)
    raises = [a for a in node["strategy"]["actions"] if a.startswith("RAISE")]
    opts = []
    for a in node["strategy"]["actions"]:
        if a == "CHECK":
            opts.append({"id": "check", "key": a, "label": "check"})
        elif a == "FOLD":
            opts.append({"id": "fold", "key": a, "label": "fold"})
        elif a == "CALL":
            to_call = st["contrib"][OTHER[seat]] - st["contrib"][seat]
            opts.append({"id": "call", "key": a, "label": f"call {fmt(to_call)}"})
        else:
            kind = a.split()[0]
            amt = S.amount_bb(a)
            if amt >= stack - 1e-6:
                opts.append({"id": "allin", "key": a, "label": f"all-in ({fmt(stack)})"})
            elif kind == "BET":
                pct = round(100 * amt / p)
                opts.append({"id": f"bet{pct}", "key": a, "label": f"bet {fmt(amt)} ({pct}% pot)"})
            else:
                to = st["street_contrib"][seat] + amt
                rid = "raise" if len([r for r in raises if S.amount_bb(r) < stack - 1e-6]) == 1 else f"raise{fmt(to)}"
                opts.append({"id": rid, "key": a, "label": f"raise to {fmt(to)}"})
    return seat, opts


def current_options(st, root=None):
    if st["street"] == "preflop":
        seat, opts = preflop_options(st)
        return seat, opts, None
    root = root or load_root(st)
    node = S.node_at(root, st["actions"])
    seat, opts = postflop_options(st, node)
    return seat, opts, node


def strategy_for(st, seat, node):
    """{option key: prob} for the seat's actual hand."""
    combo = "".join(st["cards"][seat])
    if node is None:
        return R.preflop_strategy(st["pf_node"], hand_class(*st["cards"][seat]))
    strat = S.combo_strategy(node, combo)
    if strat is None:
        strat = S.range_strategy(node, st["ranges"][seat])
    return strat


# ---------- applying actions ----------

def put(st, seat, amount):
    amount = min(amount, behind(st, seat))
    st["contrib"][seat] += amount
    st["street_contrib"][seat] += amount


def apply_preflop(st, seat, a):
    node = st["pf_node"]
    who = say(st, seat)
    if a == "fold":
        st["history"].append(f"{who} folds")
        return finish(st, winner=OTHER[seat])
    if node == "sb_open":  # raise
        put(st, "SB", 2.5 - st["contrib"]["SB"])
        st["history"].append(f"{who} raises to 2.5")
        st["pf_node"] = "bb_vs_open"
    elif node == "bb_vs_open":
        if a == "call":
            put(st, "BB", 2.5 - st["contrib"]["BB"])
            st["history"].append(f"{who} calls")
            st["line"] = "srp"
            return start_street(st, "flop")
        put(st, "BB", 10 - st["contrib"]["BB"])
        st["history"].append(f"{who} 3-bets to 10")
        st["pf_node"] = "sb_vs_3bet"
    elif node == "sb_vs_3bet":
        if a == "call":
            put(st, "SB", 10 - st["contrib"]["SB"])
            st["history"].append(f"{who} calls")
            st["line"] = "3bp"
            return start_street(st, "flop")
        put(st, "SB", STACK)
        st["history"].append(f"{who} moves all-in")
        st["pf_node"] = "bb_vs_jam"
    elif node == "bb_vs_jam":  # call
        put(st, "BB", STACK)
        st["history"].append(f"{who} calls all-in")
        return showdown(st)
    st["to_act"] = R.NODES[st["pf_node"]][0]


def apply_postflop(st, root, seat, a):
    node = S.node_at(root, st["actions"])
    who = say(st, seat)
    if a == "CHECK":
        st["history"].append(f"{who} checks")
    elif a == "FOLD":
        st["history"].append(f"{who} folds")
    elif a == "CALL":
        put(st, seat, st["contrib"][OTHER[seat]] - st["contrib"][seat])
        st["history"].append(f"{who} calls")
    else:
        kind = a.split()[0]
        put(st, seat, S.amount_bb(a))
        allin = behind(st, seat) <= 1e-6
        if kind == "BET":
            st["history"].append(f"{who} {'bets all-in' if allin else 'bets'} {fmt(st['street_contrib'][seat])}")
        else:
            st["history"].append(f"{who} {'raises all-in to' if allin else 'raises to'} {fmt(st['street_contrib'][seat])}")
    st["actions"].append(a)
    if a == "FOLD":
        return finish(st, winner=OTHER[seat])
    child = node.get("childrens", {}).get(a)
    if child is not None and child.get("node_type") == "action_node":
        st["to_act"] = S.PLAYER_TO_SEAT[child["player"]]
        return
    # street over
    st["ranges"] = S.narrow(root, st["actions"], st["ranges"])
    for s in ("SB", "BB"):
        if sum(st["ranges"][s].values()) < 1.0:  # off-tree line: keep the prior range
            st["ranges"][s] = st["prev_ranges"][s]
    if st["street"] == "river" or behind(st, "SB") <= 1e-6 or behind(st, "BB") <= 1e-6:
        return showdown(st)
    start_street(st, {"flop": "turn", "turn": "river"}[st["street"]])


LINE_POT = {"srp": 5.0, "3bp": 20.0}


def flop_solve_args(cards, board, line):
    """(board, pot, stack, ranges, floor) for a flop solve; same for presolves."""
    floor = [hand_class(*cards["SB"]), hand_class(*cards["BB"])]
    p = LINE_POT[line]
    return board[:3], p, STACK - p / 2, R.postflop_class_ranges(line), floor


def deal_key(cards, board_full):
    raw = "".join(cards["SB"] + cards["BB"] + board_full)
    return hashlib.sha1(raw.encode()).hexdigest()[:8]


def presolve_path(hand_no, key, line):
    return os.path.join(SOLVES, f"hand{hand_no}_{key}_flop_{line}.json")


def pid_alive(path):
    try:
        with open(path) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def wait_for_presolve(st):
    """Path of a finished background flop solve, waiting if one is running."""
    out = presolve_path(st["hand_no"], deal_key(st["cards"], st["board_full"]), st["line"])
    waited = False
    while not os.path.exists(out) and pid_alive(out + ".pid"):
        if not waited:
            print("(waiting for the background flop solve...)", file=sys.stderr, flush=True)
            waited = True
        time.sleep(1)
    return out if os.path.exists(out) else None


def spawn_presolves(targets):
    """Solve SRP flops in the background, one after another, at low priority."""
    os.makedirs(SOLVES, exist_ok=True)
    script = os.path.abspath(__file__)
    cmds = " ; ".join(f"nice -n 10 {sys.executable} {script} presolve {n}" for n in targets)
    log = open(os.path.join(DATA_DIR, "presolve.log"), "a")
    subprocess.Popen(["/bin/sh", "-c", cmds], stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True, env=os.environ.copy())


def cmd_presolve(args):
    n = int(args[0])
    deal = load(DEALS, {}).get(str(n))
    if not deal:
        return
    out = presolve_path(n, deal_key(deal["cards"], deal["board_full"]), "srp")
    while pid_alive(out + ".pid"):  # another chain is on it; wait so solves don't overlap
        time.sleep(1)
    if not deal or os.path.exists(out):
        return
    with open(out + ".pid", "w") as f:
        f.write(str(os.getpid()))
    try:
        S.solve(*flop_solve_args(deal["cards"], deal["board_full"], "srp"), out)
    finally:
        os.remove(out + ".pid")


def start_street(st, street):
    n = {"flop": 3, "turn": 4, "river": 5}[street]
    st["street"] = street
    st["board"] = st["board_full"][:n]
    st["actions"] = []
    st["street_contrib"] = {"SB": 0.0, "BB": 0.0}
    st["street_start_pot"] = pot(st)
    st["history"].append(f"--- {street.upper()}: {' '.join(st['board'])} (pot {fmt(pot(st))})")
    if street == "flop":
        st["ranges"] = R.postflop_ranges(st["line"])
    st["prev_ranges"] = st["ranges"]
    os.makedirs(SOLVES, exist_ok=True)
    t0 = time.time()
    ready = wait_for_presolve(st) if street == "flop" else None
    if ready:
        out = ready
        root = load_root({"solve_file": out})
    else:
        out = os.path.join(SOLVES, f"hand{st['hand_no']}_{street}.json")
        print(f"(solving the {street}...)", file=sys.stderr, flush=True)
        if street == "flop":
            args = flop_solve_args(st["cards"], st["board_full"], st["line"])
        else:
            floor = ["".join(st["cards"]["SB"]), "".join(st["cards"]["BB"])]
            args = (st["board"], pot(st), behind(st, "SB"), st["ranges"], floor)
        root = S.solve(*args, out)
    st["solve_file"] = out
    st["solve_secs"] = round(time.time() - t0, 1)
    st["to_act"] = S.PLAYER_TO_SEAT[root["player"]]


def showdown(st):
    st["board"] = st["board_full"][:]
    st["history"].append(f"--- SHOWDOWN: board {' '.join(st['board'])}")
    hs = eval_hand(st["cards"]["SB"] + st["board"])
    hb = eval_hand(st["cards"]["BB"] + st["board"])
    st["revealed"] = True
    winner = "SB" if hs > hb else "BB" if hb > hs else None
    return finish(st, winner)


def finish(st, winner):
    st["over"] = True
    st["to_act"] = None
    hero = st["hero"]
    if winner is not None:  # return any uncalled bet
        loser = OTHER[winner]
        st["contrib"][winner] = min(st["contrib"][winner], st["contrib"][loser])
    total = pot(st)
    if winner is None:
        won = total / 2
        st["history"].append(f"Split pot ({fmt(total)})")
    else:
        won = total if winner == hero else 0.0
        st["history"].append(f"{say(st, winner)} wins {fmt(total)}")
    st["hero_net"] = won - st["contrib"][hero]
    meta = load(META, {"hands": 0, "net": 0.0})
    meta["net"] = meta.get("net", 0.0) + st["hero_net"]
    Mx.check_due(meta)
    save(META, meta)
    append_jsonl(HANDS, {
        "hand": st["hand_no"], "hero": hero, "hole": st["cards"][hero],
        "villain_hole": st["cards"][OTHER[hero]] if st.get("revealed") else None,
        "board": st["board"], "history": st["history"], "hero_net": st["hero_net"],
    })


def villain_turns(st):
    """Let the villain act until it is the hero's turn or the hand ends."""
    while not st["over"] and st["to_act"] != st["hero"]:
        seat = st["to_act"]
        root = None if st["street"] == "preflop" else load_root(st)
        _, opts, node = current_options(st, root)
        strat = strategy_for(st, seat, node)
        keys = [o["key"] for o in opts]
        weights = [max(strat.get(k, 0.0), 0.0) for k in keys]
        if sum(weights) <= 0:
            weights = [1.0] * len(keys)
        choice = random.choices(keys, weights=weights)[0]
        if root is None:
            apply_preflop(st, seat, choice)
        else:
            apply_postflop(st, root, seat, choice)


# ---------- grading ----------

def villain_range_now(st, node_root=None):
    villain = OTHER[st["hero"]]
    if st["street"] == "preflop":
        node = st["pf_node"]
        hist = {
            "sb_open": None,
            "bb_vs_open": ("sb_open", "raise"),
            "sb_vs_3bet": ("bb_vs_open", "raise"),
            "bb_vs_jam": ("sb_vs_3bet", "allin"),
        }[node]
        if hist is None:
            return {c: 1.0 for c in R._to_combos({cls: 1.0 for cls in R.all_classes()})}
        return R._to_combos(R.class_weights_for(*hist))
    root = node_root or load_root(st)
    return S.narrow(root, st["actions"], st["ranges"])[villain]


def range_breakdown(weights, board, top=6):
    buckets = {}
    total = 0.0
    for combo, w in weights.items():
        if w <= 0.005:
            continue
        a, b = combo[:2], combo[2:]
        if a in board or b in board:
            continue
        key = describe([a, b], board)
        buckets[key] = buckets.get(key, 0.0) + w
        total += w
    items = sorted(buckets.items(), key=lambda kv: -kv[1])[:top]
    return ", ".join(f"{k} {100 * v / total:.0f}%" for k, v in items), total


def realization(st, seat):
    """Equity share a preflop flat realizes postflop: 1.0 when the call is
    all-in, else the out-of-position factors the BB defend chart assumes
    (0.88 for hands within two ranks, 0.80 otherwise; see ranges.py)."""
    to_call = max(st["contrib"].values()) - st["contrib"][seat]
    if st["street"] != "preflop" or to_call >= behind(st, seat) - 1e-6:
        return 1.0
    a, b = (RANK_VAL[c[0]] for c in st["cards"][seat])
    return 0.88 if abs(a - b) <= 2 else 0.80


def price_ev_loss(strat, chosen, eq, pot_now, to_call, extra=None, realize=1.0):
    """bb lost vs the chart's top action, from equity vs the price.

    fold = 0, call = eq * realize * (pot + call) - call, plus any other
    action EVs passed in `extra`. None when the chosen or the top action has
    no estimate (opens, 3-bets, ...).
    """
    evs = {"fold": 0.0}
    if to_call > 0 and eq is not None:
        evs["call"] = eq * realize * (pot_now + to_call) - to_call
    evs.update(extra or {})
    best = max(strat, key=strat.get)
    if best not in evs or chosen not in evs:
        return None
    return round(max(0.0, evs[best] - evs[chosen]), 2)


def jam_ev(st, hero, vrange):
    """EV (bb, from now) of jamming as SB vs the 3-bet, using BB's call-jam chart."""
    dead = set(st["cards"][hero])
    total = called = 0.0
    call_range = {}
    for combo, w in vrange.items():
        if combo[:2] in dead or combo[2:] in dead or w <= 0:
            continue
        c = R.preflop_strategy("bb_vs_jam", hand_class(combo[:2], combo[2:]))["call"]
        total += w
        called += w * c
        if c > 0:
            call_range[combo] = w * c
    if total == 0:
        return None
    eq = equity(st["cards"][hero], call_range, [], samples=3000) if call_range else 0.0
    risk = behind(st, hero)
    final_pot = pot(st) + risk + min(risk, behind(st, OTHER[hero]))
    f = 1 - called / total
    return f * pot(st) + (1 - f) * (eq * final_pot - risk)


def close_in_ev(verdict, loss, pot_now):
    """Downgrade a frequency MISTAKE that costs < max(0.25bb, 1% of pot)."""
    if verdict.startswith("MISTAKE") and loss is not None and loss < max(0.25, 0.01 * pot_now):
        return "OK (close in EV)"
    return verdict


def ev_tag(loss):
    return "" if loss is None else f" — costs ~{loss:.1f}bb"


def grade(st, opt, opts, node):
    hero = st["hero"]
    strat = strategy_for(st, hero, node)
    chosen_p = strat.get(opt["key"], 0.0)
    best_key = max(strat, key=strat.get)
    verdict = "GOOD" if chosen_p >= GOOD else "OK (low-frequency mix)" if chosen_p >= OK else "MISTAKE"
    if chosen_p >= GOOD and opt["key"] != best_key and strat[best_key] - chosen_p > 0.4:
        verdict = "OK (solver prefers another line)"
    to_call = max(0.0, st["contrib"][OTHER[hero]] - st["contrib"][hero])
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M"), "hand": st["hand_no"], "street": st["street"],
        "seat": hero, "hole": st["cards"][hero], "board": st["board"],
        "history": list(st["history"]), "options": [o["id"] for o in opts],
        "strategy": {next(o["id"] for o in opts if o["key"] == k): round(p, 3) for k, p in strat.items()},
        "chosen": opt["id"], "chosen_freq": round(chosen_p, 3), "verdict": verdict, "note": None, "drill": st.get("drill"),
    }
    lines = [
        f"GRADE: {verdict}",
        f"You chose: {opt['label']} — solver/chart plays it {chosen_p * 100:.0f}% with this hand",
        f"Solver/chart with your hand: {fmt_strategy(strat, opts)}",
        "",
        "COACH NOTES (for the coach; don't reveal before the student explains):",
    ]
    root = None if node is None else load_root(st)
    vrange = villain_range_now(st, root)
    if st["board"]:
        lines.append(f"  Your hand: {describe(st['cards'][hero], st['board'])}")
        hero_range = S.narrow(root, st["actions"], st["ranges"])[hero]
        lines.append(f"  Your whole range here: {fmt_strategy(S.range_strategy(node, hero_range), opts)}")
        bd, n = range_breakdown(vrange, st["board"])
        lines.append(f"  Villain range (~{n:.0f} weighted combos): {bd}")
    else:
        lines.append(f"  Hand class: {hand_class(*st['cards'][hero])}")
    eq = equity(st["cards"][hero], vrange, st["board"], samples=3000)
    if eq is not None:
        lines.append(f"  Your equity vs villain's range: {eq * 100:.0f}%")
    if node is not None:
        evs = S.combo_evs(node, "".join(st["cards"][hero]))
        loss = S.ev_loss(evs, opt["key"])
        if evs:
            lines.append("  Solver EVs (bb): " + " | ".join(
                f"{next(o['id'] for o in opts if o['key'] == k)} {v:+.1f}" for k, v in evs.items()))
    else:
        extra = {}
        if st["pf_node"] == "sb_vs_3bet":
            j = jam_ev(st, hero, vrange)
            if j is not None:
                extra["allin"] = j
        loss = price_ev_loss(strat, opt["key"], eq, pot(st), to_call, extra, realization(st, hero))
    rec["ev_loss_bb"] = loss
    new_verdict = close_in_ev(verdict, loss, pot(st))
    if new_verdict != verdict:
        lines[0] = lines[0].replace(verdict, new_verdict, 1)
        verdict = rec["verdict"] = new_verdict
    lines[0] += ev_tag(loss)
    if to_call > 0:
        need = to_call / (pot(st) + to_call)
        lines.append(f"  Pot odds: call {fmt(to_call)} into {fmt(pot(st))} -> need {need * 100:.0f}% equity")
    p = pot(st)
    spr = min(behind(st, "SB"), behind(st, "BB")) / p if p else 0
    lines.append(f"  Pot {fmt(p)}, effective stack {fmt(min(behind(st, 'SB'), behind(st, 'BB')))}, SPR {spr:.1f}")
    rec["coach_notes"] = lines[5:]
    # The notes go to a file, not stdout: the student can see tool output.
    with open(NOTES, "w") as f:
        f.write("\n".join(lines[4:]) + "\n")
    return verdict, "\n".join(lines[:3]), rec


# ---------- rendering ----------

def render(st):
    hero = st["hero"]
    pos = "button / in position postflop" if hero == "SB" else "big blind / out of position postflop"
    out = [f"Hand #{st['hand_no']} — you are {hero} ({pos}), 100bb deep, blinds 0.5/1"]
    if st.get("drill"):
        out.append(f"Drill: {st['drill']} — {Dr.DRILLS[st['drill']]}")
    out.append(f"Your cards: {' '.join(st['cards'][hero])}")
    if st["board"]:
        out.append(f"Board: {' '.join(st['board'])}")
        out.append(f"Your hand: {describe(st['cards'][hero], st['board'])}")
    out.append("Action:")
    out += [f"  {h}" for h in st["history"]] or ["  (none yet)"]
    if st["over"]:
        if st.get("revealed"):
            out.append(f"Villain showed: {' '.join(st['cards'][OTHER[hero]])}")
            out.append(f"  You: {describe(st['cards'][hero], st['board'])} | Villain: {describe(st['cards'][OTHER[hero]], st['board'])}")
        out.append(f"HAND OVER — your result: {'+' if st['hero_net'] >= 0 else ''}{fmt(st['hero_net'])}bb")
        meta = load(META, {"hands": 0, "net": 0.0})
        out.append(f"Session: {meta['hands']} hands, {'+' if meta['net'] >= 0 else ''}{fmt(meta['net'])}bb. Run `new` for the next hand.")
        return "\n".join(out)
    if st.get("pending_review"):
        out.append("PAUSED for review of your last decision. Run `next` to continue.")
        return "\n".join(out)
    out.append(f"Pot: {fmt(pot(st))} | you have {fmt(behind(st, hero))} behind | villain has {fmt(behind(st, OTHER[hero]))}")
    _, opts, _ = current_options(st)
    out.append("YOUR OPTIONS: " + " | ".join(f"[{o['id']}] {o['label']}" for o in opts))
    return "\n".join(out)


# ---------- commands ----------

def cmd_new(args):
    st = load(STATE, None)
    if st and not st["over"] and "--force" not in args:
        print("A hand is in progress. Use `new --force` to abandon it.\n")
        print(render(st))
        return
    meta = load(META, {"hands": 0, "net": 0.0})
    meta["hands"] += 1
    save(META, meta)
    n = meta["hands"]
    deals = {k: v for k, v in load(DEALS, {}).items() if int(k) >= n}  # drop played deals
    for k in (n, n + 1):  # deal this hand and the next one in advance
        if str(k) not in deals:
            deals[str(k)] = make_deal(meta.get("drill"), k)
    save(DEALS, deals)
    cleanup_solves(n)
    spawn_presolves([n, n + 1])
    deal = deals[str(n)]
    hero = hero_seat(n)
    st = {
        "hand_no": n, "hero": hero, "drill": deal.get("drill"),
        "cards": deal["cards"], "board_full": deal["board_full"], "board": [],
        "street": "preflop", "pf_node": "sb_open", "line": None,
        "contrib": {"SB": 0.5, "BB": 1.0}, "street_contrib": {"SB": 0.5, "BB": 1.0},
        "history": ["SB posts 0.5, BB posts 1"], "actions": [], "ranges": None,
        "to_act": "SB", "over": False, "pending_review": False,
    }
    villain_turns(st)
    save(STATE, st)
    print(render(st))


def hero_seat(n):
    return "BB" if n % 2 else "SB"


def read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG) as f:
        return [json.loads(line) for line in f if line.strip()]


def make_deal(drill, n):
    """A random deal for hand n, filtered to the active drill if there is one."""
    if drill:
        kind = Dr.resolve(drill, n, read_log() if drill == "adaptive" else ())
        d = Dr.deal(kind, hero_seat(n))
        d["drill"] = kind
        return d
    deck = DECK[:]
    random.shuffle(deck)
    return {"cards": {"SB": deck[0:2], "BB": deck[2:4]}, "board_full": deck[4:9]}


def cmd_drill(args):
    meta = load(META, {"hands": 0, "net": 0.0})
    want = (args[0] if args else "").lower()
    if want not in list(Dr.DRILLS) + list(Dr.MODES) + ["off"]:
        print("Usage: drill <type|mix|adaptive|off>. Types:")
        for k, v in {**Dr.DRILLS, **Dr.MODES}.items():
            print(f"  {k:8s} {v}")
        print(f"Current: {meta.get('drill') or 'off'}")
        return
    meta["drill"] = None if want == "off" else want
    save(META, meta)
    # Re-deal the upcoming hand so it follows the new setting.
    deals = {k: v for k, v in load(DEALS, {}).items() if int(k) <= meta["hands"]}
    save(DEALS, deals)
    print(f"Drill: {meta['drill'] or 'off'} (applies from the next `new`)")
    if meta["drill"] == "adaptive":
        w = Dr.adaptive_weights(read_log())
        tot = sum(v[0] for v in w.values())
        print("Adaptive weights (from your decision log):")
        for k, (wt, n, m, ev) in sorted(w.items(), key=lambda kv: -kv[1][0]):
            print(f"  {k:7s} {wt / tot:4.0%}  ({n} decisions, {m:.0f} mistakes, {ev:.1f}bb EV lost)")


def cleanup_solves(n):
    """Delete solve files from hands before the previous one."""
    if not os.path.isdir(SOLVES):
        return
    for name in os.listdir(SOLVES):
        if name.startswith("hand"):
            num = name[4:].split("_")[0]
            if num.isdigit() and int(num) < n - 1:
                os.remove(os.path.join(SOLVES, name))


def cmd_act(args):
    st = load(STATE, None)
    if not st or st["over"]:
        print("No hand in progress. Run `new`.")
        return
    if st.get("pending_review"):
        print("Still paused on the last decision. Run `next` first.")
        return
    if st["to_act"] != st["hero"]:
        print("It's not your turn.")
        return
    want = " ".join(args).strip().lower()
    _, opts, node = current_options(st)
    opt = next((o for o in opts if o["id"] == want), None)
    if opt is None:
        print(f"Unknown option '{want}'. Options: " + ", ".join(o["id"] for o in opts))
        return
    verdict, report, rec = grade(st, opt, opts, node)
    if node is None:
        apply_preflop(st, st["hero"], opt["key"])
    else:
        apply_postflop(st, load_root(st), st["hero"], opt["key"])
    append_jsonl(LOG, rec)
    print(report)
    print()
    if verdict == "MISTAKE" and not st["over"]:
        st["pending_review"] = True
        save(STATE, st)
        print("PAUSED for review. Run `next` when the discussion is done.")
        return
    villain_turns(st)
    save(STATE, st)
    print(render(st))


def cmd_next(args):
    st = load(STATE, None)
    if not st:
        print("No hand. Run `new`.")
        return
    st["pending_review"] = False
    villain_turns(st)
    save(STATE, st)
    print(render(st))


def cmd_status(args):
    st = load(STATE, None)
    print(render(st) if st else "No hand yet. Run `new`.")


def cmd_notes(args):
    try:
        with open(NOTES) as f:
            print(f.read(), end="")
    except FileNotFoundError:
        print("No graded decision yet.")


def cmd_note(args):
    text = " ".join(args).strip()
    try:
        with open(LOG) as f:
            lines = f.readlines()
    except FileNotFoundError:
        print("No decisions logged yet.")
        return
    rec = json.loads(lines[-1])
    rec["note"] = text
    lines[-1] = json.dumps(rec) + "\n"
    with open(LOG, "w") as f:
        f.writelines(lines)
    print(f"Noted on hand #{rec['hand']} {rec['street']} decision.")


def action_type(r):
    """Leak category for a decision: what was chosen, bucketed (bet/raise sizes merged)."""
    c = r["chosen"]
    for prefix, name in (("bet", "bet"), ("raise", "raise"), ("allin", "all-in")):
        if c.startswith(prefix):
            return name
    return c


def ev_stats(recs):
    """bb lost to mistakes, per 100 hands, and top leaks.

    Records from before EV tracking lack the ev_loss_bb key and are skipped;
    bb/100 counts only hands logged since then (HU and ring alike).
    """
    tracked = [r for r in recs if "ev_loss_bb" in r]
    costed = [r for r in tracked if r["ev_loss_bb"] is not None]
    if not costed:
        return
    hands = len({str(r["hand"]) for r in tracked})
    lost = sum(r["ev_loss_bb"] for r in costed if r["verdict"] == "MISTAKE")
    all_lost = sum(r["ev_loss_bb"] for r in costed)
    rate = f", {100 * lost / hands:.1f} bb/100 over {hands} hands"
    print(f"EV lost: {lost:.1f}bb to mistakes{rate} ({all_lost:.1f}bb incl. small slips;"
          f" {len(costed)} of {len(recs)} decisions costed)")
    leaks = {}
    for r in costed:
        if r["ev_loss_bb"] > 0:
            k = f"{r['street']} {action_type(r)}"
            leaks[k] = leaks.get(k, 0.0) + r["ev_loss_bb"]
    top = sorted(leaks.items(), key=lambda kv: -kv[1])[:5]
    if top:
        print("Top leaks (bb lost): " + ", ".join(f"{k} {v:.1f}" for k, v in top))


def cmd_stats(args):
    if "--mixing" in args:
        Mx.report(LOG)
        return
    meta = load(META, {"hands": 0, "net": 0.0})
    recs = []
    try:
        with open(LOG) as f:
            recs = [json.loads(line) for line in f]
    except FileNotFoundError:
        pass
    n = meta["hands"]
    print(f"Hands: {n} | net {'+' if meta['net'] >= 0 else ''}{fmt(meta['net'])}bb"
          + (f" ({100 * meta['net'] / n:.0f} bb/100)" if n else ""))
    if recs:
        counts = {}
        for r in recs:
            k = r["verdict"].split(" ")[0]
            counts[k] = counts.get(k, 0) + 1
        print("Decisions: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + f" (of {len(recs)})")
        by_street = {}
        for r in recs:
            if r["verdict"] == "MISTAKE":
                by_street[r["street"]] = by_street.get(r["street"], 0) + 1
        if by_street:
            print("Mistakes by street: " + ", ".join(f"{k} {v}" for k, v in by_street.items()))
        ev_stats(recs)
        mistakes = [r for r in recs if r["verdict"] == "MISTAKE"][-5:]
        for r in mistakes:
            if r.get("strategy"):
                best = max(r["strategy"], key=r["strategy"].get)
                ref = f"chose {r['chosen']} ({r['chosen_freq'] * 100:.0f}%), solver {best} ({r['strategy'][best] * 100:.0f}%)"
            else:
                ref = f"chose {r['chosen']} (multiway, approximate grade)"
            if r.get("ev_loss_bb") is not None:
                ref += f", cost ~{r['ev_loss_bb']:.1f}bb"
            print(f"  #{r['hand']} {r['street']} {r['seat']} {' '.join(r['hole'])} board {' '.join(r['board']) or '-'}: {ref}"
                  + (f" — {r['note']}" if r.get("note") else ""))


def cmd_table(args):
    meta = load(META, {"hands": 0, "net": 0.0})
    want = (args[0] if args else "")
    if want not in ("2", "4", "6"):
        print(f"Usage: table <2|4|6>. Current: {meta.get('table', 2)}-handed.")
        return
    meta["table"] = int(want)
    save(META, meta)
    print(f"Table: {want}-handed. `new` deals a {'heads-up' if want == '2' else want + '-max'} hand.")


SAVE_NAME = "poker-coach-save.tar.gz"


def _save_files():
    """Top-level progress files in the data dir: state, settings and logs, not solves."""
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(f for f in os.listdir(DATA_DIR) if f.endswith((".json", ".jsonl")))


def cmd_export(args):
    """Write the progress files to <dir>/poker-coach-save.tar.gz (for chats, whose sandbox resets)."""
    import tarfile
    out_dir = os.path.expanduser(args[0]) if args else "."
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, SAVE_NAME)
    names = _save_files()
    with tarfile.open(path, "w:gz") as tar:
        for name in names:
            tar.add(os.path.join(DATA_DIR, name), arcname=name)
    print(f"Saved {len(names)} files to {path}")


def cmd_import(args):
    """Restore progress files from a save made by `export`, replacing the current ones."""
    import tarfile
    if not args:
        print("Usage: import <path to poker-coach-save.tar.gz>")
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    with tarfile.open(os.path.expanduser(args[0]), "r:gz") as tar:
        # Only plain top-level progress files: never paths, links or anything else.
        members = [m for m in tar.getmembers() if m.isfile() and "/" not in m.name
                   and m.name.endswith((".json", ".jsonl"))]
        for m in members:
            with tar.extractfile(m) as src, open(os.path.join(DATA_DIR, m.name), "wb") as dst:
                dst.write(src.read())
    meta = load(META, {"hands": 0, "net": 0.0})
    print(f"Restored {len(members)} files: {meta.get('hands', 0)} heads-up hands, "
          f"table {meta.get('table', 2)}-handed, drill {meta.get('drill') or 'off'}.")


COMMANDS = {"new": cmd_new, "act": cmd_act, "next": cmd_next, "status": cmd_status,
            "note": cmd_note, "stats": cmd_stats, "presolve": cmd_presolve, "notes": cmd_notes, "drill": cmd_drill,
            "table": cmd_table, "export": cmd_export, "import": cmd_import}
RING_COMMANDS = ("new", "act", "next", "status")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] in RING_COMMANDS and load(META, {}).get("table", 2) in (4, 6):
        import ring
        ring.COMMANDS[sys.argv[1]](sys.argv[2:])
    else:
        COMMANDS[sys.argv[1]](sys.argv[2:])
