"""Saved power simulation for the follow-up's confirmatory study (model-free, no API calls).

Reproduces the repeat-count choice. Every number it prints comes with its Monte Carlo standard error.

  python3 followup/power_sim.py --sims 4000 --seed 20261112 --output results/followup/power_sim.json

Model of one simulated study (all assumptions are parameters and are written to the output):
  * F families (default 9). Each family has 2 test tasks (probability 0.7) or 3 (0.3).
  * Opportunity: with probability `opportunity` a family needs editing; otherwise the initial rewrite passes both
    practice tasks, both arms keep it, every run is shared, and the family difference is exactly 0.
  * Each dev loop: with probability `same_selection` both arms select the same text (shared runs, difference exactly 0
    in that loop). Otherwise each arm's selected skill has its own success rate: base + editor noise (sd `editor_sd`),
    and repair also gets the family's true effect. Arms with equal true rates still get different observed scores.
  * Family base rate ~ N(0.45, 0.2); task difficulty offset ~ N(0, 0.1), shared by the two arms; rates clipped to
    [0.02, 0.98]. Each selected skill runs R times on every test task (Bernoulli).
  * Effects: 'null' (0 everywhere); 'shift' N(effect, 0.15) per family; 'asymmetric_zero' many small gains and a few
    large losses with mean 0 (+a with probability 0.8, -4a with probability 0.2).
Test: exact two-sided sign-flip test on the family differences, 5%. Interval: family bootstrap (500 resamples per
simulated study, percentile 95%). False positives count rejections in either direction.
Cost: agent runs at most, per design: calibration 5 per family; dev per loop up to 2 full + 2 initial + 8 edited
(2 arms x 2 attempts x 2 practice tasks); test 2 arms x tasks x R per loop.
"""
import argparse, itertools, json
from pathlib import Path
import numpy as np

DESIGNS = ((2, 2), (2, 3), (3, 2), (3, 3))


def simulate(rng, sims, families, loops, runs, scenario, effect, opportunity, same_selection, editor_sd, signs, base_rate=0.45,
             task_counts=None):
    tasks = (np.tile(np.array(task_counts), (sims, 1)) if task_counts is not None
             else np.where(rng.random((sims, families)) < 0.7, 2, 3))
    base = rng.normal(base_rate, 0.2, (sims, families))
    if scenario == 'null':
        delta = np.zeros((sims, families))
    elif scenario == 'shift':
        delta = rng.normal(effect, 0.15, (sims, families))
    elif scenario == 'asymmetric_zero':
        delta = np.where(rng.random((sims, families)) < 0.8, effect, -4 * effect)
    else:
        raise ValueError(scenario)
    needs_edit = rng.random((sims, families)) < opportunity
    diff_obs = np.zeros((sims, families)); diff_true = np.zeros((sims, families))
    for l in range(loops):
        same = (rng.random((sims, families)) < same_selection) | ~needs_edit
        e_rep = rng.normal(0, editor_sd, (sims, families)); e_ret = rng.normal(0, editor_sd, (sims, families))
        loop_obs = np.zeros((sims, families)); loop_true = np.zeros((sims, families))
        for t in range(3):
            active = tasks > t
            u = rng.normal(0, 0.1, (sims, families))
            p_ret = np.clip(base + e_ret + u, 0.02, 0.98)
            p_rep = np.clip(base + e_rep + delta + u, 0.02, 0.98)
            k_rep = rng.binomial(runs, p_rep) / runs; k_ret = rng.binomial(runs, p_ret) / runs
            d = np.where(same, 0.0, k_rep - k_ret)
            loop_obs += np.where(active, d, 0); loop_true += np.where(active & ~same, p_rep - p_ret, 0)
        diff_obs += loop_obs / tasks; diff_true += loop_true / tasks
    diff_obs /= loops; diff_true /= loops
    # exact two-sided sign-flip test: every sign pattern of the family differences
    observed = np.abs(diff_obs.sum(1))
    totals = np.abs(diff_obs @ signs.T)                       # sims x 2^F
    p = (totals >= observed[:, None] - 1e-12).mean(1)
    mean = diff_obs.mean(1)
    reject = (p < 0.05) & (mean != 0)
    idx = rng.integers(0, families, (sims, 500, families))
    boots = np.take_along_axis(np.repeat(diff_obs[:, None, :], 500, 1), idx, 2).mean(2)
    lo, hi = np.percentile(boots, 2.5, axis=1), np.percentile(boots, 97.5, axis=1)
    nonzero = (diff_obs != 0).sum(1)
    return {'rejection_rate': float(reject.mean()), 'rejection_rate_mc_se': float(np.sqrt(reject.mean() * (1 - reject.mean()) / sims)),
            'rejected_positive': float((reject & (mean > 0)).mean()), 'rejected_negative': float((reject & (mean < 0)).mean()),
            'realized_true_mean_effect': float(diff_true.mean()), 'mean_interval_width': float((hi - lo).mean()),
            'mean_interval_width_mc_se': float((hi - lo).std() / np.sqrt(sims)),
            'share_with_5_or_fewer_nonzero_families': float((nonzero <= 5).mean())}


