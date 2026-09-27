cmake_minimum_required(VERSION 3.20)
project(tracing_analysis LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)
add_executable(write_read_analyzer src/write_read_analyzer.cpp)
target_link_libraries(write_read_analyzer PRIVATE Threads::Threads OpenSSL::Crypto)
target_compile_options(write_read_analyzer PRIVATE -Wall -Wextra -Wconversion -Wshadow)
option(ENABLE_SANITIZERS "Enable address and undefined-behavior sanitizers" OFF)
if(ENABLE_SANITIZERS)
  target_compile_options(write_read_analyzer PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
  target_link_options(write_read_analyzer PRIVATE -fsanitize=address,undefined)
endif()
enable_testing()
find_package(Python3 REQUIRED COMPONENTS Interpreter)
add_test(NAME analysis_reference COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_analysis.py $<TARGET_FILE:write_read_analyzer>)
add_test(NAME workflow COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_workflow.py $<TARGET_FILE:write_read_analyzer>)
add_test(NAME plotting COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_plot_results.py)
add_test(NAME execution_timeline COMMAND ${Python3_EXECUTABLE}
  ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_execution_timeline.py)
