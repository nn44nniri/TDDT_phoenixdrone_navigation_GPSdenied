from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

def test_python_files_have_explicit_runtime_or_deployment_location():
    deployment = {
        "scripts/apply_r3_02_environment_alignment.py",
        "scripts/apply_r3_03_model_alignment.py",
        "scripts/apply_r4_02_environment_binding.py",
        "resources/r4_02_v1/phoenix_env_adapter/bound.py",
        "resources/r4_02_v1/phoenix_env_adapter/native_forcing.py",
        "resources/r4_02_v1/phoenix_env_adapter/cli.py",
    }
    offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                 if "FlightStack" not in p.parts and ROOT / "src" not in p.parents
                 and str(p.relative_to(ROOT)) not in deployment]
    assert offenders == []
    assert all((ROOT / name).is_file() for name in deployment)

def test_direct_demo_help_runs_and_preserves_switches():
    cp = subprocess.run([sys.executable, str(ROOT / "src" / "demo_layer6.py"), "--help"], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    for sw in ["--seed","--time-scale","--rate-hz","--connect-timeout","--verify-timeout","--landing-timeout","--disarm-retries","--restart-at-end","--stop-at-end","--flightstack-dir","--terminal","--restart-debug","--startup-check","--start-if-needed"]:
        assert sw in cp.stdout

def test_demo_is_thin_client_entrypoint():
    text = (ROOT / "src" / "demo_layer6.py").read_text()
    assert "from layer6.client import demo_main" in text
    assert "DigitalPilotExecutor" not in text
    assert "run_flightstack" not in text

def test_demo_lifecycle_flags_are_exclusive():
    cp = subprocess.run([sys.executable, str(ROOT / "src" / "demo_layer6.py"), "--restart-at-end", "--stop-at-end"], capture_output=True, text=True)
    assert cp.returncode != 0

def test_client_cli_has_start_if_needed():
    cp = subprocess.run([sys.executable, str(ROOT / "src" / "layer6" / "client.py"), "--help"], capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    assert "--start-if-needed" in cp.stdout
