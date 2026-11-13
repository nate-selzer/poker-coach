"""Mixing / calibration report: do you take each solver option as often as the solver does?

Reads decisions.jsonl. Each record's `strategy` is the solver/chart frequency per option
for the hero's exact hand; records with an empty strategy (multiway APPROX) are skipped.

Sections:
  1. expected vs actual counts per action family (aggressive/check/call/fold), sliced
     by street, preflop vs postflop and spot type, with a binomial z-score
  2. calibration buckets: every (decision, option) pair bucketed by solver probability
  3. surprise: mean -log(p_chosen) vs the solver-sampling baseline (entropy)
  4. the same headline numbers restricted to non-indifferent spots (solver EV spread >= 0.25bb)
"""
import json
import math
import re

REVIEW_EVERY = 200   # completed hands (HU + ring) between automatic mixing reviews
MIN_SAMPLES = 50     # below this a bucket/slice is "not enough data yet"
EV_SPREAD = 0.25     # bb; spots where all options are closer than this count as indifferent
FAMILIES = ("aggressive", "check", "call", "fold")
BUCKETS = ((0.0, 0.10), (0.10, 0.30), (0.30, 0.70), (0.70, 0.90), (0.90, 1.0001))
P_FLOOR = 0.01
VAR_FLOOR = 0.01     # per-decision variance floor so near-pure spots don't give infinite z
PLAUSIBLE = 0.10     # chosen option below this solver freq = a misread spot (mistake), not a mixing choice
PURE = 0.90          # top option >= this: a "pure" spot, where deviation is a mistake, not mixing


def family(opt):
    if opt.startswith(("bet", "raise", "allin")):
        return "aggressive"
    return opt if opt in ("check", "call", "fold") else "aggressive"


def street_actions(history):
    """History lines on the current street (after the last '--- STREET' marker)."""
    out = []
    for h in history:
        if h.startswith("---"):
            out = []
        else:
            out.append(h)
    return out


def preflop_raiser_is_hero(history):
    raiser = None
    for h in history:
        if h.startswith("---"):
            break
        if " raises" in h:
            raiser = "(you)" in h
    return bool(raiser)


def spot_type(r):
    opts = r.get("options") or list(r["strategy"])
    facing = "call" in opts
    if r["street"] == "preflop":
        return "preflop facing raise" if facing and any(" raises" in h for h in r["history"]) else "preflop open/limp"
    if facing:
        return "facing a bet"
    if r["street"] == "flop" and preflop_raiser_is_hero(r["history"]):
        cur = street_actions(r["history"])
        if not any(" bets" in h or " raises" in h for h in cur):
            return "flop c-bet spot"
    return "checked to / first in (no c-bet)"


def ev_spread(r):
    """Max-min solver EV across options, parsed from coach notes; None if not logged."""
    for n in r.get("coach_notes") or []:
        if "Solver EVs (bb):" in n:
            vals = [float(v) for v in re.findall(r"(-?\d+(?:\.\d+)?)(?=\s*(?:\||$))", n.split(":", 1)[1])]
            if len(vals) >= 2:
                return max(vals) - min(vals)
    return None


def load_records(path):
    try:
        with open(path) as f:
            recs = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []
    out = []
    for r in recs:
        strat = r.get("strategy") or {}
        tot = sum(strat.values())
        if tot <= 0 or r.get("chosen") not in strat:
            continue
        r = dict(r, strategy={k: v / tot for k, v in strat.items()})
        out.append(r)
    return out


def z(actual, expected, var):
    return (actual - expected) / math.sqrt(var) if var > 1e-9 else 0.0


def family_table(recs):
    rows = {}
    for f in FAMILIES:
        exp = var = act = 0.0
        for r in recs:
            p = sum(v for k, v in r["strategy"].items() if family(k) == f)
            if p == 0 and family(r["chosen"]) != f and not any(family(k) == f for k in r["strategy"]):
                continue  # family not available here
            exp += p
            var += max(p * (1 - p), VAR_FLOOR)
            act += family(r["chosen"]) == f
        if exp or act:
            rows[f] = (exp, act, z(act, exp, var))
    return rows


def print_families(title, recs, findings, rank=True):
    rows = family_table(recs)
    if not rows:
        return
    thin = " — not enough data yet" if len(recs) < MIN_SAMPLES else ""
    print(f"  {title} (n={len(recs)}{thin})")
    for f, (exp, act, zz) in rows.items():
        flag = "  <-- notable" if abs(zz) >= 2 and len(recs) >= MIN_SAMPLES else ""
        print(f"    {f:<10} expected {exp:6.1f}  actual {act:4.0f}  diff {act - exp:+6.1f}  z {zz:+5.1f}{flag}")
        if rank:
            findings.append((abs(zz) if len(recs) >= MIN_SAMPLES else 0, f"{title}: {f} expected {exp:.1f}, actual {act:.0f} (z {zz:+.1f})"))


