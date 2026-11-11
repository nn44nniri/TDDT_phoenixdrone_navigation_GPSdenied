#include "fmi2Functions.h"
#include "phoenix_v3_params.h"
#include "generated_vr.h"
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>
#include <numeric>
#include <array>
struct Base { std::vector<double> r; std::vector<int> i; std::vector<int> b; std::vector<std::string> s; double t=0; bool init=false; virtual ~Base(){}; virtual void step(double dt)=0; };
namespace {
constexpr double G=9.80665, Rair=287.05287, T0=288.15, P0=101325.0, L0=0.0065;
inline double sq(double x){return x*x;} inline double clamp(double x,double a,double b){return std::min(b,std::max(a,x));}
inline double norm3(double x,double y,double z){return std::sqrt(x*x+y*y+z*z);} 
inline void qnormalize(double &q0,double &q1,double &q2,double &q3){double n=std::sqrt(q0*q0+q1*q1+q2*q2+q3*q3); if(n<1e-12){q0=1;q1=q2=q3=0;} else {q0/=n;q1/=n;q2/=n;q3/=n;}}
inline void quatRbn(double q0,double q1,double q2,double q3,double R[9]){qnormalize(q0,q1,q2,q3);R[0]=1-2*(q2*q2+q3*q3);R[1]=2*(q1*q2-q0*q3);R[2]=2*(q1*q3+q0*q2);R[3]=2*(q1*q2+q0*q3);R[4]=1-2*(q1*q1+q3*q3);R[5]=2*(q2*q3-q0*q1);R[6]=2*(q1*q3-q0*q2);R[7]=2*(q2*q3+q0*q1);R[8]=1-2*(q1*q1+q2*q2);}
inline double smoothWing(double Va){double x=clamp((Va-phoenix_v3::stall_speed_m_s)/(phoenix_v3::min_command_speed_m_s-phoenix_v3::stall_speed_m_s),0.0,1.0);return x*x*(3-2*x);}
inline double motorNext(double omega,double cmd,double dt){cmd=clamp(cmd,0.0,1.0);double wc=phoenix_v3::motor_omega_max_rad_s*cmd;double tau=(wc>=omega)?phoenix_v3::motor_tau_up_s:phoenix_v3::motor_tau_down_s;return wc+(omega-wc)*std::exp(-std::max(0.0,dt)/tau);}
// Official Phoenix SDF axes: forward=[0,0,1], upward=[1,0,0]. Canonical body is FRD.
inline void phoenixSourceToFRD(double sx,double sy,double sz,double &bx,double &by,double &bz){bx=sz;by=sy;bz=-sx;}

struct W:Base{
 static constexpr int BASE=95, OP=95, IN=96, OUT=288;
 W(){r.assign(512,0.0);i.assign(8,0);b.assign(16,0);s.assign(4,"");r[22]=5;r[23]=.8;r[24]=2.5;r[25]=.8;r[26]=.03;r[27]=1.4;r[31]=1.225;r[32]=288.15;r[33]=101325;r[48]=22.2;r[49]=.08;r[50]=10;r[51]=1;r[46]=.9;r[47]=.95;r[55]=.75;r[56]=1;}
 double I(int k)const{return r[IN+k];} void O(int k,double v){r[OUT+k]=v;} void clearO(){for(int k=0;k<128;k++)r[OUT+k]=0;}
 void atmosphere(){double h=I(0), Text=I(1), pext=I(2), rhoext=I(3), RH=clamp(I(4),0,1); double T=Text>1?Text:std::max(180.0,T0-L0*h);double p=pext>1?pext:P0*std::pow(T/T0,G/(Rair*L0));double es=610.94*std::exp(17.625*(T-273.15)/(T-30.11));double Tv=T/(1-0.378*RH*es/std::max(1.0,p));double rho=rhoext>0?rhoext:p/(Rair*Tv);double Va=I(5);O(0,T);O(1,p);O(2,rho);O(3,Tv);O(4,.5*rho*Va*Va);}
 void wind(){double wx=I(0),wy=I(1),wz=I(2),vx=I(3),vy=I(4),vz=I(5);double ax=I(6)+I(9)*vx+I(10)*vy+I(11)*vz;double ay=I(7)+I(12)*vx+I(13)*vy+I(14)*vz;double az=I(8)+I(15)*vx+I(16)*vy+I(17)*vz;O(0,wx);O(1,wy);O(2,wz);O(3,norm3(wx,wy,wz));O(4,std::atan2(wy,wx));for(int k=0;k<9;k++)O(5+k,I(9+k));O(14,I(6));O(15,I(7));O(16,I(8));O(17,ax);O(18,ay);O(19,az);}
 void kinematics(){double dt=std::max(1e-9,I(0)),x=I(1),y=I(2),h=I(3),vax=I(4),vay=I(5),vah=I(6),wx=I(7),wy=I(8),wh=I(9);double gx=vax+wx,gy=vay+wy,gh=vah+wh;double dx=I(10)-x,dy=I(11)-y,dh=I(12)-h,d=norm3(dx,dy,dh),ux=dx/std::max(1e-12,d),uy=dy/std::max(1e-12,d),uh=dh/std::max(1e-12,d);O(0,gx);O(1,gy);O(2,gh);O(3,x+dt*gx);O(4,y+dt*gy);O(5,h+dt*gh);O(6,gx*ux+gy*uy+gh*uh);O(7,norm3(gx,gy,gh));O(8,std::atan2(gy,gx));O(9,std::atan2(gh,std::hypot(gx,gy)));O(10,wx*ux+wy*uy+wh*uh);O(11,norm3(wx,wy,wh)-std::fabs(Oread(10)));}
 double Oread(int k)const{return r[OUT+k];}

