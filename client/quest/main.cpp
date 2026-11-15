// Headset app (Meta Quest 2 and later): android.app.NativeActivity + EGL / OpenGL ES 3 + OpenXR. No Java, no engine.
// Everything that is not tracking, buttons, haptics or the swapchain lives in client/core (shared with the desktop
// client).
//
// Optional launch arguments (intent extras):
//   adb shell am start -n com.entangledquantum.questteleop/android.app.NativeActivity \
//       --es server 192.168.1.20[:47800]      connect to this address instead of discovering the server
//       --es hz 90                            ask for this display refresh rate (default: leave the system's, 72)
//       --es passthrough 0                    start with the dark background instead of passthrough
#include <EGL/egl.h>
#include <GLES3/gl3.h>
#include <android/looper.h>
#include <android_native_app_glue.h>
#include <jni.h>
#include <sys/system_properties.h>
#include <time.h>
#include <unistd.h>

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#define XR_USE_PLATFORM_ANDROID
#define XR_USE_GRAPHICS_API_OPENGL_ES
#include <openxr/openxr.h>
#include <openxr/openxr_platform.h>

#include "../core/app.h"
#include "../core/log.h"

using namespace qt;

#define CHECK_XR(call)                                                   \
    do {                                                                 \
        XrResult _r = (call);                                            \
        if (XR_FAILED(_r)) {                                             \
            QT_LOG("OpenXR call failed (%d): %s", (int)_r, #call);       \
            return false;                                                \
        }                                                                \
    } while (0)

static double monoSeconds() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<double>(ts.tv_sec) + 1e-9 * static_cast<double>(ts.tv_nsec);
}

static Pose toPose(const XrPosef& p) {
    return {Vec3(p.position.x, p.position.y, p.position.z),
            Quat(p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w)};
}

static std::string intentExtra(android_app* app, JNIEnv* env, const char* key) {
    std::string out;
    jobject activity = app->activity->clazz;
    jclass ac = env->GetObjectClass(activity);
    jmethodID getIntent = env->GetMethodID(ac, "getIntent", "()Landroid/content/Intent;");
    jobject intent = env->CallObjectMethod(activity, getIntent);
    if (intent) {
        jclass ic = env->GetObjectClass(intent);
        jmethodID get = env->GetMethodID(ic, "getStringExtra", "(Ljava/lang/String;)Ljava/lang/String;");
        jstring k = env->NewStringUTF(key);
        jstring v = static_cast<jstring>(env->CallObjectMethod(intent, get, k));
        if (v) {
            const char* c = env->GetStringUTFChars(v, nullptr);
            out = c;
            env->ReleaseStringUTFChars(v, c);
            env->DeleteLocalRef(v);
        }
        env->DeleteLocalRef(k);
        env->DeleteLocalRef(ic);
        env->DeleteLocalRef(intent);
    }
    env->DeleteLocalRef(ac);
    if (env->ExceptionCheck()) env->ExceptionClear();
    return out;
}

struct Eye {
    XrSwapchain swapchain = XR_NULL_HANDLE;
    int32_t width = 0, height = 0;
    std::vector<XrSwapchainImageOpenGLESKHR> images;
    std::vector<GLuint> fbos;                 // one resolve framebuffer per swapchain image
};

struct Xr {
    android_app* app = nullptr;
    EGLDisplay eglDisplay = EGL_NO_DISPLAY;
    EGLConfig eglConfig = nullptr;
    EGLContext eglContext = EGL_NO_CONTEXT;
    EGLSurface eglSurface = EGL_NO_SURFACE;

    XrInstance instance = XR_NULL_HANDLE;
    XrSystemId systemId = XR_NULL_SYSTEM_ID;
    XrSession session = XR_NULL_HANDLE;
    XrSpace stage = XR_NULL_HANDLE, viewSpace = XR_NULL_HANDLE;
    XrSessionState state = XR_SESSION_STATE_UNKNOWN;
    bool running = false, exitRequested = false, srgb = false;
    bool hasRefreshExt = false, hasPassthroughExt = false, passthroughOk = false;
    float displayHz = 72.0f;
    Eye eyes[2];
    GLuint msFbo = 0, msColor = 0, msDepth = 0;
    int32_t msW = 0, msH = 0;
    GLenum colorFormat = GL_RGBA8;

    XrPassthroughFB passthrough = XR_NULL_HANDLE;
    XrPassthroughLayerFB passthroughLayer = XR_NULL_HANDLE;

    XrActionSet actionSet = XR_NULL_HANDLE;
    XrPath handPath[2] = {XR_NULL_PATH, XR_NULL_PATH};
    XrAction aGrip = XR_NULL_HANDLE, aAim = XR_NULL_HANDLE, aTrigger = XR_NULL_HANDLE, aSqueeze = XR_NULL_HANDLE,
             aStick = XR_NULL_HANDLE, aStickClick = XR_NULL_HANDLE, aPrimary = XR_NULL_HANDLE, aSecondary = XR_NULL_HANDLE,
             aMenu = XR_NULL_HANDLE, aTriggerTouch = XR_NULL_HANDLE, aStickTouch = XR_NULL_HANDLE,
             aPrimaryTouch = XR_NULL_HANDLE, aSecondaryTouch = XR_NULL_HANDLE, aThumbrest = XR_NULL_HANDLE,
             aHaptic = XR_NULL_HANDLE;
    XrSpace gripSpace[2] = {XR_NULL_HANDLE, XR_NULL_HANDLE}, aimSpace[2] = {XR_NULL_HANDLE, XR_NULL_HANDLE};
};

