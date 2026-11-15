"""Connect ReelRecon to your Instagram account.

Instagram hides profile listings from logged-out requests, so ReelRecon
borrows the session of a browser you are already logged in to. The session
is checked against Instagram, then saved (owner-only permissions) to
~/.reelrecon/instagram_cookies.txt, where every ReelRecon run picks it up.

    npx -y reelrecon login                  # pick a browser, import, verify
    npx -y reelrecon login --browser firefox
    npx -y reelrecon login --cookies-file ~/Downloads/cookies.txt
    npx -y reelrecon login --status
    npx -y reelrecon login --logout
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path
from typing import Optional

from ig_transcriber import pipeline

BROWSERS = ("chrome", "safari", "firefox", "edge", "brave", "arc", "chromium", "opera", "vivaldi")
# Arc stores Chrome-format cookies but yt-dlp has no "arc" name; read it as chromium with Arc's profile dir.
_ARC_PROFILE = "~/Library/Application Support/Arc/User Data/Default"
SESSION_COOKIE = "sessionid"
VERIFY_USERNAME = "instagram"


class LoginError(RuntimeError):
    pass


def _say(message: str = "") -> None:
    # Keep all output on stderr, matching the launcher (stdout can belong to an MCP client).
    print(message, file=sys.stderr)


def instagram_cookies(jar: CookieJar) -> list[Cookie]:
    return [cookie for cookie in jar if "instagram.com" in (cookie.domain or "")]


def has_session(cookies: list[Cookie]) -> bool:
    return any(cookie.name == SESSION_COOKIE and cookie.value for cookie in cookies)


class _QuietLogger:
    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass
    def progress_bar(self): return None


def read_browser_cookies(browser: str, profile: Optional[str] = None) -> list[Cookie]:
    from yt_dlp.cookies import extract_cookies_from_browser

    name = browser.lower()
    if name == "arc":
        name, profile = "chromium", profile or str(Path(_ARC_PROFILE).expanduser())
    try:
        jar = extract_cookies_from_browser(name, profile, logger=_QuietLogger())
    except Exception as exc:
        hint = ""
        if name == "safari":
            hint = " Safari's cookies need Full Disk Access for your terminal (System Settings > Privacy & Security)."
        raise LoginError(f"Could not read {browser} cookies: {exc}.{hint}") from exc
    return instagram_cookies(jar)


def read_cookies_file(path: Path) -> list[Cookie]:
    jar = MozillaCookieJar()
    try:
        jar.load(str(path.expanduser()), ignore_discard=True, ignore_expires=True)
    except (OSError, ValueError) as exc:
        raise LoginError(f"Could not read {path}: {exc}. It must be a Netscape-format cookies.txt export.") from exc
    return instagram_cookies(jar)


def save_session(cookies: list[Cookie], destination: Optional[Path] = None) -> Path:
    if not has_session(cookies):
        raise LoginError("No Instagram login found (no 'sessionid' cookie). Log in at instagram.com in that browser first.")
    destination = destination or pipeline.saved_session_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only before any secret is written to it.
    os.close(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    os.chmod(destination, 0o600)
    jar = MozillaCookieJar(str(destination))
    for cookie in cookies:
        jar.set_cookie(cookie)
    jar.save(ignore_discard=True, ignore_expires=True)
    return destination


def verify_session() -> None:
    """Load one profile page with the saved session; raise LoginError unless Instagram sees you as logged in."""
    try:
        pipeline.fetch_profile_page(VERIFY_USERNAME, pipeline._instagram_session_cookies())
    except pipeline.PipelineError as exc:
        raise LoginError(f"Instagram did not accept the session: {exc}") from exc


def _choose_browser() -> str:
    if not sys.stdin.isatty():
        return "chrome"
    _say("Which browser are you logged in to Instagram with?")
    for index, name in enumerate(BROWSERS, start=1):
        _say(f"  {index}. {name}")
    answer = input("Number or name [1]: ").strip().lower() or "1"
    if answer.isdigit() and 1 <= int(answer) <= len(BROWSERS):
        return BROWSERS[int(answer) - 1]
    if answer in BROWSERS:
        return answer
    raise LoginError(f"Unknown browser: {answer}")


def cmd_status() -> int:
    source = pipeline.session_source()
    if source is None:
        _say("Not connected to Instagram. Run: npx -y reelrecon login")
        return 1
    _say({
        "saved_login": f"Using the saved login at {pipeline.saved_session_path()}",
        "env_cookies_file": "Using REELRECON_COOKIES_FILE",
        "env_browser": "Using REELRECON_COOKIES_FROM_BROWSER (reels only; run `reelrecon login` for profile scans)",
    }[source])
    if source == "env_browser":
        return 0
    try:
        verify_session()
    except LoginError as exc:
        _say(f"But it no longer works: {exc}\nRun `npx -y reelrecon login` again.")
        return 1
    _say("Session works.")
    return 0


def cmd_logout() -> int:
    path = pipeline.saved_session_path()
    if path.exists():
        path.unlink()
        _say(f"Removed the saved Instagram session at {path}.")
    else:
        _say("No saved Instagram session to remove.")
    return 0


def cmd_login(browser: Optional[str], profile: Optional[str], cookies_file: Optional[str], open_browser: bool) -> int:
    if cookies_file:
        cookies = read_cookies_file(Path(cookies_file))
    else:
        browser = browser or _choose_browser()
        if open_browser and sys.stdin.isatty():
            _say(f"\nOpening instagram.com. Log in there in {browser} if you aren't already, then come back here.")
            webbrowser.open("https://www.instagram.com/accounts/login/")
            input("Press Enter once you're logged in... ")
        _say(f"Reading your Instagram login from {browser} (your OS may ask to allow access to its saved data)...")
        cookies = read_browser_cookies(browser, profile)

    path = save_session(cookies)
    _say("Checking the session with Instagram...")
    try:
        verify_session()
    except LoginError:
        path.unlink(missing_ok=True)
        raise
    _say(f"\nConnected. Saved to {path} (readable only by you). Every ReelRecon run, including MCP agents, uses it.")
    _say("This file is a live login to your account: don't share it. Remove it with `npx -y reelrecon login --logout`.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="reelrecon login", description="Connect ReelRecon to your Instagram account.")
    parser.add_argument("--browser", choices=BROWSERS, help="Browser you're logged in to Instagram with (asked if omitted).")
    parser.add_argument("--profile", help="Browser profile name or path, if not the default one.")
    parser.add_argument("--cookies-file", help="Import a cookies.txt export instead of reading a browser.")
    parser.add_argument("--no-open", action="store_true", help="Don't open instagram.com before importing.")
    parser.add_argument("--status", action="store_true", help="Show whether a working session is set up.")
    parser.add_argument("--logout", action="store_true", help="Delete the saved session.")
    args = parser.parse_args(argv)

    try:
        if args.status:
            return cmd_status()
        if args.logout:
            return cmd_logout()
        return cmd_login(args.browser, args.profile, args.cookies_file, open_browser=not args.no_open)
    except LoginError as exc:
        _say(f"\nLogin failed: {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        _say("\nCancelled.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
