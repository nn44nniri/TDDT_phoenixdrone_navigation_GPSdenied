#pragma once
#include <array>
namespace micromet {
constexpr int MAX_SOURCES = 5;
struct Source {
  double lat=0, lon=0, elevation=0, temperature=0, rh=0, wind_speed=0,
         wind_dir_deg=0, precipitation=0, pressure_hpa=1013.0;
  bool enabled=false;
};
struct In {
  double lat=0, lon=0, elevation=0;
  double slope_rad=0, aspect_rad=0, dzdx=0, dzdy=0;
  bool derive_slope_aspect=true;
  double curvature_scaled_input=0;
  double lapse_rate=0.0068, vapor_coeff=0.0004;
  double gamma_s=0.58, gamma_c=0.42;
  double hour_local=12, day_of_year=180, rh700=-1;
  double barnes_filter_m2=1.0e8;
  bool barnes_second_pass=true;
  double barnes_second_pass_radius_factor=0.5;
  double curvature_radius=1000;
  double zW=0,zE=0,zS=0,zN=0,zSW=0,zNE=0,zNW=0,zSE=0;
  double curvature_min=-0.5, curvature_max=0.5;
  double precip_chi_km=0.25;
  bool use_dem_curvature=true;
  // Optional empirical calibration layer. The physical MicroMet equations remain
  // available as raw diagnostic outputs; these coefficients adjust the primary
  // outputs after the formalism is evaluated.
  bool calibration_enabled=true;
  double temperature_bias_c=0.3613073476800489;
  double relative_humidity_bias_pct=-0.11912168691995378;
  double wind_speed_scale=1.11095;
  double wind_direction_bias_deg=7.5226122639580115;
  double shortwave_scale=1.69731;
  double pressure_bias_pa=0.0;
  // Optional forecast anchoring for shortwave radiation. A negative reference disables blending.
  double shortwave_reference_wm2=-1.0;
  double shortwave_reference_blend=0.75;
  std::array<Source,MAX_SOURCES> sources{};
};
struct Out {
  double barnes_distance=0,barnes_w=1;
  double t0=0,t=0,es_source=0,e=0,td_source=0,td_lapse=0,td_ref=0,td=0,es_local=0,e_local=0,rh=0;
  double u=0,v=0,w=0,theta=0;
  double slope=0,aspect=0,curvature_raw=0,curvature=0,omega_s_raw=0,omega_s=0,wind_weight=1,wind_t=0,theta_div=0,theta_t=0,u_t=0,v_t=0;
  double rh700_used=0,cloud=0,zenith=0,hour_angle=0,declination=0,incidence=0,solar_azimuth=0,tau_dir=0,tau_dif=0,qsi=0;
  double coeff_x=0,coeff_y=0,coeff_z=0,emissivity=0,qli=0,pressure=0,pressure_forecast=0,precip=0,air_density=0;
  double source_elevation_barnes=0, precip_reference=0;
  double enabled_source_count=0, formula_valid=1, formula_error_code=0;
  double dzdx_used=0,dzdy_used=0,second_pass_applied=0;
  double raw_temperature=0,raw_relative_humidity=0,raw_wind_speed=0,
         raw_wind_direction=0,raw_shortwave=0,raw_pressure=0;
};
double haversine_m(double lat1,double lon1,double lat2,double lon2);
double barnes_weight(double r,double filter_m2);
double saturation_vapor_pressure(double T,bool ice=false);
double dewpoint(double e,bool ice=false);
double dewpoint_lapse(double vapor_coeff,bool ice=false);
double surface_pressure(double z);
double moist_air_density(double pressure_pa,double temperature_c,double relative_humidity_pct);
double terrain_slope(double dzdx,double dzdy);
double terrain_aspect(double dzdx,double dzdy);
double terrain_curvature(double z,double zW,double zE,double zS,double zN,double zSW,double zNE,double zNW,double zSE,double eta);
Out compute(const In& in);
}
