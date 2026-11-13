"""Stage controller for the follow-up study: 1. Calibration -> 2. Dev (3 loops) -> 3. Test (3 runs per task).

Follows design/PROCEDURE.md and the stop rules S1-S7 (docs/FOLLOWUP_IMPLEMENTATION.md). It is written against two
small interfaces so it can be tested offline and run for real with the same code:

  editor(identity, messages, temperature) -> raw response text      (followup.client.LedgerClient.call)
  runner.run(family, task, skill_files_or_None, label) -> TrialResult (server adapter, built and checked at preflight)

Every finished step is appended to a JSON-lines journal, so a stopped or paused stage resumes without repeating
completed calls or runs. Test outcomes are never printed or summarised before the whole test step has finished (S5).
"""
import hashlib, json, random, sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src_v2')]
from capstudy.bundles import load_bundle                       # noqa: E402
from capstudy.tokenization import bundle_count                 # noqa: E402
from followup import editing, feedback                         # noqa: E402

STRATEGIES = ('source_native_compact', 'workflow', 'api_code', 'rule_formula')   # fixed order (last tie-break)
DEFAULT_STRATEGY = 'workflow'
EDIT_TEMPERATURE, REWRITE_TEMPERATURE = 0.7, 0.0
ARMS = ('repair', 'retry')
SELECTOR_VERSION = 'n21-2026-11-12'   # Notion N21: passes, then check-level practice score, then shorter, then earlier


class Stop(RuntimeError):
    """A stop rule fired: no further runs in this stage until the owner decides."""


class Pause(RuntimeError):
    """A pause rule fired: runs wait and resume unchanged once the cause is fixed."""


class InfraError(RuntimeError):
    """Raised by a runner for the pre-defined infrastructure failures before grading (rule S4)."""


class EvidencePending(Pause):
    """The run was graded but its evidence is not yet collected or valid. The completed attempt is preserved and its
    evidence recovered on resume; it is never replaced by a new model run."""


@dataclass
class TrialResult:
    status: str                      # 'pass' or 'task_fail' (graded by the native verifier)
    checks_passed: int
    checks_total: int
    cost_usd: float
    timed_out: bool = False
    junit_path: str = ''
    trace_path: str = ''
    fingerprint: str = ''
    run_id: str = ''


class Journal:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.done = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    item = json.loads(line); self.done[item['key']] = item['value']

    def get(self, key): return self.done.get(key)

    def put(self, key, value):
        if key in self.done: raise ValueError('Journal key already recorded: ' + key)
        with open(self.path, 'a') as f:
            f.write(json.dumps({'key': key, 'value': value}, sort_keys=True) + '\n'); f.flush()
        self.done[key] = value
        return value


def files_sha256(files):
    """Identity of a skill text: hash of its files in path order."""
    if files is None: return None
    h = hashlib.sha256()
    for rel in sorted(files): h.update(rel.encode() + b'\0' + files[rel].encode() + b'\0')
    return h.hexdigest()


def edit_distance(before, after):
    """Normalized token edit distance between two skill texts (Levenshtein over whitespace tokens of all files in
    path order, divided by the longer length). 0 = identical, 1 = nothing in common. A diagnostic of how much the
    text changed, not of whether the change helped."""
    if before is None or after is None: return None
    a = ' '.join(before[k] for k in sorted(before)).split(); b = ' '.join(after[k] for k in sorted(after)).split()
    if not a and not b: return 0.0
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1] / max(len(a), len(b))


