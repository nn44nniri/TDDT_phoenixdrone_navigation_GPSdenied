"""Pinned JSBSim research rollout, separate from the live Native plant.

RunIC reconstructs visible initial conditions only. This wrapper explicitly does
not claim restoration of contact solver, lag filters or controller hidden state.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import math
import os
import subprocess
import tempfile

from .native_operational import require, vector, finite

NATIVE_PHYSICS_RATE_HZ = 1200.


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def native_vertical_origin(environment, connector_sha256):
    """Map exported FCU NED to the actual JSBSim local-ground datum.

    SIM_JSBSim exports home.alt + fdm.agl. Home altitude is an export offset,
    not the terrain/atmosphere altitude inside this AGL-initialized JSBSim run.
    """
    origin = environment['origin']
    contract = environment['vertical_contract']
    require(contract['schema'] == 'Native-JSBSim-AGL-export-v1'
            and contract['native_connector_sha256'] == connector_sha256
            and contract['JSBSim_local_terrain_m'] == 0., 'Native vertical frame contract unavailable')
    offset = contract['home_export_offset_msl_m']
    require(finite(offset) and all(finite(origin.get(k)) for k in
                ('latitude_deg','longitude_deg','altitude_msl_m','terrain_elevation_msl_m')),
            'Native export/coordinate origin invalid')
    return [origin['latitude_deg'], origin['longitude_deg'], origin['altitude_msl_m']-offset, 0.]


class NativePhysicsRollout:
    def __init__(self, executable, jsbsim_root, aircraft_path, expected):
        self.executable = Path(executable).resolve()
        self.jsbsim_root = Path(jsbsim_root).resolve()
        self.aircraft_path = Path(aircraft_path).resolve()
        self.expected = deepcopy(expected)
        self.identity()

    def identity(self):
        current = {'helper_sha256': sha(self.executable),
                   'physics_library_sha256': sha(self.jsbsim_root/'build/src/libJSBSim.a'),
                   'aircraft_sha256': sha(self.aircraft_path/'phoenix_v3/phoenix_v3.xml'),
                   'native_connector_sha256': sha(self.aircraft_path.parents[2]/'libraries/SITL/SIM_JSBSim.cpp'),
                   'integration_rate_hz': NATIVE_PHYSICS_RATE_HZ}
        require(current == self.expected, 'Native physics deployment/source identity changed')
        return {**current, 'scope': 'isolated RunIC visible-state dynamics, NOT a full-state checkpoint',
                'contact_and_filter_state_restored': False, 'controller_included': False}

    def rollout(self, initial, schedule, offsets, environment):
        before = self.identity()
        require(vector(initial, 13) and schedule and offsets and
                vector(environment['wind_ned_m_s'], 3), 'physics inputs invalid')
        require(abs(sum(q*q for q in initial[6:10])-1) <= .002, 'physics initial quaternion')
        origin = environment['origin']
        keys = ('latitude_deg', 'longitude_deg', 'altitude_msl_m', 'terrain_elevation_msl_m')
        require(all(finite(origin.get(k)) for k in keys), 'physics origin/terrain missing')
        require(all(finite(t) and t > 0 and abs(round(t*NATIVE_PHYSICS_RATE_HZ)/NATIVE_PHYSICS_RATE_HZ-t)<1e-9 for t in offsets),
                'physics sample time outside pinned Native integration grid')
        require(all(finite(row['offset_s']) and vector(row['pwm_us'], 4)
                    and all(1000 <= p <= 2000 for p in row['pwm_us']) for row in schedule),
                'physics PWM schedule invalid')
        end = schedule[-1]['offset_s']
        require(all(b['offset_s'] > a['offset_s'] for a, b in zip(schedule, schedule[1:]))
                and end <= 3 and max(offsets) <= end+1e-9, 'physics schedule time invalid')
        values = initial+native_vertical_origin(environment,before['native_connector_sha256'])+environment['wind_ned_m_s']
        lines = [' '.join(map(str, values)), str(len(schedule))]
        lines += [' '.join(map(str, [r['offset_s']]+r['pwm_us'])) for r in schedule]
        lines += [str(len(offsets)), ' '.join(map(str, offsets))]
        # A separate process/cwd prevents model output from reaching active
        # mission files. There is no socket, MAVLink, GUI or actuation call.
        with tempfile.TemporaryDirectory(prefix='tddt_native_physics_', dir='/tmp') as scratch:
            run = subprocess.run([str(self.executable), str(self.jsbsim_root), str(self.aircraft_path)],
                input='\n'.join(lines)+'\n', text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=scratch, env=dict(os.environ, JSBSIM_DEBUG='0'), timeout=10, check=True)
        rows = [json.loads(line[5:]) for line in run.stdout.splitlines() if line.startswith('TDDT ')]
        require(len(rows) == len(offsets) and all(vector(r['plant_state'], 13) for r in rows),
                'physics output incomplete/nonfinite')
        require(self.identity() == before, 'physics binary/model changed during rollout')
        return rows
