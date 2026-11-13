"""Protocol-defined ranking; never substitutes full instructions in a primary arm."""
from __future__ import annotations

STRATEGY_ORDER = ("source_native_compact", "workflow", "api_code", "rule_formula")

class UnresolvedTrial(ValueError):
    pass

def dev_passes(trials):
    if len(trials) != 2 or len({t['instance_id'] for t in trials}) != 2:
        raise UnresolvedTrial('Exactly two distinct assigned dev results required')
    if any(t['status'] not in ('pass', 'task_fail') for t in trials):
        raise UnresolvedTrial('Dev selection requires resolved native grading')
    return sum(t['status'] == 'pass' for t in trials)

def select_candidate(attempts, dev_results, config=None):
    ranked = []
    for c in attempts:
        if not c['eligible']:
            continue
        count = c['candidate_tokens']
        if not isinstance(count, int) or count < 0 or count > c['token_cap']:
            raise ValueError('Eligible candidate has invalid count/cap')
        score = dev_passes(dev_results[c['candidate_id']])
        ranked.append((-score, count, c['attempt_index'], c['candidate_id']))
    if not ranked:
        return {'status': 'infeasible', 'candidate_id': None, 'dev_passes': None}
    score, count, index, cid = min(ranked)
    return {'status': 'selected', 'candidate_id': cid, 'dev_passes': -score,
            'candidate_tokens': count, 'attempt_index': index}

def fallback_selection(initial, initial_results):
    if initial['eligible'] and dev_passes(initial_results) == 2:
        return {'condition': 'full_skill_fallback', 'candidate_id': initial['candidate_id'],
                'uses_full_skill': False, 'reason': 'initial_passes_both_dev'}
    return {'condition': 'full_skill_fallback', 'candidate_id': None,
            'uses_full_skill': True, 'reason': 'initial_ineligible_or_dev_failure'}

def choose_strategy(rows, families):
    expected = {(f,s) for f in families for s in STRATEGY_ORDER}
    keys = [(r['family'],r['strategy_id']) for r in rows]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError('Calibration strategy matrix must be complete and unique')
    if any(r['status'] == 'infra_error' for r in rows):
        raise UnresolvedTrial('Calibration has unresolved infrastructure errors')
    scores = {s: sum(r['eligible'] and r['status']=='pass' for r in rows
                     if r['strategy_id']==s) for s in STRATEGY_ORDER}
    best = max(scores.values())
    tied = [s for s in STRATEGY_ORDER if scores[s]==best]
    # Genuine all-zero scores have an explicit fixed default in Section 7.
    if best == 0:
        winner, reason = STRATEGY_ORDER[0], 'all_zero_fixed_default'
    else:
        comparable = all(r['status'] in ('pass','task_fail') and
                         isinstance(r.get('cost_usd'), (int,float)) and r['cost_usd'] >= 0
                         for r in rows if r['strategy_id'] in tied)
        means = {s: sum(r['cost_usd'] for r in rows if r['strategy_id']==s)/len(families)
                 for s in tied} if comparable else {}
        winner = min(tied, key=lambda s:(means.get(s,0),STRATEGY_ORDER.index(s)))
        reason = 'qualifying_passes_then_comparable_cost_then_fixed_order' if comparable else 'qualifying_passes_then_fixed_order_cost_unavailable'
    return {'chosen_strategy':winner,'scores':scores,'reason':reason,'denominator':len(families)}
