"""Formal calibration matrix planning and fixed strategy selection."""
import argparse
import json
from .planning import load_config,phase_plan,require_ready
from .selection import choose_strategy

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True)
    p.add_argument('--plan',action='store_true');p.add_argument('--execute',action='store_true')
    a=p.parse_args();c=load_config(a.config)
    if a.execute:
        require_ready(c,'calibration')
        raise SystemExit('Live calibration adapters require integration validation before dispatch.')
    print(json.dumps(phase_plan(c,'calibration'),indent=2))
if __name__=='__main__':main()
