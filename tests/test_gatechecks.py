"""gatechecks: screenshots must render (7a); entry needs staging to serve the head (6)."""
import urllib.error

from tangentswarm import gatechecks as G

REPO = 'owner/repo'
ATTACH = 'https://github.com/user-attachments/assets/0c5b998d-7a0c-4e80-bc87-a74779e4169f'
BLOB = f'https://github.com/{REPO}/blob/swarm-shots/shots/pr1-a.png?raw=true'
RAW = f'https://raw.githubusercontent.com/{REPO}/swarm-shots/shots/pr1-b.png'


def never(*a):
    raise AssertionError('must not be called')


def test_image_urls_reads_markdown_and_img_tags_in_order():
    body = f'x ![a]({ATTACH}) <img width=3 src="{RAW}"> ![b](<{BLOB}> "t") ![a]({ATTACH})'
    assert G.image_urls(body) == [ATTACH, RAW, BLOB]


def test_a_relative_name_fails_like_platform_479():
    ok, why = G.check_images('![x](pr479-attr-gui.png)', REPO, exists=never, probe=never)
    assert not ok and 'not an absolute https URL' in why


def test_no_image_fails():
    assert G.check_images('just text, a .png word', REPO, exists=never, probe=never) == (False, 'no image in body')


def test_user_attachments_pass_on_form_without_a_fetch():
    assert G.check_images(f'![a]({ATTACH})', REPO, exists=never, probe=never)[0]
    ok, why = G.check_images('![a](https://github.com/user-attachments/assets/x)', REPO,
                             exists=never, probe=lambda u: (False, 'probe: 404'))
    assert not ok and 'probe: 404' in why        # not the GitHub-issued form: it is probed


def test_swarm_shots_urls_must_exist_on_the_branch():
    seen = []
    ok, _ = G.check_images(f'![a]({BLOB}) ![b]({RAW})', REPO, exists=lambda p: seen.append(p) or True, probe=never)
    assert ok and seen == ['shots/pr1-a.png', 'shots/pr1-b.png']
    ok, why = G.check_images(f'![a]({BLOB})', REPO, exists=lambda p: False, probe=never)
    assert not ok and 'not on the swarm-shots branch' in why
    assert G.shots_path(f'https://github.com/{REPO}/raw/swarm-shots/s/x.png', REPO) == 's/x.png'
    assert G.shots_path(f'https://github.com/other/repo/raw/swarm-shots/s/x.png', REPO) is None


def test_other_https_hosts_must_load_as_an_image():
    class R:
        def __init__(self, kind, status=200):
            self.headers, self.status = {'Content-Type': kind}, status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    assert G.probe_image('https://x/y.png', opener=lambda req, timeout: R('image/png'))[0]
    ok, why = G.probe_image('https://x/y.png', opener=lambda req, timeout: R('text/html'))
    assert not ok and 'not an image' in why

    def gone(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, 'nf', {}, None)
    ok, why = G.probe_image('https://x/y.png', opener=gone)
    assert not ok and '404' in why


def test_served_check_is_strict_on_entry_and_lenient_on_a_regate():
    web = ['web/a.ts', 'README.md']
    assert G.check_served(['README.md'], True, None, never)[0]
    assert G.check_served(web, True, 'abc12345', lambda s: True)[0]
    ok, why = G.check_served(web, True, 'abc12345', lambda s: False)
    assert not ok and 'LACKS the head' in why
    ok, why = G.check_served(web, False, 'abc12345', lambda s: False)
    assert ok and why.startswith('lenient re-gate')
    assert not G.check_served(['api/x.py'], True, None, never, err='cannot read')[0]
    assert G.check_served(['app/x'], False, None, never, err='cannot read')[0]
