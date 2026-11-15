#pragma once
#ifdef __ANDROID__
#include <android/log.h>
#define QT_LOG(...) __android_log_print(ANDROID_LOG_INFO, "quest-teleop", __VA_ARGS__)
#else
#include <cstdio>
#define QT_LOG(...) (std::fprintf(stderr, "[quest-teleop] "), std::fprintf(stderr, __VA_ARGS__), std::fprintf(stderr, "\n"))
#endif
