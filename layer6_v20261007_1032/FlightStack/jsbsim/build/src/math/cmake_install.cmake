# Install script for directory: /media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math

# Set the install prefix
if(NOT DEFINED CMAKE_INSTALL_PREFIX)
  set(CMAKE_INSTALL_PREFIX "/usr/local")
endif()
string(REGEX REPLACE "/$" "" CMAKE_INSTALL_PREFIX "${CMAKE_INSTALL_PREFIX}")

# Set the install configuration name.
if(NOT DEFINED CMAKE_INSTALL_CONFIG_NAME)
  if(BUILD_TYPE)
    string(REGEX REPLACE "^[^A-Za-z0-9_]+" ""
           CMAKE_INSTALL_CONFIG_NAME "${BUILD_TYPE}")
  else()
    set(CMAKE_INSTALL_CONFIG_NAME "Release")
  endif()
  message(STATUS "Install configuration: \"${CMAKE_INSTALL_CONFIG_NAME}\"")
endif()

# Set the component getting installed.
if(NOT CMAKE_INSTALL_COMPONENT)
  if(COMPONENT)
    message(STATUS "Install component: \"${COMPONENT}\"")
    set(CMAKE_INSTALL_COMPONENT "${COMPONENT}")
  else()
    set(CMAKE_INSTALL_COMPONENT)
  endif()
endif()

# Install shared libraries without execute permission?
if(NOT DEFINED CMAKE_INSTALL_SO_NO_EXE)
  set(CMAKE_INSTALL_SO_NO_EXE "1")
endif()

# Is this installation the result of a crosscompile?
if(NOT DEFINED CMAKE_CROSSCOMPILING)
  set(CMAKE_CROSSCOMPILING "FALSE")
endif()

# Set path to fallback-tool for dependency-resolution.
if(NOT DEFINED CMAKE_OBJDUMP)
  set(CMAKE_OBJDUMP "/usr/bin/objdump")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/include/JSBSim/math" TYPE FILE FILES
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGColumnVector3.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGFunction.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGLocation.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGMatrix33.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGParameter.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGPropertyValue.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGQuaternion.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGRealValue.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGTable.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGCondition.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGRungeKutta.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGModelFunctions.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/LagrangeMultiplier.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGTemplateFunc.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGFunctionValue.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGParameterValue.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/math/FGStateSpace.h"
    )
endif()

string(REPLACE ";" "\n" CMAKE_INSTALL_MANIFEST_CONTENT
       "${CMAKE_INSTALL_MANIFEST_FILES}")
if(CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/math/install_local_manifest.txt"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
