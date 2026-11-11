"""Read-only Linux inotify evidence of actual resource opens during Native flight."""
import argparse
import ctypes
import json
from pathlib import Path
import select
import signal
import struct
import time

ap = argparse.ArgumentParser()
ap.add_argument('--data-dir', type=Path, required=True)
ap.add_argument('--output', type=Path, required=True)
args = ap.parse_args()
libc = ctypes.CDLL('libc.so.6', use_errno=True)
fd = libc.inotify_init1(0x800 | 0x80000)
if fd < 0:
    raise OSError(ctypes.get_errno(), 'inotify_init1')
wd = libc.inotify_add_watch(fd, str(args.data_dir.resolve()).encode(), 0x20)
if wd < 0:
    raise OSError(ctypes.get_errno(), 'inotify_add_watch')
active = True
def stop(*_):
    global active
    active = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
with args.output.open('w', buffering=1) as stream:
    stream.write(json.dumps({'kind': 'watch_started', 'data_dir': str(args.data_dir.resolve()),
                             'wall_time_s': time.time(), 'attribution': 'directory events; no PID attribution claimed'}) + '\n')
    while active:
        if not select.select([fd], [], [], .5)[0]:
            continue
        import os
        data = os.read(fd, 65536)
        cursor = 0
        while cursor < len(data):
            watch, mask, cookie, length = struct.unpack_from('iIII', data, cursor)
            name = data[cursor+16:cursor+16+length].rstrip(b'\0').decode()
            cursor += 16+length
            if name.endswith('.png'):
                stream.write(json.dumps({'kind': 'resource_open', 'filename': name,
                    'wall_time_s': time.time(), 'mask': mask}) + '\n')
