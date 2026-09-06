#!/usr/bin/env python3
"""creds_expired()'s fail-open rule, which is the whole safety property.

    python3 services/loop/tests/test_creds.py

It gates every cycle before the strike counter, so a false positive stops the estate's two
continuous agents on a file it merely failed to parse. Only a timestamp it actually read, that is
actually in the past, may return True.
"""
import json, os, sys, tempfile, shutil, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import loop

tmp = Path(tempfile.mkdtemp(prefix="creds-"))
ok = []


def case(name, content, want):
    p = tmp / "c.json"
    if content is None:
        p.unlink(missing_ok=True)
    elif isinstance(content, str):
        p.write_text(content)
    else:
        p.write_text(json.dumps(content))
    os.environ["CLAUDE_CREDS"] = str(p)
    dead, why = loop.creds_expired()
    good = dead is want
    print(f"{'PASS' if good else 'FAIL'}  {name}: dead={dead} {why}")
    ok.append(good)


now_ms = time.time() * 1000
case("missing file fails open", None, False)
case("unparseable file fails open", "{not json", False)
case("wrong shape fails open", {"somethingElse": 1}, False)
case("oauth present, no expiry field, fails open", {"claudeAiOauth": {"accessToken": "x"}}, False)
case("expiry as a bool is not epoch 1", {"claudeAiOauth": {"refreshTokenExpiresAt": True}}, False)
case("expiry in the future is alive", {"claudeAiOauth": {"refreshTokenExpiresAt": now_ms + 86400e3}}, False)
case("expiry in the past is dead", {"claudeAiOauth": {"refreshTokenExpiresAt": now_ms - 86400e3}}, True)
# the exact shape the 2026-09-04 outage left on claude-loop: emptied tokens, expiry in the past
case("the real outage shape", {"claudeAiOauth": {
    "accessToken": "", "refreshToken": "", "expiresAt": 0,
    "refreshTokenExpiresAt": 1788540963611, "subscriptionType": "max"}}, True)

# and a check that a missing creds-check.py does not take the loop down with it
loop.CREDS_CHECK = tmp / "not-here.py"
dead, _ = loop.creds_expired()
ok.append(dead is False)
print(f"{'PASS' if ok[-1] else 'FAIL'}  a missing creds-check.py fails open too")

print("\n%d/%d passed" % (sum(ok), len(ok)))
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(0 if all(ok) else 1)
