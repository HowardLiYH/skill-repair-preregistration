"""Editor requests for the follow-up: rewrite with a budget loop, and repair/retry edits.

Pure functions (no network, no files). The first-study rewrite prompt, post-processing, parsing and
validation are reused unchanged from src_v2/capstudy.
"""
import copy, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src_v2')]
from capstudy.bundles import validate_candidate            # noqa: E402
from capstudy.bundles import split_front_matter           # noqa: E402
from capstudy.compress.xing import STRATEGIES, render_prompt, parse_response, postprocess, select_anchors   # noqa: E402

REWRITE_SYSTEM = 'Compress only the provided source bundle. Return the requested complete JSON files mapping.'
EDIT_SYSTEM = ('You edit a compressed skill bundle so that an agent can use it to solve tasks of this family. '
               'Return one JSON object {"files": {"path/SKILL.md": "complete text", ...}, "changes": ["..."]} containing every '
               'original SKILL.md path. Preserve original front matter byte-for-byte. Write reusable procedures, not answers '
               'to the practice tasks. Obey the token cap. Do not invent APIs, thresholds, paths or rules that the original '
               'source does not support.')
EDIT_DIRECTIVE = ('Produce an improved version of the current text within the token cap. Your answer must differ from the '
                  'current text. List the changes you made in the "changes" field.')
MAX_BUDGET_REASKS = 3
TARGET_MARGIN = 0.75      # re-ask target as a share of the editable budget; 0.9 left answers 5-10% over the cap


