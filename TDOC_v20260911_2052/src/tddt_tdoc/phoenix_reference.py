"""R3.01 immutable source reference, without installation or flight authority."""
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

REFERENCE_REVISION = "phoenix-fourfoot-r3.01-v1"
MANIFEST_SHA256 = "7f174eb475785c164b9e693b93d52e71c2b40c2bd34fb5a236b42048815f59b1"
EXPECTED_FILES = {
    "aircraft": ("Phoenix_FourFoot_JSBSim_Original_v1.xml", 12259,
                 "9118bfac6cb1bbaa05142f1c0f419fe570414be8b1dbdeffb591e7b898f64da9"),
    "reset": ("Phoenix_FourFoot_Reset_Original_v1.xml", 445,
              "a03dad713d4f3c1f72093c6db01e6fefab2bb144b600b223f81009cc7dd8a78d"),
}
CONTACT_NAMES = ("TAIL_FOOT_L1", "TAIL_FOOT_L2", "TAIL_FOOT_R1", "TAIL_FOOT_R2")


def reference_root():
    return Path(__file__).resolve().parent / "resources/phoenix_fourfoot/r3_01_v1"


def _quantity(element):
    if element is None:
        raise ValueError("required XML quantity missing")
    value = float(element.text.strip())
    if not math.isfinite(value):
        raise ValueError("non-finite XML quantity")
    return {"value": value, **element.attrib}


def _location(element):
    if element is None:
        raise ValueError("required XML location missing")
    return {"unit": element.get("unit"),
            "xyz": [_quantity(element.find(axis))["value"] for axis in ("x", "y", "z")]}


def inspect_reference(root=None):
    """Verify the exact source pair and inventory raw units; never launch or write.

    The manifest itself is pinned in code. A changed model requires an explicit
    new revision; rehashing the manifest alone cannot authorize substituted XML.
    """
    claims = {"actuation_authority": False, "flight_ready": False,
              "ground_load_bearing_verified": False, "active_installation": False}
    root = Path(root) if root is not None else reference_root()
    try:
        path = root / "REFERENCE_MANIFEST.json"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
            raise ValueError("reference manifest missing or not a bounded regular file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
            raise ValueError("reference manifest hash mismatch")
        manifest = json.loads(raw)
        if manifest["reference_revision"] != REFERENCE_REVISION:
            raise ValueError("reference revision mismatch")
        xml = {}
        for role, (name, size, digest) in EXPECTED_FILES.items():
            path = root / name
            record = manifest["files"][role]
            if (record["filename"], record["bytes"], record["sha256"]) != (name, size, digest):
                raise ValueError("reference identity mismatch: " + role)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise ValueError("reference file missing/size/type mismatch: " + role)
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("reference file hash mismatch: " + role)
            xml[role] = ET.fromstring(raw)
        aircraft, reset = xml["aircraft"], xml["reset"]
        if aircraft.tag != "fdm_config" or reset.tag != "initialize":
            raise ValueError("reference XML root contract mismatch")
        contacts = aircraft.findall("ground_reactions/contact")
        if tuple(c.get("name") for c in contacts) != CONTACT_NAMES:
            raise ValueError("exact four TAIL_FOOT contacts required")
        geometry = []
        for contact in contacts:
            if contact.get("type") != "STRUCTURE":
                raise ValueError("unexpected contact type")
            item = {"name": contact.get("name"), "type": contact.get("type"),
                    "location": _location(contact.find("location"))}
            item.update({key: _quantity(contact.find(key)) for key in (
                "static_friction", "dynamic_friction", "rolling_friction", "spring_coeff", "damping_coeff")})
            geometry.append(item)
        if len({tuple(c["location"]["xyz"]) for c in geometry}) != 4:
            raise ValueError("four distinct contact locations required")
        reset_fields = {key: _quantity(reset.find(key)) for key in (
            "latitude", "longitude", "altitude", "ubody", "vbody", "wbody", "phi", "theta", "psi")}
        return {"ok": True, "status": "REFERENCE_PINNED_ONLY", "reference_revision": REFERENCE_REVISION,
                "manifest_sha256": MANIFEST_SHA256, "files": manifest["files"],
                "contacts": geometry, "mass": _quantity(aircraft.find("mass_balance/emptywt")),
                "CG": _location(aircraft.find("mass_balance/location[@name='CG']")),
                "reset": reset_fields, "reset_datum_AGL_interpretation": "PENDING_R3.02; raw XML only",
                "source_outputs": [dict(node.attrib) for node in aircraft.findall("output")],
                "scope": "source identity and XML inventory only; no simulator or physical validation",
                **claims}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ET.ParseError) as exc:
        return {"ok": False, "status": "REFERENCE_REJECTED", "reason": str(exc),
                "reference_revision": REFERENCE_REVISION, **claims}
