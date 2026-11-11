#ifndef FMI2_FUNCTIONS_H
#define FMI2_FUNCTIONS_H
#include <stddef.h>
#ifdef _WIN32
#define FMI2_Export __declspec(dllexport)
#else
#define FMI2_Export __attribute__((visibility("default")))
#endif
#ifdef __cplusplus
extern "C" {
#endif

typedef void* fmi2Component;
typedef void* fmi2ComponentEnvironment;
typedef void* fmi2FMUstate;
typedef const char* fmi2String;
typedef double fmi2Real;
typedef int fmi2Integer;
typedef int fmi2Boolean;
typedef unsigned int fmi2ValueReference;
typedef unsigned char fmi2Byte;

typedef enum {fmi2OK=0,fmi2Warning=1,fmi2Discard=2,fmi2Error=3,fmi2Fatal=4,fmi2Pending=5} fmi2Status;
typedef enum {fmi2ModelExchange=0,fmi2CoSimulation=1} fmi2Type;
typedef enum {
  fmi2DoStepStatus=0,
  fmi2PendingStatus=1,
  fmi2LastSuccessfulTime=2,
  fmi2Terminated=3
} fmi2StatusKind;

typedef void (*fmi2CallbackLogger)(fmi2ComponentEnvironment,fmi2String,fmi2Status,fmi2String,fmi2String,...);
typedef void* (*fmi2CallbackAllocateMemory)(size_t,size_t);
typedef void (*fmi2CallbackFreeMemory)(void*);
typedef void (*fmi2StepFinished)(fmi2ComponentEnvironment,fmi2Status);
typedef struct {
  fmi2CallbackLogger logger;
  fmi2CallbackAllocateMemory allocateMemory;
  fmi2CallbackFreeMemory freeMemory;
  fmi2StepFinished stepFinished;
  fmi2ComponentEnvironment componentEnvironment;
} fmi2CallbackFunctions;

#define fmi2True 1
#define fmi2False 0

FMI2_Export const char* fmi2GetTypesPlatform(void);
FMI2_Export const char* fmi2GetVersion(void);
FMI2_Export fmi2Status fmi2SetDebugLogging(fmi2Component,fmi2Boolean,size_t,const fmi2String[]);
FMI2_Export fmi2Component fmi2Instantiate(fmi2String,fmi2Type,fmi2String,fmi2String,const fmi2CallbackFunctions*,fmi2Boolean,fmi2Boolean);
FMI2_Export void fmi2FreeInstance(fmi2Component);
FMI2_Export fmi2Status fmi2SetupExperiment(fmi2Component,fmi2Boolean,fmi2Real,fmi2Real,fmi2Boolean,fmi2Real);
FMI2_Export fmi2Status fmi2EnterInitializationMode(fmi2Component);
FMI2_Export fmi2Status fmi2ExitInitializationMode(fmi2Component);
FMI2_Export fmi2Status fmi2Terminate(fmi2Component);
FMI2_Export fmi2Status fmi2Reset(fmi2Component);
FMI2_Export fmi2Status fmi2GetReal(fmi2Component,const fmi2ValueReference[],size_t,fmi2Real[]);
FMI2_Export fmi2Status fmi2SetReal(fmi2Component,const fmi2ValueReference[],size_t,const fmi2Real[]);
FMI2_Export fmi2Status fmi2GetInteger(fmi2Component,const fmi2ValueReference[],size_t,fmi2Integer[]);
FMI2_Export fmi2Status fmi2SetInteger(fmi2Component,const fmi2ValueReference[],size_t,const fmi2Integer[]);
FMI2_Export fmi2Status fmi2GetBoolean(fmi2Component,const fmi2ValueReference[],size_t,fmi2Boolean[]);
FMI2_Export fmi2Status fmi2SetBoolean(fmi2Component,const fmi2ValueReference[],size_t,const fmi2Boolean[]);
FMI2_Export fmi2Status fmi2GetString(fmi2Component,const fmi2ValueReference[],size_t,fmi2String[]);
FMI2_Export fmi2Status fmi2SetString(fmi2Component,const fmi2ValueReference[],size_t,const fmi2String[]);
FMI2_Export fmi2Status fmi2DoStep(fmi2Component,fmi2Real,fmi2Real,fmi2Boolean);
FMI2_Export fmi2Status fmi2CancelStep(fmi2Component);
FMI2_Export fmi2Status fmi2GetStatus(fmi2Component,fmi2StatusKind,fmi2Status*);
FMI2_Export fmi2Status fmi2GetRealStatus(fmi2Component,fmi2StatusKind,fmi2Real*);
FMI2_Export fmi2Status fmi2GetIntegerStatus(fmi2Component,fmi2StatusKind,fmi2Integer*);
FMI2_Export fmi2Status fmi2GetBooleanStatus(fmi2Component,fmi2StatusKind,fmi2Boolean*);
FMI2_Export fmi2Status fmi2GetStringStatus(fmi2Component,fmi2StatusKind,fmi2String*);
#ifdef __cplusplus
}
#endif
#endif