class StageController:
    def __init__(self, *, splits, data_root, skills_root, editor, runner, journal_path, counter,
                 loops=3, runs=3, test_order_seed=20261111, infra_pause_fraction=0.10, s2_min_share=0.75,
                 inventories=None, not_applicable=None, max_attempts=None, limit_check=None, attempts_offset=0,
                 check_descriptions=None):
        """inventories: {task: [native check names]}, the frozen list of each task's checks, recorded before any run.
        When given, every run is scored against it (skipped or unreached checks count as not passed) and a task
        without an inventory stops the stage. not_applicable: {task: [names]} marked in advance, left out for both
        arms. max_attempts: ceiling on physical agent attempts, replacements included. limit_check(): called before
        every agent attempt and editor call; raises Stop when a spending ceiling would be passed. attempts_offset: attempts
        already used by earlier pilot versions (the ceiling is one running total). check_descriptions: {family: {native
        check name: description}}, frozen and identical for both arms."""
        self.splits, self.data_root, self.skills_root = splits, Path(data_root), Path(skills_root)
        self.editor, self.runner, self.counter = editor, runner, counter
        self.journal = Journal(journal_path)
        self.loops, self.runs, self.seed = loops, runs, test_order_seed
        self.infra_pause_fraction, self.s2_min_share = infra_pause_fraction, s2_min_share
        self.fingerprint = None
        self.families = sorted(splits)
        self.inventories, self.not_applicable = inventories, not_applicable or {}
        self.max_attempts, self.limit_check, self.attempts_offset = max_attempts, limit_check, attempts_offset
        self.check_descriptions = check_descriptions or {}

    # ------------------------------------------------------------------ shared pieces
    def source(self, family): return load_bundle(self.skills_root / family)

    def cap(self, family): return bundle_count(self.source(family).files, self.counter) // 2

    def dev_instructions(self, family):
        return [{'instance_id': t, 'instruction': self.runner.instruction(family, t)} for t in self.splits[family]['practice']]

    def run(self, key, family, task, files, label):
        """One agent run, journaled. Rule S4: one recorded replacement for an infrastructure failure before grading.
        An attempt that was dispatched but never resolved is reconciled (runner.recover), never relaunched. A graded
        run whose evidence is missing or invalid pauses for evidence recovery; it is never rerun."""
        adopted = self.journal.get('adopt:' + key)                    # Amendment 3: the corrected choice governs
        if adopted is not None: return TrialResult(**adopted['result'])
        saved = self.journal.get('run:' + key)
        if saved is not None: return TrialResult(**saved)
        result = attempt_key = None
        exception = self.journal.get('exception-attempt:' + key)        # owner-approved extra attempt for this cell
        for attempt in ((1, 2, 3) if exception else (1, 2)):
            attempt_key = f'{key}#a{attempt}'
            if self.journal.get('infra:' + attempt_key) is not None: continue
            try:
                if self.journal.get('attempt:' + attempt_key) is not None:      # dispatched earlier: reconcile, never relaunch
                    result = self._recover(self.journal.get('label:' + attempt_key) or f'{label}#a{attempt}@{key}', attempt_key); break
                self._before_attempt(key, attempt)
                self._record_label(attempt_key, f'{label}#a{attempt}@{key}', task)
                result = self.runner.run(family, task, files, f'{label}#a{attempt}@{key}')
                break
            except InfraError as exc:
                self.journal.put(f'infra:{attempt_key}', {'error': str(exc)[:300]})       # recorded immediately
                if attempt == 2 and not exception: raise Stop(f'Rule S4: second infrastructure failure for {key}: {exc}')
                if attempt == 3: raise Stop(f'Owner-approved additional attempt for {key} failed: {exc}; stop and report')
                self._check_infra_share()
        if result is None: raise Stop(f'Rule S4: both attempts for {key} ended in infrastructure failures')
        if result.status not in ('pass', 'task_fail'): raise ValueError('Runner returned an ungraded status')
        if self.inventories is not None:
            if task not in self.inventories: raise Stop(f'No frozen check inventory for {task}')
            score = self._score(result, task, self.journal.get('label:' + attempt_key) or f'{label}#a{attempt_key[-1]}@{key}', attempt_key)
            self.journal.get('checks:' + key) or self.journal.put('checks:' + key, score)
            result.checks_passed, result.checks_total = score['checks_passed'], score['checks_total']
        if not result.fingerprint:                              # an absent fingerprint is not proof of a stable model
            self.journal.get('nofp:' + key) or self.journal.put('nofp:' + key, True)
        if result.fingerprint:                                  # rule S7: model drift
            if self.fingerprint is None: self.fingerprint = self.journal.get('fingerprint') or self.journal.put('fingerprint', result.fingerprint)
            if result.fingerprint != self.fingerprint:
                raise Pause(f'Rule S7: model fingerprint changed from {self.fingerprint} to {result.fingerprint}')
        saved = TrialResult(**self.journal.put('run:' + key, asdict(result)))   # the grade is saved first
        self._check_infra_share()                                               # S4 after every resolved attempt
        return saved

    def _recover(self, label, attempt_key):
        recover = getattr(self.runner, 'recover', None)
        if recover is None:
            raise Stop(f'Attempt {attempt_key} was dispatched but has no recorded outcome: reconcile before any new attempt')
        return recover(label)

    def _score(self, result, task, label, attempt_key):
        """Score against the frozen inventory. Invalid evidence: recollect once, then pause for recovery."""
        args = (self.inventories[task], self.not_applicable.get(task, ()))
        try:
            return feedback.score_junit(result.junit_path, *args)
        except feedback.EvidenceError as first:
            recollect = getattr(self.runner, 'recollect', None)
            if recollect is not None:
                try:
                    result.junit_path = recollect(label).junit_path
                    return feedback.score_junit(result.junit_path, *args)
                except (feedback.EvidenceError, InfraError) as second:
                    first = second
            raise EvidencePending(f'Graded run {attempt_key} has invalid evidence ({first}); recover it, do not rerun')

    def _before_attempt(self, key, attempt):
        """Every physical attempt is recorded before dispatch and counted against the ceiling (replacements too).
        Rule S4 is checked before admitting any new paid attempt, so a pause survives a restart."""
        if self.limit_check is not None: self.limit_check()
        self._check_infra_share()
        used = self.attempts_offset + sum(k.startswith('attempt:') for k in self.journal.done)
        if self.max_attempts is not None and used >= self.max_attempts:
            raise Stop(f'Pilot ceiling: {used} agent attempts used of {self.max_attempts}')
        self.journal.put(f'attempt:{key}#a{attempt}', {'n': used + 1})

    def _record_label(self, attempt_key, label, task):
        if self.journal.get('label:' + attempt_key) is None:
            self.journal.put('label:' + attempt_key, label); self.journal.put('label:' + attempt_key + ':task', task)

    def attempts_used(self): return self.attempts_offset + sum(k.startswith('attempt:') for k in self.journal.done)

    def apply_s4_amendment(self, recover):
        """S4 amendment (pilot v1, November 12). For every recorded infrastructure failure, ask the runner to
        re-classify it from its saved evidence (recover(label) -> (summary, TrialResult or None); no model call).
        A confirmed agent-image refusal by the text-only relay: the earliest such attempt of a cell that has no graded
        run becomes the cell's run (its native grade); later attempts of the same cell are kept as diagnostics only.
        Nothing recorded is overwritten; the cell is never run again."""
        report = []
        infra = sorted(k[len('infra:'):] for k in self.journal.done if k.startswith('infra:'))
        for attempt_key in infra:
            if any(self.journal.get(p + attempt_key) is not None for p in ('amend:', 'diagnostic:', 'amend-checked:')): continue
            cell, _, n = attempt_key.rpartition('#a')
            label = self.journal.get('label:' + attempt_key)
            if label is None: raise Stop(f'No recorded label for {attempt_key}; reconcile by hand')
            summary, result = recover(label, self.journal.get('label:' + attempt_key + ':task'))
            if not summary.get('amendment') or result is None:
                self.journal.put('amend-checked:' + attempt_key, {'confirmed': False, 'reason': ((summary.get('amendment') or {}).get('evidence') or {}).get('reason', summary.get('diagnostic'))})
                report.append((attempt_key, 'stays_infrastructure')); continue
            task = self.journal.get('label:' + attempt_key + ':task')
            score = self._score(result, task, label, attempt_key) if self.inventories is not None else None
            record = {'summary': summary, 'status': result.status, 'checks': score, 'cost_usd': result.cost_usd, 'run_id': result.run_id}
            if self.journal.get('run:' + cell) is None:
                if score: result.checks_passed, result.checks_total = score['checks_passed'], score['checks_total']
                self.journal.put('amend:' + attempt_key, dict(record, adopted_as_planned_run=True))
                if score: self.journal.put('checks:' + cell, score)
                self.journal.put('run:' + cell, asdict(result))
                report.append((attempt_key, 'adopted_as_planned_run'))
            else:
                self.journal.put('diagnostic:' + attempt_key, dict(record, adopted_as_planned_run=False,
                                 note='additional diagnostic attempt; not an independent observation'))
                report.append((attempt_key, 'kept_as_diagnostic'))
        return report

    def planned_attempt(self, cell):
        """The attempt whose grade is the cell's planned run: the adopted one (Amendment 3), else the one that produced
        run:cell (the S4-amendment adoption, or the first attempt without an infrastructure record)."""
        adopted = self.journal.get('adopt:' + cell)
        if adopted is not None: return adopted['attempt']
        if self.journal.get('run:' + cell) is None: return None
        for n in (1, 2, 3):
            a = f'{cell}#a{n}'
            if self.journal.get('amend:' + a) is not None or (self.journal.get('attempt:' + a) is not None and self.journal.get('infra:' + a) is None):
                return a
        return None

    def apply_amendment3(self, recover, approved_by, approval, tag='a3', rule='Amendment 3: earliest qualifying attempt'):
        """Amendment 3 (owner approval, November 13, 2026): re-read every recorded infrastructure failure uniformly under
        the corrected native rule (consistent skipped checks count as not passed), with no model call:
        recover(label, task) -> (summary, TrialResult or None), the same re-reading as the S4 amendment (an image refusal
        still needs the relay's own record). For each cell, the earliest attempt that qualifies is adopted through a
        separate adopt: record, even when a later attempt was already graded and scored better; every other graded attempt
        of the cell is kept as a diagnostic. Nothing recorded is changed or removed (run:, checks:, infra:, amend:,
        costs); two attempts of one cell are never two observations. Amendment 5 (November 13, 2026) reuses this
        uniform re-reading with its own records (tag 'a5') under the corrected image trace check."""
        report = []
        infra = sorted(k[len('infra:'):] for k in self.journal.done if k.startswith('infra:'))
        for attempt_key in infra:
            if self.journal.get(f'{tag}:' + attempt_key) is not None: continue
            cell, _, n = attempt_key.rpartition('#a')
            if (self.journal.get('amend:' + attempt_key) is not None or self.journal.get('diagnostic:' + attempt_key) is not None
                    or any((self.journal.get(t + attempt_key) or {}).get('qualifies') for t in ('a3:', 'a5:') if t != tag + ':')):
                self.journal.put(f'{tag}:' + attempt_key, {'qualifies': None, 'note': 'already graded under an earlier amendment; unchanged'})
                report.append((attempt_key, 'already_resolved')); continue
            label, task = self.journal.get('label:' + attempt_key), self.journal.get('label:' + attempt_key + ':task')
            if label is None: raise Stop(f'No recorded label for {attempt_key}; reconcile by hand')
            summary, result = recover(label, task)
            if result is None:
                reason = (summary.get('verifier_reclassification') or {}).get('amended_verifier_health') or summary.get('diagnostic')
                self.journal.put(f'{tag}:' + attempt_key, {'qualifies': False, 'reason': reason})
                report.append((attempt_key, 'stays_infrastructure')); continue
            score = self._score(result, task, label, attempt_key) if self.inventories is not None else None
            if score: result.checks_passed, result.checks_total = score['checks_passed'], score['checks_total']
            record = {'qualifies': True, 'status': result.status, 'checks': score, 'cost_usd': result.cost_usd,
                      'run_id': result.run_id, 'summary': summary, 'result': asdict(result)}
            current = self.planned_attempt(cell)
            if current is None or int(n) < int(current.rpartition('#a')[2]):
                self.journal.put(f'{tag}:' + attempt_key, dict(record, role='adopted_as_planned_run'))
                self.journal.put('adopt:' + cell, {'attempt': attempt_key, 'result': asdict(result), 'checks': score,
                                                   'previous_planned_attempt': current,
                                                   'previous_planned_run': self.journal.get('run:' + cell),
                                                   'rule': rule, 'approved_by': approved_by,
                                                   'approval': approval})
                if current is not None:
                    self.journal.put('superseded:' + current, {'note': 'graded attempt kept as a diagnostic; not an independent observation',
                                                               'adopted_attempt': attempt_key})
                report.append((attempt_key, 'adopted_as_planned_run', current))
            else:
                self.journal.put(f'{tag}:' + attempt_key, dict(record, role='diagnostic', note='later than the planned attempt; not an independent observation'))
                report.append((attempt_key, 'kept_as_diagnostic'))
        return report

    def infra_counts(self):
        """Rule S4 (amended November 12, 2026): cumulative resolved executor attempts in this stage's journal, original
        attempts and replacements alike; editor calls and shared result rows are not attempts. An attempt is resolved
        when it ended in a recorded infrastructure failure or produced the cell's graded run. Infrastructure failures
        re-classified under the S4 image amendment (adopted or diagnostic) or graded under Amendments 3 and 5 are not failures. Read from the journal, so
        a restart never resets the count."""
        failures = resolved = 0
        for key in self.journal.done:
            if not key.startswith('attempt:'): continue
            attempt = key[len('attempt:'):]; cell = attempt.rpartition('#a')[0]
            if self.journal.get('infra:' + attempt) is not None:
                resolved += 1
                if (self.journal.get('amend:' + attempt) is None and self.journal.get('diagnostic:' + attempt) is None
                        and not any((self.journal.get(t + attempt) or {}).get('qualifies') for t in ('a3:', 'a5:'))): failures += 1
            elif self.journal.get('run:' + cell) is not None:
                resolved += 1
        return failures, resolved

    def _check_infra_share(self):
        """Pause when, after at least 20 resolved attempts, more than 10% ended in infrastructure failure. The pause
        holds across restarts (counts are read from the journal). Only the owner can release it, with a recorded
        acknowledgement (acknowledge_s4_pause); a later new infrastructure failure re-applies the rule."""
        failures, resolved = self.infra_counts()
        if resolved >= 20 and failures > self.infra_pause_fraction * resolved:
            acknowledged = [v for k, v in self.journal.done.items() if k.startswith('s4-ack:')]
            if not any(a['failures'] >= failures for a in acknowledged):
                raise Pause(f'Rule S4: {failures} infrastructure failures in {resolved} resolved attempts')

    def grant_exception_attempt(self, key, approved_by, reason):
        """Owner's explicit exception to the one-replacement limit: exactly one additional attempt for one cell, with
        the same candidate, inputs, settings and grading. Both earlier attempt records stay."""
        if self.journal.get('run:' + key) is not None: raise ValueError('The cell already has a graded run')
        if self.journal.get('exception-attempt:' + key) is not None: raise ValueError('An exception was already granted for this cell')
        if not all(self.journal.get(f'infra:{key}#a{n}') is not None for n in (1, 2)):
            raise ValueError('Exception applies only to a cell whose two attempts both ended in infrastructure failures')
        return self.journal.put('exception-attempt:' + key, {'approved_by': approved_by, 'reason': reason, 'attempts_added': 1})

    def acknowledge_s4_pause(self, approved_by, reason):
        """Owner's recorded release of an S4 pause after the cause was fixed (never automatic)."""
        failures, resolved = self.infra_counts()
        n = sum(k.startswith('s4-ack:') for k in self.journal.done) + 1
        return self.journal.put(f's4-ack:{n}', {'failures': failures, 'resolved': resolved, 'approved_by': approved_by, 'reason': reason})

    def loop_edit(self, key, messages, source, strategy, cap, temperature, current=None):
        saved = self.journal.get('edit:' + key)
        if saved is not None: return saved
        def call(msgs, rnd):                          # every editor dispatch, first answers and re-asks alike
            if self.limit_check is not None: self.limit_check()
            self._check_infra_share()                 # S4 holds editor calls too while a pause is unreleased
            return self.editor({'step': key, 'round': rnd}, msgs, temperature)
        out = editing.run_loop(call, messages, source, strategy, self.counter, cap, current_files=current)
        record = {'files': out['files'], 'eligible': out['eligible'], 'accepted': out['accepted'],
                  'tokens': out['candidate_tokens'], 'cap': cap, 'reasons': out['ineligibility_reasons'],
                  'rounds': out['rounds'], 'valid': out['files'] is not None,
                  'changed': out['files'] is not None and (current is None or out['files'] != current)}
        return self.journal.put('edit:' + key, record)

    # ------------------------------------------------------------------ 1. Calibration
    def calibration(self, description):
        saved = self.journal.get('calibration:choice')
        if saved is not None: return saved
        table = {}
        for family in self.families:
            task = self.splits[family]['calibration'][0]; source = self.source(family); cap = self.cap(family)
            full = self.run(f'cal:{family}:full', family, task, source.files, 'calibration/full')
            table[family] = {'full': asdict(full), 'strategies': {}}
            for strategy in STRATEGIES:
                messages = editing.rewrite_messages(source, strategy, bundle_count(source.files, self.counter), cap, description)
                rewrite = self.loop_edit(f'cal:{family}:{strategy}', messages, source, strategy, cap, REWRITE_TEMPERATURE)
                cell = {'fits': rewrite['eligible'], 'tokens': rewrite['tokens']}
                if rewrite['eligible']:
                    r = self.run(f'cal:{family}:{strategy}', family, task, rewrite['files'], f'calibration/{strategy}')
                    cell.update(passed=r.status == 'pass', cost_usd=r.cost_usd)
                table[family]['strategies'][strategy] = cell
        return self.journal.put('calibration:choice', choose_strategy(table))

    def calibration_table(self):
        """The calibration table rebuilt from the journal only (the adopted run of every cell); no editor or agent call.
        Raises Stop if any cell the calibration needs is missing."""
        table = {}
        for family in self.families:
            def graded(key):
                adopted = self.journal.get('adopt:' + key)
                saved = adopted['result'] if adopted is not None else self.journal.get('run:' + key)
                if saved is None: raise Stop(f'Calibration record missing: {key}')
                return saved
            table[family] = {'full': graded(f'cal:{family}:full'), 'strategies': {}}
            for strategy in STRATEGIES:
                rewrite = self.journal.get(f'edit:cal:{family}:{strategy}')
                if rewrite is None: raise Stop(f'Calibration rewrite missing: {family} {strategy}')
                cell = {'fits': rewrite['eligible'], 'tokens': rewrite['tokens']}
                if rewrite['eligible']:
                    r = graded(f'cal:{family}:{strategy}')
                    cell.update(passed=r['status'] == 'pass', cost_usd=r['cost_usd'])
                table[family]['strategies'][strategy] = cell
        return table

    def apply_amendment4(self, approved_by, approval):
        """Amendment 4 (owner approval, November 13, 2026, after calibration and before any dev run). The original
        choice and its S2 failure are kept. Among the strategies tied for the highest calibration pass count, those that
        meet the existing S2 threshold are kept, then the existing cost and fixed-order tie-breakers apply; none -> Stop.
        Reuses the recorded rewrites and calibration runs; records the frozen continuation as gate:S2."""
        if self.journal.get('gate:S2') is not None: return self.journal.get('gate:S2')
        original = self.journal.get('calibration:choice')
        if original is None: raise Stop('Calibration has not finished')
        try:
            self.check_s2(original['strategy'])
            raise Stop('The original choice meets S2; Amendment 4 does not apply')
        except Stop as exc:
            if 'does not apply' in str(exc): raise
            failure = str(exc)
        table = self.calibration_table()
        if choose_strategy(table) != original: raise Stop('Rebuilt calibration table does not reproduce the original choice')
        fits = {s: [f for f in self.families if table[f]['strategies'][s]['fits']] for s in STRATEGIES}
        choice = choose_strategy(table, s2_eligible={s for s in STRATEGIES if len(fits[s]) >= self.s2_min_share * len(self.families)})
        strategy = choice['strategy']
        frozen = {f: files_sha256(self.journal.get(f'edit:cal:{f}:{strategy}')['files'] or {}) if self.journal.get(f'edit:cal:{f}:{strategy}')['files'] else None
                  for f in self.families}
        self.journal.put('amendment4:original', {'choice': original, 's2_failure': failure})
        return self.journal.put('gate:S2', {'strategy': strategy, 'fits': self.check_s2(strategy), 'choice': choice,
                                            'amendment': 'Amendment 4 (adopted after calibration)', 'approved_by': approved_by,
                                            'approval': approval, 'fits_by_strategy': fits, 'frozen_rewrites_sha256': frozen})

    def fixed_strategy_rewrites(self, strategy, description):
        """Pilot only: the fixed strategy's rewrite for every family (step 1.2 without calibration runs)."""
        out = {}
        for family in self.families:
            source, cap = self.source(family), self.cap(family)
            messages = editing.rewrite_messages(source, strategy, bundle_count(source.files, self.counter), cap, description)
            rewrite = self.loop_edit(f'cal:{family}:{strategy}', messages, source, strategy, cap, REWRITE_TEMPERATURE)
            out[family] = {'fits': rewrite['eligible'], 'tokens': rewrite['tokens'], 'cap': cap, 'editor_calls': len(rewrite['rounds'])}
        return out

    def initial_rewrite(self, family, strategy):
        return self.journal.get(f'edit:cal:{family}:{strategy}')

    def check_s2(self, strategy):
        fits = [f for f in self.families if (self.initial_rewrite(f, strategy) or {}).get('eligible')]
        if len(fits) < self.s2_min_share * len(self.families):
            raise Stop(f'Rule S2: the chosen strategy fits the cap in only {len(fits)} of {len(self.families)} families')
        return fits

    # ------------------------------------------------------------------ 2. Dev
    def dev(self, strategy, loop):
        selections = {}
        for family in self.families:
            key = f'dev{loop}:{family}'
            saved = self.journal.get('select:' + key)
            if saved is not None: selections[family] = saved; continue
            source, cap = self.source(family), self.cap(family)
            practice = self.splits[family]['practice']; instructions = self.dev_instructions(family)
            descriptions = None
            if self.check_descriptions.get(family) and self.inventories is not None:
                descriptions = {t: feedback.describe(None, self.inventories[t], self.check_descriptions[family]) for t in practice}
            for task in practice:
                self.run(f'{key}:full:{task}', family, task, source.files, f'dev{loop}/full')
            initial = self.initial_rewrite(family, strategy)
            attempts = [{'id': 'initial', 'files': initial['files'], 'eligible': initial['eligible'], 'tokens': initial['tokens'], 'index': 0}]
            outcomes = {}
            if initial['eligible']:
                outcomes['initial'] = [self.run(f'{key}:initial:{t}', family, t, initial['files'], f'dev{loop}/initial') for t in practice]
            both = initial['eligible'] and all(r.status == 'pass' for r in outcomes['initial'])
            arm_attempts = {}
            # Arm order for this family and loop is drawn from the fixed seed (balances drift between arms).
            order = list(ARMS); random.Random(f'{self.seed}:dev{loop}:{family}').shuffle(order)
            self.journal.get('armorder:' + key) or self.journal.put('armorder:' + key, order)
            for arm in order:
                arm_list = list(attempts); current = initial['files'] if initial['files'] else source.files
                latest = outcomes.get('initial', []); latest_tasks = practice if initial['eligible'] else []
                if not both:
                    for index in (1, 2):
                        fb = None
                        if arm == 'repair':
                            fb = feedback.assemble([feedback.run_feedback(t, r.junit_path, r.trace_path, r.timed_out,
                                                                          (self.inventories or {}).get(t))
                                                    for t, r in zip(latest_tasks, latest) if r.status != 'pass'])
                        messages = editing.edit_messages(source.files, initial['files'] or source.files, current, instructions,
                                                         cap, index, strategy, fb, descriptions)
                        edit = self.loop_edit(f'{key}:{arm}:{index}', messages, source, strategy, cap, EDIT_TEMPERATURE, current=current)
                        self.journal.get('editlog:' + f'{key}:{arm}:{index}') or self.journal.put('editlog:' + f'{key}:{arm}:{index}', {
                            'family': family, 'loop': loop, 'arm': arm, 'attempt': index, 'changed': edit['changed'],
                            'within_cap': edit['eligible'], 'had_feedback': fb is not None, 'valid': edit['valid'],
                            # Editor calls actually used by this attempt (first answer plus re-asks); the allowance is
                            # the same for both arms: 1 + MAX_BUDGET_REASKS + 1 copy re-ask.
                            'editor_calls': len(edit['rounds']), 'editor_calls_allowed': 2 + editing.MAX_BUDGET_REASKS,
                            # A failed practice run existed for this arm's current version, so feedback was due.
                            'feedback_opportunity': any(r.status != 'pass' for r in latest),
                            'evaluated': bool(edit['eligible']),
                            'edit_distance': edit_distance(current, edit['files'])})
                        cid = f'{arm}{index}'
                        arm_list.append({'id': cid, 'files': edit['files'], 'eligible': edit['eligible'], 'tokens': edit['tokens'], 'index': index})
                        if edit['files'] is not None: current = edit['files']
                        if edit['eligible']:
                            outcomes[cid] = latest = [self.run(f'{key}:{cid}:{t}', family, t, edit['files'], f'dev{loop}/{arm}') for t in practice]
                            latest_tasks = practice
                            if all(r.status == 'pass' for r in latest): break
                arm_attempts[arm] = arm_list
            self.journal.get('outcomes:' + key) or self.journal.put('outcomes:' + key, {
                'attempts': {arm: [{k: a[k] for k in ('id', 'eligible', 'tokens', 'index')} for a in arm_attempts[arm]] for arm in ARMS},
                'passes': {cid: [r.status for r in rs] for cid, rs in outcomes.items()},
                'scores': {cid: [[r.checks_passed, r.checks_total] for r in rs] for cid, rs in outcomes.items()}, 'cap': cap})
            selections[family] = self.journal.put('select:' + key, {arm: select(arm_attempts[arm], outcomes, cap) for arm in ARMS})
        return selections

    def check_s3(self):
        """Rule S3 before any test run. S3a implementation integrity: repair lacked feedback although a failed
        practice run existed, or retry received feedback -> Stop (a defect). S3b opportunity: no editing needed,
        invalid or unchanged edits, little feedback available -> reported, never a reason to change the method."""
        log = [v for k, v in self.journal.done.items() if k.startswith('editlog:')]
        repair = [e for e in log if e['arm'] == 'repair']
        due = [e for e in repair if e['feedback_opportunity']]
        missing = [e for e in due if not e['had_feedback']]
        leaked = [e for e in log if e['arm'] == 'retry' and e['had_feedback']]
        integrity = {'repair_calls_missing_feedback_despite_failed_run': len(missing), 'retry_calls_with_feedback': len(leaked)}
        if missing or leaked: raise Stop(f'Rule S3a: feedback delivery defect {integrity}')
        opportunity = {'edit_attempts': len(log), 'valid_and_changed': sum(e['valid'] and e['changed'] for e in log),
                       'invalid': sum(not e['valid'] for e in log), 'repair_calls': len(repair),
                       'repair_calls_with_failed_run_available': len(due),
                       'repair_calls_without_any_run_to_report': len(repair) - len(due)}
        return {'S3a_integrity': {**integrity, 'passed': True}, 'S3b_opportunity': opportunity}

    def path_report(self):
        """Per family and dev loop: whether the complete path was exercised, and if editing was not, why."""
        log = [v for k, v in self.journal.done.items() if k.startswith('editlog:')]
        out = {}
        for k in self.journal.done:
            if not k.startswith('select:'): continue
            loop_key = k[len('select:'):]; loop = int(loop_key.split(':')[0][3:]); family = loop_key.split(':', 1)[1]
            rec = self.journal.get('outcomes:' + loop_key)
            initial = rec['passes'].get('initial')
            arms = {}
            for arm in ARMS:
                edits = [e for e in log if e['family'] == family and e['loop'] == loop and e['arm'] == arm]
                if initial is not None and all(st == 'pass' for st in initial): arms[arm] = 'no_editing_needed_initial_passed_both'
                elif not edits: arms[arm] = 'no_edit_recorded'
                elif any(e.get('evaluated') for e in edits): arms[arm] = 'edited_and_evaluated'
                else: arms[arm] = 'editing_attempted_no_skill_within_limit'
            fed = any(e['had_feedback'] for e in log if e['family'] == family and e['loop'] == loop and e['arm'] == 'repair')
            tested = any(x.startswith(f'run:test{loop}:{family}:') for x in self.journal.done)
            scored = any(x.startswith(f'checks:test{loop}:{family}:') for x in self.journal.done)
            out[loop_key] = {'arms': arms, 'repair_received_feedback': fed, 'held_out_runs_graded': tested,
                             'evidence_scored_against_inventory': scored,
                             'complete_path': all(v == 'edited_and_evaluated' for v in arms.values()) and fed and tested and scored}
        return out

    def replay_selection(self, loop_key, arm, version=SELECTOR_VERSION):
        """Recompute one arm's selection from the recorded practice results only (no model call). Records made before
        the scores were stored are read from the per-run check records."""
        rec = self.journal.get('outcomes:' + loop_key)
        family = loop_key.split(':', 1)[1]
        practice = self.splits[family]['practice']
        outcomes = {}
        for cid, statuses in rec['passes'].items():
            scores = (rec.get('scores') or {}).get(cid)
            if scores is None:
                scores = []
                for task in practice:
                    checks = self.journal.get(f'checks:{loop_key}:{cid}:{task}')
                    scores.append([checks['checks_passed'], checks['checks_total']] if checks else [0, 0])
            outcomes[cid] = [TrialResult(status=st, checks_passed=sc[0], checks_total=sc[1], cost_usd=0)
                             for st, sc in zip(statuses, scores)]
        attempts = [dict(a, files=None) for a in rec['attempts'][arm]]
        return select(attempts, outcomes, rec['cap'], n_practice=len(practice), version=version)

    def pilot_checks(self):
        """The pilot's pass checks (Notion N2, N14). Whether editing was exercised is reported with its reason, not
        required; final candidate diversity and edit size are observations only."""
        out = {'S3': self.check_s3()}
        log = [v for k, v in self.journal.done.items() if k.startswith('editlog:')]
        mismatches, same = [], 0
        for k, v in self.journal.done.items():
            if not k.startswith('select:'): continue
            loop_key = k[len('select:'):]
            for arm in ARMS:
                again = self.replay_selection(loop_key, arm, v[arm].get('selector', 'pilot-v1'))
                if again['id'] != v[arm]['id'] or not v[arm].get('reason') or (v[arm]['id'] and not v[arm].get('sha256')):
                    mismatches.append((k, arm))
            same += v['repair'].get('sha256') is not None and v['repair'].get('sha256') == v['retry'].get('sha256')
        out['selection_follows_rule_and_recorded'] = not mismatches
        out['selection_mismatches'] = mismatches
        out['paths'] = self.path_report()
        out['families_with_complete_path'] = sorted({k.split(':', 1)[1] for k, v in out['paths'].items() if v['complete_path']})
        out['observations'] = {'loops_where_both_arms_selected_the_same_text': same,
                               'edit_distance': [e.get('edit_distance') for e in log]}
        out['passed'] = out['S3']['S3a_integrity']['passed'] and not mismatches
        return out

    # ------------------------------------------------------------------ 3. Test
    def test_plan(self, selections_by_loop):
        """Test runs in matched blocks (rule S6): for each (dev loop, family, task, run) the repair and retry runs are
        scheduled back to back in a random arm order, and the blocks are shuffled, all with the pre-registered seed.
        When both arms selected byte-identical text in a loop, one run serves both arms (flagged as shared, not as two
        independent observations)."""
        rng = random.Random(self.seed); blocks = []
        for loop in sorted(selections_by_loop):
            for family in self.families:
                chosen = selections_by_loop[loop][family]
                same = chosen['repair']['files'] is not None and chosen['repair']['files'] == chosen['retry']['files']
                arms = [a for a in ARMS if chosen[a]['files'] is not None and not (same and a == 'retry')]
                for task in self.splits[family]['test']:
                    for run in range(1, self.runs + 1):
                        order = list(arms); rng.shuffle(order)
                        blocks.append([(loop, family, arm, task, run) for arm in order])
        rng.shuffle(blocks)
        return [job for block in blocks for job in block]

    def test(self, selections_by_loop):
        for loop, family, arm, task, run in self.test_plan(selections_by_loop):
            files = selections_by_loop[loop][family][arm]['files']
            self.run(f'test{loop}:{family}:{arm}:{task}:{run}', family, task, files, f'test{loop}/{arm}')
        # Rows are written only after every test run has finished (rule S5).
        rows = []
        for loop, selections in selections_by_loop.items():
            for family in self.families:
                chosen = selections[family]
                same = chosen['repair']['files'] is not None and chosen['repair']['files'] == chosen['retry']['files']
                for arm in ARMS:
                    source_arm = 'repair' if same else arm
                    for task in self.splits[family]['test']:
                        for run in range(1, self.runs + 1):
                            row = {'family': family, 'task': task, 'arm': arm, 'loop': loop, 'run': run,
                                   'shared_run': int(same),
                                   'sensitivity14': int(task in self.splits.get('_sensitivity', [task]))}
                            if chosen[arm]['files'] is None:
                                row.update(status='infeasible', checks_passed=0, checks_total=0)
                            else:
                                r = TrialResult(**self.journal.get(f'run:test{loop}:{family}:{source_arm}:{task}:{run}'))
                                row.update(status=r.status, checks_passed=r.checks_passed, checks_total=r.checks_total)
                            rows.append(row)
        return rows

    def fingerprint_report(self):
        runs = sum(k.startswith('run:') for k in self.journal.done)
        return {'runs': runs, 'runs_without_fingerprint': sum(k.startswith('nofp:') for k in self.journal.done),
                'fingerprint': self.journal.get('fingerprint')}

    def edit_rows(self):
        return [v for k, v in sorted(self.journal.done.items()) if k.startswith('editlog:')]