 void aero(){double rho=std::max(.01,I(0)),Va=std::max(.01,I(1)),m=std::max(.01,I(2)),S=std::max(.001,I(3)),CD0=I(4),k=I(5),CLmax=std::max(.01,I(6)),phi=I(7),gamma=I(8),Lcmd=I(9);double q=.5*rho*Va*Va,n=1/std::max(.05,std::cos(phi)*std::cos(gamma)),L=Lcmd!=0?Lcmd:n*m*G,CL=L/(q*S),CD=CD0+k*CL*CL,D=q*S*CD,Vstall=std::sqrt(2*m*G/(rho*S*CLmax));O(0,q);O(1,n);O(2,L);O(3,CL);O(4,CD);O(5,D);O(6,Vstall);O(7,D*Va);O(8,std::fabs(CL)<=CLmax);}

 void safety(){double h=I(0),terrain=I(1),clr=I(2),delev=I(3),dground=I(4),rsafe=I(5),sigmaP=std::max(0.0,I(6)),sigmaW=std::max(0.0,I(7)),k=std::max(0.0,I(8));double inflate=rsafe+k*(sigmaP+sigmaW),mt=h-terrain-clr,me=delev-inflate,mg=dground-inflate,mo=std::min(me,mg);O(0,mt);O(1,me);O(2,mg);O(3,mo);O(4,mt>=0);O(5,me>=0);O(6,mg>=0);O(7,mt>=0&&mo>=0);}
 void accounting(){double dt=std::max(0.0,I(0)),gs=std::max(0.0,I(1)),P=std::max(0.0,I(2));double ds=gs*dt,dE=P*dt;O(0,ds);O(1,dt);O(2,dE);O(3,I(3)+ds);O(4,I(4)+dt);O(5,I(5)+dE);}
 void goal(){double x=I(0),y=I(1),h=I(2),gx=I(3),gy=I(4),gh=I(5),d0=std::max(1e-9,I(6)),tol=std::max(0.0,I(7));double dx=gx-x,dy=gy-y,dh=gh-h,d=norm3(dx,dy,dh);O(0,d);O(1,clamp(1-d/d0,0,1));O(2,std::atan2(dy,dx));O(3,std::atan2(dh,std::hypot(dx,dy)));O(4,d<=tol);}
 void objective(){double JL=I(0),JT=I(1),JE=I(2),JS=I(3),Lr=std::max(1e-9,I(4)),Tr=std::max(1e-9,I(5)),Er=std::max(1e-9,I(6)),Sr=std::max(1e-9,I(7));double cL=JL/Lr,cT=JT/Tr,cE=JE/Er,cS=JS/Sr,J=I(8)*cL+I(9)*cT+I(10)*cE+I(11)*cS;O(0,cL);O(1,cT);O(2,cE);O(3,cS);O(4,J);}
 void twoStage(){double L=I(0),Lmin=I(1),eps=std::max(0.0,I(2)),T=I(3),E=I(4),wT=I(5),wE=I(6);bool adm=L<=Lmin+eps;O(0,L-Lmin);O(1,adm);O(2,adm?wT*T+wE*E:1e30);}

