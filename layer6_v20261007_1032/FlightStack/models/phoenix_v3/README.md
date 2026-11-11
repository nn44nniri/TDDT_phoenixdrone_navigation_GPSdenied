# phoenix_v3 JSBSim model v3

Phoenix v3 is the one-command integration package for the dual-rotor Phoenix tailsitter with ArduPlane SITL + JSBSim.

## What v3 finalizes

1. Keeps the v2 blocker fix: Phoenix runscript has no stock `propulsion/engine[0]/set-running` event and no conventional `simulation/do_simple_trim` event.
2. Keeps independent Phoenix output mapping: SERVO1 left elevon, SERVO2 right elevon, SERVO3 left motor, SERVO4 right motor.
3. Fixes the remaining ground-start attitude issue. The Phoenix reset no longer declares `gamma=0`; instead it declares zero BODY velocity (`ubody/vbody/wbody=0`) and `theta=89 deg`. This prevents a JSBSim flight-path-angle constraint from recomputing/overriding the requested pitch at zero speed.
4. Enables the simulated airspeed backend for forward-flight control: `ARSPD_TYPE=100`, `ARSPD_USE=1`, `ARSPD_SKIP_CAL=1`, `ARSPD_AUTOCAL=0`.
5. Adds `scripts/preinstall.sh`, which backs up, installs, upgrades v2/stock connector code, validates and optionally clean-builds ArduPlane SITL in one command.
6. Adds safe upgrade logic for both stock `SIM_JSBSim.cpp` and an already-installed Phoenix v2 connector.
7. Expands runtime validation to reject a generated Phoenix reset that still contains `gamma` and to optionally verify a captured ArduPlane runtime log.

## One-command installation

From the extracted package:

```bash
./scripts/preinstall.sh \
  --ardupilot-root /path/to/ardupilot \
  --build
```

The script performs package validation, creates a timestamped backup under the ArduPilot checkout, installs all Phoenix files, installs/upgrades `libraries/SITL/SIM_JSBSim.cpp`, validates the installed checkout, then runs a clean SITL build.

If you only want installation without compilation, omit `--build`.

## Runtime

Make sure the intended JSBSim executable is first in `PATH`, then run:

```bash
cd /path/to/ardupilot
./Tools/autotest/sim_vehicle.py \
  -v ArduPlane \
  -f jsbsim:phoenix_v3 \
  -L PhoenixBoulder \
  -w \
  --add-param-file=Tools/autotest/default_params/phoenix_v3.parm \
  --console --map \
  --out=udp:127.0.0.1:14551 \
  --out=udp:127.0.0.1:14552
```

After the first Phoenix launch:

```bash
/path/to/phoenix_v3_jsbsim_model_v3/scripts/verify_runtime.sh /path/to/ardupilot
```

If you captured the ArduPlane terminal output:

```bash
/path/to/phoenix_v3_jsbsim_model_v3/scripts/verify_runtime.sh \
  /path/to/ardupilot /path/to/terminal_1.txt
```

Expected gates:

- generated `jsbsim_start_0.xml` contains neither the stock engine-start event nor simple trim;
- generated `reset.xml` contains `theta=89`, zero body velocity and no `gamma` element;
- ArduPlane output reaches `Opened JSBSim control socket`;
- MAVProxy receives heartbeat and `MasterIn` increases above zero.

## Notes and validation boundary

The aircraft XML remains a source-informed integration model. Its forward-flight stability derivatives remain engineering estimates rather than identified Phoenix flight data.

The `BELLY_GUARD` remains a conservative JSBSim ground-contact integration guard. The official Phoenix Gazebo model provides a base-link collision body, but this package does not claim that the reduced JSBSim contact model is a measured or fully validated landing-gear/contact representation.

## Convenience launcher for this project region

To avoid dependence on a separate `locations.txt`, v3 includes a launcher that passes the current Boulder home directly with `-l`:

```bash
./scripts/run_phoenix_sitl.sh /path/to/ardupilot --wipe
```

Use `--wipe` for the first clean run. Omit it on subsequent runs when you want to retain ArduPilot parameter state.