def practice_score(results):
    """Mean check-level practice score: per task passed/applicable checks, averaged equally over the tasks."""
    fractions = []
    for r in results:
        if not r.checks_total or not 0 <= r.checks_passed <= r.checks_total:
            raise ValueError('Practice result without a valid check-level score')
        fractions.append(r.checks_passed / r.checks_total)
    return sum(fractions) / len(fractions)


def select(attempts, outcomes, cap, n_practice=2, version=SELECTOR_VERSION):
    """Notion N21 (amended November 12, 2026), identical for both conditions. Among versions that are eligible (within
    the limit, valid) and have valid graded results on every practice task: (1) most practice tasks passed; (2) higher
    mean check-level practice score; (3) shorter; (4) earlier. version='pilot-v1' reproduces the original rule
    (passes, shorter, earlier) for the record."""
    ranked = []
    for a in attempts:
        rs = outcomes.get(a['id'])
        if not a['eligible'] or rs is None or len(rs) != n_practice or any(r.status not in ('pass', 'task_fail') for r in rs):
            continue
        if not isinstance(a['tokens'], int) or a['tokens'] < 0 or a['tokens'] > cap:
            raise ValueError('Eligible candidate has an invalid count or cap')
        passes = sum(r.status == 'pass' for r in rs)
        score = practice_score(rs) if version != 'pilot-v1' else 0.0
        ranked.append((-passes, -score, a['tokens'], a['index'], a['id']))
    if not ranked:
        return {'status': 'infeasible', 'id': None, 'files': None, 'sha256': None, 'reason': 'no_candidate_within_cap_was_run',
                'selector': version}
    ranked.sort()
    best = ranked[0]
    same_passes = [x for x in ranked if x[0] == best[0]]
    same_score = [x for x in same_passes if x[1] == best[1]]
    same_length = [x for x in same_score if x[2] == best[2]]
    if len(same_passes) == 1: reason = 'most_practice_passes'
    elif len(same_score) == 1: reason = 'tie_on_passes_higher_check_score' if version != 'pilot-v1' else 'tie_on_passes_shortest'
    elif len(same_length) == 1: reason = 'tie_on_passes_and_score_shortest' if version != 'pilot-v1' else 'tie_on_passes_shortest'
    else: reason = 'tie_on_passes_score_and_length_earliest' if version != 'pilot-v1' else 'tie_on_passes_and_length_earliest'
    files = next(a['files'] for a in attempts if a['id'] == best[4])
    return {'status': 'selected', 'id': best[4], 'files': files, 'dev_passes': -best[0], 'practice_check_score': -best[1],
            'tokens': best[2], 'sha256': files_sha256(files), 'reason': reason, 'selector': version}