// ------------------------------------------------------------------------------------------------- EGL
static bool initEgl(Xr& x) {
    x.eglDisplay = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    if (!eglInitialize(x.eglDisplay, nullptr, nullptr)) return false;
    const EGLint attr[] = {EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RED_SIZE, 8,
                           EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_DEPTH_SIZE, 0, EGL_NONE};
    EGLint n = 0;
    if (!eglChooseConfig(x.eglDisplay, attr, &x.eglConfig, 1, &n) || n < 1) return false;
    const EGLint ctxAttr[] = {EGL_CONTEXT_CLIENT_VERSION, 3, EGL_NONE};
    x.eglContext = eglCreateContext(x.eglDisplay, x.eglConfig, EGL_NO_CONTEXT, ctxAttr);
    const EGLint pb[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
    x.eglSurface = eglCreatePbufferSurface(x.eglDisplay, x.eglConfig, pb);
    if (x.eglContext == EGL_NO_CONTEXT || x.eglSurface == EGL_NO_SURFACE) return false;
    return eglMakeCurrent(x.eglDisplay, x.eglSurface, x.eglSurface, x.eglContext) == EGL_TRUE;
}

// ------------------------------------------------------------------------------------------------- OpenXR setup
static bool createInstance(Xr& x) {
    PFN_xrInitializeLoaderKHR initLoader = nullptr;
    xrGetInstanceProcAddr(XR_NULL_HANDLE, "xrInitializeLoaderKHR", reinterpret_cast<PFN_xrVoidFunction*>(&initLoader));
    if (initLoader) {
        XrLoaderInitInfoAndroidKHR li{XR_TYPE_LOADER_INIT_INFO_ANDROID_KHR};
        li.applicationVM = x.app->activity->vm;
        li.applicationContext = x.app->activity->clazz;
        CHECK_XR(initLoader(reinterpret_cast<const XrLoaderInitInfoBaseHeaderKHR*>(&li)));
    }
    uint32_t n = 0;
    CHECK_XR(xrEnumerateInstanceExtensionProperties(nullptr, 0, &n, nullptr));
    std::vector<XrExtensionProperties> props(n, {XR_TYPE_EXTENSION_PROPERTIES});
    CHECK_XR(xrEnumerateInstanceExtensionProperties(nullptr, n, &n, props.data()));
    auto has = [&](const char* name) {
        for (auto& p : props)
            if (!std::strcmp(p.extensionName, name)) return true;
        return false;
    };
    std::vector<const char*> exts = {XR_KHR_ANDROID_CREATE_INSTANCE_EXTENSION_NAME, XR_KHR_OPENGL_ES_ENABLE_EXTENSION_NAME};
    for (const char* e : exts)
        if (!has(e)) {
            QT_LOG("required OpenXR extension missing: %s", e);
            return false;
        }
    if ((x.hasRefreshExt = has(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME))) exts.push_back(XR_FB_DISPLAY_REFRESH_RATE_EXTENSION_NAME);
    if ((x.hasPassthroughExt = has(XR_FB_PASSTHROUGH_EXTENSION_NAME))) exts.push_back(XR_FB_PASSTHROUGH_EXTENSION_NAME);

    XrInstanceCreateInfoAndroidKHR android{XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR};
    android.applicationVM = x.app->activity->vm;
    android.applicationActivity = x.app->activity->clazz;
    XrInstanceCreateInfo ci{XR_TYPE_INSTANCE_CREATE_INFO};
    ci.next = &android;
    std::strncpy(ci.applicationInfo.applicationName, "quest-teleop", XR_MAX_APPLICATION_NAME_SIZE - 1);
    ci.applicationInfo.applicationVersion = 1;
    std::strncpy(ci.applicationInfo.engineName, "none", XR_MAX_ENGINE_NAME_SIZE - 1);
    ci.applicationInfo.apiVersion = XR_MAKE_VERSION(1, 0, 34);     // OpenXR 1.0: what the Quest 2 runtime implements
    ci.enabledExtensionCount = static_cast<uint32_t>(exts.size());
    ci.enabledExtensionNames = exts.data();
    CHECK_XR(xrCreateInstance(&ci, &x.instance));
    XrSystemGetInfo si{XR_TYPE_SYSTEM_GET_INFO};
    si.formFactor = XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY;
    CHECK_XR(xrGetSystem(x.instance, &si, &x.systemId));
    return true;
}

static bool createSession(Xr& x) {
    PFN_xrGetOpenGLESGraphicsRequirementsKHR getReq = nullptr;
    CHECK_XR(xrGetInstanceProcAddr(x.instance, "xrGetOpenGLESGraphicsRequirementsKHR", reinterpret_cast<PFN_xrVoidFunction*>(&getReq)));
    XrGraphicsRequirementsOpenGLESKHR req{XR_TYPE_GRAPHICS_REQUIREMENTS_OPENGL_ES_KHR};
    CHECK_XR(getReq(x.instance, x.systemId, &req));
    XrGraphicsBindingOpenGLESAndroidKHR gb{XR_TYPE_GRAPHICS_BINDING_OPENGL_ES_ANDROID_KHR};
    gb.display = x.eglDisplay;
    gb.config = x.eglConfig;
    gb.context = x.eglContext;
    XrSessionCreateInfo sci{XR_TYPE_SESSION_CREATE_INFO};
    sci.next = &gb;
    sci.systemId = x.systemId;
    CHECK_XR(xrCreateSession(x.instance, &sci, &x.session));

    XrReferenceSpaceCreateInfo rs{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
    rs.poseInReferenceSpace.orientation.w = 1.0f;
    rs.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_STAGE;
    if (XR_FAILED(xrCreateReferenceSpace(x.session, &rs, &x.stage))) {
        QT_LOG("no STAGE space (no boundary set up?): using LOCAL");
        rs.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
        CHECK_XR(xrCreateReferenceSpace(x.session, &rs, &x.stage));
    }
    rs.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_VIEW;
    CHECK_XR(xrCreateReferenceSpace(x.session, &rs, &x.viewSpace));
    return true;
}

static void setupRefreshRate(Xr& x, float wanted) {
    if (!x.hasRefreshExt) return;
    PFN_xrGetDisplayRefreshRateFB getRate = nullptr;
    PFN_xrRequestDisplayRefreshRateFB reqRate = nullptr;
    xrGetInstanceProcAddr(x.instance, "xrGetDisplayRefreshRateFB", reinterpret_cast<PFN_xrVoidFunction*>(&getRate));
    xrGetInstanceProcAddr(x.instance, "xrRequestDisplayRefreshRateFB", reinterpret_cast<PFN_xrVoidFunction*>(&reqRate));
    if (wanted > 0 && reqRate) {
        XrResult r = reqRate(x.session, wanted);
        QT_LOG("requested %.0f Hz: %s", wanted, XR_SUCCEEDED(r) ? "ok" : "refused");
    }
    float hz = 0;
    if (getRate && XR_SUCCEEDED(getRate(x.session, &hz)) && hz > 1.0f) x.displayHz = hz;
    QT_LOG("display refresh rate: %.0f Hz", x.displayHz);
}

static void setupPassthrough(Xr& x) {
    if (!x.hasPassthroughExt) return;
    XrSystemPassthroughPropertiesFB pp{XR_TYPE_SYSTEM_PASSTHROUGH_PROPERTIES_FB};
    XrSystemProperties sp{XR_TYPE_SYSTEM_PROPERTIES};
    sp.next = &pp;
    if (XR_FAILED(xrGetSystemProperties(x.instance, x.systemId, &sp)) || !pp.supportsPassthrough) return;
    PFN_xrCreatePassthroughFB createPt = nullptr;
    PFN_xrCreatePassthroughLayerFB createLayer = nullptr;
    xrGetInstanceProcAddr(x.instance, "xrCreatePassthroughFB", reinterpret_cast<PFN_xrVoidFunction*>(&createPt));
    xrGetInstanceProcAddr(x.instance, "xrCreatePassthroughLayerFB", reinterpret_cast<PFN_xrVoidFunction*>(&createLayer));
    if (!createPt || !createLayer) return;
    XrPassthroughCreateInfoFB pci{XR_TYPE_PASSTHROUGH_CREATE_INFO_FB};
    pci.flags = XR_PASSTHROUGH_IS_RUNNING_AT_CREATION_BIT_FB;
    if (XR_FAILED(createPt(x.session, &pci, &x.passthrough))) return;
    XrPassthroughLayerCreateInfoFB lci{XR_TYPE_PASSTHROUGH_LAYER_CREATE_INFO_FB};
    lci.passthrough = x.passthrough;
    lci.flags = XR_PASSTHROUGH_IS_RUNNING_AT_CREATION_BIT_FB;
    lci.purpose = XR_PASSTHROUGH_LAYER_PURPOSE_RECONSTRUCTION_FB;
    if (XR_FAILED(createLayer(x.session, &lci, &x.passthroughLayer))) return;
    x.passthroughOk = true;
    QT_LOG("passthrough available");
}

static bool createSwapchains(Xr& x) {
    uint32_t n = 0;
    CHECK_XR(xrEnumerateViewConfigurationViews(x.instance, x.systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, 0, &n, nullptr));
    std::vector<XrViewConfigurationView> views(n, {XR_TYPE_VIEW_CONFIGURATION_VIEW});
    CHECK_XR(xrEnumerateViewConfigurationViews(x.instance, x.systemId, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO, n, &n, views.data()));
    if (n < 2) return false;
    uint32_t nf = 0;
    CHECK_XR(xrEnumerateSwapchainFormats(x.session, 0, &nf, nullptr));
    std::vector<int64_t> formats(nf);
    CHECK_XR(xrEnumerateSwapchainFormats(x.session, nf, &nf, formats.data()));
    x.colorFormat = 0;
    for (int64_t want : {static_cast<int64_t>(GL_SRGB8_ALPHA8), static_cast<int64_t>(GL_RGBA8)}) {
        for (int64_t f : formats)
            if (f == want) x.colorFormat = static_cast<GLenum>(want);
        if (x.colorFormat) break;
    }
    if (!x.colorFormat) {
        QT_LOG("no usable swapchain format");
        return false;
    }
    x.srgb = x.colorFormat == GL_SRGB8_ALPHA8;
    for (int e = 0; e < 2; ++e) {
        Eye& eye = x.eyes[e];
        XrSwapchainCreateInfo sc{XR_TYPE_SWAPCHAIN_CREATE_INFO};
        sc.usageFlags = XR_SWAPCHAIN_USAGE_COLOR_ATTACHMENT_BIT | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
        sc.format = x.colorFormat;
        sc.sampleCount = 1;
        sc.width = views[e].recommendedImageRectWidth;
        sc.height = views[e].recommendedImageRectHeight;
        sc.faceCount = sc.arraySize = sc.mipCount = 1;
        CHECK_XR(xrCreateSwapchain(x.session, &sc, &eye.swapchain));
        eye.width = static_cast<int32_t>(sc.width);
        eye.height = static_cast<int32_t>(sc.height);
        uint32_t ni = 0;
        CHECK_XR(xrEnumerateSwapchainImages(eye.swapchain, 0, &ni, nullptr));
        eye.images.assign(ni, {XR_TYPE_SWAPCHAIN_IMAGE_OPENGL_ES_KHR});
        CHECK_XR(xrEnumerateSwapchainImages(eye.swapchain, ni, &ni, reinterpret_cast<XrSwapchainImageBaseHeader*>(eye.images.data())));
        eye.fbos.assign(ni, 0);
        glGenFramebuffers(static_cast<GLsizei>(ni), eye.fbos.data());
        for (uint32_t i = 0; i < ni; ++i) {
            glBindFramebuffer(GL_FRAMEBUFFER, eye.fbos[i]);
            glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, eye.images[i].image, 0);
        }
    }
    // one multisampled render target shared by both eyes (they have the same size), resolved into the swapchain image
    x.msW = x.eyes[0].width;
    x.msH = x.eyes[0].height;
    GLint maxSamples = 4;
    glGetIntegerv(GL_MAX_SAMPLES, &maxSamples);
    GLsizei samples = maxSamples >= 4 ? 4 : maxSamples;
    glGenFramebuffers(1, &x.msFbo);
    glBindFramebuffer(GL_FRAMEBUFFER, x.msFbo);
    glGenRenderbuffers(1, &x.msColor);
    glBindRenderbuffer(GL_RENDERBUFFER, x.msColor);
    glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, x.colorFormat, x.msW, x.msH);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, x.msColor);
    glGenRenderbuffers(1, &x.msDepth);
    glBindRenderbuffer(GL_RENDERBUFFER, x.msDepth);
    glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, GL_DEPTH_COMPONENT24, x.msW, x.msH);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, x.msDepth);
    GLenum st = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    QT_LOG("swapchains: %dx%d per eye, %s, %dx MSAA, framebuffer %s", x.msW, x.msH, x.srgb ? "sRGB" : "RGBA8", (int)samples,
           st == GL_FRAMEBUFFER_COMPLETE ? "complete" : "INCOMPLETE");
    return st == GL_FRAMEBUFFER_COMPLETE;
}

