#!/usr/bin/env python3
"""One-time Audible device registration. Run INTERACTIVELY on claude-dev, then never again.

  ~/.local/share/moprox/audible-venv/bin/python audible_bootstrap.py

RUN IT IN A REAL TERMINAL (ssh), not through the Claude session's `!` escape: the CAPTCHA url and
any prompts would otherwise be captured into a conversation transcript.

WHY A SEPARATE SCRIPT FROM THE FETCHER
  Audible's login is OpenID auth-code + PKCE ending in a DEVICE REGISTRATION, and the first login can
  demand a CAPTCHA, a 2FA code, a CVF code by email/SMS, or an emailed approval. None of that can be
  automated, so it is deliberately quarantined here. What comes out is a refresh token; the daily
  fetcher renews 60-minute access tokens from it forever and never sees the password. Same shape as
  webscout: login out-of-band, the always-on job only replays.

THE PASSWORD IS NEVER WRITTEN TO DISK. It is prompted for, used once, and discarded — keep it in
Vaultwarden and paste it here. Only auth.json (the device registration) persists, 0600.
"""
import getpass
import os
import pathlib
import stat
import sys

import audible

AUTH_DIR = pathlib.Path(os.environ.get("AUDIBLE_DIR", pathlib.Path.home() / ".config/claude-dev/audible"))
AUTH_FILE = AUTH_DIR / "auth.json"
# Marketplace, NOT interface language. Wrong value = a login that succeeds and a library that is
# empty, which is a far more confusing failure than an error would be.
LOCALE = os.environ.get("AUDIBLE_LOCALE", "uk")


def captcha(url: str) -> str:
    # The library's default renders the image with Pillow, which needs a display. On a headless box
    # print the url instead and let the operator open it.
    print("\n  CAPTCHA required. Open this in a browser:\n    " + url)
    return input("  Answer: ").strip()


def otp() -> str:
    return input("  2FA / OTP code: ").strip()


def cvf() -> str:
    print("\n  Amazon sent a verification code by email or SMS.")
    return input("  CVF code: ").strip()


def approval():
    print("\n  Amazon sent an approval email. Click the link in it, THEN press Enter here.")
    input("  ...")


def main():
    if AUTH_FILE.exists():
        print(f"{AUTH_FILE} already exists — device already registered.")
        print("Delete it and re-run only if you actually want to re-register this device.")
        return 1

    print(f"Audible device registration · marketplace '{LOCALE}'")
    print("(set AUDIBLE_LOCALE=us|de|fr|ca|it|au|in|jp|es|br if that is wrong)\n")
    username = input("Amazon/Audible email: ").strip()
    password = getpass.getpass("Password (not echoed, not stored): ")

    try:
        auth = audible.Authenticator.from_login(
            username, password, locale=LOCALE,
            captcha_callback=captcha, otp_callback=otp,
            cvf_callback=cvf, approval_callback=approval)
    except Exception as exc:                       # noqa: BLE001 — surface whatever Amazon said
        print(f"\nregistration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        del password

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_DIR.chmod(stat.S_IRWXU)                   # 0700
    auth.to_file(AUTH_FILE, encryption=False)      # encrypting it would need a passphrase at every run
    AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 0600

    print(f"\nregistered. auth written to {AUTH_FILE} (0600)")
    print("The daily fetcher now runs unattended. You will not need the password again.")
    print("\nThis file IS a credential — it can read your library and, if asked, download it.")
    print("It is outside every git repo on purpose. Keep it that way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