def choose_strategy(table, s2_eligible=None):
    """Most qualifying passes; tie -> lower mean execution cost on the calibration cases where every tied strategy ran;
    still tied or no shared cases -> fixed order. All zero -> full-skill check, then the default `workflow`.
    s2_eligible (Amendment 4): the strategies meeting the existing S2 threshold. Among those tied for the highest pass
    count only these are kept before the tie-breakers; none -> Stop. The threshold is not lowered and a lower-scoring
    strategy is never chosen."""
    scores = {s: sum(bool(table[f]['strategies'][s].get('passed')) for f in table) for s in STRATEGIES}
    best = max(scores.values())
    full_passes = sum(table[f]['full']['status'] == 'pass' for f in table)
    if best == 0:
        if s2_eligible is not None and DEFAULT_STRATEGY not in s2_eligible:
            raise Stop('Amendment 4: the default strategy does not meet S2; stop')
        return {'strategy': DEFAULT_STRATEGY, 'reason': 'all_zero_default', 'scores': scores, 'full_skill_passes': full_passes}
    tied = [s for s in STRATEGIES if scores[s] == best]
    if s2_eligible is not None:
        top = tied
        tied = [s for s in top if s in s2_eligible]
        if not tied: raise Stop('Amendment 4: no strategy tied for the most passes meets S2; stop')
        if len(tied) == 1:
            return {'strategy': tied[0], 'reason': 'amendment4_only_top_strategy_meeting_s2', 'scores': scores,
                    'top_tied': top, 'tied': tied, 'full_skill_passes': full_passes}
    if len(tied) == 1:
        return {'strategy': tied[0], 'reason': 'most_qualifying_passes', 'scores': scores, 'full_skill_passes': full_passes}
    shared = [f for f in table if all('cost_usd' in table[f]['strategies'][s] for s in tied)]
    costs = {s: sum(table[f]['strategies'][s]['cost_usd'] for f in shared) / len(shared) for s in tied} if shared else {}
    winner = min(tied, key=lambda s: (costs.get(s, 0), STRATEGIES.index(s)))
    reason = 'tie_broken_by_mean_cost' if shared and len({round(costs[s], 9) for s in tied}) > 1 else 'tie_broken_by_fixed_order'
    return {'strategy': winner, 'reason': reason, 'scores': scores, 'tied': tied, 'shared_cases': shared,
            'mean_cost_usd': costs, 'full_skill_passes': full_passes}
