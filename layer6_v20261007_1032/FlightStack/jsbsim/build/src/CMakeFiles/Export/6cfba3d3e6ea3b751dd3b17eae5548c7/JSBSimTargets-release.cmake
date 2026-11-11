#----------------------------------------------------------------
# Generated CMake target import file for configuration "Release".
#----------------------------------------------------------------

# Commands may need to know the format version.
set(CMAKE_IMPORT_FILE_VERSION 1)

# Import target "JSBSim::JSBSim" for configuration "Release"
set_property(TARGET JSBSim::JSBSim APPEND PROPERTY IMPORTED_CONFIGURATIONS RELEASE)
set_target_properties(JSBSim::JSBSim PROPERTIES
  IMPORTED_LINK_INTERFACE_LANGUAGES_RELEASE "C;CXX"
  IMPORTED_LOCATION_RELEASE "${_IMPORT_PREFIX}/lib/libJSBSim.a"
  )

list(APPEND _cmake_import_check_targets JSBSim::JSBSim )
list(APPEND _cmake_import_check_files_for_JSBSim::JSBSim "${_IMPORT_PREFIX}/lib/libJSBSim.a" )

# Commands beyond this point should not need to know the version.
set(CMAKE_IMPORT_FILE_VERSION)
