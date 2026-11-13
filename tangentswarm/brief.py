"""swarm-brief: put the coordinator's brief for a PR into that PR's body.

The coordinator writes one brief per worker PR to BRIEF_DIR/<PR>.txt. A draft PR that
is scaffolded with only "Fixes #N" in its body hides the brief from reviewers. This
module keeps the brief in a marked section at the end of the PR body:

    <!-- swarm-brief:start -->
    <details><summary>Brief</summary>
    ...
    </details>
    <!-- swarm-brief:end -->

The markers are the contract. Gate checks that read the body (hr-gate: screenshots,
where-to-see line, served commit, code-only) must ignore this section, because a brief
can name those things before the work exists; use strip_brief() for that.

swarm-tick calls missing_briefs() and write_brief() on each pass. The CLI does the same
by hand:

    swarm-brief N [N...]       write the brief file for each PR into its body
    swarm-brief N --file F     copy F to BRIEF_DIR/N.txt first, then write it
    swarm-brief --check N...   only report which PRs lack the section
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

BRIEF_DIR = Path(os.environ.get('SWARM_BRIEF_DIR', str(Path.home() / 'var' / 'briefs')))
REPO = os.environ.get('SWARM_BRIEF_REPO', 'tangentcode/platform')
START = '<!-- swarm-brief:start -->'
END = '<!-- swarm-brief:end -->'
SECTION_RE = re.compile(re.escape(START) + r'.*?' + re.escape(END) + r'\n?', re.S)


def has_brief(body):
    return bool(SECTION_RE.search(body or ''))


def strip_brief(body):
    """The body without the brief section (for gate checks that read the body)."""
    return SECTION_RE.sub('', body or '')


def section(text):
    text = (text or '').replace(START, '').replace(END, '').strip()
    return f"{START}\n<details><summary>Brief</summary>\n\n{text}\n\n</details>\n{END}\n"


def with_brief(body, text):
    """The body with the brief section added at the end, or replaced if it is there."""
    body = body or ''
    if has_brief(body):
        return SECTION_RE.sub(lambda _: section(text), body, count=1)
    return body.rstrip() + ('\n\n' if body.strip() else '') + section(text)


def brief_path(n):
    return BRIEF_DIR / f"{int(n)}.txt"


def read_brief(n):
    """The brief text for PR n, or None if there is no non-empty brief file."""
    try:
        text = brief_path(n).read_text()
    except OSError:
        return None
    return text if text.strip() else None


def missing_briefs(prs):
    """[(n, has_file)] for PRs whose body has no brief section. prs: {n: {'body': ...}}."""
    return [(n, read_brief(n) is not None) for n, pr in sorted(prs.items()) if not has_brief(pr.get('body'))]


def _gh(*args, input=None):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, input=input, timeout=120)
    if r.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}...: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def live_body(n, repo=REPO):
    return json.loads(_gh('pr', 'view', str(n), '-R', repo, '--json', 'body'))['body'] or ''


def patch_body(n, body, repo=REPO):
    """Replace the PR body (gh api PATCH; gh pr edit fails on project-linked PRs)."""
    _gh('api', '-X', 'PATCH', f"repos/{repo}/pulls/{int(n)}", '--input', '-', input=json.dumps({'body': body}))


def write_brief(n, repo=REPO):
    """Write the brief file into PR n's live body. Returns 'wrote', 'present' or 'no-file'."""
    text = read_brief(n)
    if text is None:
        return 'no-file'
    body = live_body(n, repo)
    if has_brief(body):
        return 'present'
    patch_body(n, with_brief(body, text), repo)
    if not has_brief(live_body(n, repo)):
        raise RuntimeError(f"#{n}: the brief section is not in the body after the edit")
    return 'wrote'


def main(argv=None):
    ap = argparse.ArgumentParser(prog='swarm-brief', description=__doc__.split('\n', 1)[0])
    ap.add_argument('prs', nargs='+', type=int)
    ap.add_argument('--file', help='brief to save as BRIEF_DIR/<PR>.txt first (one PR only)')
    ap.add_argument('--check', action='store_true', help='report only; change nothing')
    ap.add_argument('-R', '--repo', default=REPO)
    a = ap.parse_args(argv)
    if a.file:
        if len(a.prs) != 1:
            ap.error('--file takes exactly one PR')
        text = Path(a.file).read_text()
        if not text.strip():
            ap.error(f'{a.file} is empty')
        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        brief_path(a.prs[0]).write_text(text)
    bad = 0
    for n in a.prs:
        try:
            if a.check:
                ok = has_brief(live_body(n, a.repo))
                print(f"#{n}: {'brief present' if ok else 'no brief in body'}"
                      + ('' if ok or read_brief(n) else f' (no file {brief_path(n)})'))
                bad += not ok
                continue
            what = write_brief(n, a.repo)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            print(f"#{n}: error: {e}", file=sys.stderr)
            bad += 1
            continue
        print(f"#{n}: " + {'wrote': 'wrote the brief into the body', 'present': 'brief already in the body',
                           'no-file': f'no brief file {brief_path(n)}'}[what])
        bad += what == 'no-file'
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
