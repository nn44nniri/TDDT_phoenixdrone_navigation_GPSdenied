#include "fmi2Functions.h"
#include <vector>
#include <string>
#include <cmath>
#include <algorithm>
#include <cstring>
struct Base { std::vector<double> r; std::vector<int> i; std::vector<int> b; std::vector<std::string> s; double t=0; bool init=false; virtual ~Base(){}; virtual void step(double dt)=0; };
extern Base* makeModel();
extern "C" {
const char* fmi2GetTypesPlatform(){return "default";} const char* fmi2GetVersion(){return "2.0";}
fmi2Component fmi2Instantiate(fmi2String,fmi2Type,fmi2String,fmi2String,const fmi2CallbackFunctions*,fmi2Boolean,fmi2Boolean){try{return makeModel();}catch(...){return nullptr;}}
void fmi2FreeInstance(fmi2Component c){delete (Base*)c;} fmi2Status fmi2SetupExperiment(fmi2Component c,fmi2Boolean,fmi2Real,fmi2Real start,fmi2Boolean,fmi2Real){((Base*)c)->t=start;return fmi2OK;}
fmi2Status fmi2EnterInitializationMode(fmi2Component){return fmi2OK;} fmi2Status fmi2ExitInitializationMode(fmi2Component c){((Base*)c)->init=true;return fmi2OK;} fmi2Status fmi2Terminate(fmi2Component){return fmi2OK;} fmi2Status fmi2Reset(fmi2Component c){auto* m=(Base*)c; std::fill(m->r.begin(),m->r.end(),0.0); std::fill(m->i.begin(),m->i.end(),0); std::fill(m->b.begin(),m->b.end(),0); m->t=0; return fmi2OK;}
fmi2Status fmi2GetReal(fmi2Component c,const fmi2ValueReference vr[],size_t n,fmi2Real v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->r.size())return fmi2Error;v[k]=m->r[vr[k]];}return fmi2OK;}
fmi2Status fmi2SetReal(fmi2Component c,const fmi2ValueReference vr[],size_t n,const fmi2Real v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->r.size())return fmi2Error;m->r[vr[k]]=v[k];}return fmi2OK;}
fmi2Status fmi2GetInteger(fmi2Component c,const fmi2ValueReference vr[],size_t n,fmi2Integer v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->i.size())return fmi2Error;v[k]=m->i[vr[k]];}return fmi2OK;}
fmi2Status fmi2SetInteger(fmi2Component c,const fmi2ValueReference vr[],size_t n,const fmi2Integer v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->i.size())return fmi2Error;m->i[vr[k]]=v[k];}return fmi2OK;}
fmi2Status fmi2GetBoolean(fmi2Component c,const fmi2ValueReference vr[],size_t n,fmi2Boolean v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->b.size())return fmi2Error;v[k]=m->b[vr[k]];}return fmi2OK;}
fmi2Status fmi2SetBoolean(fmi2Component c,const fmi2ValueReference vr[],size_t n,const fmi2Boolean v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->b.size())return fmi2Error;m->b[vr[k]]=v[k];}return fmi2OK;}
fmi2Status fmi2GetString(fmi2Component c,const fmi2ValueReference vr[],size_t n,fmi2String v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->s.size())return fmi2Error;v[k]=m->s[vr[k]].c_str();}return fmi2OK;}
fmi2Status fmi2SetString(fmi2Component c,const fmi2ValueReference vr[],size_t n,const fmi2String v[]){auto*m=(Base*)c;for(size_t k=0;k<n;k++){if(vr[k]>=m->s.size())return fmi2Error;m->s[vr[k]]=v[k]?v[k]:"";}return fmi2OK;}
fmi2Status fmi2DoStep(fmi2Component c,fmi2Real t,fmi2Real dt,fmi2Boolean){auto*m=(Base*)c;m->t=t;m->step(dt);m->t=t+dt;return fmi2OK;} fmi2Status fmi2CancelStep(fmi2Component){return fmi2OK;}
fmi2Status fmi2GetStatus(fmi2Component,const int,fmi2Status*){return fmi2Discard;} fmi2Status fmi2GetRealStatus(fmi2Component,const int,fmi2Real*){return fmi2Discard;} fmi2Status fmi2GetIntegerStatus(fmi2Component,const int,fmi2Integer*){return fmi2Discard;} fmi2Status fmi2GetBooleanStatus(fmi2Component,const int,fmi2Boolean*){return fmi2Discard;} fmi2Status fmi2GetStringStatus(fmi2Component,const int,fmi2String*){return fmi2Discard;}
}