static bool createActions(Xr& x) {
    XrActionSetCreateInfo asi{XR_TYPE_ACTION_SET_CREATE_INFO};
    std::strcpy(asi.actionSetName, "teleop");
    std::strcpy(asi.localizedActionSetName, "Teleoperation");
    CHECK_XR(xrCreateActionSet(x.instance, &asi, &x.actionSet));
    CHECK_XR(xrStringToPath(x.instance, "/user/hand/left", &x.handPath[0]));
    CHECK_XR(xrStringToPath(x.instance, "/user/hand/right", &x.handPath[1]));
    auto make = [&](XrAction& a, XrActionType type, const char* name, const char* nice) -> XrResult {
        XrActionCreateInfo ci{XR_TYPE_ACTION_CREATE_INFO};
        ci.actionType = type;
        std::strcpy(ci.actionName, name);
        std::strcpy(ci.localizedActionName, nice);
        ci.countSubactionPaths = 2;
        ci.subactionPaths = x.handPath;
        return xrCreateAction(x.actionSet, &ci, &a);
    };
    CHECK_XR(make(x.aGrip, XR_ACTION_TYPE_POSE_INPUT, "grip_pose", "Grip pose"));
    CHECK_XR(make(x.aAim, XR_ACTION_TYPE_POSE_INPUT, "aim_pose", "Aim pose"));
    CHECK_XR(make(x.aTrigger, XR_ACTION_TYPE_FLOAT_INPUT, "trigger", "Trigger"));
    CHECK_XR(make(x.aSqueeze, XR_ACTION_TYPE_FLOAT_INPUT, "squeeze", "Grip button"));
    CHECK_XR(make(x.aStick, XR_ACTION_TYPE_VECTOR2F_INPUT, "thumbstick", "Thumbstick"));
    CHECK_XR(make(x.aStickClick, XR_ACTION_TYPE_BOOLEAN_INPUT, "thumbstick_click", "Thumbstick click"));
    CHECK_XR(make(x.aPrimary, XR_ACTION_TYPE_BOOLEAN_INPUT, "primary", "A / X button"));
    CHECK_XR(make(x.aSecondary, XR_ACTION_TYPE_BOOLEAN_INPUT, "secondary", "B / Y button"));
    CHECK_XR(make(x.aMenu, XR_ACTION_TYPE_BOOLEAN_INPUT, "menu", "Menu button"));
    CHECK_XR(make(x.aTriggerTouch, XR_ACTION_TYPE_BOOLEAN_INPUT, "trigger_touch", "Trigger touch"));
    CHECK_XR(make(x.aStickTouch, XR_ACTION_TYPE_BOOLEAN_INPUT, "thumbstick_touch", "Thumbstick touch"));
    CHECK_XR(make(x.aPrimaryTouch, XR_ACTION_TYPE_BOOLEAN_INPUT, "primary_touch", "A / X touch"));
    CHECK_XR(make(x.aSecondaryTouch, XR_ACTION_TYPE_BOOLEAN_INPUT, "secondary_touch", "B / Y touch"));
    CHECK_XR(make(x.aThumbrest, XR_ACTION_TYPE_BOOLEAN_INPUT, "thumbrest_touch", "Thumb rest touch"));
    CHECK_XR(make(x.aHaptic, XR_ACTION_TYPE_VIBRATION_OUTPUT, "haptic", "Vibration"));

    // `b`: every binding; `core`: without the capacitive touch inputs (used if the runtime rejects the full list)
    std::vector<XrActionSuggestedBinding> b, core;
    auto bind = [&](XrAction a, const char* path) {
        XrPath p = XR_NULL_PATH;
        if (XR_FAILED(xrStringToPath(x.instance, path, &p))) return;
        b.push_back({a, p});
        if (!std::strstr(path, "/touch")) core.push_back({a, p});
    };
    for (const char* hand : {"/user/hand/left", "/user/hand/right"}) {
        std::string h = hand;
        bool left = h.find("left") != std::string::npos;
        bind(x.aGrip, (h + "/input/grip/pose").c_str());
        bind(x.aAim, (h + "/input/aim/pose").c_str());
        bind(x.aTrigger, (h + "/input/trigger/value").c_str());
        bind(x.aSqueeze, (h + "/input/squeeze/value").c_str());
        bind(x.aStick, (h + "/input/thumbstick").c_str());
        bind(x.aStickClick, (h + "/input/thumbstick/click").c_str());
        bind(x.aPrimary, (h + (left ? "/input/x/click" : "/input/a/click")).c_str());
        bind(x.aSecondary, (h + (left ? "/input/y/click" : "/input/b/click")).c_str());
        bind(x.aTriggerTouch, (h + "/input/trigger/touch").c_str());
        bind(x.aStickTouch, (h + "/input/thumbstick/touch").c_str());
        bind(x.aPrimaryTouch, (h + (left ? "/input/x/touch" : "/input/a/touch")).c_str());
        bind(x.aSecondaryTouch, (h + (left ? "/input/y/touch" : "/input/b/touch")).c_str());
        bind(x.aThumbrest, (h + "/input/thumbrest/touch").c_str());
        bind(x.aHaptic, (h + "/output/haptic").c_str());
        if (left) bind(x.aMenu, (h + "/input/menu/click").c_str());
    }
    XrPath profile = XR_NULL_PATH;
    CHECK_XR(xrStringToPath(x.instance, "/interaction_profiles/oculus/touch_controller", &profile));
    XrInteractionProfileSuggestedBinding sb{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING};
    sb.interactionProfile = profile;
    sb.countSuggestedBindings = static_cast<uint32_t>(b.size());
    sb.suggestedBindings = b.data();
    if (XR_FAILED(xrSuggestInteractionProfileBindings(x.instance, &sb))) {
        QT_LOG("full binding list rejected: retrying without touch inputs");
        sb.countSuggestedBindings = static_cast<uint32_t>(core.size());
        sb.suggestedBindings = core.data();
        CHECK_XR(xrSuggestInteractionProfileBindings(x.instance, &sb));
    }

    for (int h = 0; h < 2; ++h) {
        XrActionSpaceCreateInfo sci{XR_TYPE_ACTION_SPACE_CREATE_INFO};
        sci.poseInActionSpace.orientation.w = 1.0f;
        sci.subactionPath = x.handPath[h];
        sci.action = x.aGrip;
        CHECK_XR(xrCreateActionSpace(x.session, &sci, &x.gripSpace[h]));
        sci.action = x.aAim;
        CHECK_XR(xrCreateActionSpace(x.session, &sci, &x.aimSpace[h]));
    }
    XrSessionActionSetsAttachInfo ai{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO};
    ai.countActionSets = 1;
    ai.actionSets = &x.actionSet;
    CHECK_XR(xrAttachSessionActionSets(x.session, &ai));
    return true;
}

