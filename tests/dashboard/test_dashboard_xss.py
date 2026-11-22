"""
XSS regression tests for dashboard/static/js/dashboard.js.

Static part: a small JS lexer finds every html`` template and checks that each
${...} in it is escapeHtml(...), a numeric formatter, or a *Html / *Cls
variable; that every .innerHTML assignment uses a builder/fragment; and that
no untagged template literal builds markup.

Behavioral part (needs node): dashboard_xss_harness.js renders cards from
hostile Manager data and validates the resulting markup.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
JS_PATH = REPO / 'dashboard' / 'static' / 'js' / 'dashboard.js'
HARNESS = Path(__file__).resolve().parent / 'dashboard_xss_harness.js'

SAFE_CALLS = ('escapeHtml', 'safeFixed', 'fmtInt', 'fmtWidth', 'formatBytes')
REGEX_PRECEDERS = set('(,=:[!&|?{};+-*%<>~^')


class JsTemplateScanner:
    """Finds template literals (tag, literal text, interpolations, line)."""

    def __init__(self, src: str):
        self.src = src
        self.templates = []

    def run(self):
        self.scan_code(0)
        return self.templates

    def _line(self, i):
        return self.src.count('\n', 0, i) + 1

    def skip_string(self, i):
        quote, j = self.src[i], i + 1
        while self.src[j] != quote:
            j += 2 if self.src[j] == '\\' else 1
        return j + 1

    def skip_regex(self, i):
        j, in_class = i + 1, False
        while True:
            c = self.src[j]
            if c == '\\':
                j += 2
                continue
            if c == '[':
                in_class = True
            elif c == ']':
                in_class = False
            elif c == '/' and not in_class:
                break
            j += 1
        j += 1
        while j < len(self.src) and self.src[j].isalpha():
            j += 1
        return j

    def scan_template(self, i, tag):
        src, j = self.src, i + 1
        literal, exprs = [], []
        while src[j] != '`':
            if src[j] == '\\':
                literal.append(src[j:j + 2])
                j += 2
            elif src.startswith('${', j):
                end = self.scan_code(j + 2, end_on_brace=True)
                exprs.append(src[j + 2:end])
                j = end + 1
            else:
                literal.append(src[j])
                j += 1
        self.templates.append({'tag': tag, 'literal': ''.join(literal), 'exprs': exprs, 'line': self._line(i)})
        return j + 1

    def scan_code(self, i, end_on_brace=False):
        src, n = self.src, len(self.src)
        depth, last, lastword = 0, '', ''
        while i < n:
            c = src[i]
            if c in ' \t\r\n':
                i += 1
            elif src.startswith('//', i):
                nl = src.find('\n', i)
                i = n if nl == -1 else nl
            elif src.startswith('/*', i):
                i = src.index('*/', i) + 2
            elif c in '\'"':
                i, last, lastword = self.skip_string(i), 'a', ''
            elif c == '`':
                m = re.search(r'([A-Za-z_$][\w$]*)$', src[:i])
                i = self.scan_template(i, m.group(1) if m else '')
                last, lastword = 'a', ''
            elif c == '/' and (last == '' or last in REGEX_PRECEDERS or lastword in ('return', 'typeof', 'case')):
                i, last, lastword = self.skip_regex(i), 'a', ''
            elif c.isalnum() or c in '_$':
                j = i
                while j < n and (src[j].isalnum() or src[j] in '_$'):
                    j += 1
                lastword, last, i = src[i:j], 'a', j
            else:
                if end_on_brace:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        if depth == 0:
                            return i
                        depth -= 1
                lastword, last, i = '', c, i + 1
        return i


def _call_spans_whole_expr(expr: str, open_idx: int) -> bool:
    """True if the paren at open_idx closes at the very end of expr."""
    depth, i = 0, open_idx
    while i < len(expr):
        c = expr[i]
        if c in '\'"`':
            quote, i = c, i + 1
            while expr[i] != quote:
                i += 2 if expr[i] == '\\' else 1
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i == len(expr) - 1
        i += 1
    return False


def is_safe_interpolation(expr: str) -> bool:
    e = expr.strip()
    if re.fullmatch(r'[A-Za-z_$][\w$]*(Html|Cls)', e):
        return True
    m = re.match(r'(%s)\(' % '|'.join(SAFE_CALLS), e)
    return bool(m) and _call_spans_whole_expr(e, m.end() - 1)


@pytest.fixture(scope='module')
def js_source():
    return JS_PATH.read_text(encoding='utf-8')


@pytest.fixture(scope='module')
def templates(js_source):
    return JsTemplateScanner(js_source).run()


def test_scanner_sanity(templates):
    html_templates = [t for t in templates if t['tag'] == 'html']
    assert len(html_templates) >= 15
    assert any('${' not in t['literal'] and 'escapeHtml(systemName)' in t['exprs'] for t in html_templates)


def test_is_safe_interpolation_rules():
    assert is_safe_interpolation("escapeHtml(g.name || 'GPU ' + idx)")
    assert is_safe_interpolation('formatBytes(hasCap ? vols.used : null)')
    assert is_safe_interpolation('rowsHtml') and is_safe_interpolation('cpuCls')
    assert not is_safe_interpolation('escapeHtml(a) + b')
    assert not is_safe_interpolation('escapeHtml(a)(b)')
    assert not is_safe_interpolation('m.cpu_percent')
    assert not is_safe_interpolation('unitSuffix(u.cpu)')
    assert not is_safe_interpolation("x ? 'a' : 'b'")


def test_every_html_interpolation_is_escaped_or_numeric(templates):
    bad = [f"line {t['line']}: ${{{e}}}"
           for t in templates if t['tag'] == 'html'
           for e in t['exprs'] if not is_safe_interpolation(e)]
    assert not bad, 'unsafe interpolations in html`` templates:\n' + '\n'.join(bad)


def test_untagged_templates_build_no_markup(templates):
    bad = [f"line {t['line']}: `{t['literal'][:60]}`"
           for t in templates if t['tag'] != 'html' and re.search(r'<[A-Za-z/!]', t['literal'])]
    assert not bad, 'markup must be built with the html`` tag:\n' + '\n'.join(bad)


def test_innerhtml_assignments_use_builders(js_source):
    allowed = re.compile(
        r"\.innerHTML\s*=\s*(?:build\w*Html\(|[A-Za-z_$][\w$]*Html\s*;|html`|''\s*;|\"\"\s*;)")
    uses = [m.start() for m in re.finditer(r'\.innerHTML\b', js_source)]
    bad = [js_source[i:js_source.find('\n', i)] for i in uses if not allowed.match(js_source, i)]
    assert uses, 'expected innerHTML assignments for the card builders'
    assert not bad, 'innerHTML must be assigned from build*Html() / *Html / html``:\n' + '\n'.join(bad)
    for api in ('insertAdjacentHTML', 'outerHTML', 'document.write'):
        assert api not in js_source


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_hostile_data_renders_safely():
    result = subprocess.run(['node', str(HARNESS), str(JS_PATH)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith('OK')
