import math, pytest
from tdoc_unit_converter import UnitConverter, convert
from tdoc_unit_converter.errors import QuantityMismatchError, ReferenceRequiredError
from tdoc_unit_converter.adapters import *

def test_exact_units():
    assert convert(1,"ft","m") == pytest.approx(.3048)
    assert convert(1,"nmi","m") == pytest.approx(1852)
    assert convert(36,"km/h","m/s") == pytest.approx(10)
    assert convert(1,"knot","m/s") == pytest.approx(1852/3600)
    assert convert(1000,"ft/min","m/s") == pytest.approx(5.08)
    assert convert(900,"hPa","Pa") == pytest.approx(90000)
    assert convert(200,"Wh","J") == pytest.approx(720000)
    assert convert(50,"uT","T") == pytest.approx(5e-5)

def test_temperature():
    assert convert(20,"degC","K") == pytest.approx(293.15)
    assert convert(32,"degF","degC") == pytest.approx(0)
    assert convert(18,"delta_degF","delta_K") == pytest.approx(10)

def test_semantics():
    with pytest.raises(QuantityMismatchError): convert(1,"N*m","J")
    with pytest.raises(QuantityMismatchError): convert(1,"m","s")

def test_frames_and_vertical():
    assert ned_to_enu((1,2,3)) == pytest.approx((2,1,-3))
    assert enu_to_ned((2,1,-3)) == pytest.approx((1,2,3))
    assert frd_to_flu((1,2,3)) == pytest.approx((1,-2,-3))
    assert msl_to_agl(1500,1000) == 500
    assert agl_to_msl(500,1000) == 1500
    assert ned_down_to_altitude(50,1200) == 1150

def test_heading_wind():
    assert heading_nav_to_enu(0) == pytest.approx(math.pi/2)
    assert wind_from_to_vector(10,0) == pytest.approx((0,-10,0))

def test_mach_requires_context():
    with pytest.raises(ReferenceRequiredError): mach_to_tas(.5)
    assert mach_to_tas(.5,speed_of_sound=340) == pytest.approx(170)

def test_wgs84_ecef_equator():
    x,y,z=wgs84_to_ecef(0,0,0)
    assert x == pytest.approx(6378137.0); assert y == pytest.approx(0); assert z == pytest.approx(0)
