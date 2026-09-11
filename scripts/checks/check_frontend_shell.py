"""The Mini App shell: three tabs, no manual copy, no orphaned navigation.

These are static assertions over the shipped bundle. They exist because the
interface is the only place an operator sees what the engine is doing, and the
two ways it silently lies are a dead nav target (a tab that renders nothing)
and a page key with no renderer (which falls back to the terminal and looks
like the tap did not register).
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / 'webapp' / 'static'
JS = (STATIC / 'product.js').read_text(encoding='utf-8')
MODEL = (STATIC / 'product-model.js').read_text(encoding='utf-8')
CSS = (STATIC / 'product.css').read_text(encoding='utf-8')
HTML = (STATIC / 'index.html').read_text(encoding='utf-8')


def ok(label, cond, detail=''):
    if not cond: raise AssertionError(label + ' :: ' + str(detail))
    print('  ok  ' + label)


print('three tabs, and every one of them renders')
tabs = re.search(r"const TABS=\[(.+?)\];", JS)
ok('TABS is declared', tabs is not None)
names = re.findall(r"\['([a-z]+)','([a-z]+)'\]", tabs.group(1))
ok('exactly three tabs', len(names) == 3, names)
ok('they are terminal / engine / journal',
   [n for n, _ in names] == ['terminal', 'engine', 'journal'], names)

router = re.search(r"\(\{([a-zA-Z:,]+)\}\[S\.page\]\|\|terminal\)\(\)", JS)
ok('the page router is present', router is not None)
routed = {entry.split(':')[0] for entry in router.group(1).split(',')}
for name, _ in names:
    ok('tab %-9s has a renderer' % name, name in routed, routed)
ok('the fallback page is the terminal', '||terminal)()' in JS)

print('every navigation target is a real page')
targets = set(re.findall(r'data-nav="([a-z]+)"', JS))
ok('no nav button points at a missing page', targets <= routed | {'settings'},
   targets - (routed | {'settings'}))
ok('settings is reachable', 'settings' in targets and 'settings' in routed)

print('the icon each tab asks for exists')
icons = set(re.findall(r"([a-z_]+):'<path", JS)) | set(re.findall(r"([a-z_]+):'<circle", JS))
for name, glyph in names:
    ok('icon %-9s is defined' % glyph, glyph in icons or ("%s:'<" % glyph) in JS, sorted(icons))

print('manual leader copy is gone from the interface')
for token in ('function manual(', 'manualConfirm', 'saveManualDraft', 'saveManual(',
              'acceptManualConfig', 'manualEvidenceMessage', 'clearManualCache',
              'data-manual-start', 'data-change-leader', 'manual-wallet',
              '/api/manual-copy'):
    ok('no %s' % token, token not in JS, token)
ok('no manual page key in the router', 'manual' not in routed, routed)
ok('no manual draft state', 'manualDraft' not in JS and 'manualDirty' not in JS)

print('the mode switch really switches the source of the numbers')
ok('LIVE reads the exchange account', "m==='LIVE'" in MODEL and 'account?.portfolio?.positions' in MODEL)
ok('every other mode reads that runtime', 'runtime(s,m)?.episodes' in MODEL)
ok('LIVE_AUTO is a mode the model knows', "'LIVE_AUTO'" in MODEL)
ok('the switch is a real control', 'data-mode="${esc(m)}"' in JS and 'b.dataset.mode' in JS)

print('refusals and attenuation stay separate')
ok('blockers refuse', "t('Отменили сделку: ','Refused by: ')" in JS)
ok('attenuation only shrinks', "t('Уменьшили размер: ','Sized down by: ')" in JS)
ok('the refusal tally is read, never written',
   "read('/api/autonomy?limit=50')" in JS and 'method:' not in JS.split("read('/api/autonomy")[1][:80])

print('the LIVE AUTO alarm cannot be hidden')
ok('the banner is driven by the snapshot flag', 'S.snapshot?.live_auto===true' in JS)
ok('a non-boolean flag is rejected as an invalid snapshot',
   "typeof s.live_auto!=='boolean'" in JS)

print('styles and cache busting are consistent')
classes = set(re.findall(r'class="(w-[a-z-]+)', JS)) | set(re.findall(r'class="w-[a-z-]+ (w-[a-z-]+)', JS))
missing = sorted(c for c in classes if '.' + c not in CSS)
ok('every new class is styled', not missing, missing)
for asset in ('product.css', 'product-model.js', 'product.js'):
    stamp = re.search(re.escape(asset) + r'\?v=([a-f0-9]{16})', HTML)
    ok('%s is cache-busted' % asset, stamp is not None)
    import hashlib
    want = hashlib.sha256((STATIC / asset).read_bytes()).hexdigest()[:16]
    ok('%s stamp matches its bytes' % asset, stamp.group(1) == want,
       '%s != %s' % (stamp.group(1), want))

print('ALL OK')
