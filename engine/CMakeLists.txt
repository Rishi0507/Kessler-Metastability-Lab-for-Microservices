cmake_minimum_required(VERSION 3.20)
project(kessler_engine VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

option(KESSLER_NATIVE "Tune for the build machine (-march=native)" OFF)
option(KESSLER_SANITIZE "Build with ASan+UBSan (debug)" OFF)
option(KESSLER_BUILD_TESTS "Build unit tests" ON)

if(MSVC)
  add_compile_options(/W4 /permissive-)
else()
  add_compile_options(-Wall -Wextra -Wpedantic -Wno-nullability-completeness)
  if(KESSLER_NATIVE)
    add_compile_options(-march=native)
  endif()
  if(KESSLER_SANITIZE)
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
  endif()
endif()

set(KESSLER_CORE_SOURCES
  src/json.cpp
  src/spec.cpp
  src/sim.cpp
  src/analysis.cpp
)

add_library(kessler_core STATIC ${KESSLER_CORE_SOURCES})
target_include_directories(kessler_core PUBLIC src include)

add_library(kessler_engine SHARED ${KESSLER_CORE_SOURCES} src/capi.cpp)
target_include_directories(kessler_engine PUBLIC include PRIVATE src)
target_compile_definitions(kessler_engine PRIVATE KESSLER_BUILD_DLL)
set_target_properties(kessler_engine PROPERTIES
  CXX_VISIBILITY_PRESET hidden
  VISIBILITY_INLINES_HIDDEN ON
  PREFIX "lib")

find_package(Threads REQUIRED)

add_executable(kessler-sim cli/main.cpp)
target_link_libraries(kessler-sim PRIVATE kessler_core Threads::Threads)

if(KESSLER_BUILD_TESTS)
  enable_testing()
  add_executable(kessler-tests
    tests/main.cpp
    tests/test_json.cpp
    tests/test_rng.cpp
    tests/test_spec.cpp
    tests/test_queueing.cpp
    tests/test_resilience.cpp
    tests/test_capi.cpp
    src/capi.cpp)
  target_compile_definitions(kessler-tests PRIVATE KESSLER_STATIC)
  target_link_libraries(kessler-tests PRIVATE kessler_core Threads::Threads)
  add_test(NAME kessler-tests COMMAND kessler-tests)
endif()

install(TARGETS kessler_engine kessler-sim
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION bin
  ARCHIVE DESTINATION lib)
install(FILES include/kessler/kessler.h DESTINATION include/kessler)
