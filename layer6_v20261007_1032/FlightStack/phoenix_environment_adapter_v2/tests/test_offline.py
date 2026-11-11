import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from phoenix_env_adapter.prepare import prepare

def main(dataset, region, vehicle, out):
    r=prepare(dataset,region,vehicle,"2025-05-20T12:00:00Z",out)
    assert r["vehicle"]["forward_flight_air_start_only"] is False
    assert 1500 < r["home"]["terrain_msl_m"] < 2100
    assert 0 <= r["climate_at_home"]["wind_direction_from_deg"] < 360
    assert Path(out,"phoenix_tailsitter.parm").exists()
    print(json.dumps(r,indent=2))
if __name__=="__main__": main(*sys.argv[1:5])
