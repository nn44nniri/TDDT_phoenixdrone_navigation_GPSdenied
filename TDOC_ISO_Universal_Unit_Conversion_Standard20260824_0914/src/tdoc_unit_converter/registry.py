import math
from .models import UnitDef

U = {}
ALIASES = {}

def add(symbol, quantity, dimension, scale=1.0, offset=0.0, canonical=None, affine=False, aliases=()):
    d = UnitDef(symbol, quantity, dimension, float(scale), float(offset), canonical or symbol, affine)
    U[symbol] = d
    for a in aliases:
        ALIASES[a] = symbol
    return d

# dimensionless / ratios
add("1", "dimensionless", "1", canonical="1", aliases=("fraction", "ratio"))
add("%", "dimensionless", "1", 1e-2, canonical="1", aliases=("percent", "pct"))
add("ppm", "dimensionless", "1", 1e-6, canonical="1")
add("ppb", "dimensionless", "1", 1e-9, canonical="1")

# length
for s,k,a in [("m",1,()),("km",1000,()),("cm",.01,()),("mm",.001,()),("um",1e-6,("µm","μm")),("nm",1e-9,()),("in",.0254,("inch","inches")),("ft",.3048,("foot","feet")),("yd",.9144,("yard",)),("mi",1609.344,("mile",)),("nmi",1852,("NM","nautical_mile"))]: add(s,"length","L",k,canonical="m",aliases=a)
# area
for s,k,a in [("m2",1,("m²",)),("km2",1e6,("km²",)),("cm2",1e-4,("cm²",)),("ha",10000,("hectare",)),("acre",4046.8564224,()),("ft2",.09290304,("ft²",)),("in2",.00064516,("in²",))]: add(s,"area","L2",k,canonical="m2",aliases=a)
# volume
for s,k,a in [("m3",1,("m³",)),("L",.001,("litre","liter")),("mL",1e-6,()),("cm3",1e-6,("cm³",)),("ft3",.028316846592,("ft³",)),("in3",.000016387064,("in³",)),("US_gal",.003785411784,("gal","US gal","usgal")),("imp_gal",.00454609,("imperial_gallon",))]: add(s,"volume","L3",k,canonical="m3",aliases=a)
# mass
for s,k,a in [("kg",1,()),("t",1000,("tonne",)),("g",.001,("gram",)),("mg",1e-6,()),("ug",1e-9,("µg","μg")),("lb",.45359237,("pound",)),("oz",.028349523125,("ounce",)),("stone",6.35029318,())]: add(s,"mass","M",k,canonical="kg",aliases=a)
# duration
for s,k,a in [("s",1,("sec","second")),("ns",1e-9,()),("us",1e-6,("µs","μs")),("ms",1e-3,()),("min",60,("minute",)),("h",3600,("hr","hour")),("day",86400,("d",))]: add(s,"duration","T",k,canonical="s",aliases=a)
# angle
add("rad","angle","A",1,canonical="rad",aliases=("radian",)); add("deg","angle","A",math.pi/180,canonical="rad",aliases=("degree","°",)); add("rev","angle","A",2*math.pi,canonical="rad",aliases=("revolution",)); add("arcmin","angle","A",math.pi/10800,canonical="rad"); add("arcsec","angle","A",math.pi/648000,canonical="rad")
# speed
for s,k,a in [("m/s",1,("mps",)),("km/h",1/3.6,("kph",)),("mph",.44704,()),("knot",1852/3600,("kt","kn","knots")),("ft/s",.3048,("fps",)),("ft/min",.3048/60,("fpm",))]: add(s,"speed","L/T",k,canonical="m/s",aliases=a)
# acceleration
for s,k,a in [("m/s2",1,("m/s²",)),("Gal",.01,()),("ft/s2",.3048,("ft/s²",)),("g0",9.80665,("standard_gravity","g-load"))]: add(s,"acceleration","L/T2",k,canonical="m/s2",aliases=a)
# angular speed
add("rad/s","angular_speed","A/T",1,canonical="rad/s"); add("deg/s","angular_speed","A/T",math.pi/180,canonical="rad/s"); add("rpm","frequency","1/T",1/60,canonical="Hz")
# force
for s,k,a in [("N",1,("newton",)),("kgf",9.80665,()),("lbf",4.4482216152605,()),("dyn",1e-5,("dyne",))]: add(s,"force","M*L/T2",k,canonical="N",aliases=a)
# pressure
for s,k,a in [("Pa",1,()),("hPa",100,()),("kPa",1000,()),("MPa",1e6,()),("bar",1e5,()),("mbar",100,()),("atm",101325,()),("torr",101325/760,()),("psi",6894.757293168361,()),("inHg",3386.389,("inHg_conventional",))]: add(s,"pressure","M/L/T2",k,canonical="Pa",aliases=a)
# temperature absolute / delta
add("K","temperature","Θ",1,0,"K",True,aliases=("kelvin",)); add("degC","temperature","Θ",1,273.15,"K",True,aliases=("°C","Celsius","Cel")); add("degF","temperature","Θ",5/9,459.67,"K",True,aliases=("°F","Fahrenheit",)); add("degR","temperature","Θ",5/9,0,"K",True,aliases=("Rankine",))
add("delta_K","temperature_difference","ΔΘ",1,canonical="delta_K"); add("delta_degC","temperature_difference","ΔΘ",1,canonical="delta_K"); add("delta_degF","temperature_difference","ΔΘ",5/9,canonical="delta_K")
# energy
for s,k,a in [("J",1,("joule",)),("kJ",1e3,()),("MJ",1e6,()),("Wh",3600,()),("kWh",3.6e6,()),("cal",4.184,()),("kcal",4184,()),("eV",1.602176634e-19,()),("ft*lbf",1.3558179483314004,("ft·lbf",))]: add(s,"energy","M*L2/T2",k,canonical="J",aliases=a)
# power
for s,k,a in [("W",1,("watt",)),("kW",1e3,()),("MW",1e6,()),("hp",745.69987158227022,("horsepower",)),("PS",735.49875,()),("BTU/h",.29307107,())]: add(s,"power","M*L2/T3",k,canonical="W",aliases=a)
# torque
for s,k,a in [("N*m",1,("N·m","Nm")),("lbf*ft",1.3558179483314004,("lbf·ft",)),("lbf*in",.1129848290276167,("lbf·in",)),("kgf*m",9.80665,("kgf·m",))]: add(s,"torque","M*L2/T2:torque",k,canonical="N*m",aliases=a)
# density
for s,k,a in [("kg/m3",1,("kg/m³",)),("g/cm3",1000,("g/cm³",)),("kg/L",1000,()),("g/L",1,()),("lb/ft3",16.01846337396014,("lb/ft³",))]: add(s,"density","M/L3",k,canonical="kg/m3",aliases=a)
# flow
for s,k,a in [("m3/s",1,("m³/s",)),("L/s",.001,()),("L/min",1/60000,()),("m3/h",1/3600,("m³/h",)),("cfm",.0004719474432,("ft3/min","ft³/min")),("US_gal/min",.0000630901964,("gpm",))]: add(s,"volumetric_flow","L3/T",k,canonical="m3/s",aliases=a)
for s,k,a in [("kg/s",1,()),("kg/h",1/3600,()),("lb/h",.45359237/3600,("lb/hr",))]: add(s,"mass_flow","M/T",k,canonical="kg/s",aliases=a)
# viscosity
for s,k,a in [("Pa*s",1,("Pa·s",)),("P",.1,("poise",)),("cP",.001,("centipoise",))]: add(s,"dynamic_viscosity","M/L/T",k,canonical="Pa*s",aliases=a)
for s,k,a in [("m2/s",1,("m²/s",)),("St",1e-4,("stokes",)),("cSt",1e-6,("centistokes",))]: add(s,"kinematic_viscosity","L2/T",k,canonical="m2/s",aliases=a)
# frequency
for s,k in [("Hz",1),("kHz",1e3),("MHz",1e6),("GHz",1e9)]: add(s,"frequency","1/T",k,canonical="Hz")
# electrical
for base,q,dim in [("A","current","I"),("C","charge","I*T"),("V","voltage","V"),("ohm","resistance","R"),("S","conductance","G"),("F","capacitance","Capa"),("H","inductance","Ind")]: add(base,q,dim,canonical=base)
# explicit common prefixes electrical
for pre,f in [("m",1e-3),("u",1e-6),("n",1e-9),("k",1e3),("M",1e6),("G",1e9)]:
    for base in ("A","V","ohm","F","H"):
        if pre+base not in U: add(pre+base,U[base].quantity,U[base].dimension,f,canonical=base,aliases=(("µ"+base,) if pre=="u" else ()))
