"""Two hr-gate conditions that let a broken PR into Human Review (platform #479).

Condition 7a (screenshots). hr-gate's IMG_RE took any markdown image, so a bare relative
name such as ![x](pr479-attr-gui.png) passed. It never renders on GitHub. Now every image
in the body must be an absolute https URL that resolves:

  https://github.com/user-attachments/assets/<id>    issued by GitHub's own upload. In a
                                                      private repo only a signed-in reader
                                                      can open it, so the form is checked,
                                                      not fetched (no user token is used).
  swarm-shots branch of the PR's repo, as
    https://github.com/<repo>/blob/swarm-shots/<path>?raw=true
    https://github.com/<repo>/raw/swarm-shots/<path>
    https://raw.githubusercontent.com/<repo>/swarm-shots/<path>
                                                      the file must exist on swarm-shots
                                                      (contents API, app quota)
  any other https URL                                 GET answers 200 with an image/* type

Condition 6 (deployed + served). It was best-effort and always passed. For a PR that
touches served files (web/, app/, api/), ENTRY (into Staging or Human Review, and the
instant promote) now needs the static .deploy-sha to contain the head. A re-gate of a card
already in Human Review (a merge-only re-gate) stays lenient.
"""
import re
import urllib.request

SHOTS_BRANCH = 'swarm-shots'
SERVED_RE = re.compile(r'^(web|api|app)/')

_MD = re.compile(r'!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
_TAG = re.compile(r'<img\s[^>]*?src\s*=\s*["\']?([^"\'\s>]+)', re.I)
_ATTACH = re.compile(r'https://github\.com/user-attachments/assets/[0-9a-fA-F-]{16,}/?')


def image_urls(body):
    """Image URLs in a PR body (markdown and <img>), in order, without repeats."""
    found = sorted([(m.start(), m.group(1)) for m in _MD.finditer(body or '')] +
                   [(m.start(), m.group(1)) for m in _TAG.finditer(body or '')])
    out = []
    for _, u in found:
        if u not in out:
            out.append(u)
    return out


def shots_path(url, repo):
    """The file path if url is a swarm-shots URL of repo, else None."""
    r = re.escape(repo)
    b = re.escape(SHOTS_BRANCH)
    for pat in (rf'https://github\.com/{r}/blob/{b}/([^?#]+)\?raw=true$',
                rf'https://github\.com/{r}/raw/{b}/([^?#]+)$',
                rf'https://raw\.githubusercontent\.com/{r}/{b}/([^?#]+)$'):
        m = re.match(pat, url)
        if m:
            return m.group(1)
    return None


def probe_image(url, timeout=15, opener=urllib.request.urlopen):
    """(ok, why) for a public https image URL: GET answers 200 with an image/* content type."""
    req = urllib.request.Request(url, headers={'User-Agent': 'hr-gate', 'Range': 'bytes=0-0'})
    try:
        with opener(req, timeout=timeout) as r:
            kind = (r.headers.get('Content-Type') or '').split(';')[0].strip()
            code = getattr(r, 'status', 200)
    except Exception as e:  # HTTPError, URLError, timeouts
        return False, f"{url[:80]} does not load ({getattr(e, 'code', None) or type(e).__name__})"
    if code not in (200, 206) or not kind.startswith('image/'):
        return False, f"{url[:80]} is not an image ({code} {kind or 'no type'})"
    return True, ''


def check_images(body, repo, exists, probe=probe_image):
    """(ok, why) for condition 7a. exists(path) -> bool says if a file is on swarm-shots."""
    urls = image_urls(body)
    if not urls:
        return False, 'no image in body'
    bad = []
    for u in urls:
        if not u.lower().startswith('https://'):
            bad.append(f"{u[:60]} is not an absolute https URL (a relative name never renders)")
        elif _ATTACH.fullmatch(u):
            continue
        elif shots_path(u, repo) is not None:
            if not exists(shots_path(u, repo)):
                bad.append(f"{u[:80]} is not on the {SHOTS_BRANCH} branch")
        else:
            ok, why = probe(u)
            if not ok:
                bad.append(why)
    if bad:
        return False, f"{len(bad)} of {len(urls)} image(s) do not render: " + '; '.join(bad[:3])
    return True, f"{len(urls)} image(s), all absolute https and resolving"


def check_served(paths, entering, static_sha, has_head, err=None):
    """(ok, why) for condition 6. has_head(sha) -> bool: the deployed commit contains the head.
    entering: the card is not already in Human Review (strict). Else lenient (a re-gate)."""
    if not any(SERVED_RE.search(p) for p in paths):
        return True, 'n/a (no served files: web/, api/, app/)'
    if err or not static_sha:
        why = f"cannot read the static .deploy-sha ({err or 'empty'})"
        return (False, why) if entering else (True, 'lenient re-gate: ' + why)
    if has_head(static_sha):
        return True, f"static .deploy-sha {static_sha[:8]} contains the head"
    why = f"static .deploy-sha {static_sha[:8]} LACKS the head: staging does not serve it yet"
    return (False, why) if entering else (True, 'lenient re-gate: ' + why)