def max_runs(families, loops, runs, mean_tasks=2.3, task_counts=None):
    test = 2 * runs * loops * (sum(task_counts) if task_counts else mean_tasks * families)
    return {'calibration': 5 * families, 'dev': 12 * loops * families, 'test': round(test),
            'total': round(5 * families + 12 * loops * families + test)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sims', type=int, default=4000); ap.add_argument('--seed', type=int, default=20261112)
    ap.add_argument('--families', type=int, default=9); ap.add_argument('--output', required=True)
    ap.add_argument('--task-counts', help='exact test tasks per family, e.g. 2,2,3,3,3,2,2 (sets --families)')
    ap.add_argument('--refresh', action='store_true', help='N22: 3 x 3 only, across identical-selection and task-success ranges')
    a = ap.parse_args(argv)
    counts = [int(x) for x in a.task_counts.split(',')] if a.task_counts else None
    if counts: a.families = len(counts)
    signs = np.array(list(itertools.product((1, -1), repeat=a.families)), dtype=float)
    base = {'opportunity': 0.75, 'same_selection': 0.3, 'editor_sd': 0.12}
    scenarios = [('no effect', 'null', 0.0, {}),
                 ('uneven, zero average (+7.5 / -30 points)', 'asymmetric_zero', 0.075, {}),
                 ('effect 15 points where editing happens', 'shift', 0.15, {}),
                 ('effect 25 points where editing happens', 'shift', 0.25, {}),
                 ('effect 15, opportunity 50%', 'shift', 0.15, {'opportunity': 0.5}),
                 ('effect 15, opportunity 90%', 'shift', 0.15, {'opportunity': 0.9}),
                 ('effect 15, same selection 10%', 'shift', 0.15, {'same_selection': 0.1}),
                 ('effect 15, same selection 50%', 'shift', 0.15, {'same_selection': 0.5}),
                 ('effect 15, editor noise halved', 'shift', 0.15, {'editor_sd': 0.06}),
                 ('effect 15, editor noise doubled', 'shift', 0.15, {'editor_sd': 0.24})]
    rows = []
    if a.refresh:
        scenarios = []
        for same in (0.25, 0.5, 0.75):
            for opp in (0.5, 0.75):
                for base_rate in (0.25, 0.45):
                    for kind, effect in (('null', 0.0), ('shift', 0.15), ('shift', 0.25)):
                        scenarios.append((f'{kind} {effect:.2f}, same selection {same:.0%}, editing needed {opp:.0%}, base rate {base_rate:.0%}',
                                          kind, effect, {'same_selection': same, 'opportunity': opp, 'base_rate': base_rate}))
    for label, kind, effect, override in scenarios:
        params = {**base, **override}
        for loops, runs in (((3, 3),) if a.refresh else DESIGNS):
            rng = np.random.default_rng([a.seed, loops, runs, len(rows)])
            out = simulate(rng, a.sims, a.families, loops, runs, kind, effect, **params, signs=signs, task_counts=counts)
            rows.append({'scenario': label, 'kind': kind, 'effect_where_editing': effect, **params,
                         'design': f'{loops} x {runs}', 'max_agent_runs': max_runs(a.families, loops, runs, task_counts=counts), **out})
            print(f"{label:45s} {loops}x{runs}  reject {out['rejection_rate']:.3f} (se {out['rejection_rate_mc_se']:.3f})"
                  f"  true mean {out['realized_true_mean_effect']:+.3f}  CI width {out['mean_interval_width']:.3f}"
                  f"  max runs {max_runs(a.families, loops, runs, task_counts=counts)['total']}")
    record = {'program': 'followup/power_sim.py', 'seed': a.seed, 'sims_per_cell': a.sims, 'families': a.families, 'task_counts': counts,
              'note': 'Power and interval widths are simulation-dependent projections under the stated assumptions, not guarantees.',
              'assumptions': __doc__, 'results': rows}
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(json.dumps(record, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
