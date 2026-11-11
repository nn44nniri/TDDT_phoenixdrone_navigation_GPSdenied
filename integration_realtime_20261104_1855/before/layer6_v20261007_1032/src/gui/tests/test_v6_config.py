from pathlib import Path
import yaml

cfg = yaml.safe_load((Path(__file__).resolve().parents[3] / "gui" / "config" / "f310_phoenix.yaml").read_text())
a = cfg["axes"]
b = cfg["buttons"]
assert cfg["version"] == 6
assert (a["left_x"], a["left_y"]) == (0, 1)
assert a["left_trigger"] == 2
assert (a["right_x"], a["right_y"]) == (3, 4)
assert a["right_trigger"] == 5
assert [b[k] for k in ("a","b","x","y","lb","rb","back","start","mode","left_stick","right_stick")] == list(range(11))
assert cfg["hat"]["index"] == 0 and cfg["hat"]["use_pygame_hat_first"] is True
print("v5 F310 pygame/SDL mapping PASS")
