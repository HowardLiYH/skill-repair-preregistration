"""Amendment 4 (owner approval, November 13, 2026): continuation after the original S2 stop. Offline, journal only."""
import sys
from dataclasses import asdict
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2'), str(ROOT / 'followup' / 'tests')]
from followup.controller import choose_strategy, TrialResult, Stop, STRATEGIES
from test_controller import build

FAMS = ('alpha', 'beta', 'gamma', 'delta')


def table(passes, fits, costs):
    """passes/fits: {strategy: set of families}; costs: {strategy: cost per run}."""
    return {f: {'full': {'status': 'task_fail'}, 'strategies': {
        s: ({'fits': True, 'passed': f in passes.get(s, ()), 'cost_usd': costs[s]} if f in fits[s] else {'fits': False})
        for s in STRATEGIES}} for f in FAMS}


ALL = set(FAMS)
# Like the study: source_native_compact and api_code tie on passes; compact is cheaper but fails S2 (2 of 4 fit);
# api_code meets it (3 of 4); rule_formula meets it but scores lower.
FITS = {'source_native_compact': {'alpha', 'beta'}, 'workflow': {'alpha', 'beta'}, 'api_code': {'alpha', 'beta', 'gamma'},
        'rule_formula': ALL}
COSTS = {'source_native_compact': 0.023, 'workflow': 0.02, 'api_code': 0.027, 'rule_formula': 0.03}
PASSES = {'source_native_compact': {'alpha', 'beta'}, 'api_code': {'alpha', 'beta'}, 'rule_formula': {'alpha'}}


def eligible(fits):
    return {s for s in STRATEGIES if len(fits[s]) >= 0.75 * len(FAMS)}


def test_original_rule_unchanged():
    assert choose_strategy(table(PASSES, FITS, COSTS))['strategy'] == 'source_native_compact'      # cheaper of the tie


def test_amended_rule_keeps_only_top_tied_strategies_meeting_s2():
    out = choose_strategy(table(PASSES, FITS, COSTS), s2_eligible=eligible(FITS))
    assert out['strategy'] == 'api_code' and out['top_tied'] == ['source_native_compact', 'api_code']


def test_amended_rule_never_selects_a_lower_scoring_strategy():
    fits = dict(FITS, api_code={'alpha', 'beta'})          # no top-tied strategy meets S2; rule_formula does but scores less
    with pytest.raises(Stop, match='no strategy tied for the most passes meets S2'):
        choose_strategy(table(PASSES, fits, COSTS), s2_eligible=eligible(fits))


def test_existing_tie_breakers_apply_among_the_retained():
    fits = dict(FITS, source_native_compact={'alpha', 'beta', 'gamma'})        # both top-tied now meet S2
    out = choose_strategy(table(PASSES, fits, COSTS), s2_eligible=eligible(fits))
    assert out['strategy'] == 'source_native_compact' and out['reason'] == 'tie_broken_by_mean_cost'
    same = dict(COSTS, api_code=0.023)
    assert choose_strategy(table(PASSES, fits, same), s2_eligible=eligible(fits))['reason'] == 'tie_broken_by_fixed_order'


def seed_calibration(c, fits=FITS, passes=PASSES):
    """Write a finished calibration into the journal, as the real stage records it (no runs)."""
    for f in FAMS:
        c.journal.put(f'run:cal:{f}:full', asdict(TrialResult('task_fail', 0, 2, 0.02)))
        for s in STRATEGIES:
            ok = f in fits[s]
            c.journal.put(f'edit:cal:{f}:{s}', {'files': {'main/SKILL.md': f'{f} {s}'}, 'eligible': ok, 'tokens': 10})
            if ok:
                c.journal.put(f'run:cal:{f}:{s}', asdict(TrialResult('pass' if f in passes.get(s, ()) else 'task_fail', 1, 2, COSTS[s])))
    c.journal.put('calibration:choice', choose_strategy(c.calibration_table()))


def test_continuation_is_recorded_and_frozen_without_any_call(tmp_path):
    c = build(tmp_path, families=FAMS)
    seed_calibration(c)
    before = dict(c.journal.done)
    with pytest.raises(Stop, match='S2'):
        c.check_s2(c.journal.get('calibration:choice')['strategy'])                 # the original S2 failure
    gate = c.apply_amendment4('owner', 'test approval')
    assert gate['strategy'] == 'api_code' and gate['fits'] == ['alpha', 'beta', 'gamma']
    assert gate['frozen_rewrites_sha256']['delta'] is not None                       # over-cap rewrite kept, not excluded
    assert c.journal.get('amendment4:original')['choice']['strategy'] == 'source_native_compact'
    assert 'Rule S2' in c.journal.get('amendment4:original')['s2_failure']
    for k, v in before.items(): assert c.journal.get(k) == v                          # nothing overwritten
    assert c.runner.calls == [] and c.editor.calls == 0                               # no regeneration, no rerun
    assert c.apply_amendment4('owner', 'again') == gate                               # idempotent


def test_amendment_does_not_apply_when_the_original_choice_meets_s2(tmp_path):
    c = build(tmp_path, families=FAMS)
    fits = dict(FITS, source_native_compact={'alpha', 'beta', 'gamma'})
    seed_calibration(c, fits=fits)
    with pytest.raises(Stop, match='does not apply'):
        c.apply_amendment4('owner', 'x')
