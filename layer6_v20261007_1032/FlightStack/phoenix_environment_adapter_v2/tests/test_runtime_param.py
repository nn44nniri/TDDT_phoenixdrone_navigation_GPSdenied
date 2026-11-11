import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from phoenix_env_adapter.runtime import _set_param_verified

class Msg:
    def __init__(self, pid, value):
        self.param_id = pid
        self.param_value = value

class Mav:
    def __init__(self, owner): self.owner = owner
    def param_set_send(self, ts, tc, name, value, ptype):
        self.owner.sent.append((ts, tc, name.decode(), value, ptype))
        self.owner.queue.append(Msg(name, value))

class Master:
    target_system = 1
    target_component = 1
    def __init__(self):
        self.sent=[]; self.queue=[]; self.mav=Mav(self)
    def recv_match(self, type=None, blocking=False, timeout=None):
        return self.queue.pop(0) if self.queue else None

m=Master()
assert abs(_set_param_verified(m,"SIM_WIND_SPD",2.46)-2.46) < 1e-6
assert abs(_set_param_verified(m,"SIM_WIND_DIR",263.9)-263.9) < 1e-6
assert [x[2] for x in m.sent] == ["SIM_WIND_SPD","SIM_WIND_DIR"]
print("PASS: verified SIM_WIND_* PARAM_SET path")