// ------------------------------------------------------------------------------------------------- per frame
static void pollXrEvents(Xr& x) {
    XrEventDataBuffer ev{XR_TYPE_EVENT_DATA_BUFFER};
    while (xrPollEvent(x.instance, &ev) == XR_SUCCESS) {
        if (ev.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
            auto* sc = reinterpret_cast<XrEventDataSessionStateChanged*>(&ev);
            x.state = sc->state;
            QT_LOG("session state %d", (int)x.state);
            if (x.state == XR_SESSION_STATE_READY) {
                XrSessionBeginInfo bi{XR_TYPE_SESSION_BEGIN_INFO};
                bi.primaryViewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
                x.running = XR_SUCCEEDED(xrBeginSession(x.session, &bi));
            } else if (x.state == XR_SESSION_STATE_STOPPING) {
                xrEndSession(x.session);
                x.running = false;
            } else if (x.state == XR_SESSION_STATE_EXITING || x.state == XR_SESSION_STATE_LOSS_PENDING) {
                x.running = false;
                x.exitRequested = true;
            }
        } else if (ev.type == XR_TYPE_EVENT_DATA_INSTANCE_LOSS_PENDING) {
            x.exitRequested = true;
        }
        ev = {XR_TYPE_EVENT_DATA_BUFFER};
    }
}

