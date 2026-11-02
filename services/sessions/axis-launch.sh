#!/bin/sh
# moprox-axis-launch: run the "MX Axis Mundi" Remote Control server under its own machine name.
#
# Started as root by moprox-axis.service (ExecStart=+...), already inside a private UTS namespace
# (unshare --uts). The app lists a Remote Control server by its hostname, so this sets the name the
# operator wants to see, then drops to mikael for good. Only this process tree sees the name; the
# host, its journal and every other unit keep "claude-dev". socket.sethostname because hostname(1)
# refuses spaces. A script file rather than an inline sh -c: systemd's own quoting ate the nested
# quotes of the inline version and the unit exited 1.
set -eu
python3 -c 'import socket; socket.sethostname("MX Axis Mundi")'
exec setpriv --reuid=mikael --regid=mikael --init-groups -- \
  /usr/bin/script -qfec "/home/mikael/.local/bin/claude remote-control --name 'MX Axis Mundi' --remote-control-session-name-prefix MX --spawn same-dir --capacity 8 --permission-mode bypassPermissions" \
  /home/mikael/.local/state/moprox-dev/axis.log
