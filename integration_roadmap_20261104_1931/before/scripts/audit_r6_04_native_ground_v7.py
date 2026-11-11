#!/usr/bin/env python3
"""Real TDOC/three-terminal R6.04 ground reacceptance; no ARM or mission release."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

from tddt_tdoc.config import TDOCConfig
from tddt_tdoc.orchestrator import TDOCOrchestrator
from tddt_tdoc.layer6_bridge import Layer6Bridge, Layer6BridgeError
from layer6 import native_session
from layer6.command_correlation import verified_send_bounds
from layer6.status import validate_observation_status
from layer6.client import Layer6RemoteError
from pymavlink import mavutil


def write(path,value):
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False)+'\n')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',required=True,type=Path);args=p.parse_args()
    cfg=TDOCConfig.from_yaml('configs/tdoc.yaml');out=args.output_dir.resolve();out.relative_to(cfg.package_root/'reports/online');out.mkdir(exist_ok=False)
    policy={'scope':'sampled disarmed fourfoot ground quiescence only','groundspeed_max_m_s':.2,'velocity_norm_max_m_s':.2,
            'body_rate_max_rad_s':.1,'altitude_span_max_m':.1,'quaternion_drift_max_rad':.03,'motor_output_max':1000,
            'minimum_source_span_s':1.,'minimum_samples':4,'airborne_HOLD_qualified':False}
    write(out/'ACCEPTANCE_POLICY_BEFORE_RUN.json',policy)
    statuses=[];unavailable=[];cases=[];windows=[];session=None;paused=None;client=None;success=False
    with TDOCOrchestrator(cfg) as orch:
        adapter=orch.adapters[6]
        def status():
            deadline=time.monotonic()+2.
            while True:
                value=client.gui_status(timeout=1.,include_imu_samples=True)
                if value and value.get('reason')=='status_validation_busy' and time.monotonic()<deadline:
                    unavailable.append({'received_monotonic':time.monotonic(),'response':value})
                    time.sleep(.04);continue
                break
            assert value and value.get('ok') and adapter._fresh(value),value and value.get('reason')
            assert value['armed'] is False and validate_observation_status(value)['ok']
            statuses.append(value);return value
        def wait(predicate,timeout=4.):
            end=time.monotonic()+timeout
            while time.monotonic()<end:
                s=status()
                if predicate(s):return s
            raise AssertionError('native status predicate timeout')
        def probe(tx,deadline_s=.9,mode=None):
            packet={'transaction_id':tx,'u_cmd':[.12,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+deadline_s}
            if mode:packet['execution_intent']={'requested_pilot_mode':mode,'discrete':[]}
            ack=client.submit(packet);adapter._active_tx=tx
            s=wait(lambda v:v['applied_transaction_id']==tx and verified_send_bounds(v,tx) is not None)
            return packet,ack,s
        def ground_window(mode,case):
            values=[];violations=[];start=time.monotonic();wall=time.time()
            while time.monotonic()-start<1.4 or len(values)<4:
                v=status();assert v['mode']==mode
                if v['landed_state']!='ON_GROUND':violations.append('not_on_ground')
                if v['groundspeed_m_s']>policy['groundspeed_max_m_s']:violations.append('groundspeed')
                if math.sqrt(sum(v['velocity_'+a+'_m_s']**2 for a in 'ned'))>policy['velocity_norm_max_m_s']:violations.append('velocity')
                if max(abs(v['attitude'][a+'_rate']) for a in ('roll','pitch','yaw'))>policy['body_rate_max_rad_s']:violations.append('body_rate')
                if max(v['servo_output_raw']['servo'+str(i)+'_raw'] for i in (3,4))>policy['motor_output_max']:violations.append('motor_output')
                assert not v['digital_pilot_active'] and v['status_quality']['manual_output_inhibited']
                assert not v['status_quality']['physical_HOLD_verified']
                values.append(v);time.sleep(.12)
            span=values[-1]['plant_time_s']-values[0]['plant_time_s']
            alt=[v['altitude_msl_m'] for v in values];q=values[0]['attitude']['quaternion_wxyz']
            angles=[2*math.acos(min(1.,abs(sum(a*b for a,b in zip(q,v['attitude']['quaternion_wxyz']))))) for v in values]
            if span<policy['minimum_source_span_s']:violations.append('source_span')
            if max(alt)-min(alt)>policy['altitude_span_max_m']:violations.append('altitude_drift')
            if max(angles)>policy['quaternion_drift_max_rad']:violations.append('quaternion_drift')
            assert any(v['environment_contract'].get('readback_verified') and v['environment_contract'].get('ok') for v in values)
            windows.append({'mode':mode,'case':case,'sample_count':len(values),'source_span_s':span,
                'source_start_s':values[0]['plant_time_s'],'source_end_s':values[-1]['plant_time_s'],
                'wall_start_s':wall,'wall_end_s':time.time(),'altitude_span_m':max(alt)-min(alt),
                'quaternion_drift_rad':max(angles),'violations':sorted(set(violations)),'statuses':values})
        try:
            preparation=orch.prepare_online_transport_session();write(out/'PREPARATION.json',preparation)
            assert preparation['ok'];client=adapter._client
            current=client.native_session_status();session=Path(current['session_dir']);assert current['process_ready'] and current['owned_by_client']
            write(out/'NATIVE_PROCESSES.json',current)
            tree=subprocess.run(['xwininfo','-root','-tree'],capture_output=True,text=True,timeout=5).stdout
            selected=[line for line in tree.splitlines() if ('FlightStack' in line and current['session_id'][:8] in line) or 'TDDT Layer-6 Digital Pilot' in line]
            assert sum('FlightStack' in line for line in selected)==3 and len(selected)==4
            (out/'OWN_WINDOWS.txt').write_text('\n'.join(selected)+'\n')
            for i,line in enumerate(selected):
                window=re.search(r'\b(0x[0-9a-f]+)\b',line).group(1)
                subprocess.run(['import','-window',window,str(out/('WINDOW_'+str(i)+'.png'))],check=True,capture_output=True,timeout=8)
            for mode in ('QHOVER','QSTABILIZE','FBWA'):
                tx='R604-v7-mode-'+mode
                ack=client.submit_command([0,0,0,0],mode=mode,ttl_s=15,transaction_id=tx,
                                          provenance={'source':'R604_disarmed_mode_probe_not_FINAL'})
                adapter._active_tx=tx;wait(lambda s:s['mode']==mode)
                assert client.cancel(transaction_id=tx,reason='mode probe ready').raw['cancelled'];adapter._active_tx=None
                time.sleep(3.3)
                # Admission deadline: service/GUI must not replay the expired packet.
                tx='R604-v7-'+mode+'-deadline';packet,ack,active=probe(tx)
                time.sleep(1.1);post=status();stop=post['status_quality']['last_timeout_stop']
                assert stop and stop['transaction_id']==tx and stop['target_absent']
                try:client.submit(packet)
                except Layer6RemoteError:pass
                else:raise AssertionError('expired admission packet was accepted')
                cut=post['manual_sent_monotonic'];time.sleep(.2);assert status()['manual_sent_monotonic']==cut
                adapter._active_tx=None;time.sleep(3.3);ground_window(mode,'packet_deadline')
                cases.append({'mode':mode,'case':'packet_deadline','tx':tx,'first_send_bounds':verified_send_bounds(active,tx),
                              'first_send_monotonic':active['command_first_sent_monotonic'],'stop':stop,'expired_resubmit_rejected':True})
                # Pause the owned real intake; actual Qt GUI and native plant continue.
                tx='R604-v7-'+mode+'-TTL';packet,ack,active=probe(tx,deadline_s=20)
                owned=client.native_session_status();row=next(r for r in owned['processes'] if r['role']=='intake')
                actual=native_session.proc(row['pid']);assert all(actual[k]==row[k] for k in ('pid','start_ticks','session_id','uid'))
                os.kill(row['pid'],signal.SIGSTOP);paused=row['pid'];time.sleep(.7)
                post=status();stop=post['status_quality']['last_timeout_stop']
                assert stop and stop['transaction_id']==tx and stop['reason']=='GUI_TTL_expired' and stop['target_absent']
                os.kill(paused,signal.SIGCONT);paused=None;time.sleep(.25)
                cancelled=client.cancel(transaction_id=tx,reason='R604_v7_TTL_complete');assert cancelled.raw['cancelled']
                cut=status()['manual_sent_monotonic'];time.sleep(.2);assert status()['manual_sent_monotonic']==cut
                adapter._active_tx=None;time.sleep(3.3);ground_window(mode,'GUI_TTL_service_paused')
                cases.append({'mode':mode,'case':'GUI_TTL_service_paused','tx':tx,'first_send_bounds':verified_send_bounds(active,tx),
                              'first_send_monotonic':active['command_first_sent_monotonic'],'stop':stop,'cancel_ack':dict(cancelled.raw)})
                # Confirmation timeout is not execution success or physical HOLD.
                pre=status();tx='R604-v7-'+mode+'-confirmation'
                packet={'transaction_id':tx,'layer6_package':{'transaction_id':tx,'u_cmd':[0,0,0,0],'tau_apply':time.time(),'tau_deadline':time.time()+15}}
                bridge=Layer6Bridge(client,fresh=adapter._fresh,confirmed=adapter._confirmed)
                try:bridge.exchange(packet,before=pre,timeout_s=.01,segment_s=.1)
                except (Layer6BridgeError,TimeoutError) as exc:error=str(exc)
                else:raise AssertionError('10ms confirmation falsely proved a 0.1s segment')
                time.sleep(3.3);ground_window(mode,'confirmation_timeout')
                cases.append({'mode':mode,'case':'confirmation_timeout','tx':tx,'expected_non_success':True,'reason':error})
            success=True
        finally:
            if paused:
                try:os.kill(paused,signal.SIGCONT)
                except ProcessLookupError:pass
            if session:
                cleanup=adapter.reset_online_session('R604_v7_owned_cleanup');write(out/'CLEANUP.json',cleanup)
                shutil.copytree(session,out/'NATIVE_SESSION',ignore=shutil.ignore_patterns('eeprom.bin','*.BIN'),dirs_exist_ok=True)
                assert cleanup['ok'] and not native_session.status(session)['alive']
            write(out/'PUBLIC_STATUSES.json',statuses);write(out/'TIMEOUT_CASES.json',cases);write(out/'GROUND_WINDOWS.json',windows)
            write(out/'UNAVAILABLE_STATUS_REQUESTS.json',unavailable)
    assert success
    connection=mavutil.mavlink_connection(str(out/'NATIVE_SESSION/mav.tlog'));messages=[];hearts=[]
    while True:
        m=connection.recv_match(blocking=False)
        if m is None:break
        if m.get_srcSystem()!=1 or m.get_srcComponent()!=1:continue
        if m.get_type()=='HEARTBEAT':hearts.append(m.to_dict());assert not m.base_mode&128
        messages.append((getattr(m,'_timestamp',None),m))
    connection.close();assert len(hearts)>20
    with (out/'RAW_MAVLINK_SELECTED.jsonl').open('w') as stream:
        for wall,m in messages:
            if m.get_type() in ('ATTITUDE_QUATERNION','SCALED_IMU','RC_CHANNELS','SERVO_OUTPUT_RAW'):
                stream.write(json.dumps({'wall_time_s':wall,'fields':m.to_dict(),'wire_hex':bytes(m.get_msgbuf()).hex()})+'\n')
    for window in windows:
        rates=[max(abs(m.rollspeed),abs(m.pitchspeed),abs(m.yawspeed)) for wall,m in messages
               if m.get_type()=='ATTITUDE_QUATERNION' and (window['source_start_s']<=m.time_boot_ms*.001<=window['source_end_s']
               or window['wall_start_s']<=wall<=window['wall_end_s'])]
        assert rates
        window['dense_body_rate_samples']=len(rates);window['maximum_dense_body_rate_rad_s']=max(rates)
        if max(rates)>policy['body_rate_max_rad_s']:window['violations'].append('dense_body_rate')
        window['observed_ground_quiescent']=not window['violations'];window['airborne_HOLD_qualified']=False
    write(out/'GROUND_WINDOWS.json',windows)
    params={line.split()[0]:float(line.split()[1]) for line in (out/'NATIVE_SESSION/mav.parm').read_text().splitlines() if line.strip() and not line.startswith('#')}
    assert params['RC_OVERRIDE_TIME']==3 and params['FS_GCS_ENABL']==0 and params['AHRS_EKF_TYPE']==10
    rc=[m for wall,m in messages if m.get_type()=='RC_CHANNELS'];retention=[]
    for case in cases:
        if 'first_send_bounds' not in case:continue
        start=case['first_send_bounds'][0];boundary=next(w['source_start_s'] for w in windows if (w['mode'],w['case'])==(case['mode'],case['case']))
        observed=[m for m in rc if start<=m.time_boot_ms*.001<=boundary]
        nonzero=[m for m in observed if m.chan1_raw!=params['RC1_TRIM']]
        assert nonzero,'nonzero pilot probe must appear in FCU RC input'
        last=max(m.time_boot_ms*.001 for m in nonzero)
        released=[m.time_boot_ms*.001 for m in observed if m.time_boot_ms*.001>last and m.chan1_raw==params['RC1_TRIM']]
        assert released
        retention.append({'mode':case['mode'],'case':case['case'],'last_nonbaseline_source_s':last,
                          'first_released_source_s':min(released),'RC_OVERRIDE_TIME_s':params['RC_OVERRIDE_TIME'],
                          'GUI_cancel_is_not_instant_FCU_release':True})
    write(out/'FCU_OVERRIDE_RELEASE.json',retention)
    result={'step':'R6.04','status':'PASS' if all(w['observed_ground_quiescent'] for w in windows) else 'FAIL',
            'scope':'three modes, disarmed ground timeout/retention and observed physical quiescence',
            'native_session_id':session.name,'mode_count':3,'timeout_cases':len(cases),'ground_windows':len(windows),
            'rejected_windows':sum(not w['observed_ground_quiescent'] for w in windows),'raw_FCU_messages':len(messages),
            'disarmed_heartbeats':len(hearts),'maximum_dense_body_rate_rad_s':max(w['maximum_dense_body_rate_rad_s'] for w in windows),
            'model_id':preparation['environment_contract']['model_id'],'handoff_hash':preparation['environment_contract']['handoff_hash'],
            'forcing_sha256':preparation['environment_contract']['forcing_file_sha256'],'real_QApplication':True,'real_terminals':3,
            'physical_ground_quiescence_verified':all(w['observed_ground_quiescent'] for w in windows),
            'airborne_HOLD_qualified':False,'flight_ready':False,'MPC_or_FINAL_fabricated':False,'ARM_requested':False}
    write(out/'NATIVE_GROUND_ACCEPTANCE.json',result);print(json.dumps(result))
    assert result['status']=='PASS',result


if __name__=='__main__':main()
