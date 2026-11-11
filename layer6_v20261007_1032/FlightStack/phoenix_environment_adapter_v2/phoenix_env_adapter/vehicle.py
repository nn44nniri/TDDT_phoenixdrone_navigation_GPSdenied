from __future__ import annotations
from pathlib import Path
import copy, math, yaml

class PhoenixVehicle:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = yaml.safe_load(self.path.read_text())

    def ground_vtol_copy(self, home_alt_msl_m: float):
        d = copy.deepcopy(self.data)
        ground=d.get('ground_geometry') or {}
        if ground.get('revision')!='phoenix-fourfoot-r3.02-v1' or ground.get('body_frame')!='BODY_FRD_CG':
            raise ValueError('versioned four-foot CG geometry required for environment preparation')
        contacts=ground.get('contacts') or []
        if [c.get('name') for c in contacts]!=['TAIL_FOOT_L1','TAIL_FOOT_L2','TAIL_FOOT_R1','TAIL_FOOT_R2']:
            raise ValueError('four-foot contact contract missing')
        pitch=math.radians(float(ground['reset_pitch_deg']))
        height=max(-math.sin(pitch)*float(c['body_frd_m'][0])+math.cos(pitch)*float(c['body_frd_m'][2]) for c in contacts)
        if not math.isfinite(height) or height<=0 or abs(height-float(ground['cg_height_agl_at_reset_m']))>1e-9:
            raise ValueError('CG AGL contradicts contact geometry')
        sim = d.setdefault("simulation_assumptions", {})
        sim["forward_flight_air_start_only"] = False
        sim["initial_agl_m"] = height
        sim["initial_altitude_msl_m"] = float(home_alt_msl_m)+height
        sim["position_origin"] = "CG"
        sim["ground_geometry_revision"] = ground['revision']
        sim["home_alt_msl_m_default"] = float(home_alt_msl_m)
        sim["home_alt_source"] = "runtime terrain start sample"
        return d

    def write_ground_vtol(self, out: str | Path, home_alt_msl_m: float):
        Path(out).write_text(yaml.safe_dump(self.ground_vtol_copy(home_alt_msl_m), sort_keys=False))
