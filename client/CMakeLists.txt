cmake_minimum_required(VERSION 3.18)
project(quest_teleop_client CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

set(CORE core/app.cpp core/net.cpp core/renderer.cpp core/scene.cpp core/ui.cpp)

if(ANDROID)
    # Headset app: one shared library loaded by android.app.NativeActivity. OPENXR_DIR = the unpacked Khronos
    # openxr_loader_for_android AAR (android/setup_toolchain.sh).
    add_library(native_app_glue STATIC ${ANDROID_NDK}/sources/android/native_app_glue/android_native_app_glue.c)
    target_include_directories(native_app_glue PUBLIC ${ANDROID_NDK}/sources/android/native_app_glue)
    add_library(openxr_loader SHARED IMPORTED)
    set_target_properties(openxr_loader PROPERTIES
        IMPORTED_LOCATION ${OPENXR_DIR}/jni/${ANDROID_ABI}/libopenxr_loader.so)
    add_library(questteleop SHARED ${CORE} quest/main.cpp)
    target_include_directories(questteleop PRIVATE ${OPENXR_DIR}/prefab/modules/headers/include)
    target_compile_options(questteleop PRIVATE -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers)
    # android_main is only referenced from the glue: keep ANativeActivity_onCreate exported
    target_link_options(questteleop PRIVATE -u ANativeActivity_onCreate -Wl,--strip-debug)
    target_link_libraries(questteleop native_app_glue openxr_loader android log EGL GLESv3 z)
else()
    find_package(X11 REQUIRED)
    find_package(ZLIB REQUIRED)
    find_package(Threads REQUIRED)
    add_executable(qt_desktop ${CORE} desktop/main.cpp)
    target_compile_options(qt_desktop PRIVATE -Wall -Wextra -Wno-unused-parameter)
    target_link_libraries(qt_desktop EGL GLESv2 X11::X11 ZLIB::ZLIB Threads::Threads)
endif()
