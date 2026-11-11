#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, select, signal, socket, time, os
from .executor import DigitalPilotExecutor

RUN=True
def _stop(*_):
    global RUN; RUN=False

def main():
    p=argparse.ArgumentParser(description='TDDT Layer-6 Layer5 decision intake service')
    p.add_argument('--listen',default='127.0.0.1:14661')
    p.add_argument('--gui',default='127.0.0.1:14660')
    p.add_argument('--rate-hz',type=float,default=30.0)
    a=p.parse_args()
    lh,lp=a.listen.rsplit(':',1); gh,gp=a.gui.rsplit(':',1)
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind((lh,int(lp))); s.setblocking(False)
    from .virtual_pilot import VirtualPilotClient
    ex=DigitalPilotExecutor(VirtualPilotClient(gh,int(gp)),a.rate_hz)
    from .cancellation import CancelJournal,OP_SCHEMA
    cancellations=CancelJournal()
    signal.signal(signal.SIGINT,_stop); signal.signal(signal.SIGTERM,_stop)
    print(f'[L6] intake udp://{lh}:{lp} -> GUI udp://{gh}:{gp} @ {a.rate_hz:g} Hz',flush=True)
    while RUN:
        period=1.0/max(1.0,ex.rate_hz)
        ready,_,_=select.select([s],[],[],period)
        if ready:
            while True:
                try:data,peer=s.recvfrom(65535)
                except BlockingIOError:break
                obj={}
                try:
                    obj=json.loads(data.decode('utf-8'))
                    if not isinstance(obj,dict):raise ValueError('object packet required')
                    native_id=os.environ.get('TDDT_NATIVE_SESSION_ID')
                    if native_id and obj.get('native_session_id')!=native_id:raise ValueError('native_session_id mismatch or missing; stale boot request rejected')
                    obj.pop('native_session_id',None)
                    if obj.get('type')=='cancel':
                        reply=cancellations.handle(obj,peer,ex)
                        if native_id:reply['native_session_id']=native_id
                        s.sendto(json.dumps(reply).encode(),peer)
                        print(f"[L6] cancel target={reply.get('transaction_id')} confirmed={reply['cancelled']}",flush=True)
                        continue
                    requested_rate=obj.get('execution_rate_hz')
                    if requested_rate is not None:
                        requested_rate=float(requested_rate)
                        if requested_rate <= 0 or requested_rate > 200:
                            raise ValueError('execution_rate_hz must be in (0,200]')
                        ex.rate_hz=requested_rate
                    env=ex.accept(obj)
                    reply={'ok':True,'transaction_id':env.transaction_id,'execution_rate_hz':ex.rate_hz,'scope':'INTAKE_ONLY',
                           'intake_request_id':obj.get('intake_request_id'),'command_instance_id':ex.active_instance_id,
                           'execution_segment':env.execution_segment}
                    print(f'[L6] accepted {env.transaction_id} u={env.command.as_gui_state()} mode={env.command.mode}',flush=True)
                except Exception as exc:
                    reply={'ok':False,'error':str(exc),'transaction_id':obj.get('transaction_id') if isinstance(obj,dict) else None,
                           'scope':'INTAKE_ONLY','intake_request_id':obj.get('intake_request_id') if isinstance(obj,dict) else None}; print(f'[L6] REJECT {exc}',flush=True)
                    if isinstance(obj,dict) and obj.get('type')=='cancel':reply.update(type='cancel_ack',requested_transaction_id=obj.get('transaction_id'),cancelled=False,scope='CANCELLATION_REJECTED',
                        operation_schema=OP_SCHEMA,cancel_session_id=obj.get('cancel_session_id'),cancel_sequence=obj.get('cancel_sequence'))
                if os.environ.get('TDDT_NATIVE_SESSION_ID'):reply['native_session_id']=os.environ['TDDT_NATIVE_SESSION_ID']
                try:s.sendto(json.dumps(reply).encode(),peer)
                except OSError:pass
        ex.tick()
    ex.client.release(); s.close(); print('[L6] service stopped',flush=True)
if __name__=='__main__': main()
