from __future__ import annotations
from dataclasses import dataclass

@dataclass
class RTClock:
    period_s:float=0.1
    max_state_age_s:float=0.2
    last_sim_time_s:float|None=None

    def reference_time(self,fallback:float)->float:
        """Model/scheduler reference only; not physical freshness authority.

        Before the first PT_LIB/JSBSim timestamp this deliberately returns the
        supplied deterministic fallback so model requests remain well-formed.  The
        caller must use ``observe_sim_time`` / ``plant_time_state`` for physical
        freshness and phase-transition authority.
        """
        return float(self.last_sim_time_s if self.last_sim_time_s is not None else fallback)

    @property
    def has_plant_time(self)->bool:
        return self.last_sim_time_s is not None

    def plant_time_state(self)->dict:
        if self.last_sim_time_s is None:
            return {
                'ok':False,'state_fresh':False,'plant_time_present':False,
                'plant_time_authority':'Layer6/MAVLink/ArduPilot',
                'reason':'simulation_time_missing',
            }
        return {
            'ok':True,'state_fresh':True,'plant_time_present':True,
            'plant_time_authority':'Layer6/MAVLink/ArduPilot',
            'simulation_time_s':float(self.last_sim_time_s),
        }

    def observe_sim_time(self,value)->dict:
        try:v=float(value)
        except Exception:
            # Missing current-cycle plant time must not inherit freshness from a
            # previously observed timestamp.
            return {
                'ok':False,'state_fresh':False,'plant_time_present':False,
                'plant_time_authority':'Layer6/MAVLink/ArduPilot',
                'reason':'simulation_time_missing',
                'last_valid_simulation_time_s':self.last_sim_time_s,
            }
        jump=None if self.last_sim_time_s is None else v-self.last_sim_time_s
        monotonic=bool(jump is None or jump>=-1e-9)
        within_grant=bool(jump is None or jump<=self.period_s*1.5)
        advanced=bool(jump is None or jump>1e-9)
        ok=bool(monotonic and within_grant)
        # A valid observation becomes the only source of plant-time authority.
        if ok:
            self.last_sim_time_s=v
        reason=None
        if not monotonic: reason='simulation_time_regressed'
        elif not within_grant: reason='simulation_time_grant_exceeded'
        elif not advanced: reason='simulation_time_not_advanced'
        return {
            'ok':ok,
            'state_fresh':bool(ok and advanced),
            'plant_time_present':True,
            'plant_time_authority':'Layer6/MAVLink/ArduPilot',
            'simulation_time_s':v,
            'jump_s':jump,
            'advanced':advanced,
            'bound_s':self.period_s*1.5,
            'reason':reason,
        }

    def observe_measured_samples(self,value,samples)->dict:
        """Consume actual buffered native timestamps without widening any grant.

        Validate on a private clock first: a missing/invalid interval cannot
        partially advance authoritative plant time. No interpolation/rebase.
        """
        import math
        from dataclasses import replace
        try:
            end=float(value)
            if not math.isfinite(end):raise ValueError('non-finite plant time')
            if self.last_sim_time_s is None or end-self.last_sim_time_s<=self.period_s*1.5:
                return self.observe_sim_time(end)
            stamps=set()
            for sample in samples or []:
                stamp=float(sample['plant_time_s'])
                if not math.isfinite(stamp):raise ValueError('non-finite IMU time')
                if self.last_sim_time_s<stamp<=end:
                    if sample.get('frame')!='BODY_FRD' or sample.get('source')!='MAVLink/SCALED_IMU':
                        raise ValueError('invalid measured IMU provenance')
                    for key in ('gyro_x_rad_s','gyro_y_rad_s','gyro_z_rad_s',
                                'accel_x_m_s2','accel_y_m_s2','accel_z_m_s2'):
                        if not math.isfinite(float(sample[key])):raise ValueError('invalid IMU value')
                    stamps.add(stamp)
            previous=self.last_sim_time_s
            trial=replace(self)
            for stamp in sorted(stamps | {end}):
                result=trial.observe_sim_time(stamp)
                if not result['ok']:return {**result,'buffered_sample_validation':True}
            self.last_sim_time_s=trial.last_sim_time_s
            return {**result,'advanced':True,'state_fresh':True,'reason':None,
                    'buffered_sample_validation':True,'measured_sample_count':len(stamps),
                    'decision_interval_s':end-previous}
        except (ValueError,TypeError,KeyError) as exc:
            return {'ok':False,'state_fresh':False,'plant_time_present':False,
                    'reason':'invalid_buffered_plant_time_evidence','error':str(exc)}

    def synchronize_after_handoff(self,value)->dict:
        """Establish the operational clock epoch after a proved FMI handoff.

        The supervised release transition is lifecycle work, not an Online control
        period.  It may rebase the epoch exactly once; normal observations remain
        subject to the unchanged 1.5-period grant thereafter.
        """
        try:v=float(value)
        except Exception:
            return {'ok':False,'state_fresh':False,'plant_time_present':False,
                    'plant_time_authority':'Layer6/MAVLink/ArduPilot','reason':'handoff_simulation_time_missing'}
        if self.last_sim_time_s is not None and v < self.last_sim_time_s-1e-9:
            return {'ok':False,'state_fresh':False,'plant_time_present':True,
                    'plant_time_authority':'Layer6/MAVLink/ArduPilot','simulation_time_s':v,
                    'reason':'handoff_simulation_time_regressed'}
        prior=self.last_sim_time_s; self.last_sim_time_s=v
        return {'ok':True,'state_fresh':True,'plant_time_present':True,
                'plant_time_authority':'Layer6/MAVLink/ArduPilot','simulation_time_s':v,
                'jump_s':None if prior is None else v-prior,'advanced':prior is None or v>prior+1e-9,
                'bound_s':self.period_s*1.5,'reason':None,
                'clock_synchronization':'SUPERVISED_RELEASE_HANDOFF_COMPLETE'}
