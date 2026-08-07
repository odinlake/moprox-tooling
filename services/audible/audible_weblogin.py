#!/usr/bin/env python3
"""Browser-based Audible device registration, in two steps — for when there is no terminal to hand.

  audible_weblogin.py start              -> prints a URL to open in any browser
  audible_weblogin.py finish "<url>"     -> completes registration from the redirect url

WHY THIS EXISTS ALONGSIDE audible_bootstrap.py
  The bootstrap script drives the login itself and so has to handle CAPTCHA, 2FA, CVF and approval
  prompts on a headless box — fine over ssh, useless from a phone. This flow hands all of that back
  to Amazon's own login page in a real browser, where those challenges are designed to be answered.
  Nothing is typed at a terminal; the operator only opens a link and copies the address they land on.

HOW THE TWO STEPS STAY CONNECTED
  This is OAuth auth-code + PKCE. `start` mints a code_verifier and a device serial and stashes them;
  the URL carries only the derived challenge. `finish` needs the SAME verifier to redeem the code,
  which is exactly what PKCE is for — an intercepted code is worthless without it. Hence the state
  file, 0600, deleted on success.

WHAT THE REDIRECT URL CONTAINS
  A single-use authorization code that expires in minutes and is dead the moment it is redeemed. It
  is not the password and not the refresh token. Still worth redeeming promptly rather than leaving
  it lying in a chat log.
"""
import json
import os
import pathlib
import stat
import sys

from urllib.parse import parse_qs

import audible
import httpx
from audible.localization import Locale
from audible.login import build_oauth_url, create_code_verifier
from audible.register import register as register_device

DIR = pathlib.Path(os.environ.get("AUDIBLE_DIR", pathlib.Path.home() / ".config/claude-dev/audible"))
AUTH_FILE = DIR / "auth.json"
PENDING = DIR / "pending.json"
LOCALE = os.environ.get("AUDIBLE_LOCALE", "uk")


def _dir():
    DIR.mkdir(parents=True, exist_ok=True)
    DIR.chmod(stat.S_IRWXU)
    return DIR


def start():
    if AUTH_FILE.exists():
        return f"already registered ({AUTH_FILE}) — delete it first to re-register"
    loc = Locale(LOCALE)
    verifier = create_code_verifier()
    url, serial = build_oauth_url(
        country_code=loc.country_code, domain=loc.domain,
        market_place_id=loc.market_place_id, code_verifier=verifier,
        serial=None, with_username=False)
    _dir()
    PENDING.write_text(json.dumps({"verifier": verifier.decode(), "serial": serial, "locale": LOCALE}))
    PENDING.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return url


def finish(response_url):
    if not PENDING.exists():
        return "no pending login — run `start` first"
    st = json.loads(PENDING.read_text())
    loc = Locale(st["locale"])
    # Same extraction external_login does, rather than a bespoke parse — the code lives in the
    # openid.oa2.authorization_code query parameter of the maplanding redirect.
    params = parse_qs(httpx.URL(response_url).query.decode())
    codes = params.get("openid.oa2.authorization_code") or []
    if not codes:
        return ("no authorization code in that url. Copy the WHOLE address you landed on — it "
                "should contain 'openid.oa2.authorization_code='. If the browser shows a blank or "
                "error page, that is expected: the address bar is the payload, not the page.")
    reg = register_device(authorization_code=codes[0], code_verifier=st["verifier"].encode(),
                          domain=loc.domain, serial=st["serial"], with_username=False)
    auth = audible.Authenticator.from_dict(reg, locale=loc)
    _dir()
    auth.to_file(AUTH_FILE, encryption=False)
    AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    PENDING.unlink(missing_ok=True)                # the verifier has done its job
    return f"registered — auth written to {AUTH_FILE} (0600)"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "start"
    if cmd == "start":
        print(start())
    elif cmd == "finish":
        if len(sys.argv) < 3:
            sys.exit('usage: audible_weblogin.py finish "<redirect url>"')
        print(finish(sys.argv[2]))
    else:
        sys.exit("usage: audible_weblogin.py [start|finish <url>]")
