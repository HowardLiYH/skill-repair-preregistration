"""Evaluator-only reference fixture (never a solver input): a mechanically constructed regulated verse that follows
one of the verifier's own tonal patterns with unambiguous modern tones and one rhyme; it checks the verifier accepts a
well-formed poem and makes no claim about poetic quality."""
PING = '天山风云花春秋来青门'
ZE = '月雨柳夜路树水草叶酒'
RHYME = '乡光霜阳'
PATTERNS = ['平平仄仄平平仄', '仄仄平平仄仄平', '仄仄平平平仄仄', '平平仄仄仄平平',
            '平平仄仄平平仄', '仄仄平平仄仄平', '仄仄平平平仄仄', '平平仄仄仄平平']
lines, r = [], 0
for i, pattern in enumerate(PATTERNS, 1):
    chars = []
    for j, mark in enumerate(pattern):
        if j == 6 and i % 2 == 0:
            chars.append(RHYME[r]); r += 1
        else:
            chars.append((PING if mark == '平' else ZE)[(i + j) % 10])
    lines.append(''.join(chars))
open('/root/poem.txt', 'w', encoding='utf-8').write('归乡\n' + '\n'.join(lines) + '\n')
