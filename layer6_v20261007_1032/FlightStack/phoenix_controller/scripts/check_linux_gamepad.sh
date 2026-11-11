#!/usr/bin/env bash
set -u
echo "== USB =="
lsusb | grep -Ei 'Logitech|F310' || true
echo
echo "== /dev/input =="
ls -l /dev/input/js* /dev/input/event* 2>/dev/null || true
echo
echo "== Suggested Debian packages =="
echo "sudo apt install joystick evtest"
echo "Then: jstest /dev/input/js0"