static bool getBool(Xr& x, XrAction a, int hand) {
    XrActionStateGetInfo gi{XR_TYPE_ACTION_STATE_GET_INFO};
    gi.action = a;
    gi.subactionPath = x.handPath[hand];
    XrActionStateBoolean s{XR_TYPE_ACTION_STATE_BOOLEAN};
    return XR_SUCCEEDED(xrGetActionStateBoolean(x.session, &gi, &s)) && s.isActive && s.currentState;
}

static float getFloat(Xr& x, XrAction a, int hand) {
    XrActionStateGetInfo gi{XR_TYPE_ACTION_STATE_GET_INFO};
    gi.action = a;
    gi.subactionPath = x.handPath[hand];
    XrActionStateFloat s{XR_TYPE_ACTION_STATE_FLOAT};
    return XR_SUCCEEDED(xrGetActionStateFloat(x.session, &gi, &s)) && s.isActive ? s.currentState : 0.0f;
}

static void readInput(Xr& x, XrTime time, FrameInput& in) {
    XrActiveActionSet active{x.actionSet, XR_NULL_PATH};
    XrActionsSyncInfo si{XR_TYPE_ACTIONS_SYNC_INFO};
    si.countActiveActionSets = 1;
    si.activeActionSets = &active;
    xrSyncActions(x.session, &si);

    XrSpaceLocation hl{XR_TYPE_SPACE_LOCATION};
    if (XR_SUCCEEDED(xrLocateSpace(x.viewSpace, x.stage, time, &hl)) &&
        (hl.locationFlags & XR_SPACE_LOCATION_ORIENTATION_VALID_BIT) && (hl.locationFlags & XR_SPACE_LOCATION_POSITION_VALID_BIT))
        in.head = toPose(hl.pose);
    for (int h = 0; h < 2; ++h) {
        HandInput& hd = in.hands[h];
        XrActionStateGetInfo gi{XR_TYPE_ACTION_STATE_GET_INFO};
        gi.action = x.aGrip;
        gi.subactionPath = x.handPath[h];
        XrActionStatePose ps{XR_TYPE_ACTION_STATE_POSE};
        hd.active = XR_SUCCEEDED(xrGetActionStatePose(x.session, &gi, &ps)) && ps.isActive;
        const XrSpaceLocationFlags valid = XR_SPACE_LOCATION_ORIENTATION_VALID_BIT | XR_SPACE_LOCATION_POSITION_VALID_BIT;
        const XrSpaceLocationFlags tracked = XR_SPACE_LOCATION_ORIENTATION_TRACKED_BIT | XR_SPACE_LOCATION_POSITION_TRACKED_BIT;
        XrSpaceVelocity vel{XR_TYPE_SPACE_VELOCITY};
        XrSpaceLocation loc{XR_TYPE_SPACE_LOCATION};
        loc.next = &vel;
        if (hd.active && XR_SUCCEEDED(xrLocateSpace(x.gripSpace[h], x.stage, time, &loc)) && (loc.locationFlags & valid) == valid) {
            hd.gripValid = true;
            hd.tracked = (loc.locationFlags & tracked) == tracked;
            hd.grip = toPose(loc.pose);
            if (vel.velocityFlags & XR_SPACE_VELOCITY_LINEAR_VALID_BIT)
                hd.velocity = Vec3(vel.linearVelocity.x, vel.linearVelocity.y, vel.linearVelocity.z);
            if (vel.velocityFlags & XR_SPACE_VELOCITY_ANGULAR_VALID_BIT)
                hd.angularVelocity = Vec3(vel.angularVelocity.x, vel.angularVelocity.y, vel.angularVelocity.z);
        }
        XrSpaceLocation al{XR_TYPE_SPACE_LOCATION};
        if (hd.active && XR_SUCCEEDED(xrLocateSpace(x.aimSpace[h], x.stage, time, &al)) && (al.locationFlags & valid) == valid) {
            hd.aimValid = true;
            hd.aim = toPose(al.pose);
        }
        hd.trigger = getFloat(x, x.aTrigger, h);
        hd.squeeze = getFloat(x, x.aSqueeze, h);
        gi.action = x.aStick;
        XrActionStateVector2f v2{XR_TYPE_ACTION_STATE_VECTOR2F};
        if (XR_SUCCEEDED(xrGetActionStateVector2f(x.session, &gi, &v2)) && v2.isActive) {
            hd.stickX = v2.currentState.x;
            hd.stickY = v2.currentState.y;
        }
        uint16_t b = 0;
        if (getBool(x, x.aPrimary, h)) b |= B_PRIMARY;
        if (getBool(x, x.aSecondary, h)) b |= B_SECONDARY;
        if (getBool(x, x.aStickClick, h)) b |= B_STICK;
        if (h == 0 && getBool(x, x.aMenu, h)) b |= B_MENU;
        if (getBool(x, x.aTriggerTouch, h)) b |= B_TRIGGER_TOUCH;
        if (getBool(x, x.aStickTouch, h)) b |= B_STICK_TOUCH;
        if (getBool(x, x.aPrimaryTouch, h)) b |= B_PRIMARY_TOUCH;
        if (getBool(x, x.aSecondaryTouch, h)) b |= B_SECONDARY_TOUCH;
        if (getBool(x, x.aThumbrest, h)) b |= B_THUMBREST_TOUCH;
        hd.buttons = b;
    }
}

