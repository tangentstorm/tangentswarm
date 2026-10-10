"""Read the claude.ai/code sidebar and act on single sessions (port of scialect src/sessions.mts).

A session's status is one of running, awaiting, ready, ci, ci-pass, ci-fail, idle or unknown.
"""
import re

from .browser import CHAT_INPUT

SESSION_ROW = '[data-row-key^="code:session_"]'
STATUSES = ['running', 'awaiting', 'ready', 'ci', 'ci-pass', 'ci-fail', 'idle', 'unknown']

# Read all rows in one evaluate() call, because round trips dominate with 100+ rows.
_LIST_JS = r"""(rowSel) => {
  const rows = Array.from(document.querySelectorAll(rowSel));
  return rows.map((row) => {
    const text = (row.innerText ?? '').trim();
    const name = text.split('\n')[0]?.trim() ?? text;
    const rowKey = row.getAttribute('data-row-key') ?? '';
    const fromKey = rowKey.startsWith('code:') ? rowKey.slice('code:'.length) : '';
    const anchor = row.querySelector('a[href*="/code/"]');
    const href = anchor?.getAttribute('href') ?? '';
    const fromHref = href ? (href.split('/code/')[1] ?? '').split(/[?#]/)[0] : '';
    const slug = fromHref || fromKey;
    const kindEl = row.querySelector('[data-kind]');
    const kind = kindEl?.getAttribute('data-kind') ?? null;
    const indicator = row.querySelector('[role="status"], [role="img"]');
    const indicatorLabel = indicator?.getAttribute('aria-label') ?? null;
    const ariaLabels = Array.from(row.querySelectorAll('[aria-label]'))
      .map((el) => el.getAttribute('aria-label') ?? '').filter(Boolean);
    return { text, name, slug, kind, indicatorLabel, ariaLabels };
  });
}"""

_LATEST_JS = r"""() => {
  const transcript = document.querySelector('[data-testid="epitaxy-virtual-transcript"]');
  if (transcript) {
    const bodies = transcript.querySelectorAll('.epitaxy-markdown');
    if (bodies.length > 0) {
      const last = bodies[bodies.length - 1];
      const txt = (last.innerText ?? last.textContent ?? '').trim();
      if (txt) return txt;
    }
  }
  for (const sel of ['.font-claude-message', '.font-claude-response']) {
    const els = document.querySelectorAll(sel);
    if (els.length > 0) {
      const last = els[els.length - 1];
      const txt = (last.innerText ?? last.textContent ?? '').trim();
      if (txt) return txt;
    }
  }
  return null;
}"""


def classify_status(kind, indicator_label, signals):
    """Classify a sidebar row (sessions.mts#classifyStatus)."""
    if kind in ('ready', 'awaiting', 'running'):
        return kind
    labels = {'Idle': 'idle', 'Running': 'running', 'Awaiting input': 'awaiting', 'Ready': 'ready'}
    if indicator_label in labels:
        return labels[indicator_label]
    blob = ' | '.join(signals).lower()
    if re.search(r'·\s*merged|·\s*closed', blob):
        return 'idle'
    if re.search(r'·\s*open', blob):
        return 'ci'
    if re.search(r'(ci\s*pass|checks? pass)', blob):
        return 'ci-pass'
    if re.search(r'(ci\s*fail|checks? fail)', blob):
        return 'ci-fail'
    if re.search(r'(ci|checks? running|pending)', blob):
        return 'ci'
    return 'unknown'


def summarize_rows(raw):
    """Convert the raw evaluate() rows to SessionSummary dicts."""
    out = []
    for r in raw:
        if not r.get('text'):
            continue
        signals = [r['text']]
        if r.get('kind'):
            signals.append(f"kind={r['kind']}")
        if r.get('indicatorLabel'):
            signals.append(f"indicator={r['indicatorLabel']}")
        signals += r.get('ariaLabels') or []
        out.append({'name': r['name'], 'slug': r.get('slug') or '',
                    'status': classify_status(r.get('kind'), r.get('indicatorLabel'), signals),
                    'pinned': False, 'rawSignals': signals})
    return out


async def list_sessions(page):
    await page.wait_for_selector(CHAT_INPUT, timeout=30_000)
    return summarize_rows(await page.evaluate(_LIST_JS, SESSION_ROW))


async def open_session(page, session_name):
    """Click a sidebar entry by exact visible name."""
    escaped = session_name.replace('"', '\\"')
    await page.locator(f'text="{escaped}"').first.click()
    await page.wait_for_selector(CHAT_INPUT, timeout=15_000)


async def send_message(page, message):
    prompt = page.locator(CHAT_INPUT)
    await prompt.fill(message)
    await prompt.press('Enter')


async def get_latest_response(page):
    """Return the text of the most recent transcript message from any author, or None."""
    return await page.evaluate(_LATEST_JS)


async def get_session_status(page, session_name):
    for s in await list_sessions(page):
        if s['name'] == session_name:
            return s['status']
    return 'unknown'
