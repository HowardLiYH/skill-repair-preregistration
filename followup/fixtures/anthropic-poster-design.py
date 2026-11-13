"""Evaluator-only reference fixture (never a solver input): renders a plain poster in the verifier's own expected
brand colours (pure Python PNG writer) and writes the metadata file the verifier reads."""
import ast, json, struct, zlib
tree = ast.parse(open('/tests/test_outputs.py', encoding='utf-8').read())
env = {}
for node in tree.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        try: env[node.targets[0].id] = eval(compile(ast.Expression(node.value), 'v', 'eval'), {'__builtins__': {}}, dict(env))
        except Exception: pass
hexes = env['EXPECTED_HEX_VALUES']
required = set(env.get('REQUIRED_KEYS', set(hexes)))
rgb = {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for k, v in hexes.items()}
background = next(k for k in hexes if 'background' in k)
others = [k for k in sorted(hexes) if k != background]
W, H = 600, 800
rows = []
for y in range(H):
    row = bytearray(b'\x00')
    for x in range(W):
        c = rgb[background]
        for n, k in enumerate(others):           # one restrained block per declared colour
            if 120 + 90 * n <= y < 170 + 90 * n and 60 <= x < (540 if n == 0 else 200): c = rgb[k]
        row += bytes(c)
    rows.append(bytes(row))
def chunk(tag, data): return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', W, H, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b''.join(rows))) + chunk(b'IEND', b'')
open(env['OUTPUT_IMAGE'], 'wb').write(png)
meta = {k: hexes[k] for k in hexes if k in required}
if 'applied_heading_font' in required:
    meta['applied_heading_font'] = env.get('EXPECTED_HEADING_FONT_SUBSTRING', 'Poppins') + ' SemiBold'
json.dump(meta, open(env['OUTPUT_META'], 'w'))