def rewrite_messages(source, strategy_id, original_tokens, token_cap, tokenizer_description):
    """The first study's rewrite request, byte for byte (system text plus the JSON-wrapped v2 prompt)."""
    prompt = render_prompt(source, strategy_id, original_tokens, token_cap, tokenizer_description, prompt_version='v2')
    user = json.dumps({'prompt': prompt}, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return [{'role': 'system', 'content': REWRITE_SYSTEM}, {'role': 'user', 'content': user}]


def edit_messages(original_files, initial_files, current_files, dev_instructions, token_cap, attempt_index,
                  strategy_id, feedback=None, check_descriptions=None):
    """Repair and retry share this request, including the static practice-check descriptions (identical for both
    arms). Repair adds exactly one key, practice_feedback."""
    if len(dev_instructions) != 2:
        raise ValueError('Both assigned dev instructions are required')
    payload = {'original': copy.deepcopy(original_files), 'initial_rewrite': copy.deepcopy(initial_files),
               'current': copy.deepcopy(current_files), 'token_cap': token_cap, 'attempt': attempt_index,
               'practice_task_instructions': [{'task': x['instance_id'], 'instruction': x['instruction']}
                                              for x in sorted(dev_instructions, key=lambda x: x['instance_id'])],
               'strategy': {'id': strategy_id, 'objective': STRATEGIES[strategy_id]['objective']},
               'directive': EDIT_DIRECTIVE}
    if check_descriptions:
        payload['practice_check_descriptions'] = {'note': 'AI-written descriptions of the practice tasks\' checks, the same for every editor.',
                                                  'tasks': copy.deepcopy(check_descriptions)}
    if feedback is not None:
        payload['practice_feedback'] = copy.deepcopy(feedback)
    user = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return [{'role': 'system', 'content': EDIT_SYSTEM}, {'role': 'user', 'content': user}]


def differs_only_by_feedback(repair_messages, retry_messages):
    """Gate C: the retry request must equal the repair request with the feedback block removed."""
    if repair_messages[0] != retry_messages[0] or len(repair_messages) != 2 or len(retry_messages) != 2:
        return False
    repair, retry = json.loads(repair_messages[1]['content']), json.loads(retry_messages[1]['content'])
    if 'practice_feedback' in retry or 'practice_feedback' not in repair:
        return False
    repair.pop('practice_feedback')
    return repair == retry


def finish(source, raw_text, strategy_id, counter, token_cap):
    """Parse, restore front matter and anchors (first-study post-processing), then validate and count."""
    try:
        files = parse_response(raw_text, source)
    except (ValueError, TypeError) as exc:
        return {'files': None, 'eligible': False, 'candidate_tokens': None, 'token_cap': token_cap,
                'ineligibility_reasons': ['invalid_json_or_file_mapping'], 'parse_error': str(exc)[:200]}
    processed = postprocess(source, files, strategy_id, counter)
    result = validate_candidate(source, processed['files'], counter, token_cap)
    count = counter.count_file
    # Tokens the editor cannot shorten: restored front matter and the mandatory source code blocks.
    fixed = sum(count(split_front_matter(text)[0]) + sum(count(block.text) for block in select_anchors(text, strategy_id))
                for text in source.files.values())
    return {'files': processed['files'], 'eligible': result['eligible'], 'candidate_tokens': result['candidate_tokens'],
            'token_cap': token_cap, 'ineligibility_reasons': result['ineligibility_reasons'],
            'anchor_insertions': len(processed['anchor_insertions']), 'fixed_tokens': fixed,
            'per_file_tokens': {path: count(text) for path, text in sorted(processed['files'].items())}}


def problem_line(candidate, current_files=None):
    """What to tell the editor in a re-ask, or None if the candidate is acceptable."""
    reasons = candidate['ineligibility_reasons']
    if candidate['files'] is None:
        return ('Your answer could not be used: it was not one JSON object with a "files" mapping that contains exactly '
                'the required paths with text values. Return the complete corrected JSON object.')
    if 'over_cap' in reasons:
        total, cap, fixed = candidate['candidate_tokens'], candidate['token_cap'], candidate['fixed_tokens']
        over = total - cap
        # The editor cannot count tokens, so the re-ask gives it a concrete target below the cap (TARGET_MARGIN).
        free_now, free_target = max(total - fixed, 1), max(int((cap - fixed) * TARGET_MARGIN), 0)
        cut = max(1, round(100 * (1 - free_target / free_now)))
        files = '; '.join(f'{path}: {n}' for path, n in candidate['per_file_tokens'].items())
        return (f'After post-processing your answer is {total} tokens; the cap is {cap}. Remove at least {over} tokens. '
                f'Tokens per file now: {files}. The front matter and the mandatory code blocks take about {fixed} tokens '
                f'and cannot be shortened, so the rest of your text must shrink from about {free_now} to at most '
                f'{free_target} tokens: cut it by about {cut}%, to roughly {int(free_target * 0.7)} words in total across '
                'all files. Delete or merge whole sections and examples; do not return the same text again. Return every '
                'file in full, in the same JSON format, with the front matter unchanged.')
    if reasons:
        return ('Your answer is structurally invalid (' + ', '.join(reasons) + '). Fix this and return every file in full, '
                'in the same JSON format. Keep the front matter unchanged.')
    if current_files is not None and candidate['files'] == current_files:
        return ('Your answer is identical to the current text. Produce a version that differs from it, within the token '
                'cap, and list the changes you made.')
    return None


def with_reask(messages, previous_raw_text, line):
    return list(messages) + [{'role': 'assistant', 'content': previous_raw_text}, {'role': 'user', 'content': line}]


def run_loop(call, messages, source, strategy_id, counter, token_cap, *, current_files=None, first_raw=None):
    """Budget and copy loop. call(messages, round) -> raw text. Returns the last candidate plus a per-round log.

    The loop never cuts text itself. The copy re-ask is allowed once; budget/validity re-asks share
    MAX_BUDGET_REASKS. first_raw lets a caller supply an already-saved first answer instead of calling.
    """
    log, copy_reasked, budget_reasks, round_index = [], False, 0, 0
    raw = first_raw if first_raw is not None else call(messages, 0)
    while True:
        candidate = finish(source, raw, strategy_id, counter, token_cap)
        line = problem_line(candidate, current_files)
        is_copy = line is not None and line.startswith('Your answer is identical')
        log.append({'round': round_index, 'tokens': candidate['candidate_tokens'], 'eligible': candidate['eligible'],
                    'reasons': candidate['ineligibility_reasons'], 'copy_of_current': is_copy,
                    'reused_saved_answer': first_raw is not None and round_index == 0})
        if line is None:
            break
        if is_copy:
            if copy_reasked:
                break
            copy_reasked = True
        else:
            if budget_reasks == MAX_BUDGET_REASKS:
                break
            budget_reasks += 1
        messages = with_reask(messages, raw, line)
        round_index += 1
        raw = call(messages, round_index)
    candidate['accepted'] = candidate['eligible'] and not (current_files is not None and candidate['files'] == current_files)
    candidate['rounds'] = log
    candidate['raw_text'] = raw
    return candidate
