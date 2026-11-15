// Desktop client: the same core as the headset app (network, scene, renderer, placement), with a window instead of
// OpenXR. The mouse and keyboard stand in for the head and the two controllers, so a server can be checked without
// a headset. `--offscreen` renders a scripted session without a window and writes PNG frames (used by the tests).
//
//   qt_desktop [--server HOST[:PORT]] [--tcp-only] [--size WxH]
//   qt_desktop --offscreen --frames 300 --out frame.png [--script circle|jump|edit|menu|still] [--every 50] [--action N] [--large-text] [--left-handed]
//   view options: --head X,Y,Z (stage metres)  --pitch RAD  --yaw RAD  --zoom FACTOR (scene scale after placement)
//
// Window controls
//   right mouse drag   look around            W A S D, Q / E     move the head, down / up
//   left mouse drag    move the active hand   wheel              active hand nearer / farther
//   shift + left drag  turn the active hand   TAB                switch the active hand (starts with the right one)
//   G (hold)           grip (clutch)          T (hold)           trigger
//   1 / 2              primary (A / X) / secondary (B / Y) button
//   M                  menu button: place / scale the scene (edit mode); arrow keys = left stick, I J K L = right stick
//   ESC                quit
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES3/gl3.h>
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <X11/keysym.h>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>
#include <zlib.h>

#include "../core/app.h"
#include "../core/log.h"

using namespace qt;

static double nowSeconds() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

static bool writePng(const std::string& path, int w, int h, const std::vector<uint8_t>& rgba) {
    std::vector<uint8_t> raw;
    raw.reserve(static_cast<size_t>(h) * (static_cast<size_t>(w) * 3 + 1));
    for (int y = h - 1; y >= 0; --y) {                 // GL rows are bottom-up
        raw.push_back(0);
        for (int x = 0; x < w; ++x) {
            const uint8_t* p = &rgba[(static_cast<size_t>(y) * w + x) * 4];
            raw.insert(raw.end(), p, p + 3);
        }
    }
    uLongf zl = compressBound(static_cast<uLong>(raw.size()));
    std::vector<uint8_t> z(zl);
    if (compress2(z.data(), &zl, raw.data(), static_cast<uLong>(raw.size()), 6) != Z_OK) return false;
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    auto be32 = [](uint32_t v, uint8_t* o) { o[0] = v >> 24; o[1] = v >> 16; o[2] = v >> 8; o[3] = v; };
    auto chunk = [&](const char* type, const uint8_t* data, uint32_t n) {
        uint8_t len[4], crc[4];
        be32(n, len);
        std::fwrite(len, 1, 4, f);
        std::fwrite(type, 1, 4, f);
        if (n) std::fwrite(data, 1, n, f);
        uLong c = crc32(0, reinterpret_cast<const Bytef*>(type), 4);
        if (n) c = crc32(c, data, n);
        be32(static_cast<uint32_t>(c), crc);
        std::fwrite(crc, 1, 4, f);
    };
    const uint8_t sig[8] = {137, 80, 78, 71, 13, 10, 26, 10};
    std::fwrite(sig, 1, 8, f);
    uint8_t ihdr[13];
    be32(static_cast<uint32_t>(w), ihdr);
    be32(static_cast<uint32_t>(h), ihdr + 4);
    ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
    chunk("IHDR", ihdr, 13);
    chunk("IDAT", z.data(), static_cast<uint32_t>(zl));
    chunk("IEND", nullptr, 0);
    std::fclose(f);
    return true;
}

