"""Physical charges, counterfactual policy attribution and reuse economics."""
from __future__ import annotations
import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

def price_usage(usage, prices, *, input_includes_cache=False, output_includes_reasoning=True):
    """Prices per million tokens; null categories stay unavailable, never zero.

    `input_tokens` is uncached for Anthropic-style usage, total input for
    OpenAI-style usage. Callers must explicitly supply the latter convention.
    Reasoning is generally a subset of output and must not be charged twice.
    """
    required=('input_tokens','output_tokens','cache_read_tokens','cache_write_tokens')
    if any(usage.get(k) is None for k in required): return {'cost':None,'reason':'usage_unavailable'}
    if any(not isinstance(usage[k],(int,float)) or isinstance(usage[k],bool) or not math.isfinite(usage[k]) or usage[k]<0 for k in required): raise ValueError('invalid token count')
    counts={k:Decimal(str(usage[k])) for k in required}
    if input_includes_cache:
        counts['input_tokens']-=counts['cache_read_tokens']+counts['cache_write_tokens']
        if counts['input_tokens']<0: raise ValueError('cached tokens exceed total input')
    if not output_includes_reasoning:
        if usage.get('reasoning_tokens') is None: return {'cost':None,'reason':'reasoning_usage_unavailable'}
        value=usage['reasoning_tokens']
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0: raise ValueError('invalid reasoning token count')
        counts['reasoning_tokens']=Decimal(str(value))
    if any(prices.get(k) is None for k,v in counts.items() if v): return {'cost':None,'reason':'pricing_unavailable'}
    for key,value in counts.items():
        price=prices.get(key)
        if value and (isinstance(price,bool) or not isinstance(price,(int,float)) or not math.isfinite(price) or price<0): raise ValueError('invalid token price')
    cost=sum(v*Decimal(str(prices[k])) for k,v in counts.items() if v)/Decimal(1000000)
    return {'cost':float(cost),'reason':None,'input_includes_cache':input_includes_cache,'output_includes_reasoning':output_includes_reasoning}

def aggregate_calls(calls, policy_call_ids=None):
    """Unique physical call IDs charged once; a policy pays its required calls."""
    unique={}
    for call in calls:
        ident=call['call_id']
        if ident in unique and unique[ident]!=call: raise ValueError('conflicting duplicate physical call')
        unique[ident]=call
    currencies={c.get('currency') for c in unique.values()}
    if len(currencies)>1: raise ValueError('cannot aggregate mixed currencies')
    def subtotal(ids):
        records=[unique[i] for i in ids]
        unknown=[c['call_id'] for c in records if c.get('known_charge') is None]
        known=sum(Decimal(str(c['known_charge'])) for c in records if c.get('known_charge') is not None)
        if any(c.get('known_charge') is not None and (not math.isfinite(c['known_charge']) or c['known_charge']<0) for c in records): raise ValueError('invalid charge')
        return {'total':None if unknown else float(known),'known_subtotal':float(known),'unavailable_call_ids':unknown,'physical_calls':len(records)}
    policies={}
    for policy,ids in (policy_call_ids or {}).items():
        if set(ids)-unique.keys(): raise ValueError('policy references missing call')
        policies[policy]=subtotal(sorted(set(ids)))
    return {'research':subtotal(sorted(unique)),'policies':policies,'currency':next(iter(currencies),None)}

def break_even(preparation_method, execution_method, preparation_baseline, execution_baseline):
    values=(preparation_method,execution_method,preparation_baseline,execution_baseline)
    if any(v is None for v in values): return {'status':'unavailable','minimum_uses':None,'maximum_uses':None}
    if any(not math.isfinite(v) or v<0 for v in values): raise ValueError('costs must be finite nonnegative')
    pm,cm,pb,cb=map(lambda v:Decimal(str(v)),values); delta=pm-pb; savings=cb-cm
    if savings>0:
        n=max(1,int((delta/savings).to_integral_value(rounding=ROUND_CEILING)))
        return {'status':'finite_long_run','minimum_uses':n,'maximum_uses':None}
    if savings==0:
        return {'status':'always_no_more_expensive' if delta<=0 else 'never','minimum_uses':1 if delta<=0 else None,'maximum_uses':None}
    maximum=int((delta/savings).to_integral_value(rounding=ROUND_FLOOR))
    return {'status':'limited_initial_range' if maximum>=1 else 'never','minimum_uses':1 if maximum>=1 else None,'maximum_uses':maximum if maximum>=1 else None}

def cumulative_cost(preparation, first_use, warm_use, uses):
    if not isinstance(uses,int) or uses<0: raise ValueError('uses must be nonnegative integer')
    if any(v is None for v in (preparation,first_use,warm_use)): return None
    return preparation+(first_use+(uses-1)*warm_use if uses else 0)
