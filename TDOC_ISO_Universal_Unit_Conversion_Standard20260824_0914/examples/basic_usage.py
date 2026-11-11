from tdoc_unit_converter import UnitConverter
from tdoc_unit_converter.adapters import ned_to_enu, msl_to_agl, mach_to_tas
c=UnitConverter()
print(c.convert(36,"km/h","m/s"))
print(c.convert(20,"degC","K"))
print(ned_to_enu((100,20,5)))
print(msl_to_agl(1500,900))
print(mach_to_tas(0.3,temperature_k=288.15))