struct Target {                       // multisampled colour + depth, resolved into a texture
    GLuint msFbo = 0, msColor = 0, msDepth = 0, fbo = 0, tex = 0;
    int w = 0, h = 0;
    void create(int width, int height, int samples) {
        w = width;
        h = height;
        glGenFramebuffers(1, &msFbo);
        glBindFramebuffer(GL_FRAMEBUFFER, msFbo);
        glGenRenderbuffers(1, &msColor);
        glBindRenderbuffer(GL_RENDERBUFFER, msColor);
        glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, GL_RGBA8, w, h);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, msColor);
        glGenRenderbuffers(1, &msDepth);
        glBindRenderbuffer(GL_RENDERBUFFER, msDepth);
        glRenderbufferStorageMultisample(GL_RENDERBUFFER, samples, GL_DEPTH_COMPONENT24, w, h);
        glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, msDepth);
        glGenFramebuffers(1, &fbo);
        glBindFramebuffer(GL_FRAMEBUFFER, fbo);
        glGenTextures(1, &tex);
        glBindTexture(GL_TEXTURE_2D, tex);
        glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, w, h);
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0);
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    }
    void destroy() {
        glDeleteFramebuffers(1, &msFbo);
        glDeleteFramebuffers(1, &fbo);
        glDeleteRenderbuffers(1, &msColor);
        glDeleteRenderbuffers(1, &msDepth);
        glDeleteTextures(1, &tex);
        *this = Target();
    }
    void resolve() {
        glBindFramebuffer(GL_READ_FRAMEBUFFER, msFbo);
        glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo);
        glBlitFramebuffer(0, 0, w, h, 0, 0, w, h, GL_COLOR_BUFFER_BIT, GL_NEAREST);
    }
};

struct Sim {                          // emulated head and controllers
    Vec3 headPos{0, 1.6f, 0.2f};
    float headYaw = 0, headPitch = -0.45f;
    Vec3 handOff[2] = {{-0.18f, -0.32f, -0.45f}, {0.18f, -0.32f, -0.45f}};   // in the head's yaw frame
    float handYaw[2] = {0, 0}, handPitch[2] = {-0.6f, -0.6f};
    float trigger[2] = {0, 0}, squeeze[2] = {0, 0};
    float stick[2][2] = {{0, 0}, {0, 0}};
    uint16_t buttons[2] = {0, 0};
    int active = 1;

    Pose head() const { return {headPos, axisAngle(Vec3(0, 1, 0), headYaw) * axisAngle(Vec3(1, 0, 0), headPitch)}; }
    void fill(FrameInput& in) const {
        in.head = head();
        Quat hy = axisAngle(Vec3(0, 1, 0), headYaw);
        for (int h = 0; h < 2; ++h) {
            HandInput& hd = in.hands[h];
            hd.active = hd.gripValid = hd.aimValid = hd.tracked = true;
            hd.grip.p = headPos + rotate(hy, handOff[h]);
            hd.grip.q = hy * axisAngle(Vec3(0, 1, 0), handYaw[h]) * axisAngle(Vec3(1, 0, 0), handPitch[h]);
            hd.aim = hd.grip;
            hd.trigger = trigger[h];
            hd.squeeze = squeeze[h];
            hd.stickX = stick[h][0];
            hd.stickY = stick[h][1];
            hd.buttons = buttons[h];
        }
    }
};

static void script(const std::string& name, double t, Sim& s) {
    if (name == "still") return;
    s.squeeze[1] = t > 0.4 ? 1.0f : 0.0f;
    s.handOff[1] = Vec3(0.18f + 0.06f * static_cast<float>(std::cos(2 * t)), -0.32f + 0.05f * static_cast<float>(std::sin(2 * t)),
                        -0.45f - 0.04f * static_cast<float>(std::sin(t)));
    s.trigger[1] = (static_cast<int>(t / 2.5) % 2) ? 1.0f : 0.0f;
    s.buttons[1] = (static_cast<int>(t / 2.5) % 2) ? 0 : B_PRIMARY;
    if (name == "jump" && t > 2.0) s.handOff[1].x += 0.30f;
    if (name == "edit" || name == "menu") {            // open the menu; "menu" also points at an action (main loop)
        s.buttons[0] = (t > 1.0 && t < 1.2) ? B_MENU : 0;
        s.buttons[1] = 0;
        s.squeeze[1] = 0;
        s.trigger[1] = 0;
        s.stick[1][0] = (name == "edit" && t > 1.5 && t < 2.5) ? 0.8f : 0.0f;
    }
}

