"""Launch the real MAVProxy --map on a one-way, evaluation-only native feed.

No FCU socket is supplied to MAVProxy. Its return packets reach an independent
discard socket, including requests made by map menus. Basemap tiles use a
private cache; network imagery is visualization only, never navigation input.
"""
from pathlib import Path
import json
import os
import time


def init(mpstate):
    from MAVProxy.modules.lib.mp_module import MPModule
    from .native_session import write

    class Observer(MPModule):
        def __init__(self):
            super().__init__(mpstate, 'replay_observer', 'evaluation-only map evidence')
            self.last = 0
            self.pending = None
            self.probe_sent = False

        def mavlink_packet(self, msg):
            if msg.get_type() != 'GLOBAL_POSITION_INT':
                return
            self.pending = (msg, time.monotonic())

        def idle_task(self):
            if self.pending is None:
                return
            msg, received = self.pending
            module = self.module('map')
            if module is None:
                return
            pos = (msg.lat*1e-7, msg.lon*1e-7, msg.hdg*.01)
            actual = module.lat_lon_heading.get(msg.get_srcSystem())
            module.map.set_position('PosVehicle%u' % msg.get_srcSystem(), pos[:2], rotation=pos[2], label='FCU evaluation only')
            if time.monotonic() - self.last < .2:
                return
            self.last = time.monotonic()
            self.pending = None
            from MAVProxy.modules.mavproxy_map.mp_slipmap_util import SlipInfoText
            module.map.add_object(SlipInfoText('reference_position',
                'FCU evaluation only (not MESA navigation)\nLat %.7f  Lon %.7f\nFCU boot %.3f s | DISARMED reference' %
                (pos[0], pos[1], msg.time_boot_ms/1000.)))
            if not self.probe_sent:
                self.master.mav.ping_send(0, 1, 0, 0)
                self.probe_sent = True
            write(Path(os.environ['TDDT_REPLAY_MAP_OUTPUT'])/'observation.json', {
                'session_id': os.environ['TDDT_NATIVE_SESSION_ID'], 'map_alive': module.map.is_alive(),
                'wx_ready': module.map.app_ready.is_set(), 'map_child_pid': module.map.child.pid,
                'source': 'MAVLink/GLOBAL_POSITION_INT; FCU estimator evaluation only',
                'source_fcu_boot_s': msg.time_boot_ms/1000., 'received_monotonic_s': received,
                'latitude_deg': pos[0], 'longitude_deg': pos[1], 'map_position': actual,
                'position_matches_map': actual == pos, 'navigation_input': False, 'control_forwarding': False,
                'harmless_return_path_ping_probe_sent': self.probe_sent})

    return Observer()


def check_dependencies():
    import importlib
    versions = {}
    for name in ('wx', 'cv2', 'numpy', 'PIL', 'pymavlink', 'MAVProxy.modules.mavproxy_map'):
        m = importlib.import_module(name)
        versions[name] = {'path': m.__file__, 'version': getattr(m, '__version__', None)}
    return versions


def main():
    import argparse
    import runpy
    import sys
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check-dependencies', action='store_true')
    p.add_argument('--port', type=int)
    p.add_argument('--output', type=Path)
    p.add_argument('--session-id')
    p.add_argument('--mavproxy', type=Path)
    a = p.parse_args()
    deps = check_dependencies()
    if a.check_dependencies:
        print(json.dumps(deps))
        return
    if not (a.port and 20200 <= a.port <= 20800 and a.output and a.session_id
            and a.session_id == os.environ.get('TDDT_NATIVE_SESSION_ID') and a.mavproxy):
        p.error('private port/output/owned session/mavproxy required')
    a.output.mkdir(parents=True, exist_ok=False)
    from .native_session import write
    write(a.output/'DEPENDENCIES.json', deps)
    os.environ['TDDT_REPLAY_MAP_OUTPUT'] = str(a.output.resolve())
    from MAVProxy.modules.mavproxy_map import mp_slipmap, mp_tile
    # Configure the actual map constructor in this standalone process only.
    # No installed dependency or mission launcher is modified.
    base = mp_slipmap.MPSlipMap
    class ReferenceMap(base):
        def __init__(self, **kw):
            kw.update(elevation=None,
                      title='Phoenix reference map ' + a.session_id[:8] + ' - FCU evaluation only')
            super().__init__(**kw)
    mp_slipmap.MPSlipMap = ReferenceMap
    mp_tile.default_cache_path = lambda: str(a.output/'tiles')
    # MAVProxy's UDP replies cannot reach an FCU: only the one-way feed's
    # discard socket. Disable normal map command modules as another boundary.
    sys.argv = [str(a.mavproxy), '--master=udpin:127.0.0.1:%s' % a.port,
                '--map', '--load-module=layer6.replay_map', '--default-modules=',
                '--streamrate=-1', '--non-interactive', '--nowait',
                '--cmd=set heartbeat 0; set mavfwd false; set mavfwd_disarmed false']
    runpy.run_path(str(a.mavproxy), run_name='__main__')


if __name__ == '__main__':
    main()
