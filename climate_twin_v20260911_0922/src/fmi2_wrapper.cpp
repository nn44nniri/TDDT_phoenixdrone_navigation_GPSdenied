#include "fmi2Functions.h"
#include "micromet.hpp"
#include <array>
#include <cstring>
#include <string>
#include <new>
#include <cmath>
#include <algorithm>

namespace {
constexpr const char* GUID="{501a9e15-f039-43fa-85da-bed5414e0ae7}";
constexpr double PI=3.14159265358979323846;
constexpr double DEG=PI/180.0;
constexpr double EARTH_R=6371008.8;
enum class State {Instantiated, ExperimentSetUp, InitializationMode, StepMode, Terminated, Error};
struct Model {
  std::array<double,512> r{};
  double time=0.0,stopTime=0.0; bool stopDefined=false,logging=false;
  State state=State::Instantiated; fmi2Status lastDoStep=fmi2OK; std::string instanceName; fmi2CallbackFunctions callbacks{};
};
static double clamp(double x,double a,double b){return std::max(a,std::min(b,x));}
static double norm2(double u,double v){return std::hypot(u,v);}
struct V3 {double u=0,v=0,w=0;};

void defaults(Model& m){
  m.r.fill(0.0);m.time=0.0;m.stopTime=0.0;m.stopDefined=false;m.state=State::Instantiated;m.lastDoStep=fmi2OK;
  m.r[0]=47.525190299169076;m.r[1]=44.29738495970912;m.r[2]=100;m.r[6]=0.0068;m.r[7]=0.0004;m.r[8]=0.58;m.r[9]=0.42;m.r[10]=12;m.r[11]=180;m.r[12]=-1;m.r[13]=1.0e8;m.r[14]=1000;
  for(int k=15;k<=22;k++)m.r[k]=100;m.r[23]=-0.5;m.r[24]=0.5;m.r[25]=0.25;m.r[26]=1;m.r[27]=1;
  for(int s=0;s<5;s++){int b=27+10*s;m.r[b]=(s==0)?1:0;m.r[b+1]=47.525190299169076;m.r[b+2]=44.29738495970912;m.r[b+3]=100;m.r[b+4]=20;m.r[b+5]=50;m.r[b+6]=4;m.r[b+7]=260;m.r[b+8]=0;m.r[b+9]=1001;}
  m.r[77]=0;m.r[78]=0;m.r[79]=1;m.r[80]=1;m.r[81]=0.5;m.r[82]=1;m.r[83]=0.3613073476800489;m.r[84]=-0.11912168691995378;
  m.r[85]=1.11095;m.r[86]=7.5226122639580115;m.r[87]=1.69731;m.r[88]=0.0;m.r[89]=-1.0;m.r[90]=0.75;
  // 3DW defaults. Disabled keeps every legacy result identical.
  m.r[170]=0;m.r[171]=10;m.r[172]=100;m.r[173]=100;m.r[174]=10;
  m.r[175]=NAN;m.r[176]=NAN;m.r[177]=NAN;m.r[178]=NAN;m.r[179]=0;m.r[180]=0;m.r[181]=0;m.r[207]=0.143;
  for(int k=0;k<5;k++){int b=182+5*k;m.r[b]=10.0+70.0*k;m.r[b+1]=m.r[b+2]=m.r[b+3]=m.r[b+4]=NAN;}
}
void log(Model* m,fmi2Status s,const char* category,const char* msg){if(m&&m->logging&&m->callbacks.logger)m->callbacks.logger(m->callbacks.componentEnvironment,m->instanceName.c_str(),s,category,"%s",msg);}
micromet::In build_in(const Model* m){
  micromet::In i;i.lat=m->r[0];i.lon=m->r[1];i.elevation=m->r[2];i.slope_rad=m->r[3];i.aspect_rad=m->r[4];i.curvature_scaled_input=m->r[5];i.lapse_rate=m->r[6];i.vapor_coeff=m->r[7];i.gamma_s=m->r[8];i.gamma_c=m->r[9];i.hour_local=m->r[10];i.day_of_year=m->r[11];i.rh700=m->r[12];i.barnes_filter_m2=m->r[13];i.curvature_radius=m->r[14];
  i.zW=m->r[15];i.zE=m->r[16];i.zS=m->r[17];i.zN=m->r[18];i.zSW=m->r[19];i.zNE=m->r[20];i.zNW=m->r[21];i.zSE=m->r[22];i.curvature_min=m->r[23];i.curvature_max=m->r[24];i.precip_chi_km=m->r[25];i.use_dem_curvature=m->r[26]>=0.5;
  for(int s=0;s<5;s++){int b=27+10*s;i.sources[s].enabled=m->r[b]>=0.5;i.sources[s].lat=m->r[b+1];i.sources[s].lon=m->r[b+2];i.sources[s].elevation=m->r[b+3];i.sources[s].temperature=m->r[b+4];i.sources[s].rh=m->r[b+5];i.sources[s].wind_speed=m->r[b+6];i.sources[s].wind_dir_deg=m->r[b+7];i.sources[s].precipitation=m->r[b+8];i.sources[s].pressure_hpa=m->r[b+9];}
  i.dzdx=m->r[77];i.dzdy=m->r[78];i.derive_slope_aspect=m->r[79]>=0.5;i.barnes_second_pass=m->r[80]>=0.5;i.barnes_second_pass_radius_factor=m->r[81];
  i.calibration_enabled=m->r[82]>=0.5;i.temperature_bias_c=m->r[83];i.relative_humidity_bias_pct=m->r[84];i.wind_speed_scale=m->r[85];i.wind_direction_bias_deg=m->r[86];i.shortwave_scale=m->r[87];i.pressure_bias_pa=m->r[88];i.shortwave_reference_wm2=m->r[89];i.shortwave_reference_blend=m->r[90];return i;
}

// Shift a request in a local tangent plane while approximating terrain by its local DEM gradient.
micromet::Out base_at(const micromet::In& center,double center_ground,double dx,double dy,double agl){
  micromet::In q=center; double latr=center.lat*DEG; q.lat=center.lat+(dy/EARTH_R)/DEG; q.lon=center.lon+(dx/(EARTH_R*std::max(std::cos(latr),1e-6)))/DEG;
  double ground=center_ground+center.dzdx*dx+center.dzdy*dy; q.elevation=ground+std::max(0.0,agl);
  q.zW=q.zE=q.zS=q.zN=q.zSW=q.zNE=q.zNW=q.zSE=q.elevation; q.use_dem_curvature=false;
  return micromet::compute(q);
}

struct ExtCtx {const Model* m; micromet::In in; micromet::Out center; double ground=0,dx=100,dy=100,dz=10,alpha=0,dt=0,power=0.143; int levels=0;};

void interp_profile(const ExtCtx& c,int endpoint,double agl,double& u,double& v,double& lo,double& hi){
  const double a=std::max(0.0,agl); int n=std::max(0,std::min(5,c.levels));
  if(n>=2){
    std::array<double,5> z{},uu{},vv{}; int valid=0;
    for(int k=0;k<n;k++){int b=182+5*k; double zk=c.m->r[b], uk=c.m->r[b+(endpoint==0?1:3)], vk=c.m->r[b+(endpoint==0?2:4)]; if(std::isfinite(zk)&&std::isfinite(uk)&&std::isfinite(vk)){z[valid]=zk;uu[valid]=uk;vv[valid]=vk;valid++;}}
    if(valid>=2){
      int j=0; while(j+1<valid && a>z[j+1])j++; if(j>=valid-1)j=valid-2; if(a<=z[0])j=0;
      double den=z[j+1]-z[j],f=std::abs(den)>1e-9?(a-z[j])/den:0; f=clamp(f,0.0,1.0); u=(1-f)*uu[j]+f*uu[j+1];v=(1-f)*vv[j]+f*vv[j+1];lo=z[j];hi=z[j+1];return;
    }
  }
  double u10=c.m->r[endpoint==0?175:177],v10=c.m->r[endpoint==0?176:178];
  if(!std::isfinite(u10)||!std::isfinite(v10)){u10=c.center.u_t;v10=c.center.v_t;}
  double f=std::pow(std::max(a,1.0)/10.0,c.power); u=u10*f;v=v10*f;lo=10;hi=10;
}

void rotate_scale(double bu,double bv,double cu,double cv,double du,double dv,double& u,double& v){
  double bs=norm2(bu,bv),cs=norm2(cu,cv),ds=norm2(du,dv); if(cs<1e-9){u=du;v=dv;return;} double sc=ds/cs;
  double ac=std::atan2(cv,cu),ad=std::atan2(dv,du),ang=ad-ac; double x=bu*std::cos(ang)-bv*std::sin(ang),y=bu*std::sin(ang)+bv*std::cos(ang); u=sc*x;v=sc*y; (void)bs;
}

void horizontal_endpoint(const ExtCtx& c,int endpoint,double dx,double dy,double agl,double& u,double& v){
  auto b=base_at(c.in,c.ground,dx,dy,10.0); double desired_u,desired_v,lo,hi;interp_profile(c,endpoint,agl,desired_u,desired_v,lo,hi);
  double center_at_agl_u,center_at_agl_v,dummy1,dummy2; // profile determines desired center vector directly.
  interp_profile(c,endpoint,agl,center_at_agl_u,center_at_agl_v,dummy1,dummy2);
  rotate_scale(b.u_t,b.v_t,c.center.u_t,c.center.v_t,center_at_agl_u,center_at_agl_v,u,v);
  if(dx==0.0&&dy==0.0){u=desired_u;v=desired_v;}
}
void horizontal_current(const ExtCtx& c,double dx,double dy,double agl,double& u,double& v){double u0,v0,u1,v1;horizontal_endpoint(c,0,dx,dy,agl,u0,v0);horizontal_endpoint(c,1,dx,dy,agl,u1,v1);u=(1-c.alpha)*u0+c.alpha*u1;v=(1-c.alpha)*v0+c.alpha*v1;}

double divergence_at(const ExtCtx& c,int endpoint,double x,double y,double agl){
  double up,vp,um,vm; double dx=std::max(c.dx,1.0),dy=std::max(c.dy,1.0);
  if(endpoint<0){horizontal_current(c,x+dx,y,agl,up,vp);horizontal_current(c,x-dx,y,agl,um,vm);}else{horizontal_endpoint(c,endpoint,x+dx,y,agl,up,vp);horizontal_endpoint(c,endpoint,x-dx,y,agl,um,vm);} double du=(up-um)/(2*dx);
  if(endpoint<0){horizontal_current(c,x,y+dy,agl,up,vp);horizontal_current(c,x,y-dy,agl,um,vm);}else{horizontal_endpoint(c,endpoint,x,y+dy,agl,up,vp);horizontal_endpoint(c,endpoint,x,y-dy,agl,um,vm);} double dv=(vp-vm)/(2*dy); return du+dv;
}
V3 velocity_at(const ExtCtx& c,int endpoint,double x,double y,double agl){
  V3 q;if(endpoint<0)horizontal_current(c,x,y,agl,q.u,q.v);else horizontal_endpoint(c,endpoint,x,y,agl,q.u,q.v);
  double boundary=std::min(10.0,std::max(1.0,agl)); double uh,vh;if(endpoint<0)horizontal_current(c,x,y,boundary,uh,vh);else horizontal_endpoint(c,endpoint,x,y,boundary,uh,vh);
  double wh=uh*c.in.dzdx+vh*c.in.dzdy; double d0=divergence_at(c,endpoint,x,y,boundary),d1=divergence_at(c,endpoint,x,y,std::max(agl,boundary)); q.w=wh-std::max(0.0,agl)*0.5*(d0+d1);return q;
}

void calc3dw(Model* m,const micromet::In& in,const micromet::Out& o){
  for(int k=220;k<246;k++)m->r[k]=0.0; if(m->r[170]<0.5){m->r[244]=0;m->r[245]=0;return;}
  ExtCtx c{m,in,o};c.ground=in.elevation-m->r[171];c.dx=std::max(std::abs(m->r[172]),1.0);c.dy=std::max(std::abs(m->r[173]),1.0);c.dz=std::max(std::abs(m->r[174]),0.5);c.dt=std::max(0.0,m->r[179]);c.alpha=clamp(m->r[180],0.0,1.0);c.levels=(int)std::llround(m->r[181]);c.power=clamp(m->r[207],0.0,1.0);
  double agl=std::max(0.0,m->r[171]); V3 v=velocity_at(c,-1,0,0,agl); m->r[220]=v.u;m->r[221]=v.v;m->r[222]=v.w;m->r[223]=std::sqrt(v.u*v.u+v.v*v.v+v.w*v.w);
  V3 xp=velocity_at(c,-1,c.dx,0,agl),xm=velocity_at(c,-1,-c.dx,0,agl),yp=velocity_at(c,-1,0,c.dy,agl),ym=velocity_at(c,-1,0,-c.dy,agl);
  V3 zp,zm; bool one=agl<c.dz; if(one){zm=velocity_at(c,-1,0,0,agl);zp=velocity_at(c,-1,0,0,agl+c.dz);}else{zm=velocity_at(c,-1,0,0,agl-c.dz);zp=velocity_at(c,-1,0,0,agl+c.dz);}
  double ddz=one?c.dz:2*c.dz;
  double g[9]={(xp.u-xm.u)/(2*c.dx),(yp.u-ym.u)/(2*c.dy),(zp.u-zm.u)/ddz,(xp.v-xm.v)/(2*c.dx),(yp.v-ym.v)/(2*c.dy),(zp.v-zm.v)/ddz,(xp.w-xm.w)/(2*c.dx),(yp.w-ym.w)/(2*c.dy),(zp.w-zm.w)/ddz};
  for(int k=0;k<9;k++)m->r[224+k]=g[k];
  V3 e0=velocity_at(c,0,0,0,agl),e1=velocity_at(c,1,0,0,agl); double du_dt=0,dv_dt=0,dw_dt=0;if(c.dt>0){du_dt=(e1.u-e0.u)/c.dt;dv_dt=(e1.v-e0.v)/c.dt;dw_dt=(e1.w-e0.w)/c.dt;}m->r[233]=du_dt;m->r[234]=dv_dt;m->r[235]=dw_dt;
  m->r[236]=du_dt+v.u*g[0]+v.v*g[1]+v.w*g[2];m->r[237]=dv_dt+v.u*g[3]+v.v*g[4]+v.w*g[5];m->r[238]=dw_dt+v.u*g[6]+v.v*g[7]+v.w*g[8];m->r[239]=g[0]+g[4]+g[8];
  double lu,lv,lo,hi;interp_profile(c,0,agl,lu,lv,lo,hi);m->r[240]=lo;m->r[241]=hi;m->r[242]=0;m->r[243]=c.dt;m->r[244]=(std::isfinite(v.u)&&std::isfinite(v.v)&&std::isfinite(v.w))?1.0:0.0;m->r[245]=(c.levels>=2)?1.0:0.0;
}

void calc(Model* m){
  auto i=build_in(m);auto o=micromet::compute(i);
  double a[]={o.barnes_w,o.t0,o.t,o.es_source,o.e,o.td,o.td_lapse,o.rh,o.u,o.v,o.w,o.theta,o.slope,o.aspect,o.curvature,o.omega_s,o.wind_weight,o.wind_t,o.theta_div,o.theta_t,o.u_t,o.v_t,o.cloud,o.zenith,o.hour_angle,o.declination,o.incidence,o.solar_azimuth,o.tau_dir,o.tau_dif,o.qsi,o.emissivity,o.qli,o.pressure,o.precip,o.barnes_distance,o.es_source,o.td_source,o.td_ref,o.es_local,o.e_local,o.curvature_raw,o.omega_s_raw,o.rh700_used,o.coeff_x,o.coeff_y,o.coeff_z,o.pressure_forecast,o.source_elevation_barnes,o.precip_reference,o.enabled_source_count,o.formula_valid,o.formula_error_code,m->time,o.dzdx_used,o.dzdy_used,o.second_pass_applied,o.raw_temperature,o.raw_relative_humidity,o.raw_wind_speed,o.raw_wind_direction,o.raw_shortwave,o.raw_pressure};
  for(size_t k=0;k<sizeof(a)/sizeof(a[0]);k++)m->r[100+k]=a[k]; calc3dw(m,i,o); m->r[246]=o.air_density;
}
fmi2Status bad(Model* m,const char* msg){if(m){m->state=State::Error;log(m,fmi2Error,"fmi",msg);}return fmi2Error;}
bool validVR(const fmi2ValueReference vr[],size_t n){if(!vr)return false;for(size_t i=0;i<n;i++)if(vr[i]>=512)return false;return true;}
bool writable(fmi2ValueReference vr){return vr<100 || (vr>=170&&vr<=207);}
}
extern "C" {
const char* fmi2GetTypesPlatform(){return "default";} const char* fmi2GetVersion(){return "2.0";}
fmi2Status fmi2SetDebugLogging(fmi2Component c,fmi2Boolean loggingOn,size_t,const fmi2String[]){auto*m=static_cast<Model*>(c);if(!m)return fmi2Error;m->logging=loggingOn!=0;return fmi2OK;}
fmi2Component fmi2Instantiate(fmi2String name,fmi2Type type,fmi2String guid,fmi2String,const fmi2CallbackFunctions* cb,fmi2Boolean,fmi2Boolean loggingOn){if(type!=fmi2CoSimulation||!guid||std::strcmp(guid,GUID)!=0)return nullptr;auto*m=new(std::nothrow) Model();if(!m)return nullptr;defaults(*m);m->instanceName=name?name:"MicroMetTwin";m->logging=loggingOn!=0;if(cb)m->callbacks=*cb;calc(m);return m;}
void fmi2FreeInstance(fmi2Component c){delete static_cast<Model*>(c);} fmi2Status fmi2SetupExperiment(fmi2Component c,fmi2Boolean,fmi2Real,fmi2Real start,fmi2Boolean stopDefined,fmi2Real stop){auto*m=static_cast<Model*>(c);if(!m||m->state!=State::Instantiated)return fmi2Error;m->time=start;m->stopDefined=stopDefined!=0;m->stopTime=stop;m->state=State::ExperimentSetUp;calc(m);return fmi2OK;}
fmi2Status fmi2EnterInitializationMode(fmi2Component c){auto*m=static_cast<Model*>(c);if(!m||(m->state!=State::Instantiated&&m->state!=State::ExperimentSetUp))return fmi2Error;m->state=State::InitializationMode;return fmi2OK;} fmi2Status fmi2ExitInitializationMode(fmi2Component c){auto*m=static_cast<Model*>(c);if(!m||m->state!=State::InitializationMode)return fmi2Error;calc(m);m->state=State::StepMode;return fmi2OK;}
fmi2Status fmi2Terminate(fmi2Component c){auto*m=static_cast<Model*>(c);if(!m||m->state==State::Terminated)return fmi2Error;m->state=State::Terminated;return fmi2OK;} fmi2Status fmi2Reset(fmi2Component c){auto*m=static_cast<Model*>(c);if(!m)return fmi2Error;auto cb=m->callbacks;auto name=m->instanceName;bool logging=m->logging;defaults(*m);m->callbacks=cb;m->instanceName=name;m->logging=logging;calc(m);return fmi2OK;}
fmi2Status fmi2GetReal(fmi2Component c,const fmi2ValueReference vr[],size_t n,fmi2Real v[]){auto*m=static_cast<Model*>(c);if(!m||!v||!validVR(vr,n))return fmi2Error;for(size_t j=0;j<n;j++)v[j]=m->r[vr[j]];return fmi2OK;} fmi2Status fmi2SetReal(fmi2Component c,const fmi2ValueReference vr[],size_t n,const fmi2Real v[]){auto*m=static_cast<Model*>(c);if(!m||!v||!validVR(vr,n)||m->state==State::Terminated||m->state==State::Error)return fmi2Error;for(size_t j=0;j<n;j++){if(!writable(vr[j]))return fmi2Error;m->r[vr[j]]=v[j];}calc(m);return fmi2OK;}
fmi2Status fmi2GetInteger(fmi2Component,const fmi2ValueReference[],size_t n,fmi2Integer v[]){if(!v)return fmi2Error;for(size_t j=0;j<n;j++)v[j]=0;return fmi2OK;} fmi2Status fmi2SetInteger(fmi2Component,const fmi2ValueReference[],size_t,const fmi2Integer[]){return fmi2Error;} fmi2Status fmi2GetBoolean(fmi2Component,const fmi2ValueReference[],size_t n,fmi2Boolean v[]){if(!v)return fmi2Error;for(size_t j=0;j<n;j++)v[j]=fmi2False;return fmi2OK;} fmi2Status fmi2SetBoolean(fmi2Component,const fmi2ValueReference[],size_t,const fmi2Boolean[]){return fmi2Error;} fmi2Status fmi2GetString(fmi2Component,const fmi2ValueReference[],size_t n,fmi2String v[]){if(!v)return fmi2Error;for(size_t j=0;j<n;j++)v[j]="";return fmi2OK;} fmi2Status fmi2SetString(fmi2Component,const fmi2ValueReference[],size_t,const fmi2String[]){return fmi2Error;}
fmi2Status fmi2DoStep(fmi2Component c,fmi2Real current,fmi2Real h,fmi2Boolean){auto*m=static_cast<Model*>(c);if(!m||m->state!=State::StepMode||h<=0)return fmi2Error;if(std::abs(current-m->time)>1e-7)m->lastDoStep=fmi2Warning;else m->lastDoStep=fmi2OK;double next=current+h;if(m->stopDefined&&next>m->stopTime+1e-12){m->lastDoStep=fmi2Discard;return fmi2Discard;}m->time=next;calc(m);return m->lastDoStep;} fmi2Status fmi2CancelStep(fmi2Component){return fmi2Error;}
fmi2Status fmi2GetStatus(fmi2Component c,fmi2StatusKind kind,fmi2Status*s){auto*m=static_cast<Model*>(c);if(!m||!s)return fmi2Error;if(kind==fmi2DoStepStatus||kind==fmi2PendingStatus){*s=m->lastDoStep;return fmi2OK;}return fmi2Discard;} fmi2Status fmi2GetRealStatus(fmi2Component c,fmi2StatusKind kind,fmi2Real*v){auto*m=static_cast<Model*>(c);if(!m||!v)return fmi2Error;if(kind==fmi2LastSuccessfulTime){*v=m->time;return fmi2OK;}return fmi2Discard;} fmi2Status fmi2GetIntegerStatus(fmi2Component,fmi2StatusKind,fmi2Integer*){return fmi2Discard;} fmi2Status fmi2GetBooleanStatus(fmi2Component c,fmi2StatusKind kind,fmi2Boolean*v){auto*m=static_cast<Model*>(c);if(!m||!v)return fmi2Error;if(kind==fmi2Terminated){*v=m->state==State::Terminated?fmi2True:fmi2False;return fmi2OK;}return fmi2Discard;} fmi2Status fmi2GetStringStatus(fmi2Component,fmi2StatusKind,fmi2String*){return fmi2Discard;}
}
