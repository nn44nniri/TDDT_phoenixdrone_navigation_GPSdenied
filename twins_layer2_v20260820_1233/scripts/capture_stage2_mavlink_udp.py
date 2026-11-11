#!/usr/bin/env python3
"""Passive MAVLink UDP datagram capture for Stage-2 trace provenance.

Never sends messages, heartbeats, commands, setpoints or ARM.  Raw MAVLink
frames are NOT paired native motor measurements. Use a dedicated MAVProxy
output, e.g. output add 127.0.0.1:14553, not the GUI's 14552 socket.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import socket
import time


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def capture(listen, seconds, path, *, sock=None):
    if not (isinstance(seconds,(float,int)) and math.isfinite(seconds) and 0 < seconds <= 3600):
        raise ValueError('seconds must be in (0,3600]')
    if path.exists():
        raise FileExistsError('output already exists: refusing to overwrite evidence')
    path.parent.mkdir(parents=True, exist_ok=True)
    owns_sock = sock is None
    if sock is None:
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(listen)
    sock.settimeout(min(.25,seconds))
    count=0
    start=time.monotonic()
    try:
        with path.open('x',encoding='utf-8') as out:
            while time.monotonic()-start < seconds:
                try:
                    data, remote=sock.recvfrom(65535)
                except socket.timeout:
                    continue
                if not data:
                    continue
                row={'kind':'raw_mavlink_udp','timestamp_utc':utcnow(),
                     'monotonic_receive_s':time.monotonic(),
                     'source_ip':remote[0], 'source_port':remote[1],
                     'payload_length':len(data),'payload_sha256':hashlib.sha256(data).hexdigest(),
                     'payload_base64':base64.b64encode(data).decode('ascii')}
                out.write(json.dumps(row,sort_keys=True)+'\n')
                out.flush()
                count+=1
    finally:
        if owns_sock:
            sock.close()
    return count


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--listen',default='127.0.0.1:14553',help='dedicated passive UDP endpoint, NOT 14552')
    ap.add_argument('--seconds',type=float,default=30)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--report',type=Path)
    args=ap.parse_args()
    report={'test':'L2_STAGE2_MAVLINK_CAPTURE','status':'BLOCKED_EXTERNAL',
            'flight_authorized':False,'native_controller_prediction_certified':False,
            'evidence_scope':'RAW_MAVLINK_ONLY_NOT_NATIVE_MOTOR_PAIRS'}
    try:
        host,port=args.listen.rsplit(':',1)
        port=int(port)
        if not host in ('127.0.0.1','localhost') or not 1<=port<=65535:
            raise ValueError('only local loopback receiver supported')
        count=capture(('127.0.0.1',port),args.seconds,args.output)
        report.update(packets=count,output=str(args.output),
                      status='RAW_CAPTURE_ONLY' if count else 'BLOCKED_EXTERNAL')
        if count==0: report['reason']='no packets received: check dedicated MAVProxy output'
    except (ValueError,OSError) as e:
        report['reason']=str(e)
    print(json.dumps(report,indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,indent=2)+'\n')
    return 0 if report['status']=='RAW_CAPTURE_ONLY' else 2


if __name__=='__main__':
    raise SystemExit(main())