 void segment(){int n=(int)clamp(I(0),1,40);double minm=1e99;for(int k=0;k<n;k++)minm=std::min(minm,I(1+k));double trig=I(50),winderr=I(51),stateerr=I(52);O(0,minm);O(1,minm>=0);O(2,minm<trig||winderr>I(53)||stateerr>I(54));}

 struct VtolOut { double a[33]{}; };
 VtolOut vtolCore(const double *u){
   VtolOut z;
   // Fail closed on malformed actuator-domain replay. A zeroed result with
   // vtol_feasible=0 and quality_score=0 is NOT a predicted plant state.
   // The caller must never treat rejected results as execution evidence.
   for(int k=0;k<25;k++) if(!std::isfinite(u[k])) return z;
   const double qnorm2=u[7]*u[7]+u[8]*u[8]+u[9]*u[9]+u[10]*u[10];
   if(!(u[0]>0.0 && u[0]<=1.0) || qnorm2<1e-12 ||
      u[14]<0.0 || u[15]<0.0 ||
      u[14]>phoenix_v3::motor_omega_max_rad_s ||
      u[15]>phoenix_v3::motor_omega_max_rad_s ||
      u[16]<0.0 || u[16]>1.0 ||
      u[17]<0.0 || u[17]>1.0 || u[18]<0.0 || u[18]>1.0 ||
      std::fabs(u[19])>phoenix_v3::elevon_limit_rad ||
      std::fabs(u[20])>phoenix_v3::elevon_limit_rad || u[24]<=0.05 ||
      (u[25]!=0.0 && u[25]!=1.0)) return z;
   double dt=u[0];
   double pn=u[1],pe=u[2],pd=u[3],vn=u[4],ve=u[5],vd=u[6];
   double qw=u[7],qx=u[8],qy=u[9],qz=u[10]; qnormalize(qw,qx,qy,qz);
   double pRate=u[11],qRate=u[12],rRate=u[13];
   double omL=std::max(0.0,u[14]),omR=std::max(0.0,u[15]),soc=clamp(u[16],0.0,1.0);
   double cmdL=clamp(u[17],0.0,1.0),cmdR=clamp(u[18],0.0,1.0),dL=clamp(u[19],-phoenix_v3::elevon_limit_rad,phoenix_v3::elevon_limit_rad),dR=clamp(u[20],-phoenix_v3::elevon_limit_rad,phoenix_v3::elevon_limit_rad);
   double wn=u[21],we=u[22],wd=u[23],rho=u[24]>0.05?u[24]:1.225;
   bool groundSupported=u[25]>.5;
   double Rbn[9]; quatRbn(qw,qx,qy,qz,Rbn);
   double arn=vn-wn,are=ve-we,ard=vd-wd;
   double vbx=Rbn[0]*arn+Rbn[3]*are+Rbn[6]*ard;
   double vby=Rbn[1]*arn+Rbn[4]*are+Rbn[7]*ard;
   double vbz=Rbn[2]*arn+Rbn[5]*are+Rbn[8]*ard;
   double Va=norm3(vbx,vby,vbz), alpha=0.0,beta=0.0;
   if(Va>1e-6){alpha=std::atan2(vbz,vbx);beta=std::atan2(vby,std::hypot(vbx,vbz));}
   double lam=smoothWing(Va), qdyn=.5*rho*Va*Va;
   double ds=.5*(dL+dR),dd=.5*(dR-dL);
   double hp=0,hq=0,hr=0; if(Va>0.25){hp=pRate*phoenix_v3::wing_span_m/(2*Va);hq=qRate*phoenix_v3::mean_chord_m/(2*Va);hr=rRate*phoenix_v3::wing_span_m/(2*Va);}
   double CL=clamp(phoenix_v3::CL0+phoenix_v3::CL_alpha*alpha+phoenix_v3::CL_q*hq+phoenix_v3::CL_delta*ds,phoenix_v3::cl_min,phoenix_v3::cl_max);
   double CD=phoenix_v3::cd0+phoenix_v3::induced_drag_factor*CL*CL;
   double Cm=phoenix_v3::Cm0+phoenix_v3::Cm_alpha*alpha+phoenix_v3::Cm_q*hq+phoenix_v3::Cm_delta*ds;
   double CY=phoenix_v3::CY_beta*beta;
   double Cl=phoenix_v3::Cl_beta*beta+phoenix_v3::Cl_p*hp+phoenix_v3::Cl_delta*dd;
   double Cn=phoenix_v3::Cn_beta*beta+phoenix_v3::Cn_r*hr;
   double Lift=qdyn*phoenix_v3::wing_area_m2*CL, Drag=qdyn*phoenix_v3::wing_area_m2*CD, Side=qdyn*phoenix_v3::wing_area_m2*CY;
   double Ffw[3]={-Drag*std::cos(alpha)+Lift*std::sin(alpha),Side,-Drag*std::sin(alpha)-Lift*std::cos(alpha)};
   double Mfw[3]={qdyn*phoenix_v3::wing_area_m2*phoenix_v3::wing_span_m*Cl,qdyn*phoenix_v3::wing_area_m2*phoenix_v3::mean_chord_m*Cm,qdyn*phoenix_v3::wing_area_m2*phoenix_v3::wing_span_m*Cn};
   double omL2=motorNext(omL,cmdL,dt),omR2=motorNext(omR,cmdR,dt);
   double TL=std::min(phoenix_v3::motor_kT*omL2*omL2,phoenix_v3::max_thrust_each_N),TR=std::min(phoenix_v3::motor_kT*omR2*omR2,phoenix_v3::max_thrust_each_N),Tsum=TL+TR;
   double lpL=phoenix_v3::propwash_k_lift*omL2*omL2*dL,lpR=phoenix_v3::propwash_k_lift*omR2*omR2*dR;
   double dpL=phoenix_v3::propwash_k_drag*omL2*omL2*dL*dL,dpR=phoenix_v3::propwash_k_drag*omR2*omR2*dR*dR;
   double mpL=phoenix_v3::propwash_k_pitch*omL2*omL2*dL,mpR=phoenix_v3::propwash_k_pitch*omR2*omR2*dR;
   // Source-frame propwash vectors are mapped once into canonical FRD.
   double FpwL[3]={-dpL,0.0,-lpL},FpwR[3]={-dpR,0.0,-lpR};
   double rLx,rLy,rLz,rRx,rRy,rRz;
   phoenixSourceToFRD(phoenix_v3::propwash_left_cp_x,phoenix_v3::propwash_left_cp_y,phoenix_v3::propwash_left_cp_z,rLx,rLy,rLz);
   phoenixSourceToFRD(phoenix_v3::propwash_right_cp_x,phoenix_v3::propwash_right_cp_y,phoenix_v3::propwash_right_cp_z,rRx,rRy,rRz);
   double Fpw[3]={FpwL[0]+FpwR[0],FpwL[1]+FpwR[1],FpwL[2]+FpwR[2]};
   double Mpw[3]={rLy*FpwL[2]-rLz*FpwL[1] + rRy*FpwR[2]-rRz*FpwR[1],
                  rLz*FpwL[0]-rLx*FpwL[2] + rRz*FpwR[0]-rRx*FpwR[2] + mpL+mpR,
                  rLx*FpwL[1]-rLy*FpwL[0] + rRx*FpwR[1]-rRy*FpwR[0]};
   double FA[3],MA[3];for(int k=0;k<3;k++){FA[k]=lam*Ffw[k]+(1-lam)*Fpw[k];MA[k]=lam*Mfw[k]+(1-lam)*Mpw[k];}
   double FB[3]={FA[0]+Tsum,FA[1],FA[2]};
   double MQx=(-phoenix_v3::motor_kQ_m*TL)+(phoenix_v3::motor_kQ_m*TR);
   double Mtot[3]={MA[0]+MQx,MA[1],MA[2]};
   double an=(Rbn[0]*FB[0]+Rbn[1]*FB[1]+Rbn[2]*FB[2])/phoenix_v3::mass_kg;
   double ae=(Rbn[3]*FB[0]+Rbn[4]*FB[1]+Rbn[5]*FB[2])/phoenix_v3::mass_kg;
   double ad=phoenix_v3::gravity+(Rbn[6]*FB[0]+Rbn[7]*FB[1]+Rbn[8]*FB[2])/phoenix_v3::mass_kg;
   double Iwx=phoenix_v3::Ixx*pRate,Iwy=phoenix_v3::Iyy*qRate,Iwz=phoenix_v3::Izz*rRate;
   double cx=qRate*Iwz-rRate*Iwy,cy=rRate*Iwx-pRate*Iwz,cz=pRate*Iwy-qRate*Iwx;
   double pdot=(Mtot[0]-cx)/phoenix_v3::Ixx,qdot=(Mtot[1]-cy)/phoenix_v3::Iyy,rdot=(Mtot[2]-cz)/phoenix_v3::Izz;
   double p2=pRate+dt*pdot,q2=qRate+dt*qdot,r2=rRate+dt*rdot;
   double dq0=.5*(-qx*pRate-qy*qRate-qz*rRate),dq1=.5*(qw*pRate+qy*rRate-qz*qRate),dq2=.5*(qw*qRate-qx*rRate+qz*pRate),dq3=.5*(qw*rRate+qx*qRate-qy*pRate);
   double qw2=qw+dt*dq0,qx2=qx+dt*dq1,qy2=qy+dt*dq2,qz2=qz+dt*dq3;qnormalize(qw2,qx2,qy2,qz2);
   double vn2=vn+dt*an,ve2=ve+dt*ae,vd2=vd+dt*ad;
   double pn2=pn+dt*vn+.5*dt*dt*an,pe2=pe+dt*ve+.5*dt*dt*ae,pd2=pd+dt*vd+.5*dt*dt*ad;
   double diskA=2*M_PI*sq(phoenix_v3::prop_diameter_m/2.0);double Pi=(Tsum>0)?std::pow(Tsum,1.5)/std::sqrt(std::max(1e-9,2*rho*diskA)):0.0;
   double Pimg=std::pow(phoenix_v3::mass_kg*phoenix_v3::gravity,1.5)/std::sqrt(std::max(1e-9,2*rho*diskA));double kh=phoenix_v3::hover_power_W/std::max(1e-9,Pimg);
   double eta=phoenix_v3::prop_efficiency*phoenix_v3::motor_efficiency*phoenix_v3::inverter_efficiency;double Ppred=clamp((1-lam)*kh*Pi+lam*Tsum*Va/std::max(.05,eta),0.0,phoenix_v3::peak_power_W);
   double dE=Ppred*dt,soc2=clamp(soc-dE/phoenix_v3::battery_usable_energy_J,0.0,1.0);
   double roll=std::atan2(2*(qw*qx+qy*qz),1-2*(qx*qx+qy*qy));double gamma=std::atan2(-vd,std::hypot(vn,ve)+1e-12);double load=norm3(FB[0],FB[1],FB[2])/(phoenix_v3::mass_kg*phoenix_v3::gravity);
   bool controls=(u[17]>=0&&u[17]<=1&&u[18]>=0&&u[18]<=1&&std::fabs(u[19])<=phoenix_v3::elevon_limit_rad&&std::fabs(u[20])<=phoenix_v3::elevon_limit_rad);
   bool rates=std::fabs(pRate)<=phoenix_v3::max_roll_rate_rad_s&&std::fabs(qRate)<=phoenix_v3::max_pitch_rate_rad_s&&std::fabs(rRate)<=phoenix_v3::max_yaw_rate_rad_s;
   bool common=controls&&rates&&Va<=phoenix_v3::max_speed_m_s&&load<=phoenix_v3::max_load_factor&&soc2>=0;
   bool wing=(Va>=phoenix_v3::stall_speed_m_s&&std::fabs(beta)<=phoenix_v3::max_sideslip_rad&&std::fabs(roll)<=phoenix_v3::max_bank_rad&&std::fabs(gamma)<=phoenix_v3::max_flightpath_rad);
   bool hover=(Tsum>=phoenix_v3::mass_kg*phoenix_v3::gravity);
   // Ground support is an explicit contact assertion supplied only by the TDOC
   // pre-release physical replay.  It qualifies the otherwise impossible rotor
   // spool interval; all control/rate/speed/load/SOC gates remain authoritative.
   bool feasible=common&&((lam>0.8)?wing:((lam<0.2)?(hover||groundSupported):true));
   double mode=(lam<0.2)?0.0:((lam>0.8)?2.0:1.0);double quality=clamp((1.0-0.20*lam)*(feasible?1.0:0.55),0.0,1.0);
   double vals[33]={pn2,pe2,pd2,vn2,ve2,vd2,qw2,qx2,qy2,qz2,p2,q2,r2,omL2,omR2,soc2,Va,alpha,beta,lam,TL,TR,Tsum,Ppred,dE,mode,feasible?1.0:0.0,quality,qdyn,CL,CD,Lift,Drag};
   for(int k=0;k<33;k++)z.a[k]=vals[k]; return z;
 }
 void vtol6dof(){double u[26]{};for(int k=0;k<25;k++)u[k]=I(k);auto z=vtolCore(u);for(int k=0;k<33;k++)O(k,z.a[k]);}
 void vtol6dofGroundSupported(){double u[26]{};for(int k=0;k<26;k++)u[k]=I(k);auto z=vtolCore(u);for(int k=0;k<33;k++)O(k,z.a[k]);}
 void motorPair(){double dt=std::max(1e-6,I(0)),omL=motorNext(I(1),I(3),dt),omR=motorNext(I(2),I(4),dt);double TL=std::min(phoenix_v3::motor_kT*omL*omL,phoenix_v3::max_thrust_each_N),TR=std::min(phoenix_v3::motor_kT*omR*omR,phoenix_v3::max_thrust_each_N);O(0,omL);O(1,omR);O(2,TL);O(3,TR);O(4,-phoenix_v3::motor_kQ_m*TL);O(5,phoenix_v3::motor_kQ_m*TR);O(6,TL+TR);}
 void propwash(){double omL=std::max(0.0,I(0)),omR=std::max(0.0,I(1)),dL=clamp(I(2),-phoenix_v3::elevon_limit_rad,phoenix_v3::elevon_limit_rad),dR=clamp(I(3),-phoenix_v3::elevon_limit_rad,phoenix_v3::elevon_limit_rad);double lL=phoenix_v3::propwash_k_lift*omL*omL*dL,lR=phoenix_v3::propwash_k_lift*omR*omR*dR,dgL=phoenix_v3::propwash_k_drag*omL*omL*dL*dL,dgR=phoenix_v3::propwash_k_drag*omR*omR*dR*dR,mL=phoenix_v3::propwash_k_pitch*omL*omL*dL,mR=phoenix_v3::propwash_k_pitch*omR*omR*dR;O(0,lL);O(1,lR);O(2,dgL);O(3,dgR);O(4,mL);O(5,mR);O(6,lL+lR);O(7,dgL+dgR);O(8,mL+mR);}
 void vtolEnvelope(){double Va=std::max(0.0,I(0)),beta=I(1),roll=I(2),gamma=I(3),pr=I(4),qr=I(5),rr=I(6),load=std::max(0.0,I(7)),T=std::max(0.0,I(8)),m=I(9)>0?I(9):phoenix_v3::mass_kg,lam=smoothWing(Va);bool mot=I(10)>=0&&I(10)<=1&&I(11)>=0&&I(11)<=1,el=std::fabs(I(12))<=phoenix_v3::elevon_limit_rad&&std::fabs(I(13))<=phoenix_v3::elevon_limit_rad,rates=std::fabs(pr)<=phoenix_v3::max_roll_rate_rad_s&&std::fabs(qr)<=phoenix_v3::max_pitch_rate_rad_s&&std::fabs(rr)<=phoenix_v3::max_yaw_rate_rad_s,maxv=Va<=phoenix_v3::max_speed_m_s,loadok=load<=phoenix_v3::max_load_factor,wing=Va>=phoenix_v3::stall_speed_m_s&&std::fabs(beta)<=phoenix_v3::max_sideslip_rad&&std::fabs(roll)<=phoenix_v3::max_bank_rad&&std::fabs(gamma)<=phoenix_v3::max_flightpath_rad,hov=T>=m*phoenix_v3::gravity,ok=mot&&el&&rates&&maxv&&loadok&&((lam>.8)?wing:((lam<.2)?hov:true));O(0,lam);O(1,lam<.2?0:(lam>.8?2:1));O(2,mot);O(3,el);O(4,rates);O(5,maxv);O(6,loadok);O(7,wing);O(8,hov);O(9,ok);}
 void powerSoc(){double dt=std::max(0.0,I(0)),rho=I(1)>.05?I(1):1.225,Va=std::max(0.0,I(2)),T=std::max(0.0,I(3)),soc=clamp(I(4),0,1),lam=smoothWing(Va),A=2*M_PI*sq(phoenix_v3::prop_diameter_m/2.0),Pi=T>0?std::pow(T,1.5)/std::sqrt(std::max(1e-9,2*rho*A)):0,PiMg=std::pow(phoenix_v3::mass_kg*phoenix_v3::gravity,1.5)/std::sqrt(std::max(1e-9,2*rho*A)),kh=phoenix_v3::hover_power_W/std::max(1e-9,PiMg),eta=phoenix_v3::prop_efficiency*phoenix_v3::motor_efficiency*phoenix_v3::inverter_efficiency,Ppred=clamp((1-lam)*kh*Pi+lam*T*Va/std::max(.05,eta),0.0,phoenix_v3::peak_power_W),Pmeas=(I(7)>.5)?std::max(0.0,I(5)*I(6)):0.0,Psel=(I(7)>.5)?Pmeas:Ppred,dE=Psel*dt,s2=clamp(soc-dE/phoenix_v3::battery_usable_energy_J,0,1);O(0,lam);O(1,kh*Pi);O(2,Ppred);O(3,Pmeas);O(4,Psel);O(5,dE);O(6,s2);O(7,Psel<=phoenix_v3::continuous_power_W);O(8,Psel<=phoenix_v3::peak_power_W);}
 void reducedTransition(){double dt=std::max(1e-6,I(0)),vx=I(1),vz=I(2),th=I(3),qr=I(4),oms=std::max(0.0,I(5)),soc=clamp(I(6),0,1),uc=clamp(I(7),0,1),de=clamp(I(8),-phoenix_v3::elevon_limit_rad,phoenix_v3::elevon_limit_rad),rho=I(9)>.05?I(9):1.225,omc=2*phoenix_v3::motor_omega_max_rad_s*uc,tau=(omc>=oms)?phoenix_v3::motor_tau_up_s:phoenix_v3::motor_tau_down_s,oms2=omc+(oms-omc)*std::exp(-dt/tau),omEach=.5*oms2,T=2*std::min(phoenix_v3::motor_kT*omEach*omEach,phoenix_v3::max_thrust_each_N),Va=std::hypot(vx,vz),lam=smoothWing(Va),q=.5*rho*Va*Va,alpha=std::atan2(vz,vx+1e-12),CL=clamp(phoenix_v3::CL0+phoenix_v3::CL_alpha*alpha+phoenix_v3::CL_delta*de,phoenix_v3::cl_min,phoenix_v3::cl_max),L=q*phoenix_v3::wing_area_m2*CL,Cm=phoenix_v3::Cm0+phoenix_v3::Cm_alpha*alpha+phoenix_v3::Cm_delta*de,M=q*phoenix_v3::wing_area_m2*phoenix_v3::mean_chord_m*Cm,ax=T*std::cos(th)/phoenix_v3::mass_kg,az=phoenix_v3::gravity-T*std::sin(th)/phoenix_v3::mass_kg-lam*L/phoenix_v3::mass_kg,qr2=qr+dt*M/phoenix_v3::Iyy,th2=th+dt*qr,vx2=vx+dt*ax,vz2=vz+dt*az;double A=2*M_PI*sq(phoenix_v3::prop_diameter_m/2.0),Pi=T>0?std::pow(T,1.5)/std::sqrt(std::max(1e-9,2*rho*A)):0,PiMg=std::pow(phoenix_v3::mass_kg*phoenix_v3::gravity,1.5)/std::sqrt(std::max(1e-9,2*rho*A)),kh=phoenix_v3::hover_power_W/std::max(1e-9,PiMg),eta=phoenix_v3::prop_efficiency*phoenix_v3::motor_efficiency*phoenix_v3::inverter_efficiency,P=clamp((1-lam)*kh*Pi+lam*T*Va/std::max(.05,eta),0.0,phoenix_v3::peak_power_W),s2=clamp(soc-P*dt/phoenix_v3::battery_usable_energy_J,0,1);O(0,vx2);O(1,vz2);O(2,th2);O(3,qr2);O(4,oms2);O(5,s2);O(6,lam);O(7,P);}
 void epistemic(){double U=std::max(0.0,I(0)),Q0=clamp(I(1),0,1),V0=clamp(I(2),0,1),R0=clamp(I(3),0,1),Q=Q0;for(int k=4;k<=8;k++)Q*=clamp(I(k),0,1);double novelty=clamp(I(9),0,1),Um=U*(1+novelty),V=V0*(novelty>0.8?0.5:1.0),Rel=R0*(novelty>0.8?0.6:1.0);Q*=novelty>0.8?0.6:1.0;Q=clamp(Q,0,1);O(0,Um);O(1,Q);O(2,V);O(3,Rel);O(4,Q>=.9?0:(Q>=.5?1:2));O(5,novelty>.8);O(6,std::max(0.0,I(10)));}
 void canonicalVtol(double dt){double u[26]={};u[0]=dt;u[1]=r[wing_vr::vtol_pn];u[2]=r[wing_vr::vtol_pe];u[3]=r[wing_vr::vtol_pd];u[4]=r[wing_vr::vtol_vn];u[5]=r[wing_vr::vtol_ve];u[6]=r[wing_vr::vtol_vd];u[7]=r[wing_vr::vtol_qw];u[8]=r[wing_vr::vtol_qx];u[9]=r[wing_vr::vtol_qy];u[10]=r[wing_vr::vtol_qz];u[11]=r[wing_vr::vtol_p_rate];u[12]=r[wing_vr::vtol_q_rate];u[13]=r[wing_vr::vtol_r_rate];u[14]=r[wing_vr::vtol_motor_omega_left];u[15]=r[wing_vr::vtol_motor_omega_right];u[16]=r[wing_vr::vtol_soc];u[17]=r[wing_vr::left_motor_cmd];u[18]=r[wing_vr::right_motor_cmd];u[19]=r[wing_vr::left_elevon_rad];u[20]=r[wing_vr::right_elevon_rad];u[21]=r[wing_vr::vtol_wind_n];u[22]=r[wing_vr::vtol_wind_e];u[23]=r[wing_vr::vtol_wind_d];u[24]=r[wing_vr::vtol_air_density];auto z=vtolCore(u);for(int k=0;k<33;k++)r[wing_vr::vtol_next_pn+k]=z.a[k];}

