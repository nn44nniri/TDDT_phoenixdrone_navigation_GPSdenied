from __future__ import annotations
import json, socket, time, math, uuid, os
from .contracts import PilotCommand
from .telemetry_history import validate_query,SCHEMA

class VirtualPilotClient:
    """Bidirectional localhost IPC into the GUI. The GUI remains the sole MAVLink executor."""
    def __init__(self, host='127.0.0.1', port=14660):
        self.addr=(host,int(port))
        self.sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1',0))
        self.seq=0
    def _send(self,obj):
        self.seq+=1
        if obj.get('type') in ('state','action','cancel','release'):
            if not getattr(self,'control_session_id',None):self.control_session_id=uuid.uuid4().hex
            obj={**obj,'control_session_id':self.control_session_id}
            if os.environ.get('TDDT_NATIVE_SESSION_ID'):obj={**obj,'native_session_id':os.environ['TDDT_NATIVE_SESSION_ID']}
        obj={"seq":self.seq,"sent_monotonic":time.monotonic(),**obj}
        self.sock.sendto(json.dumps(obj,separators=(',',':')).encode(),self.addr)
        return self.seq
    def state(self, cmd: PilotCommand, ttl_s=0.25, transaction_id=None, *, command_instance_id=None, execution_segment=None, admission_deadline=None):
        from .command_correlation import identity
        if transaction_id is None:
            if not getattr(self,'_direct_transaction_id',None):self._direct_transaction_id='virtual-'+uuid.uuid4().hex
            transaction_id=self._direct_transaction_id
        command_id=identity(transaction_id,cmd.as_gui_state(),cmd.mode,cmd.discrete,execution_segment)
        previous=getattr(self,'_state_identity',None)
        if command_instance_id is None:
            command_instance_id=previous[1] if previous is not None and previous[0]==command_id else uuid.uuid4().hex
        self._state_identity=(command_id,command_instance_id)
        self._last_state_target={'transaction_id':transaction_id,'command_instance_id':command_instance_id}
        return self._send({"type":"state","source":"TDDT-L5","transaction_id":transaction_id,
                    **cmd.as_gui_state(),"ttl_s":float(ttl_s),'command_id':command_id,
                    'command_instance_id':command_instance_id,'command_mode':cmd.mode,'command_discrete':list(cmd.discrete),
                    **({'execution_segment':execution_segment} if execution_segment is not None else {}),
                    **({'admission_deadline_unix':admission_deadline} if admission_deadline is not None else {})})
    def segment_status(self, *, transaction_id, command_instance_id, timeout=.8):
        from .segments import STATUS_SCHEMA
        from .cancellation import token
        from .command_correlation import valid_tx
        if not valid_tx(transaction_id) or not token(command_instance_id):raise ValueError('invalid segment query')
        if not isinstance(timeout,(int,float)) or isinstance(timeout,bool) or not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid segment timeout')
        request_id=uuid.uuid4().hex;peer=(socket.gethostbyname(self.addr[0]),self.addr[1]);end=time.monotonic()+timeout;old=self.sock.gettimeout()
        try:
            self._send({'type':'segment_status_request','request_id':request_id,'transaction_id':transaction_id,'command_instance_id':command_instance_id})
            while time.monotonic()<end:
                self.sock.settimeout(max(.000001,end-time.monotonic()))
                data,addr=self.sock.recvfrom(65535)
                if addr!=peer:continue
                try:reply=json.loads(data.decode())
                except (ValueError,UnicodeError):continue
                if (isinstance(reply,dict) and reply.get('schema')==STATUS_SCHEMA and reply.get('request_id')==request_id
                    and reply.get('transaction_id')==transaction_id and reply.get('command_instance_id')==command_instance_id):return reply
        except socket.timeout:return None
        finally:self.sock.settimeout(old)
        return None
    def segment_notices(self):
        from .segments import NOTICE_SCHEMA
        peer=(socket.gethostbyname(self.addr[0]),self.addr[1]);old=self.sock.gettimeout();found=[]
        try:
            self.sock.setblocking(False)
            while True:
                try:data,addr=self.sock.recvfrom(65535)
                except BlockingIOError:break
                if addr!=peer:continue
                try:r=json.loads(data.decode())
                except (ValueError,UnicodeError):continue
                if isinstance(r,dict) and r.get('schema')==NOTICE_SCHEMA and r.get('control_session_id')==getattr(self,'control_session_id',None) and r.get('latch_closed') is True:found.append(r)
        finally:self.sock.settimeout(old)
        return found
    def action(self, action, transaction_id=None, *, command_instance_id=None):
        previous=getattr(self,'_state_identity',None)
        command_instance_id=command_instance_id or (previous[1] if previous else uuid.uuid4().hex)
        return self._send({"type":"action","source":"TDDT-L5","transaction_id":transaction_id or 'direct-action',
                          'command_instance_id':command_instance_id,"action":str(action)})
    def cancel(self, *, transaction_id, command_instance_id, reason='TDOC_HOLD', timeout=.6):
        from .cancellation import SCHEMA,token,reason_text
        from .command_correlation import valid_tx
        if not valid_tx(transaction_id) or not token(command_instance_id) or not reason_text(reason):raise ValueError('invalid targeted cancellation')
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid cancel timeout')
        request_id=uuid.uuid4().hex;peer=(socket.gethostbyname(self.addr[0]),self.addr[1]);deadline=time.monotonic()+timeout;old=self.sock.gettimeout()
        try:
            self._send({'type':'cancel','source':'TDDT-L6-CANCEL','request_id':request_id,'transaction_id':transaction_id,
                'command_instance_id':command_instance_id,'reason':reason})
            while time.monotonic()<deadline:
                self.sock.settimeout(max(.000001,deadline-time.monotonic()))
                data,addr=self.sock.recvfrom(65535)
                if addr!=peer:continue
                try:reply=json.loads(data.decode())
                except (ValueError,UnicodeError):continue
                if (isinstance(reply,dict) and reply.get('type')=='cancel_ack' and reply.get('schema')==SCHEMA
                    and reply.get('request_id')==request_id and reply.get('transaction_id')==transaction_id
                    and reply.get('command_instance_id')==command_instance_id
                    and reply.get('control_session_id')==self.control_session_id and token(reply.get('gui_session_id'))):
                    if reply.get('ok') is True and reply.get('cancelled') is True:self._state_identity=None
                    return reply
        except socket.timeout:return None
        finally:self.sock.settimeout(old)
        return None
    def connect_mavlink(self):
        return self._send({"type":"connect_mavlink","source":"TDDT-L6-DEMO"})
    def release(self):
        self._state_identity=None
        return self._send({"type":"release","source":"TDDT-L5",**getattr(self,'_last_state_target',{})})
    def status(self, timeout=0.8, *, include_imu_samples=False):
        """Ask GUI for its current MAVLink/vehicle execution state."""
        from .status import SCHEMA as STATUS_SCHEMA,status_error
        if type(include_imu_samples) is not bool:raise ValueError('invalid_include_imu_samples')
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:raise ValueError('invalid_timeout')
        request_id=uuid.uuid4().hex;peer=(socket.gethostbyname(self.addr[0]),self.addr[1]);deadline=time.monotonic()+timeout
        self._send({"type":"status_request","source":"TDDT-L6-READ","request_id":request_id,
                    "status_schema":STATUS_SCHEMA,"include_imu_samples":include_imu_samples})
        old=self.sock.gettimeout()
        try:
            while time.monotonic()<deadline:
                self.sock.settimeout(max(.000001,deadline-time.monotonic()))
                data,addr=self.sock.recvfrom(65535)
                if addr!=peer:continue
                try:msg=json.loads(data.decode('utf-8'))
                except (ValueError,UnicodeError):continue
                if not isinstance(msg,dict) or msg.get('type')!='status' or msg.get('request_id')!=request_id:continue
                if msg.get('schema')!=STATUS_SCHEMA or msg.get('schema_version')!=1:
                    return status_error('status_schema_mismatch',request_id,expected_schema=STATUS_SCHEMA,
                        actual_schema=str(msg.get('schema'))[:80],expected_version=1,actual_version=msg.get('schema_version'))
                return msg
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old)
        return None
    def wait_for(self, predicate, timeout=10.0, interval=0.15):
        end=time.monotonic()+float(timeout); last=None
        while time.monotonic()<end:
            last=self.status(timeout=min(0.5,max(0.05,end-time.monotonic())))
            if last is not None and predicate(last): return last
            time.sleep(float(interval))
        return last

    def telemetry_history(self, *, after_seq=0, limit=16, channels=None,
                          session_id=None, through_seq=None, timeout=0.8):
        query=validate_query(after_seq=after_seq,limit=limit,channels=channels,
            session_id=session_id,through_seq=through_seq)
        if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
            raise ValueError('invalid_timeout')
        request_id=uuid.uuid4().hex
        peer=(socket.gethostbyname(self.addr[0]),self.addr[1]);deadline=time.monotonic()+timeout
        old=self.sock.gettimeout()
        try:
            self._send({'type':'telemetry_history_request','source':'TDDT-L6-READ',
                        'request_id':request_id,**query})
            while time.monotonic()<deadline:
                self.sock.settimeout(max(.000001,deadline-time.monotonic()))
                data,addr=self.sock.recvfrom(65535)
                if addr!=peer:continue
                try:message=json.loads(data.decode('utf-8'))
                except (ValueError,UnicodeError):continue
                if (isinstance(message,dict) and message.get('type')=='telemetry_history'
                    and message.get('schema')==SCHEMA and message.get('request_id')==request_id):return message
        except socket.timeout:return None
        finally:self.sock.settimeout(old)
        return None
