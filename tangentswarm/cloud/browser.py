"""Persistent-context Playwright launcher (port of scialect src/browser.mts).

The Chromium profile (with the claude.ai login cookie) lives OUTSIDE the
repo, by default in ~/.local/share/tangentswarm/playwright-profile
(override: TANGENTSWARM_PROFILE_DIR).  Credentials are never stored in the
repo.  Optionally TANGENTSWARM_STORAGE_STATE may point at a Playwright
storage-state JSON file (outside the repo) whose cookies are loaded into
the context at launch.

First login is manual, exactly as in scialect: `swarm cloud login` opens a
headed browser at claude.ai/code; a human signs in, and the cookie stays in
the profile.  Shut down cleanly (Ctrl-C, not SIGKILL) so it is flushed.
"""
import asyncio
import json
import os
import sys
from dataclasses import dataclass

from .. import paths

CLAUDE_CODE_URL = 'https://claude.ai/code'
CHAT_INPUT = 'div[contenteditable="true"]'


def default_profile_dir():
    return os.environ.get('TANGENTSWARM_PROFILE_DIR') or str(paths.data_dir() / 'playwright-profile')


class NotLoggedIn(RuntimeError):
    pass


class BotChallenge(NotLoggedIn):
    """Cloudflare's "Just a moment..." interstitial is blocking the page."""


@dataclass
class BrowserHandle:
    playwright: object
    context: object
    page: object

    async def close(self):
        try:
            await self.context.close()
        finally:
            await self.playwright.stop()


async def launch_browser(profile_dir=None, headed=True, slow_mo=0, channel=None, args=None,
                         storage_state=None):
    """Launch Chromium with a persistent profile. Returns a BrowserHandle."""
    from playwright.async_api import async_playwright

    profile_dir = profile_dir or default_profile_dir()
    paths.ensure_private_dir(profile_dir)
    pw = await async_playwright().start()
    kwargs = dict(headless=not headed, slow_mo=slow_mo or 0,
                  viewport={'width': 1400, 'height': 900},
                  args=['--disable-blink-features=AutomationControlled'] + list(args or []))
    if channel:
        kwargs['channel'] = channel
    try:
        context = await pw.chromium.launch_persistent_context(profile_dir, **kwargs)
    except Exception:
        await pw.stop()
        raise
    state_file = storage_state or os.environ.get('TANGENTSWARM_STORAGE_STATE')
    if state_file:
        with open(os.path.expanduser(state_file)) as f:
            cookies = json.load(f).get('cookies', [])
        if cookies:
            await context.add_cookies(cookies)
    page = context.pages[0] if context.pages else await context.new_page()
    return BrowserHandle(pw, context, page)


async def is_logged_in(page):
    if '/login' in page.url:
        return False
    if await page.locator('a[href="/login"]').count() > 0:
        return False
    return await page.locator(CHAT_INPUT).count() > 0


async def goto_claude_code(page, timeout_ms=30_000):
    """Open claude.ai/code and wait for the app shell. Raises NotLoggedIn."""
    await page.goto(CLAUDE_CODE_URL, wait_until='domcontentloaded', timeout=timeout_ms)
    try:
        await page.wait_for_load_state('networkidle', timeout=timeout_ms)
    except Exception:
        pass
    if '/login' in page.url or await page.locator('a[href="/login"]').count() > 0:
        raise NotLoggedIn(f"Not logged in. Run `swarm cloud login`, sign in at {CLAUDE_CODE_URL} "
                          "in the browser window, then retry.")
    try:
        await page.wait_for_selector(CHAT_INPUT, timeout=timeout_ms)
    except Exception as e:
        title = ''
        try:
            title = await page.title()
        except Exception:
            pass
        if 'just a moment' in title.lower():
            raise BotChallenge("claude.ai is showing a Cloudflare bot check. Run `swarm cloud login` "
                               "(headed) and complete it by hand; headless browsers on server IPs "
                               "are often challenged.") from e
        raise NotLoggedIn(f"claude.ai/code did not show the chat input (url={page.url!r}, "
                          f"title={title!r}). Run `swarm cloud login` and retry.") from e


async def login(profile_dir=None, timeout=900, poll=2.0, channel=None, out=sys.stderr):
    """Manual first login: open a HEADED browser at claude.ai/code and wait
    (up to `timeout` s) for a human to finish signing in, then close cleanly
    so the cookie is flushed to the profile.  Returns True if logged in."""
    handle = await launch_browser(profile_dir, headed=True, channel=channel)
    try:
        await handle.page.goto(CLAUDE_CODE_URL, wait_until='domcontentloaded')
        print(f"Browser open (profile: {profile_dir or default_profile_dir()}).\n"
              f"Sign in to {CLAUDE_CODE_URL} in that window; this command finishes by itself "
              "once the Claude Code chat box appears (Ctrl-C to give up).", file=out, flush=True)
        waited = 0.0
        while waited < timeout:
            try:
                if await is_logged_in(handle.page):
                    print('Logged in; the session cookie is saved in the profile.', file=out)
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll)
            waited += poll
        print('Timed out waiting for login.', file=out)
        return False
    finally:
        await handle.close()
