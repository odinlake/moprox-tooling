# audible — the audiobook shelf as data

Daily sync of the Audible library into `private-data/audible/library.json`: every title with authors,
narrators, series, publisher, runtime, genres and the **synopsis**.

```
audible_bootstrap.py   ONCE, interactively, by a human   -> ~/.config/claude-dev/audible/auth.json
audible_fetch.py       daily, unattended (timer)         -> private-data/audible/library.json
```

## Why two scripts

There is no public Audible API. The community client (`audible`, mkb79) speaks the same internal API
the mobile app uses, and getting in means **device registration**: OpenID auth-code + PKCE, which on
first login can demand a CAPTCHA, a 2FA code, a CVF code by email/SMS, or an emailed approval. None
of that is automatable, so it is quarantined in the bootstrap script and run once by a human.

What survives is a refresh token. Access tokens last 60 minutes and are renewed from it, so the daily
fetcher runs unattended indefinitely and **never sees the password**. Same shape as webscout: log in
out-of-band, let the always-on job replay.

## Running it

```sh
V=~/.local/share/moprox/audible-venv
$V/bin/python audible_bootstrap.py     # once, in a REAL terminal (see below)
$V/bin/python audible_fetch.py         # any time; the timer does it at 03:30 daily
```

Run the bootstrap over ssh, **not** through the Claude session's `!` escape — the CAPTCHA url and
prompts would land in a conversation transcript. The password is prompted for, used once and
discarded; keep it in Vaultwarden. Only `auth.json` persists, 0600, outside every repo.

`AUDIBLE_LOCALE` sets the marketplace (default `uk`). It is a marketplace, not a language: the wrong
value logs in fine and returns an **empty library**, which is a much more confusing failure than an
error. `AUDIBLE_DIR` moves the auth directory.

## Its own venv

`audible` is not packaged in Debian and Debian 13 refuses pip into the system interpreter (PEP 668),
so this lane uses `~/.local/share/moprox/audible-venv`. The sibling fetchers use `/usr/bin/python3`
only because their dependencies happen to be apt-installable.

## What is stored

One lean record per title — asin, title, subtitle, authors, narrators, series (+sequence), publisher,
release and purchase dates, runtime, language, genres, synopsis, cover url, finished/percent. The raw
API payload is roughly ten times larger and almost all of it is plumbing.

`/1.0/library` returns **almost nothing** unless you name `response_groups`; `product_desc` is the one
carrying the synopsis and the easiest to leave out by accident. Synopses arrive as HTML and are
stored as text, so consumers do not each have to strip tags.

Written atomically via a temp file and rename, so a reader never sees a half-written catalogue.

## Scope

Metadata only. The same credential **can** download and decrypt the audio — the access is not
read-only, and it would be a mistake to assume otherwise. This lane deliberately does not, and
nothing here should grow that ability by accident.

## Fragility

The API is undocumented and reverse-engineered. `audible` is actively maintained (0.12.0), but Amazon
can change it, and when they do this lane breaks. It has no freshness check yet: a stale
`library.json` looks exactly like a library nobody added to. Same gap as technogym on 3–4 Aug.