int main(int argc, char** argv) {
    NetConfig net;
    net.clientName = "qt_desktop";
    net.device = "desktop";
    bool offscreen = false;
    int frames = 300, every = 0, width = 1280, height = 800;
    std::string out = "frame.png", scriptName = "circle";
    float zoom = 1.0f;
    int menuAction = 0;
    bool largeText = false, leftHanded = false;
    bool zoomed = false;
    Sim sim;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() { return i + 1 < argc ? std::string(argv[++i]) : std::string(); };
        if (a == "--server") {
            std::string v = next();
            size_t c = v.find(':');
            net.host = v.substr(0, c);
            if (c != std::string::npos) net.port = std::atoi(v.c_str() + c + 1);
        } else if (a == "--port") net.port = std::atoi(next().c_str());
        else if (a == "--tcp-only") net.tcpOnly = true;
        else if (a == "--offscreen") offscreen = true;
        else if (a == "--frames") frames = std::atoi(next().c_str());
        else if (a == "--every") every = std::atoi(next().c_str());
        else if (a == "--out") out = next();
        else if (a == "--script") scriptName = next();
        else if (a == "--action") menuAction = std::atoi(next().c_str());
        else if (a == "--large-text") largeText = true;
        else if (a == "--left-handed") leftHanded = true;
        else if (a == "--zoom") zoom = static_cast<float>(std::atof(next().c_str()));
        else if (a == "--pitch") sim.headPitch = static_cast<float>(std::atof(next().c_str()));
        else if (a == "--yaw") sim.headYaw = static_cast<float>(std::atof(next().c_str()));
        else if (a == "--head") {
            std::string v = next();
            std::sscanf(v.c_str(), "%f,%f,%f", &sim.headPos.x, &sim.headPos.y, &sim.headPos.z);
        } else if (a == "--size") {
            std::string v = next();
            std::sscanf(v.c_str(), "%dx%d", &width, &height);
        } else {
            std::printf("usage: qt_desktop [--server HOST[:PORT]] [--tcp-only] [--size WxH]\n"
                        "       qt_desktop --offscreen --frames N --out frame.png [--script circle|jump|edit|still] [--every K]\n");
            return a == "--help" ? 0 : 1;
        }
    }

    // ---- EGL context (GLES 3) on an X11 window or on a pbuffer
    Display* xd = nullptr;
    Window win = 0;
    Atom wmDelete = 0;
    EGLDisplay dpy = EGL_NO_DISPLAY;
    if (!offscreen) {
        xd = XOpenDisplay(nullptr);
        if (!xd) {
            std::fprintf(stderr, "no X display: use --offscreen\n");
            return 1;
        }
        dpy = eglGetDisplay(reinterpret_cast<EGLNativeDisplayType>(xd));
    } else {
        auto queryDevices = reinterpret_cast<PFNEGLQUERYDEVICESEXTPROC>(eglGetProcAddress("eglQueryDevicesEXT"));
        auto getPlatformDisplay = reinterpret_cast<PFNEGLGETPLATFORMDISPLAYEXTPROC>(eglGetProcAddress("eglGetPlatformDisplayEXT"));
        if (queryDevices && getPlatformDisplay) {
            EGLDeviceEXT devs[8];
            EGLint n = 0;
            if (queryDevices(8, devs, &n) && n > 0) dpy = getPlatformDisplay(EGL_PLATFORM_DEVICE_EXT, devs[0], nullptr);
        }
        if (dpy == EGL_NO_DISPLAY) dpy = eglGetDisplay(EGL_DEFAULT_DISPLAY);
    }
    if (dpy == EGL_NO_DISPLAY || !eglInitialize(dpy, nullptr, nullptr)) {
        std::fprintf(stderr, "EGL initialisation failed\n");
        return 1;
    }
    eglBindAPI(EGL_OPENGL_ES_API);
    const EGLint cfgAttr[] = {EGL_SURFACE_TYPE, offscreen ? EGL_PBUFFER_BIT : EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT,
                              EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
    EGLConfig cfg;
    EGLint nCfg = 0;
    if (!eglChooseConfig(dpy, cfgAttr, &cfg, 1, &nCfg) || nCfg < 1) {
        std::fprintf(stderr, "no EGL config\n");
        return 1;
    }
    EGLSurface surf;
    if (offscreen) {
        const EGLint pb[] = {EGL_WIDTH, 16, EGL_HEIGHT, 16, EGL_NONE};
        surf = eglCreatePbufferSurface(dpy, cfg, pb);
    } else {
        EGLint vid = 0;
        eglGetConfigAttrib(dpy, cfg, EGL_NATIVE_VISUAL_ID, &vid);
        XVisualInfo tmpl{};
        tmpl.visualid = static_cast<VisualID>(vid);
        int nvi = 0;
        XVisualInfo* vi = XGetVisualInfo(xd, VisualIDMask, &tmpl, &nvi);
        XSetWindowAttributes swa{};
        Window root = DefaultRootWindow(xd);
        swa.colormap = XCreateColormap(xd, root, vi ? vi->visual : DefaultVisual(xd, DefaultScreen(xd)), AllocNone);
        swa.event_mask = KeyPressMask | KeyReleaseMask | ButtonPressMask | ButtonReleaseMask | PointerMotionMask | StructureNotifyMask;
        win = XCreateWindow(xd, root, 0, 0, static_cast<unsigned>(width), static_cast<unsigned>(height), 0,
                            vi ? vi->depth : CopyFromParent, InputOutput, vi ? vi->visual : CopyFromParent,
                            CWColormap | CWEventMask, &swa);
        XStoreName(xd, win, "quest-teleop desktop client");
        wmDelete = XInternAtom(xd, "WM_DELETE_WINDOW", False);
        XSetWMProtocols(xd, win, &wmDelete, 1);
        XMapWindow(xd, win);
        surf = eglCreateWindowSurface(dpy, cfg, static_cast<EGLNativeWindowType>(win), nullptr);
    }
    const EGLint ctxAttr[] = {EGL_CONTEXT_MAJOR_VERSION, 3, EGL_CONTEXT_MINOR_VERSION, 0, EGL_NONE};
    EGLContext ctx = eglCreateContext(dpy, cfg, EGL_NO_CONTEXT, ctxAttr);
    if (surf == EGL_NO_SURFACE || ctx == EGL_NO_CONTEXT || !eglMakeCurrent(dpy, surf, surf, ctx)) {
        std::fprintf(stderr, "EGL context creation failed (0x%x)\n", eglGetError());
        return 1;
    }
    if (!offscreen) eglSwapInterval(dpy, 1);
    QT_LOG("GL: %s | %s", glGetString(GL_VERSION), glGetString(GL_RENDERER));

    App app;
    app.init(net);
    app.passthrough = false;
    app.largeText = largeText;
    app.leftHanded = leftHanded;
    Target target;
    target.create(width, height, 4);
    if (!offscreen)
        std::printf("right drag: look | WASD QE: move | left drag: move hand (shift: turn) | wheel: hand depth | TAB: other hand\n"
                    "G: grip (clutch) | T: trigger | 1 / 2: primary / secondary button | M: menu (place the scene) | ESC: quit\n");

    bool keys[256] = {};
    auto keyIndex = [](KeySym k) -> int {
        if (k >= XK_a && k <= XK_z) return static_cast<int>(k - XK_a) + 'a';
        if (k >= XK_0 && k <= XK_9) return static_cast<int>(k - XK_0) + '0';
        switch (k) {
            case XK_Left: return 1; case XK_Right: return 2; case XK_Up: return 3; case XK_Down: return 4;
            case XK_Shift_L: case XK_Shift_R: return 5; case XK_Tab: return 6; case XK_Escape: return 7;
            default: return 0;
        }
    };
    bool lmb = false, rmb = false, quit = false;
    int mx = 0, my = 0, frame = 0;
    double t0 = nowSeconds(), tPrev = t0;
    const double dtFixed = 1.0 / 72.0;

    while (!quit) {
        double now = nowSeconds();
        FrameInput in;
        in.time = now;
        in.dt = offscreen ? dtFixed : std::min(0.1, std::max(1e-4, now - tPrev));
        tPrev = now;
        if (offscreen) {
            script(scriptName, frame * dtFixed, sim);
        } else {
            while (XPending(xd)) {
                XEvent e;
                XNextEvent(xd, &e);
                if (e.type == ClientMessage && static_cast<Atom>(e.xclient.data.l[0]) == wmDelete) quit = true;
                if (e.type == ConfigureNotify && (e.xconfigure.width != width || e.xconfigure.height != height)) {
                    width = std::max(16, e.xconfigure.width);
                    height = std::max(16, e.xconfigure.height);
                    target.destroy();
                    target.create(width, height, 4);
                }
                if (e.type == KeyPress || e.type == KeyRelease) {
                    int k = keyIndex(XLookupKeysym(&e.xkey, 0));
                    bool down = e.type == KeyPress;
                    if (e.type == KeyRelease && XEventsQueued(xd, QueuedAfterReading)) {   // drop auto-repeat pairs
                        XEvent nx;
                        XPeekEvent(xd, &nx);
                        if (nx.type == KeyPress && nx.xkey.time == e.xkey.time && nx.xkey.keycode == e.xkey.keycode) {
                            XNextEvent(xd, &nx);
                            continue;
                        }
                    }
                    if (k == 7 && down) quit = true;
                    if (k == 6 && down) sim.active ^= 1;
                    keys[k] = down;
                }
                if (e.type == ButtonPress || e.type == ButtonRelease) {
                    bool down = e.type == ButtonPress;
                    if (e.xbutton.button == 1) lmb = down;
                    if (e.xbutton.button == 3) rmb = down;
                    if (down && (e.xbutton.button == 4 || e.xbutton.button == 5))
                        sim.handOff[sim.active].z += e.xbutton.button == 4 ? -0.02f : 0.02f;
                    mx = e.xbutton.x;
                    my = e.xbutton.y;
                }
                if (e.type == MotionNotify) {
                    float dx = static_cast<float>(e.xmotion.x - mx), dy = static_cast<float>(e.xmotion.y - my);
                    mx = e.xmotion.x;
                    my = e.xmotion.y;
                    if (rmb) {
                        sim.headYaw -= dx * 0.004f;
                        sim.headPitch = std::max(-1.5f, std::min(1.5f, sim.headPitch - dy * 0.004f));
                    } else if (lmb && keys[5]) {
                        sim.handYaw[sim.active] -= dx * 0.008f;
                        sim.handPitch[sim.active] -= dy * 0.008f;
                    } else if (lmb) {
                        sim.handOff[sim.active].x += dx * 0.0012f;
                        sim.handOff[sim.active].y -= dy * 0.0012f;
                    }
                }
            }
            float dt = static_cast<float>(in.dt);
            Quat hy = axisAngle(Vec3(0, 1, 0), sim.headYaw);
            Vec3 mv(static_cast<float>(keys['d'] - keys['a']), static_cast<float>(keys['e'] - keys['q']), static_cast<float>(keys['s'] - keys['w']));
            sim.headPos += rotate(hy, mv) * (0.8f * dt);
            int h = sim.active;
            for (int k = 0; k < 2; ++k) sim.squeeze[k] = sim.trigger[k] = 0, sim.buttons[k] = 0;
            sim.squeeze[h] = keys['g'] ? 1.0f : 0.0f;
            sim.trigger[h] = keys['t'] ? 1.0f : 0.0f;
            sim.buttons[h] = static_cast<uint16_t>((keys['1'] ? B_PRIMARY : 0) | (keys['2'] ? B_SECONDARY : 0));
            if (keys['m']) sim.buttons[0] |= B_MENU;
            sim.stick[0][0] = static_cast<float>(keys[2] - keys[1]);
            sim.stick[0][1] = static_cast<float>(keys[3] - keys[4]);
            sim.stick[1][0] = static_cast<float>(keys['l'] - keys['j']);
            sim.stick[1][1] = static_cast<float>(keys['i'] - keys['k']);
        }
        sim.fill(in);
        if (offscreen && scriptName == "menu") {       // hover the chosen action button, then hold it
            app.debugPointer = frame * dtFixed > 1.6;
            app.debugAction = menuAction;
            app.debugPress = frame * dtFixed > 2.2;
        }
        app.update(in);
        if (!zoomed && app.scene().loaded) {      // --zoom: applied once, after the automatic placement
            zoomed = true;
            if (zoom != 1.0f) app.zoomAboutFocus(zoom);
        }
        for (int h = 0; h < 2; ++h)
            if (app.haptics[h].fire && !offscreen)
                std::printf("haptic %s: amplitude %.2f for %.0f ms\n", h ? "right" : "left", app.haptics[h].amplitude,
                            app.haptics[h].duration * 1e3f);

        glBindFramebuffer(GL_FRAMEBUFFER, target.msFbo);
        glViewport(0, 0, target.w, target.h);
        glClearColor(app.clearColor[0], app.clearColor[1], app.clearColor[2], 1.0f);
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
        float tanV = std::tan(0.5f * 70.0f * 3.14159265f / 180.0f), tanH = tanV * static_cast<float>(target.w) / static_cast<float>(target.h);
        Pose head = sim.head();
        app.render(viewFromPose(head), projectionFov(-tanH, tanH, -tanV, tanV, 0.05f, 50.0f), head.p, true);
        target.resolve();

        ++frame;
        if (offscreen) {
            bool last = frame >= frames;
            if (last || (every > 0 && frame % every == 0)) {
                std::vector<uint8_t> px(static_cast<size_t>(target.w) * target.h * 4);
                glBindFramebuffer(GL_READ_FRAMEBUFFER, target.fbo);
                glReadPixels(0, 0, target.w, target.h, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
                std::string path = out;
                if (!last) {
                    char suf[32];
                    std::snprintf(suf, sizeof(suf), "_%04d.png", frame);
                    size_t dot = out.rfind(".png");
                    path = (dot == std::string::npos ? out : out.substr(0, dot)) + suf;
                }
                writePng(path, target.w, target.h, px);
                std::printf("wrote %s (frame %d, scene %s, %zu nodes, latency %.1f ms)\n", path.c_str(), frame,
                            app.scene().loaded ? app.scene().name.c_str() : "-", app.scene().nodes.size(), app.latencyMs());
            }
            if (last) break;
            std::this_thread::sleep_until(std::chrono::steady_clock::time_point(
                std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::duration<double>(t0 + frame * dtFixed))));
        } else {
            glBindFramebuffer(GL_READ_FRAMEBUFFER, target.fbo);
            glBindFramebuffer(GL_DRAW_FRAMEBUFFER, 0);
            glBlitFramebuffer(0, 0, target.w, target.h, 0, 0, target.w, target.h, GL_COLOR_BUFFER_BIT, GL_NEAREST);
            eglSwapBuffers(dpy, surf);
        }
    }
    if (offscreen) {
        const Scene& s = app.scene();
        std::printf("frames %llu, datagrams %llu, bytes %llu, lost %llu, latency %.1f ms\n",
                    static_cast<unsigned long long>(s.statFrames), static_cast<unsigned long long>(s.statDatagrams),
                    static_cast<unsigned long long>(s.statBytes), static_cast<unsigned long long>(s.statLostFrames), app.latencyMs());
    }
    app.shutdown();
    eglMakeCurrent(dpy, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
    eglTerminate(dpy);
    return 0;
}
