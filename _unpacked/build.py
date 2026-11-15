"""Reconstruye ../index.html a partir de template.html (mismo bundle, solo cambia la plantilla)."""
import re, json, subprocess, pathlib
root = pathlib.Path(__file__).resolve().parent
idx = root.parent / 'index.html'
src = subprocess.run(['git', 'show', 'HEAD:index.html'], cwd=root.parent, capture_output=True).stdout.decode('utf-8')
tpl = (root / 'template.html').read_text(encoding='utf-8')
enc = json.dumps(tpl, ensure_ascii=False).replace('<' + chr(47), '<' + chr(92) + 'u002F')
pat = re.compile(r'(<script type="__bundler/template">)(.*?)(</script>)', re.S)
assert pat.search(src)
out = pat.sub(lambda m: m.group(1) + enc + m.group(3), src, count=1)
idx.write_text(out, encoding='utf-8')
print('index.html reconstruido', len(out))