 void step(double dt)override{if(r[0]>0)dt=r[0];dt=std::max(1e-9,dt);clearO();int op=(int)std::llround(r[OP]);switch(op){case 1:O(0,3);O(1,21);O(2,35);break;case 2:O(0,I(2));O(1,I(0)-I(2));break;case 3:break;case 4:O(0,norm3(I(3),I(4),I(5)));O(1,std::atan2(I(5),std::hypot(I(3),I(4))));O(2,std::atan2(I(4),I(3)));break;case 5:atmosphere();break;case 6:wind();break;case 7:kinematics();break;case 8:break;case 9:aero();break;case 10:break;case 11:break;case 12:break;case 13:safety();break;case 14:accounting();break;case 15:goal();break;case 16:objective();break;case 17:twoStage();break;case 18:break;case 19:break;case 20:segment();break;case 21:break;case 22:break;case 23:break;case 24:break;case 25:break;case 26:break;case 27:vtol6dof();break;case 28:motorPair();break;case 29:propwash();break;case 30:vtolEnvelope();break;case 31:powerSoc();break;case 32:reducedTransition();break;case 33:epistemic();break;case 34:O(0,1);O(1,1);O(2,1);O(3,1);break;case 35:vtol6dofGroundSupported();break;default:canonicalVtol(dt);break;}}
};}
Base* makeModel(){return new W();}
