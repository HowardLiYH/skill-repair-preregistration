"""Evaluator-only reference fixture (never a solver input): writes the verifier's own expected answers to the output
path, to confirm the native verifier accepts a correct output (as the first study's G2 fixtures did)."""
import json, sys
import ast
tree = ast.parse(open('/tests/test_outputs.py', encoding='utf-8').read())
env = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        try: env[node.targets[0].id] = eval(compile(ast.Expression(node.value), 'v', 'eval'), {'__builtins__': {}}, dict(env))
        except Exception: pass
json.dump({q: {'answer': list(v), 'tokens': 0} for q, v in env['EXPECTED_ANSWERS'].items()},
          open(env.get('ANSWER_PATH', '/root/answer.json'), 'w'), ensure_ascii=False)
