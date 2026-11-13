"""Collect the 9 previously unused SkillLearnBench families for the confirmatory study, with the first study's own
collection code and pin (commit 726e1f79). Same split rule per family: first eligible instance = calibration (train),
next two = practice (dev), the rest = test. Downloads public benchmark files only; runs nothing.

  python3.11 followup/collect_new_families.py --output followup-data
"""
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEW_FAMILIES = ['anthropic-poster-design', 'chinese-poem-generator', 'court-form-filling', 'dependency-vulnerability-check',
                'enterprise-information-search', 'github-repo-analytics', 'offer-letter-generator', 'travel-planning',
                'video-object-counting']

spec = importlib.util.spec_from_file_location('m1_collect', ROOT / 'milestone 1/code/collect_skilllearnbench.py')
m1 = importlib.util.module_from_spec(spec); spec.loader.exec_module(m1)
m1.FAMILIES = NEW_FAMILIES

if __name__ == '__main__':
    # The first study had a quarantined instance; these families have none, so the empty split folder must be made
    # when its (empty) instance list is written. Nothing else in the collector changes.
    import pathlib
    original = pathlib.Path.write_text
    def write_text(self, *a, **k):
        self.parent.mkdir(parents=True, exist_ok=True); return original(self, *a, **k)
    pathlib.Path.write_text = write_text
    m1.main()
