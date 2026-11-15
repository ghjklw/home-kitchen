#!/usr/bin/env bash
set -euo pipefail
image=${1:?Usage: verify.sh IMAGE}

docker run --rm -i --memory=6g --memory-swap=8g --cpus=4 \
  --user 999:999 --cap-drop ALL --security-opt no-new-privileges=true \
  --read-only --network none \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,uid=999,gid=999,mode=0700 \
  --tmpfs /cache:rw,nosuid,nodev,noexec,uid=999,gid=999,mode=0700 \
  --entrypoint /bin/sh "$image" -es <<'SH'
test "$(id -u)" = 999
test "$(id -g)" = 999
test "$HOME" = /tmp
find /usr -maxdepth 0 | head -n 1
python3 --version
borg --version
borgmatic --version
ssh -V
# Offline expansion checks that OpenSSH can resolve the numeric runtime user.
ssh -G localhost > /tmp/ssh-config
mkdir -p "$BORG_CACHE_DIR"
touch "$BORG_CACHE_DIR/write-test" "$HOME/write-test"
python3 - <<'PY'
import os
import pathlib
import pwd
import stat

assert pwd.getpwuid(999)
assert pathlib.Path('/proc/self/status').read_text().split('NoNewPrivs:')[1].split()[0] == '1'
assert pathlib.Path('/proc/self/status').read_text().split('Seccomp:')[1].split()[0] == '2'
assert int(pathlib.Path('/proc/self/status').read_text().split('CapEff:')[1].split()[0], 16) == 0
assert any(line.split()[1] == '/' and 'ro' in line.split()[3].split(',')
           for line in pathlib.Path('/proc/mounts').read_text().splitlines())

def check(path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return
    assert info.st_uid == 0 and info.st_gid == 0, path
    assert not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID), path
    assert not os.access(path, os.W_OK), path
    assert os.access(path, os.R_OK), path
    if path.is_dir():
        assert os.access(path, os.X_OK), path

def fail(error):
    raise error

check(pathlib.Path('/usr'))
for root, dirs, files in os.walk('/usr', onerror=fail):
    for name in dirs + files:
        check(pathlib.Path(root, name))
for name in ('/etc/passwd', '/etc/group'):
    check(pathlib.Path(name))
PY
SH
