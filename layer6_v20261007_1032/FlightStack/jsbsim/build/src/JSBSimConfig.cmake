
####### Expanded from @PACKAGE_INIT@ by configure_package_config_file() #######
####### Any changes to this file will be overwritten by the next CMake run ####
####### The input file was JSBSimConfig.cmake.in                            ########

get_filename_component(PACKAGE_PREFIX_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../" ABSOLUTE)

macro(set_and_check _var _file)
  set(${_var} "${_file}")
  if(NOT EXISTS "${_file}")
    message(FATAL_ERROR "File or directory ${_file} referenced by variable ${_var} does not exist !")
  endif()
endmacro()

macro(check_required_components _NAME)
  foreach(comp ${${_NAME}_FIND_COMPONENTS})
    if(NOT ${_NAME}_${comp}_FOUND)
      if(${_NAME}_FIND_REQUIRED_${comp})
        set(${_NAME}_FOUND FALSE)
      endif()
    endif()
  endforeach()
endmacro()

####################################################################################

# JSBSim::JSBSim - the JSBSim flight dynamics library.
#
#   find_package(JSBSim CONFIG REQUIRED)
#   target_link_libraries(my_app PRIVATE JSBSim::JSBSim)
#
# Headers are included relative to include/JSBSim, as with pkg-config:
#   #include "FGFDMExec.h"

set(JSBSIM_SYSTEM_EXPAT OFF)
set(JSBSIM_SHARED_LIBS )

include(CMakeFindDependencyMacro)
# A static libJSBSim built against the system libExpat needs it at link time.
if(JSBSIM_SYSTEM_EXPAT AND NOT JSBSIM_SHARED_LIBS)
  find_dependency(EXPAT)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/JSBSimTargets.cmake")

check_required_components(JSBSim)