# magnetic
for s,k,a in [("T",1,("tesla",)),("uT",1e-6,("µT","μT")),("nT",1e-9,()),("gauss",1e-4,("G_field",))]: add(s,"magnetic_flux_density","B",k,canonical="T",aliases=a)
# illuminance / radiation
add("lx","illuminance","Ill",canonical="lx"); add("foot-candle","illuminance","Ill",10.7639104167097,canonical="lx",aliases=("fc",))
for s,q,dim,k,canon,a in [("Bq","activity","Act",1,"Bq",()),("Ci","activity","Act",3.7e10,"Bq",()),("Gy","absorbed_dose","Dose",1,"Gy",()),("rad_dose","absorbed_dose","Dose",.01,"Gy",("rad-dose",)),("Sv","equivalent_dose","EqDose",1,"Sv",()),("rem","equivalent_dose","EqDose",.01,"Sv",()),("C/kg","exposure","Exposure",1,"C/kg",()),("R","exposure","Exposure",2.58e-4,"C/kg",("roentgen",))]: add(s,q,dim,k,canonical=canon,aliases=a)
# information
for s,k,a in [("bit",1/8,()),("byte",1,("B",)),("kbit",1000/8,()),("Mbit",1e6/8,()),("Gbit",1e9/8,()),("kB",1e3,()),("MB",1e6,()),("GB",1e9,()),("KiB",2**10,()),("MiB",2**20,()),("GiB",2**30,()),("TiB",2**40,()),("PiB",2**50,()),("EiB",2**60,())]: add(s,"information","Info",k,canonical="byte",aliases=a)

def resolve(symbol):
    s = symbol.strip().replace("²","2").replace("³","3").replace("μ","µ")
    if s in U: return U[s]
    if s in ALIASES: return U[ALIASES[s]]
    # normalize common separators/spellings
    s2=s.replace("·","*")
    if s2 in U: return U[s2]
    if s2 in ALIASES: return U[ALIASES[s2]]
    return None