def bucket_pairs(recs):
    out = [[] for _ in BUCKETS]
    for r in recs:
        for k, p in r["strategy"].items():
            for i, (lo, hi) in enumerate(BUCKETS):
                if lo <= p < hi:
                    out[i].append((p, k == r["chosen"], r, k))
                    break
    return out


def print_buckets(recs, findings, title="2. Calibration: every option, bucketed by solver frequency"):
    print("\n" + title)
    print("   bucket      n  predicted  actual   chosen/expected")
    for (lo, hi), pairs in zip(BUCKETS, bucket_pairs(recs)):
        label = f"{lo * 100:.0f}-{min(hi, 1) * 100:.0f}%"
        if not pairs:
            print(f"   {label:<9} {0:4}")
            continue
        n = len(pairs)
        pred = sum(p for p, *_ in pairs) / n
        act = sum(c for _, c, *_ in pairs) / n
        var = sum(max(p * (1 - p), VAR_FLOOR) for p, *_ in pairs)
        zz = z(act * n, pred * n, var)
        note = ""
        if n < MIN_SAMPLES:
            note = "  (not enough data yet)"
        elif lo == 0.0 or lo >= 0.90:
            note = "  (pure-ish options: gaps here are mistakes, see `stats`)" if abs(zz) >= 2 else ""
        elif zz <= -2:
            note = "  <-- UNDER-used (under-mixing)" if hi <= 0.30 else "  <-- under-used"
        elif zz >= 2:
            note = "  <-- OVER-used"
        print(f"   {label:<9} {n:4}  {pred * 100:6.1f}%  {act * 100:6.1f}%   {act * n:.0f}/{pred * n:.1f}  z {zz:+.1f}{note}")
        if n >= MIN_SAMPLES and 0.0 < lo < 0.90:
            findings.append((abs(zz), f"options the solver plays {label}: predicted {pred * 100:.1f}%, you {act * 100:.1f}% (n={n}, z {zz:+.1f})"))
    # concrete examples of skipped low-frequency branches with real weight
    mixed = [(p, r, k) for p, c, r, k in sum(bucket_pairs(recs)[1:3], []) if not c]
    mixed.sort(key=lambda t: -t[0])
    if mixed:
        print("   Mixed options you passed on (highest solver freq):")
        for p, r, k in mixed[:3]:
            print(f"     #{r['hand']} {r['street']} {' '.join(r['hole'])} {' '.join(r['board']) or '-'}:"
                  f" solver {k} {p * 100:.0f}%, you {r['chosen']} {r['strategy'][r['chosen']] * 100:.0f}%")


def print_mistakes_vs_mixing(recs, mixed, spots, findings):
    """Split off misreads (chosen option < PLAUSIBLE) so what's left measures mixing itself."""
    p = PLAUSIBLE * 100
    print(f"\n2b. Mistakes vs mixing: a choice the solver plays < {p:.0f}% is a misread spot, not a mixing choice")
    for name, rs in (("all decisions", recs), ("mixed spots", mixed)):
        bad = [r for r in rs if r["strategy"][r["chosen"]] < PLAUSIBLE]
        if rs:
            print(f"   {name:<14} {len(bad)} of {len(rs)} ({100 * len(bad) / len(rs):.0f}%) chose a < {p:.0f}% option")
    bad = sorted((r for r in mixed if r["strategy"][r["chosen"]] < PLAUSIBLE),
                 key=lambda r: r["strategy"][r["chosen"]])
    if bad:
        print("   Examples (mixed spots you misread):")
        for r in bad[:3]:
            best = max(r["strategy"], key=r["strategy"].get)
            print(f"     #{r['hand']} {r['street']} {' '.join(r['hole'])} {' '.join(r['board']) or '-'}:"
                  f" you {r['chosen']} {r['strategy'][r['chosen']] * 100:.0f}%, solver {best} {r['strategy'][best] * 100:.0f}%")
    clean = [r for r in mixed if r["strategy"][r["chosen"]] >= PLAUSIBLE]
    print(f"\n   Mixing among plausible choices only (mixed spots where your pick had >= {p:.0f}% weight;"
          f" expectations renormalised over the >= {p:.0f}% options):")
    renorm = []
    for r in clean:
        st = {k: v for k, v in r["strategy"].items() if v >= PLAUSIBLE}
        t = sum(st.values())
        renorm.append(dict(r, strategy={k: v / t for k, v in st.items()}))
    print_families("plausible mixed", renorm, findings)
    for name in ("preflop", "postflop"):
        print_families(f"plausible {name}", [r for r in renorm if (r["street"] == "preflop") == (name == "preflop")], findings)
    for name in sorted(spots):
        print_families(f"plausible {name}", [r for r in renorm if spot_type(r) == name], findings)
    print_buckets(renorm, findings, "   Calibration among plausible choices:")


