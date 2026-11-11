# Install script for directory: /media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src

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
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/initialization/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/input_output/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/math/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/models/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/simgear/cmake_install.cmake")
endif()

if(NOT CMAKE_INSTALL_LOCAL_ONLY)
  # Include the install script for the subdirectory.
  include("/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/GeographicLib/cmake_install.cmake")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib" TYPE STATIC_LIBRARY FILES "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/libJSBSim.a")
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "runtime" OR NOT CMAKE_INSTALL_COMPONENT)
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim")
    file(RPATH_CHECK
         FILE "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim"
         RPATH "")
  endif()
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/bin" TYPE EXECUTABLE FILES "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/JSBSim")
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim" AND
     NOT IS_SYMLINK "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim")
    if(CMAKE_INSTALL_DO_STRIP)
      execute_process(COMMAND "/usr/bin/strip" "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/bin/JSBSim")
    endif()
  endif()
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/include/JSBSim" TYPE FILE FILES
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/FGFDMExec.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/FGJSBBase.h"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/src/JSBSim_API.h"
    )
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  if(EXISTS "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim/JSBSimTargets.cmake")
    file(DIFFERENT _cmake_export_file_changed FILES
         "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim/JSBSimTargets.cmake"
         "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/CMakeFiles/Export/6cfba3d3e6ea3b751dd3b17eae5548c7/JSBSimTargets.cmake")
    if(_cmake_export_file_changed)
      file(GLOB _cmake_old_config_files "$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim/JSBSimTargets-*.cmake")
      if(_cmake_old_config_files)
        string(REPLACE ";" ", " _cmake_old_config_files_text "${_cmake_old_config_files}")
        message(STATUS "Old export file \"$ENV{DESTDIR}${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim/JSBSimTargets.cmake\" will be replaced.  Removing files [${_cmake_old_config_files_text}].")
        unset(_cmake_old_config_files_text)
        file(REMOVE ${_cmake_old_config_files})
      endif()
      unset(_cmake_old_config_files)
    endif()
    unset(_cmake_export_file_changed)
  endif()
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim" TYPE FILE FILES "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/CMakeFiles/Export/6cfba3d3e6ea3b751dd3b17eae5548c7/JSBSimTargets.cmake")
  if(CMAKE_INSTALL_CONFIG_NAME MATCHES "^([Rr][Ee][Ll][Ee][Aa][Ss][Ee])$")
    file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim" TYPE FILE FILES "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/CMakeFiles/Export/6cfba3d3e6ea3b751dd3b17eae5548c7/JSBSimTargets-release.cmake")
  endif()
endif()

if(CMAKE_INSTALL_COMPONENT STREQUAL "devel" OR NOT CMAKE_INSTALL_COMPONENT)
  file(INSTALL DESTINATION "${CMAKE_INSTALL_PREFIX}/lib/cmake/JSBSim" TYPE FILE FILES
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/JSBSimConfig.cmake"
    "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/JSBSimConfigVersion.cmake"
    )
endif()

string(REPLACE ";" "\n" CMAKE_INSTALL_MANIFEST_CONTENT
       "${CMAKE_INSTALL_MANIFEST_FILES}")
if(CMAKE_INSTALL_LOCAL_ONLY)
  file(WRITE "/media/p1/DigitalTwin/TDDT_navigation/arduPilot/Layer6/layer6_v20261007_1032/FlightStack/jsbsim/build/src/install_local_manifest.txt"
     "${CMAKE_INSTALL_MANIFEST_CONTENT}")
endif()
