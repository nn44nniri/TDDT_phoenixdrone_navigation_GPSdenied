# `Tools/autotest/aircraft/phoenix_v3`

`-f jsbsim:phoenix_v3` selects `aircraft/phoenix_v3/phoenix_v3.xml`.

For Phoenix v3 the ArduPilot JSBSim connector rewrites `reset.xml` on every launch. The generated reset intentionally uses:

- altitude `0.08 m` in the local flat JSBSim terrain model;
- zero body velocity (`ubody`, `vbody`, `wbody`);
- no `gamma` element;
- local pitch `theta=89 deg`;
- the requested home heading as `psi`.

Omitting `gamma` is intentional: supplying a flight-path-angle constraint can cause JSBSim initial-condition processing to recompute theta, which is undesirable for a stationary tailsitter ground start.

Do not place ERA5, DEM, mission or Environment Adapter data in this aircraft directory. Those remain environment/mission inputs.