static void playHaptics(Xr& x, const App& app) {
    for (int h = 0; h < 2; ++h) {
        const HapticCommand& c = app.haptics[h];
        if (!c.fire) continue;
        XrHapticActionInfo hi{XR_TYPE_HAPTIC_ACTION_INFO};
        hi.action = x.aHaptic;
        hi.subactionPath = x.handPath[h];
        if (c.amplitude <= 0.0f || c.duration <= 0.0f) {
            xrStopHapticFeedback(x.session, &hi);
            continue;
        }
        XrHapticVibration v{XR_TYPE_HAPTIC_VIBRATION};
        v.amplitude = c.amplitude > 1.0f ? 1.0f : c.amplitude;
        v.duration = static_cast<XrDuration>(c.duration * 1e9f);
        v.frequency = c.frequency > 0.0f ? c.frequency : XR_FREQUENCY_UNSPECIFIED;
        xrApplyHapticFeedback(x.session, &hi, reinterpret_cast<const XrHapticBaseHeader*>(&v));
    }
}

static void renderFrame(Xr& x, App& app) {
    XrFrameWaitInfo wi{XR_TYPE_FRAME_WAIT_INFO};
    XrFrameState fs{XR_TYPE_FRAME_STATE};
    if (XR_FAILED(xrWaitFrame(x.session, &wi, &fs))) return;
    XrFrameBeginInfo bi{XR_TYPE_FRAME_BEGIN_INFO};
    if (XR_FAILED(xrBeginFrame(x.session, &bi))) return;

    FrameInput in;
    in.time = monoSeconds();
    in.dt = fs.predictedDisplayPeriod > 0 ? 1e-9 * static_cast<double>(fs.predictedDisplayPeriod) : 1.0 / x.displayHz;
    in.focused = x.state == XR_SESSION_STATE_FOCUSED;
    in.passthroughAvailable = x.passthroughOk;
    in.head.p = Vec3(0, 1.6f, 0);
    readInput(x, fs.predictedDisplayTime, in);
    app.update(in);                       // receives the newest state right before drawing, sends this frame's input
    playHaptics(x, app);

    bool seeRoom = x.passthroughOk && app.passthrough;
    XrView views[2] = {{XR_TYPE_VIEW}, {XR_TYPE_VIEW}};
    XrCompositionLayerProjectionView pv[2] = {{XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}, {XR_TYPE_COMPOSITION_LAYER_PROJECTION_VIEW}};
    bool rendered = false;
    if (fs.shouldRender) {
        XrViewLocateInfo li{XR_TYPE_VIEW_LOCATE_INFO};
        li.viewConfigurationType = XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO;
        li.displayTime = fs.predictedDisplayTime;
        li.space = x.stage;
        XrViewState vs{XR_TYPE_VIEW_STATE};
        uint32_t nv = 0;
        if (XR_SUCCEEDED(xrLocateViews(x.session, &li, &vs, 2, &nv, views)) && nv == 2 &&
            (vs.viewStateFlags & XR_VIEW_STATE_ORIENTATION_VALID_BIT) && (vs.viewStateFlags & XR_VIEW_STATE_POSITION_VALID_BIT)) {
            rendered = true;
            for (int e = 0; e < 2 && rendered; ++e) {
                Eye& eye = x.eyes[e];
                uint32_t idx = 0;
                XrSwapchainImageAcquireInfo ai{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
                if (XR_FAILED(xrAcquireSwapchainImage(eye.swapchain, &ai, &idx))) { rendered = false; break; }
                XrSwapchainImageWaitInfo swi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO};
                swi.timeout = XR_INFINITE_DURATION;
                xrWaitSwapchainImage(eye.swapchain, &swi);

                glBindFramebuffer(GL_FRAMEBUFFER, x.msFbo);
                glViewport(0, 0, eye.width, eye.height);
                if (seeRoom) glClearColor(0, 0, 0, 0);
                else glClearColor(app.clearColor[0], app.clearColor[1], app.clearColor[2], 1.0f);
                glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
                Pose ep = toPose(views[e].pose);
                const XrFovf& f = views[e].fov;
                Mat4 proj = projectionFov(std::tan(f.angleLeft), std::tan(f.angleRight), std::tan(f.angleDown), std::tan(f.angleUp), 0.05f, 60.0f);
                app.render(viewFromPose(ep), proj, ep.p, !x.srgb);
                glBindFramebuffer(GL_READ_FRAMEBUFFER, x.msFbo);
                glBindFramebuffer(GL_DRAW_FRAMEBUFFER, eye.fbos[idx]);
                glBlitFramebuffer(0, 0, eye.width, eye.height, 0, 0, eye.width, eye.height, GL_COLOR_BUFFER_BIT, GL_NEAREST);
                const GLenum discard[2] = {GL_COLOR_ATTACHMENT0, GL_DEPTH_ATTACHMENT};
                glInvalidateFramebuffer(GL_READ_FRAMEBUFFER, 2, discard);
                glBindFramebuffer(GL_FRAMEBUFFER, 0);

                XrSwapchainImageReleaseInfo ri{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
                xrReleaseSwapchainImage(eye.swapchain, &ri);
                pv[e].pose = views[e].pose;
                pv[e].fov = views[e].fov;
                pv[e].subImage.swapchain = eye.swapchain;
                pv[e].subImage.imageRect.offset = {0, 0};
                pv[e].subImage.imageRect.extent = {eye.width, eye.height};
                pv[e].subImage.imageArrayIndex = 0;
            }
        }
    }
    XrCompositionLayerPassthroughFB ptLayer{XR_TYPE_COMPOSITION_LAYER_PASSTHROUGH_FB};
    ptLayer.flags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
    ptLayer.layerHandle = x.passthroughLayer;
    XrCompositionLayerProjection proj{XR_TYPE_COMPOSITION_LAYER_PROJECTION};
    proj.space = x.stage;
    proj.viewCount = 2;
    proj.views = pv;
    if (seeRoom) proj.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
    const XrCompositionLayerBaseHeader* layers[2];
    uint32_t nLayers = 0;
    if (rendered) {
        if (seeRoom) layers[nLayers++] = reinterpret_cast<const XrCompositionLayerBaseHeader*>(&ptLayer);
        layers[nLayers++] = reinterpret_cast<const XrCompositionLayerBaseHeader*>(&proj);
    }
    XrFrameEndInfo ei{XR_TYPE_FRAME_END_INFO};
    ei.displayTime = fs.predictedDisplayTime;
    ei.environmentBlendMode = XR_ENVIRONMENT_BLEND_MODE_OPAQUE;
    ei.layerCount = nLayers;
    ei.layers = layers;
    xrEndFrame(x.session, &ei);
}

// ------------------------------------------------------------------------------------------------- entry point
static void onAppCmd(android_app* app, int32_t cmd) {
    if (cmd == APP_CMD_DESTROY) QT_LOG("activity destroyed");
    (void)app;
}

static void pumpAndroid(android_app* app, int timeoutMs) {
    for (;;) {
        android_poll_source* source = nullptr;
        int events = 0;
        int id = ALooper_pollOnce(timeoutMs, nullptr, &events, reinterpret_cast<void**>(&source));
        if (id < 0) break;                       // timeout or error: nothing more pending
        if (source) source->process(app, source);
        timeoutMs = 0;
        if (app->destroyRequested) break;
    }
}

void android_main(android_app* app) {
    JNIEnv* env = nullptr;
    app->activity->vm->AttachCurrentThread(&env, nullptr);
    app->onAppCmd = onAppCmd;

    Xr x;
    x.app = app;
    NetConfig net;
    net.clientName = "quest-teleop";
    char model[PROP_VALUE_MAX] = "unknown";
    __system_property_get("ro.product.model", model);
    net.device = model;
    std::string server = intentExtra(app, env, "server");
    if (!server.empty()) {
        size_t c = server.find(':');
        net.host = server.substr(0, c);
        if (c != std::string::npos) net.port = std::atoi(server.c_str() + c + 1);
    }
    std::string hz = intentExtra(app, env, "hz");
    std::string pt = intentExtra(app, env, "passthrough");
    QT_LOG("starting on %s, server '%s'", model, server.empty() ? "(discover)" : server.c_str());

    bool ok = initEgl(x) && createInstance(x) && createSession(x);
    if (ok) {
        setupRefreshRate(x, hz.empty() ? 0.0f : static_cast<float>(std::atof(hz.c_str())));
        setupPassthrough(x);
        ok = createSwapchains(x) && createActions(x);
    }
    App teleop;
    if (ok) {
        net.displayHz = x.displayHz;
        std::string settings = app->activity->internalDataPath ? std::string(app->activity->internalDataPath) + "/settings.txt" : "";
        teleop.init(net, settings);
        if (!pt.empty()) teleop.passthrough = pt != "0";
    } else {
        QT_LOG("initialisation failed");
        ANativeActivity_finish(app->activity);
    }

    // The Quest stops drawing an app that is not in front (headset taken off, Quest menu or another app in front) but
    // keeps its process, and its network thread, alive. A connection kept open then shows on the server as a client
    // that sends no input and never loads the scene, and the messages it does not read pile up. So the app goes
    // offline 3 s after it stops being visible and reconnects as soon as it is visible again.
    double hiddenSince = -1.0;
    while (!app->destroyRequested) {
        pumpAndroid(app, (ok && x.running) ? 0 : 100);
        if (!ok) continue;
        pollXrEvents(x);
        if (x.exitRequested) {
            ANativeActivity_finish(app->activity);
            x.exitRequested = false;
            continue;
        }
        bool visible = x.running && (x.state == XR_SESSION_STATE_VISIBLE || x.state == XR_SESSION_STATE_FOCUSED);
        timespec ts{};
        clock_gettime(CLOCK_MONOTONIC, &ts);
        double now = static_cast<double>(ts.tv_sec) + 1e-9 * static_cast<double>(ts.tv_nsec);
        if (visible) hiddenSince = -1.0;
        else if (hiddenSince < 0.0) hiddenSince = now;
        teleop.setOnline(visible || now - hiddenSince < 3.0);
        if (x.running) renderFrame(x, teleop);
    }

    if (ok) teleop.shutdown();
    if (x.session != XR_NULL_HANDLE) xrDestroySession(x.session);
    if (x.instance != XR_NULL_HANDLE) xrDestroyInstance(x.instance);
    if (x.eglDisplay != EGL_NO_DISPLAY) {
        eglMakeCurrent(x.eglDisplay, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
        eglTerminate(x.eglDisplay);
    }
    app->activity->vm->DetachCurrentThread();
}
