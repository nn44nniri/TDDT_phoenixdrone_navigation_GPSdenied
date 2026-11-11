"""Python L5/L6/L7 exchange owned by TDOC, using the documented Layer6Client API.

This module owns neither MAVLink nor model/decision authority. Intake ACK and
post-command execution are deliberately separate. Clients are injectable so the
entire exchange can be tested without starting a GUI or a flight.
"""
from __future__ import annotations

import copy
import math
import time


class Layer6BridgeError(RuntimeError):
    pass


class Layer6Bridge:
    def __init__(self, client, *, fresh, confirmed):
        self.client = client
        self.fresh = fresh
        self.confirmed = confirmed

    def cancel(self, *, transaction_id=None, reason="TDOC_HOLD"):
        # Cancel the service latch, not just the GUI's current TTL state.
        return self.client.cancel(transaction_id=transaction_id, reason=reason)

    def exchange(self, packet, *, before, timeout_s, segment_s,
                 prediction_evidence=None, network_snapshot=None):
        packet = copy.deepcopy(packet)
        tx = packet.get("transaction_id")
        package = packet.get("layer6_package") or {}
        if not tx or package.get("transaction_id") != tx:
            raise Layer6BridgeError("missing/mismatched execution transaction")
        segment_s = float(segment_s)
        if not math.isfinite(segment_s) or segment_s <= 0:
            raise Layer6BridgeError("invalid execution segment")
        # Reject NaN/Inf and expired deadlines before the first submission.
        # An intake timeout is distinct from the authorized plant-time segment.
        try:
            timeout=float(timeout_s)
            deadline=float(package["tau_deadline"])
        except (KeyError,TypeError,ValueError) as exc:
            raise Layer6BridgeError("invalid confirmation timeout/deadline") from exc
        if not all(math.isfinite(v) and v>0 for v in (timeout,deadline)):
            raise Layer6BridgeError("non-finite confirmation timeout/deadline")
        started = time.monotonic()
        budget = min(timeout, deadline-time.time())
        if budget <= 0 or not self.fresh(before):
            raise Layer6BridgeError("expired packet or stale pre-command state")
        if before.get('schema')=='Layer6-GUI-status-v1':
            return self._bounded_exchange(packet,before=before,budget=budget,started=started,segment_s=segment_s,
                prediction_evidence=prediction_evidence,network_snapshot=network_snapshot)
        try:
            ack = self.client.submit(packet, timeout=budget)
            if not ack.ok or ack.transaction_id != tx:
                raise Layer6BridgeError("intake ACK transaction mismatch")
            end = started + budget
            while time.monotonic() < end:
                status = self.client.gui_status(timeout=min(0.03, max(0.001, end-time.monotonic())))
                if status and 'command_correlation' in status:
                    instance=((status.get('command_correlation') or {}).get('record') or {}).get('context',{}).get('instance_id')
                    if not instance or instance!=(ack.raw or {}).get('command_instance_id'):continue
                if not self.confirmed(status, before, package, tx):
                    continue
                sent_plant = status.get("command_first_sent_plant_time_s")
                post = status.get("plant_time_s")
                try:
                    actual = float(post)-float(sent_plant)
                except (TypeError, ValueError):
                    continue
                if not all(math.isfinite(v) for v in (float(post),float(sent_plant))):
                    continue
                if before.get("plant_time_s") is not None:
                    try:
                        pre=float(before["plant_time_s"])
                        if not math.isfinite(pre) or float(sent_plant)<pre:
                            continue
                    except (TypeError,ValueError):
                        continue
                if not math.isfinite(actual) or actual < segment_s:
                    continue
                if actual > segment_s+0.05:
                    raise Layer6BridgeError("execution sample outside prediction segment")
                result = self.client.build_layer7_input(
                    packet, status=status, ack=ack,
                    prediction_evidence=prediction_evidence,
                    network_snapshot=network_snapshot)
                result["ExecutionEvidence"].update(
                    native_execution_segment_s=actual,
                    execution_start_plant_time_s=float(sent_plant),
                    plant_time_s=float(post),
                    execution_confirmed=True,
                    intake_ack_is_execution_evidence=False)
                # tau_deadline is the packet's admission/confirmation deadline;
                # it does not authorize holding the first move for that entire
                # window. Only this measured prediction segment was committed.
                stopped=self.cancel(transaction_id=tx,reason="verified_execution_segment_complete")
                stop_ack=dict(getattr(stopped,"raw",None) or {})
                if getattr(stopped,"ok",False) is not True or stop_ack.get("cancelled") is not True:
                    raise Layer6BridgeError("execution segment latch cancellation not confirmed")
                result["ExecutionEvidence"].update(
                    execution_segment_closed=True,latch_cancel_ack=stop_ack,
                    remaining_horizon_executed=False)
                return {"ack": ack, "status": status, "layer7_input": result}
            raise Layer6BridgeError("intake ACK without matching post-command plant sample")
        except Exception:
            try:
                self.cancel(transaction_id=tx, reason="execution_not_confirmed")
            except Exception:
                pass  # Deadline watchdog remains independently active in Layer6.
            raise

    def _bounded_exchange(self,packet,*,before,budget,started,segment_s,prediction_evidence,network_snapshot):
        from layer6.segments import contract,validate_result
        tx=packet['transaction_id'];package=packet['layer6_package']
        native_id=(before.get('status_quality') or {}).get('native_session_id')
        if native_id:packet['native_session_id']=native_id
        packet['execution_segment_s']=segment_s
        try:policy=contract(packet)
        except (ValueError,TypeError) as exc:raise Layer6BridgeError('invalid segment policy') from exc
        packet['execution_segment']=policy
        try:
            ack=self.client.submit(packet,timeout=budget)
            raw=ack.raw or {};instance=raw.get('command_instance_id')
            if not ack.ok or ack.transaction_id!=tx or not instance or raw.get('execution_segment')!=policy:
                raise Layer6BridgeError('segment admission/instance mismatch')
            end=started+budget
            last_segment=None;responses=0;timeouts=0
            while time.monotonic()<end:
                # Allow a GUI tick and its telemetry drain to answer this exact
                # request. The original overall/packet deadlines still bound it.
                response=self.client.segment_status(transaction_id=tx,command_instance_id=instance,
                    timeout=min(.15,max(.001,end-time.monotonic())))
                if not response:
                    timeouts+=1;continue
                responses+=1
                if not response.get('available'):
                    if response.get('reason')=='segment_response_too_large':
                        raise Layer6BridgeError('segment transport budget exceeded: '+str({k:response.get(k)
                            for k in ('encoded_bytes','max_bytes','segment_state','segment_failure')}))
                    continue
                row=response.get('segment') or {}
                last_segment={k:row.get(k) for k in ('state','failure','stop_reason','qualified','duration_bounds_s')}
                if row.get('state')=='FAILED':raise Layer6BridgeError('segment failed: '+str(row.get('failure')))
                if row.get('state')!='CLOSED':continue
                if not validate_result(row,tx,instance,policy,before_session=before.get('session_id')):
                    raise Layer6BridgeError('invalid local segment closure proof')
                status=row['snapshot']
                if not self.confirmed(status,before,package,tx):
                    raise Layer6BridgeError('segment observation stale or unconfirmed')
                stopped=self.cancel(transaction_id=tx,reason='verified_local_segment_closed')
                stop_ack=dict(getattr(stopped,'raw',None) or {})
                if (getattr(stopped,'ok',False) is not True or stop_ack.get('cancelled') is not True
                    or stop_ack.get('command_instance_id')!=instance):
                    raise Layer6BridgeError('execution segment latch cancellation not confirmed')
                # build_layer7_input re-evaluates existing freshness; no archived-time exception.
                result=self.client.build_layer7_input(packet,status=status,ack=ack,
                    prediction_evidence=prediction_evidence,network_snapshot=network_snapshot)
                result['ExecutionEvidence'].update(native_execution_segment_s=None,
                    native_execution_duration_bounds_s=row['duration_bounds_s'],execution_segment_policy=policy,
                    execution_start_plant_time_s=None,execution_start_plant_time_bounds_s=row['first_event']['plant_time_bounds'],
                    execution_close_plant_time_bounds_s=row['close']['bounds'],plant_time_s=status['plant_time_s'],
                    execution_confirmed=True,execution_confirmation_scope='bounded_continuous_command_latch_only',
                    execution_segment_closed=True,segment_closure_proof=row,latch_cancel_ack=stop_ack,
                    intake_ack_is_execution_evidence=False,remaining_horizon_executed=False,
                    physical_stabilization_verified=False)
                return {'ack':ack,'status':status,'layer7_input':result}
            raise Layer6BridgeError('segment closure proof not received before confirmation deadline: '+str({'responses':responses,'rpc_timeouts':timeouts,'last_segment':last_segment,'budget_s':budget}))
        except Exception:
            try:self.cancel(transaction_id=tx,reason='segment_execution_not_confirmed')
            except Exception:pass
            raise
