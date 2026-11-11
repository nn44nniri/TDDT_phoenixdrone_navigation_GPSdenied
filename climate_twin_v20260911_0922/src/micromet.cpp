#include "micromet.hpp"
#include <cmath>
#include <algorithm>
#include <array>
#include <vector>
namespace micromet {
static constexpr double PI=3.14159265358979323846;
static constexpr double DEG=PI/180.0;
static double clamp(double x,double a,double b){return std::max(a,std::min(b,x));}
static double wrap2pi(double x){x=std::fmod(x,2*PI); return x<0?x+2*PI:x;}
double haversine_m(double lat1,double lon1,double lat2,double lon2){
  const double R=6371008.8; double p1=lat1*DEG,p2=lat2*DEG,dp=(lat2-lat1)*DEG,dl=(lon2-lon1)*DEG;
  double a=std::sin(dp/2)*std::sin(dp/2)+std::cos(p1)*std::cos(p2)*std::sin(dl/2)*std::sin(dl/2);
  return 2*R*std::asin(std::sqrt(clamp(a,0,1)));
}
double barnes_weight(double r,double filter_m2){return std::exp(-(r*r)/std::max(filter_m2,1e-12));}
double saturation_vapor_pressure(double T,bool ice){double a=ice?611.15:611.21,b=ice?22.452:17.502,c=ice?272.55:240.97;return a*std::exp(b*T/(c+T));}
double dewpoint(double e,bool ice){double a=ice?611.15:611.21,b=ice?22.452:17.502,c=ice?272.55:240.97;double l=std::log(std::max(e,1e-12)/a);return c*l/(b-l);}
double dewpoint_lapse(double vc,bool ice){double b=ice?22.452:17.502,c=ice?272.55:240.97;return vc*c/b;}
double surface_pressure(double z){return 101300.0*std::exp(-z/8000.0);}
double moist_air_density(double pressure_pa,double temperature_c,double relative_humidity_pct){
  // Operational thermodynamic extension outside the original MicroMet equations:
  // rho = p_d/(R_d T) + e/(R_v T), with e derived from operational T and RH.
  constexpr double R_D=287.05;
  constexpr double R_V=461.495;
  const double tk=temperature_c+273.15;
  if(!std::isfinite(pressure_pa)||!std::isfinite(tk)||tk<=0.0||pressure_pa<=0.0)return NAN;
  const double es=saturation_vapor_pressure(temperature_c,false);
  const double e=clamp(relative_humidity_pct,0.0,100.0)/100.0*es;
  const double pd=std::max(0.0,pressure_pa-e);
  return pd/(R_D*tk)+e/(R_V*tk);
}
double terrain_slope(double dzdx,double dzdy){return std::atan(std::sqrt(dzdx*dzdx+dzdy*dzdy));}
double terrain_aspect(double dzdx,double dzdy){return wrap2pi(1.5*PI-std::atan2(dzdy,dzdx));}
double terrain_curvature(double z,double zW,double zE,double zS,double zN,double zSW,double zNE,double zNW,double zSE,double eta){
  eta=std::max(std::abs(eta),1e-9);
  return 0.25*((z-0.5*(zW+zE))/(2*eta)+(z-0.5*(zS+zN))/(2*eta)+(z-0.5*(zSW+zNE))/(2*std::sqrt(2.0)*eta)+(z-0.5*(zNW+zSE))/(2*std::sqrt(2.0)*eta));
}

struct Prepared { const Source* s; double r,w,t0,td0,u,v,pr,pressure,z,es,e; };
static double first_at(const std::vector<Prepared>& p,double lat,double lon,double filter,int field){
  double sw=0,sv=0;
  for(const auto&q:p){double w=barnes_weight(haversine_m(lat,lon,q.s->lat,q.s->lon),filter); const double vals[]={q.t0,q.td0,q.u,q.v,q.pr,q.pressure,q.z,q.es,q.e}; sw+=w;sv+=w*vals[field];}
  return sw>1e-15?sv/sw:0.0;
}
static double two_pass_target(const std::vector<Prepared>& p,double lat,double lon,double filter,double radius_factor,int field,bool enabled){
  double base=first_at(p,lat,lon,filter,field);
  if(!enabled || p.size()<2) return base;
  std::vector<double> residual(p.size());
  for(size_t j=0;j<p.size();++j){const double vals[]={p[j].t0,p[j].td0,p[j].u,p[j].v,p[j].pr,p[j].pressure,p[j].z,p[j].es,p[j].e}; residual[j]=vals[field]-first_at(p,p[j].s->lat,p[j].s->lon,filter,field);}
  double f2=filter*clamp(radius_factor,0.05,1.0)*clamp(radius_factor,0.05,1.0), sw=0,sc=0;
  for(size_t j=0;j<p.size();++j){double w=barnes_weight(haversine_m(lat,lon,p[j].s->lat,p[j].s->lon),f2);sw+=w;sc+=w*residual[j];}
  return base+(sw>1e-15?sc/sw:0.0);
}

Out compute(const In& in){
  Out o; std::vector<Prepared> p; p.reserve(MAX_SOURCES); o.td_lapse=dewpoint_lapse(in.vapor_coeff,false);
  for(const auto&s:in.sources){if(!s.enabled)continue; double r=haversine_m(in.lat,in.lon,s.lat,s.lon);double bw=barnes_weight(r,in.barnes_filter_m2);
    double t0=s.temperature-in.lapse_rate*(0.0-s.elevation);double es=saturation_vapor_pressure(s.temperature,false);double e=clamp(s.rh,0,100)/100.0*es;double td=dewpoint(e,false);double td0=td-o.td_lapse*(0.0-s.elevation);double th=s.wind_dir_deg*DEG;
    p.push_back(Prepared{&s,r,bw,t0,td0,-s.wind_speed*std::sin(th),-s.wind_speed*std::cos(th),s.precipitation,s.pressure_hpa*100.0,s.elevation,es,e});
  }
  o.enabled_source_count=(double)p.size();
  if(p.empty()){o.formula_valid=0;o.formula_error_code=1;return o;}
  o.barnes_distance=p.front().r;o.barnes_w=p.front().w;o.second_pass_applied=(in.barnes_second_pass&&p.size()>1)?1.0:0.0;
  auto fld=[&](int k){return two_pass_target(p,in.lat,in.lon,in.barnes_filter_m2,in.barnes_second_pass_radius_factor,k,in.barnes_second_pass);};
  o.t0=fld(0);o.t=o.t0-in.lapse_rate*in.elevation;o.td_ref=fld(1);o.td=o.td_ref-o.td_lapse*in.elevation;o.u=fld(2);o.v=fld(3);o.precip_reference=fld(4);o.pressure_forecast=fld(5);o.source_elevation_barnes=fld(6);o.es_source=fld(7);o.e=fld(8);o.td_source=dewpoint(o.e,false);
  o.es_local=saturation_vapor_pressure(o.t,false);o.e_local=saturation_vapor_pressure(o.td,false);o.rh=clamp(100.0*o.e_local/std::max(o.es_local,1e-12),0,100);
  o.w=std::hypot(o.u,o.v);o.theta=wrap2pi(1.5*PI-std::atan2(o.v,o.u));
  o.dzdx_used=in.dzdx;o.dzdy_used=in.dzdy;
  o.slope=in.derive_slope_aspect?terrain_slope(in.dzdx,in.dzdy):in.slope_rad;
  o.aspect=in.derive_slope_aspect?terrain_aspect(in.dzdx,in.dzdy):wrap2pi(in.aspect_rad);
  o.curvature_raw=terrain_curvature(in.elevation,in.zW,in.zE,in.zS,in.zN,in.zSW,in.zNE,in.zNW,in.zSE,in.curvature_radius);
  if(in.use_dem_curvature){double den=in.curvature_max-in.curvature_min;o.curvature=std::abs(den)>1e-12?clamp(-0.5+(o.curvature_raw-in.curvature_min)/den,-0.5,0.5):clamp(o.curvature_raw,-0.5,0.5);}else{o.curvature=clamp(in.curvature_scaled_input,-0.5,0.5);}
  o.omega_s_raw=o.slope*std::cos(o.theta-o.aspect);o.omega_s=clamp(o.omega_s_raw,-0.5,0.5);o.wind_weight=1+in.gamma_s*o.omega_s+in.gamma_c*o.curvature;o.wind_t=o.wind_weight*o.w;
  o.theta_div=-0.5*o.omega_s*std::sin(2*(o.aspect-o.theta));o.theta_t=wrap2pi(o.theta+o.theta_div);o.u_t=-o.wind_t*std::sin(o.theta_t);o.v_t=-o.wind_t*std::cos(o.theta_t);
  if(in.rh700>=0){o.rh700_used=clamp(in.rh700,0,100);}else{double z700=-8000.0*std::log(70000.0/101300.0);double T700=o.t0-in.lapse_rate*z700;double Td700=o.td_ref-o.td_lapse*z700;double es700=saturation_vapor_pressure(T700,false);double e700=saturation_vapor_pressure(Td700,false);o.rh700_used=clamp(100.0*e700/std::max(es700,1e-12),0,100);}
  o.cloud=clamp(0.832*std::exp((o.rh700_used-100.0)/41.6),0,1);
  double phi=in.lat*DEG,phiT=23.45*DEG;o.hour_angle=PI*(in.hour_local/12.0-1.0);o.declination=phiT*std::cos(2*PI*(in.day_of_year-172.0)/365.25);
  double cz=std::sin(phi)*std::sin(o.declination)+std::cos(phi)*std::cos(o.declination)*std::cos(o.hour_angle);o.zenith=std::acos(clamp(cz,-1,1));double sz=std::max(std::sin(o.zenith),1e-12);
  o.solar_azimuth=std::asin(clamp(std::cos(o.declination)*std::sin(o.hour_angle)/sz,-1,1));double aspectSouth=wrap2pi(o.aspect-PI);double ci=std::cos(o.slope)*std::cos(o.zenith)+std::sin(o.slope)*std::sin(o.zenith)*std::cos(o.solar_azimuth-aspectSouth);o.incidence=std::acos(clamp(ci,-1,1));
  o.tau_dir=(0.6-0.2*std::cos(o.zenith))*(1-o.cloud);o.tau_dif=(0.3-0.1*std::cos(o.zenith))*o.cloud;o.qsi=1370.0*std::max(0.0,o.tau_dir*std::cos(o.incidence)+o.tau_dif*std::cos(o.zenith));
  double z=in.elevation;auto interp=[z](double c1,double c2){if(z<200)return c1;if(z>3000)return c2;return c1+(z-200)*(c2-c1)/2800.0;};o.coeff_x=interp(0.35,0.51);o.coeff_y=interp(0.100,0.130);o.coeff_z=interp(0.224,1.100);double TK=o.t+273.15;
  o.emissivity=1.083*(1+o.coeff_z*o.cloud*o.cloud)*(1-o.coeff_x*std::exp(-o.coeff_y*o.e_local/TK));o.qli=o.emissivity*5.670374419e-8*std::pow(TK,4);
  // Equation (32) remains the fallback pressure model. When forecast surface
  // pressure is available, anchor the exponential height correction at the
  // Barnes-interpolated source elevation. This preserves the formalism while
  // retaining the time-varying pressure information supplied by the forecast.
  if(std::isfinite(o.pressure_forecast) && o.pressure_forecast>1000.0){
    o.pressure=o.pressure_forecast*std::exp(-(in.elevation-o.source_elevation_barnes)/8000.0);
  }else{
    o.pressure=surface_pressure(in.elevation);
  }
  double dz_km=(in.elevation-o.source_elevation_barnes)/1000.0;double den=1-in.precip_chi_km*dz_km;o.precip=den>1e-12?o.precip_reference*(1+in.precip_chi_km*dz_km)/den:o.precip_reference;

  // Preserve the uncalibrated formalism outputs for traceability.
  o.raw_temperature=o.t;o.raw_relative_humidity=o.rh;o.raw_wind_speed=o.wind_t;
  o.raw_wind_direction=o.theta_t;o.raw_shortwave=o.qsi;o.raw_pressure=o.pressure;

  // Optional calibration layer derived from the packaged consistency evaluation.
  // It is deliberately simple, transparent, and disable-able through FMI inputs.
  if(in.calibration_enabled){
    o.t += in.temperature_bias_c;
    o.rh = clamp(o.rh + in.relative_humidity_bias_pct,0.0,100.0);
    o.wind_t = std::max(0.0,o.wind_t*std::max(0.0,in.wind_speed_scale));
    o.theta_t = wrap2pi(o.theta_t + in.wind_direction_bias_deg*DEG);
    o.u_t=-o.wind_t*std::sin(o.theta_t);o.v_t=-o.wind_t*std::cos(o.theta_t);
    o.qsi = std::max(0.0,o.qsi*std::max(0.0,in.shortwave_scale));
    // When a coarse forecast shortwave value is supplied, retain the MicroMet
    // terrain/solar solution while anchoring its magnitude to the forcing data.
    // A blend of zero returns the pure formalism; a blend of one follows the
    // forecast reference. Night-time zero is preserved explicitly.
    if(std::isfinite(in.shortwave_reference_wm2) && in.shortwave_reference_wm2>=0.0){
      const double a=clamp(in.shortwave_reference_blend,0.0,1.0);
      o.qsi=(1.0-a)*o.qsi+a*in.shortwave_reference_wm2;
      if(in.shortwave_reference_wm2<=1e-9)o.qsi=0.0;
    }
    o.pressure += in.pressure_bias_pa;
  }
  // Derived operational air density. It intentionally uses the final operational
  // temperature, RH and pressure so downstream twins receive a thermodynamically
  // consistent rho with the values exposed by this FMU.
  o.air_density=moist_air_density(o.pressure,o.t,o.rh);
  if(!std::isfinite(o.t)||!std::isfinite(o.rh)||!std::isfinite(o.wind_t)||!std::isfinite(o.pressure)||!std::isfinite(o.air_density)){o.formula_valid=0;o.formula_error_code=2;}
  return o;
}
}
