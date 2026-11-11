# Install script for directory: /media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models

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

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/models/atmosphere/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/models/propulsion/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/models/flight_control/cmake_install.cmake")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/include/JSBSim/models" TYPE FILE FILES
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGAerodynamics.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGAircraft.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGAtmosphere.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGAuxiliary.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGFCS.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGSurface.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGGroundReactions.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGInertial.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGLGear.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGMassBalance.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGModel.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGOutput.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGPropagate.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGPropulsion.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGInput.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGExternalReactions.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGExternalForce.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGBuoyantForces.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGGasCell.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGAccelerations.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/models/FGFCSChannel.h"
    )
endif()

string(REPLACE ";" "\n" CMAKE_INSTALL_MANIFEST_CONTENT
       "${CMAKE_INSTALL_MANIFEST_FILES}")
if(CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/models/install_local_manifest.txt"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