def surprise(recs):
    if not recs:
        return 0.0, 0.0
    s = sum(-math.log(max(r["strategy"][r["chosen"]], P_FLOOR)) for r in recs) / len(recs)
    b = sum(sum(-p * math.log(max(p, P_FLOOR)) for p in r["strategy"].values() if p > 0) for r in recs) / len(recs)
    return s, b


def print_surprise(recs, by_street):
    print("\n3. Surprise: mean -log(p_chosen) vs solver-sampling baseline (higher = more off-solver)")
    for name, rs in [("all", recs)] + by_street:
        if rs:
            s, b = surprise(rs)
            thin = "  (not enough data yet)" if len(rs) < MIN_SAMPLES else ""
            print(f"   {name:<9} n={len(rs):4}  you {s:.2f}  solver {b:.2f}  excess {s - b:+.2f}{thin}")
    print("   (excess > 0: your picks are less likely than a solver-sampler's; < 0: you pick the"
          " top option more than the solver would — i.e. you don't mix)")


def report(path):
    recs = load_records(path)
    if not recs:
        print("No graded decisions with solver strategies yet.")
        return
    findings = []
    streets = ["preflop", "flop", "turn", "river"]
    by_street = [(s, [r for r in recs if r["street"] == s]) for s in streets]
    print(f"MIXING REPORT — {len(recs)} decisions with solver strategies"
          f" (multiway approximate grades skipped)")
    print("\n1. Expected vs actual action counts (expected = sum of solver probabilities)")
    print_families("all", recs, findings, rank=False)
    print_families("preflop", [r for r in recs if r["street"] == "preflop"], findings, rank=False)
    print_families("postflop", [r for r in recs if r["street"] != "preflop"], findings, rank=False)
    for s, rs in by_street[1:]:
        print_families(s, rs, findings, rank=False)
    spots = {}
    for r in recs:
        spots.setdefault(spot_type(r), []).append(r)
    for name, rs in sorted(spots.items()):
        print_families(name, rs, findings, rank=False)
    mixed = [r for r in recs if max(r["strategy"].values()) < PURE]
    print(f"\n  Mixed spots only (solver's top option < {PURE * 100:.0f}%) — the real mixing test:")
    print_families("mixed spots", mixed, [])
    for name in ("preflop", "postflop"):
        print_families(f"mixed {name}", [r for r in mixed if (r["street"] == "preflop") == (name == "preflop")], [])
    for name in sorted(spots):
        print_families(f"mixed {name}", [r for r in mixed if spot_type(r) == name], [])
    print_buckets(recs, [])
    print_mistakes_vs_mixing(recs, mixed, spots, findings)
    print_surprise(recs, by_street)

    real = [r for r in recs if (ev_spread(r) or 0) >= EV_SPREAD]
    known = sum(1 for r in recs if ev_spread(r) is not None)
    print(f"\n4. Non-indifferent spots only (solver EV spread >= {EV_SPREAD}bb; {len(real)} of {known}"
          f" decisions with logged EVs)")
    if real:
        print_families("non-indifferent", real, [])
        s, b = surprise(real)
        print(f"   surprise you {s:.2f}  solver {b:.2f}  excess {s - b:+.2f}")

    print("\nTop findings (largest |z| with >= %d samples):" % MIN_SAMPLES)
    print("  (ranked from section 2b only: misreads and pure-spot deviations are mistakes,"
          " which `stats` covers)")
    top = [f for f in sorted(findings, key=lambda t: -t[0]) if f[0] > 0][:3]
    for score, text in top:
        print(f"  - {text}")
    if not top:
        print("  - not enough data yet")
    if len(recs) < 500:
        print(f"\nCaution: {len(recs)} decisions is a small sample; |z| < 2 is noise, and slices"
              f" under {MIN_SAMPLES} are not enough data yet. The picture firms up around 500.")


def total_hands(meta):
    return meta.get("hands", 0) + sum(v.get("hands", 0) for k, v in meta.items()
                                      if k.startswith("ring") and isinstance(v, dict))


def check_due(meta):
    """Call after a hand finishes with the loaded meta; mutates it. Returns True if a review is due."""
    n = total_hands(meta)
    last = meta.get("mixing_reviewed_at", 0)
    if n // REVIEW_EVERY > last // REVIEW_EVERY:
        meta["mixing_reviewed_at"] = n
        print(f"MIXING REVIEW DUE ({n} hands): run `stats --mixing`")
        return True
    return False
