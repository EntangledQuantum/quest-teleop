#include "app.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <fstream>

#include "log.h"

namespace qt {

using namespace theme;

static uint64_t monoUs() {
    using namespace std::chrono;
    return static_cast<uint64_t>(duration_cast<microseconds>(steady_clock::now().time_since_epoch()).count());
}
static Quat yawQuat(float a) { return axisAngle(Vec3(0, 1, 0), a); }
static float horizontalAngle(const Vec3& v) { return std::atan2(-v.x, -v.z); }   // 0 = -z, same convention as yawOf

enum LocalKey { kKeyLarge = 1, kKeyLeft, kKeyPass, kKeyPlace, kKeySmaller, kKeyBigger, kKeyClose, kKeyShadows, kKeyAction0 = 1000 };

// ------------------------------------------------------------------------------------------------- lifecycle
void App::init(const NetConfig& cfg, const std::string& settingsFile) {
    settingsFile_ = settingsFile;
    loadSettings();
    renderer_.init();
    ui_.init();
    net_.start(cfg);
}

void App::shutdown() {
    net_.stop();
    ui_.shutdown();
    renderer_.shutdown();
}

void App::loadSettings() {
    if (settingsFile_.empty()) return;
    std::ifstream f(settingsFile_);
    std::string k;
    int v;
    while (f >> k >> v) {
        if (k == "large_text") largeText = v != 0;
        if (k == "left_handed") leftHanded = v != 0;
        if (k == "passthrough") passthrough = v != 0;
        if (k == "shadows") renderer_.shadowsEnabled = v != 0;
    }
}

void App::saveSettings() {
    if (settingsFile_.empty()) return;
    std::ofstream f(settingsFile_);
    f << "large_text " << (largeText ? 1 : 0) << "\nleft_handed " << (leftHanded ? 1 : 0) << "\npassthrough "
      << (passthrough ? 1 : 0) << "\nshadows " << (renderer_.shadowsEnabled ? 1 : 0) << "\n";
}

void App::localHaptic(int hand, float amplitude, float duration) {
    haptics[hand].fire = true;
    haptics[hand].amplitude = amplitude;
    haptics[hand].duration = duration;
    haptics[hand].frequency = 0;
}

// ------------------------------------------------------------------------------------------------- placement
Quat App::upFix() const {
    if (scene_.upAxis == 2) return axisAngle(Vec3(1, 0, 0), -1.5707963f);   // scene z -> stage y
    if (scene_.upAxis == 0) return axisAngle(Vec3(0, 0, 1), 1.5707963f);    // scene x -> stage y
    return Quat();
}

Quat App::placementQuat() const { return yawQuat(placeYaw) * upFix(); }

// Put the scene's focus region in front of the user at a comfortable height and size.
void App::autoPlace(const Pose& head) {
    float yawHead = yawOf(head.q);
    Vec3 d = rotate(upFix(), scene_.viewDir);
    d.y = 0;
    placeYaw = yawHead - (length(d) > 1e-4f ? horizontalAngle(d) : 0.0f);
    placeScale = std::min(50.0f, std::max(0.02f, 0.30f / std::max(scene_.focusRadius, 1e-3f)));
    Vec3 fwd = rotate(yawQuat(yawHead), Vec3(0, 0, -1));
    Vec3 target = head.p + fwd * 0.55f;
    target.y = head.p.y - 0.40f;
    placePos = target - rotate(placementQuat(), scene_.focusCenter) * placeScale;
    placed_ = true;
    placedFor_ = scene_.name;
    QT_LOG("scene placed: scale %.2f, yaw %.0f deg", placeScale, placeYaw * 57.29578f);
}

void App::zoomAboutFocus(float factor) {
    Vec3 pivot = placePos + rotate(placementQuat(), scene_.focusCenter) * placeScale;
    float ns = std::min(50.0f, std::max(0.02f, placeScale * factor));
    placePos = pivot + (placePos - pivot) * (ns / placeScale);
    placeScale = ns;
}

// Menu open: grab with one grip to drag and turn the scene, with both to also scale it; the sticks nudge it.
void App::editPlacement(const FrameInput& in) {
    const HandInput& L = in.hands[0];
    const HandInput& R = in.hands[1];
    bool gl = L.active && L.gripValid && L.squeeze > 0.6f, gr = R.active && R.gripValid && R.squeeze > 0.6f;
    int mode = (gl ? 1 : 0) | (gr ? 2 : 0);
    auto anchorOf = [&](int m) { return m == 3 ? (L.grip.p + R.grip.p) * 0.5f : (m == 1 ? L.grip.p : R.grip.p); };
    auto angleOf = [&](int m) {
        if (m == 3) return horizontalAngle(R.grip.p - L.grip.p);
        return yawOf(m == 1 ? L.grip.q : R.grip.q);
    };
    if (mode != grabMode_) {                     // (re)anchor whenever the set of grabbing hands changes
        grabMode_ = mode;
        if (mode) {
            grabAnchor_ = anchorOf(mode);
            grabAngle0_ = angleOf(mode);
            grabDist0_ = mode == 3 ? std::max(0.02f, length(R.grip.p - L.grip.p)) : 1.0f;
            grabPos0_ = placePos;
            grabYaw0_ = placeYaw;
            grabScale0_ = placeScale;
        }
    }
    if (mode) {
        Vec3 a = anchorOf(mode);
        float da = angleOf(mode) - grabAngle0_;
        float s = mode == 3 ? std::max(0.02f, length(R.grip.p - L.grip.p)) / grabDist0_ : 1.0f;
        placeYaw = grabYaw0_ + da;
        placeScale = std::min(50.0f, std::max(0.02f, grabScale0_ * s));
        s = placeScale / grabScale0_;
        placePos = a + rotate(yawQuat(da), grabPos0_ - grabAnchor_) * s;
        return;
    }
    float dt = static_cast<float>(in.dt);
    auto dead = [](float v) { return std::fabs(v) < 0.15f ? 0.0f : v; };
    Quat hy = yawQuat(yawOf(in.head.q));
    placePos += rotate(hy, Vec3(dead(L.stickX), 0, -dead(L.stickY))) * (0.6f * dt);
    placePos.y += dead(R.stickY) * 0.4f * dt;
    Vec3 pivot = placePos + rotate(placementQuat(), scene_.focusCenter) * placeScale;
    float dyaw = -dead(R.stickX) * 1.2f * dt;
    placePos = pivot + rotate(yawQuat(dyaw), placePos - pivot);
    placeYaw += dyaw;
}

// ------------------------------------------------------------------------------------------------- network
void App::sendInput(const FrameInput& in) {
    if (net_.state() != NetClient::CONNECTED) return;
    Writer w;
    w.buf.reserve(300);
    w.datagramHeader(D_INPUT, net_.session());
    w.put<uint32_t>(++inputSeq_);
    w.put<uint64_t>(monoUs());
    w.put<uint32_t>(scene_.ackFrame);
    w.put<uint16_t>(scene_.id);
    uint16_t flags = (editMode ? IF_EDIT_MODE : 0) | (in.focused ? IF_FOCUSED : 0) |
                     ((passthrough && in.passthroughAvailable) ? IF_PASSTHROUGH : 0);
    w.put<uint16_t>(flags);
    w.put<uint32_t>(latencyUs_);
    w.vec3(placePos);
    w.quat(placementQuat());
    w.put<float>(placeScale);
    w.vec3(in.head.p);
    w.quat(in.head.q);
    for (int role = 0; role < 2; ++role) {             // left-handed: the roles of the two controllers are swapped
        const HandInput& h = in.hands[phys(role)];
        uint16_t hf = (h.active ? HAND_ACTIVE : 0) | (h.gripValid ? HAND_GRIP_VALID : 0) | (h.aimValid ? HAND_AIM_VALID : 0) |
                      (h.tracked ? HAND_TRACKED : 0);
        w.put<uint16_t>(hf);
        w.put<uint16_t>(h.buttons);
        w.put<float>(h.trigger);
        w.put<float>(h.squeeze);
        w.put<float>(h.stickX);
        w.put<float>(h.stickY);
        w.vec3(h.grip.p);
        w.quat(h.grip.q);
        w.vec3(h.aim.p);
        w.quat(h.aim.q);
        w.vec3(h.velocity);
        w.vec3(h.angularVelocity);
    }
    net_.sendDatagram(w.buf.data(), w.buf.size());
}

void App::update(const FrameInput& in) {
    std::vector<NetMsg> msgs;
    std::vector<std::vector<uint8_t>> dgrams;
    net_.drain(msgs, dgrams);
    for (const NetMsg& m : msgs) {
        if (m.type == M_LOCAL_DISCONNECTED) {
            scene_.clear();
            for (auto& t : scene_.texts) t = TextItem();
            scene_.panel = PanelInfo();
            continue;
        }
        if (scene_.onMessage(m, in.time)) {
            uint16_t sid = scene_.id;
            net_.sendMsg(M_SCENE_READY, reinterpret_cast<const uint8_t*>(&sid), 2);
            if (!placed_ || placedFor_ != scene_.name) {
                autoPlace(in.head);
                hintUntil_ = in.time + 12.0;
            }
        }
    }
    for (const auto& d : dgrams) scene_.onDatagram(d.data(), d.size());
    if (scene_.newEcho) {
        scene_.newEcho = false;
        if (scene_.echoClientTimeUs) {
            uint64_t now = monoUs();
            uint64_t lat = now > scene_.echoClientTimeUs ? now - scene_.echoClientTimeUs : 0;
            latencyUs_ = static_cast<uint32_t>(std::min<uint64_t>(lat, 4000000000ull));
            latencyMs_ = static_cast<float>(lat) / 1000.0f;
            latencyAvg_ += 0.05f * (latencyMs_ - latencyAvg_);
        }
    }

    // haptics asked for by the server (roles), then whatever the UI adds this frame (physical hands)
    for (int h = 0; h < 2; ++h) haptics[h].fire = false;
    for (int role = 0; role < 2; ++role) {
        HapticState& hs = scene_.haptics[role];
        if (hs.pending) {
            HapticCommand& c = haptics[phys(role)];
            c.fire = true;
            c.amplitude = hs.amplitude;
            c.duration = hs.duration;
            c.frequency = hs.frequency;
            hs.pending = false;
        }
    }

    uint16_t pressed[2];
    for (int h = 0; h < 2; ++h) {
        pressed[h] = static_cast<uint16_t>(in.hands[h].buttons & ~prevButtons_[h]);
        prevButtons_[h] = in.hands[h].buttons;
    }
    if (pressed[0] & B_MENU) {                       // the menu button is on the physical left controller
        editMode = !editMode;
        grabMode_ = 0;
        holdKey_ = hoverKey_ = -1;
        if (editMode) {
            float yaw = yawOf(in.head.q);
            menuYaw_ = yaw;
            menuTop_ = in.head.p + rotate(yawQuat(yaw), Vec3(0, 0, -1.05f)) + Vec3(0, -0.06f, 0);
        }
        localHaptic(0, 0.4f, 0.02f);
    }
    if (editMode && scene_.loaded) editPlacement(in);
    sendInput(in);

    parents_[P_SCENE] = trs(placePos, placementQuat(), Vec3(placeScale, placeScale, placeScale));
    parents_[P_STAGE] = Mat4();
    parents_[P_HEAD] = poseMatrix(in.head);
    parents_[P_LEFT_GRIP] = poseMatrix(in.hands[phys(0)].grip);
    parents_[P_RIGHT_GRIP] = poseMatrix(in.hands[phys(1)].grip);
    parents_[P_LEFT_AIM] = poseMatrix(in.hands[phys(0)].aim);
    parents_[P_RIGHT_AIM] = poseMatrix(in.hands[phys(1)].aim);
    renderer_.sync(scene_);
    if (scene_.loaded) {
        Vec3 c = placePos + rotate(placementQuat(), scene_.focusCenter) * placeScale;
        renderer_.renderShadow(scene_, parents_, c, std::min(3.0f, std::max(0.3f, scene_.focusRadius * placeScale * 2.6f)));
    }

    ++frameCount_;
    if (in.time - statTime_ >= 1.0) {
        float dt = static_cast<float>(in.time - statTime_);
        stateHz_ = static_cast<float>(scene_.statFrames - statFrames0_) / dt;
        kbps_ = static_cast<float>(scene_.statBytes - statBytes0_) * 8e-3f / dt;
        lostPerS_ = static_cast<float>(scene_.statLostFrames - statLost0_) / dt;
        fps_ = static_cast<float>(frameCount_) / dt;
        statFrames0_ = scene_.statFrames;
        statBytes0_ = scene_.statBytes;
        statLost0_ = scene_.statLostFrames;
        statTime_ = in.time;
        frameCount_ = 0;
    }
    last_ = in;
    buildUi(in);
    for (int h = 0; h < 2; ++h) prevTrigger_[h] = in.hands[h].trigger;
}

// ------------------------------------------------------------------------------------------------- user interface
// Role tokens in the server's strings, so its legend stays right when the user swaps hands:
// $R / $L = the controller in the right / left role, $Rp $Rs $Lp $Ls = that controller's primary / secondary button.
std::string App::subst(const std::string& s) const {
    std::string o;
    for (size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '$' && i + 1 < s.size() && (s[i + 1] == 'R' || s[i + 1] == 'L')) {
            bool right = (s[i + 1] == 'R') != leftHanded;          // physical side
            char n = i + 2 < s.size() ? s[i + 2] : 0;
            if (n == 'p') { o += right ? "A" : "X"; i += 2; }
            else if (n == 's') { o += right ? "B" : "Y"; i += 2; }
            else { o += right ? "Right" : "Left"; i += 1; }
        } else {
            o += s[i];
        }
    }
    return o;
}

std::string App::linkName() const {
#ifdef __ANDROID__
    if (const_cast<NetClient&>(net_).tcpOnly()) return "USB cable (adb reverse)";
    return "Wi-Fi (UDP)";
#else
    return const_cast<NetClient&>(net_).tcpOnly() ? "TCP tunnel" : "Network (UDP)";
#endif
}

static UiPanel billboard(const Vec3& bottomCenter, const Vec3& head, float w, float h) {
    Vec3 z = normalize(head - bottomCenter);
    Vec3 x = normalize(cross(Vec3(0, 1, 0), z));
    if (length(x) < 0.5f) x = Vec3(1, 0, 0);
    Vec3 y = cross(z, x);
    Vec3 o = bottomCenter - x * (0.5f * w) + y * h;
    UiPanel p;
    p.w = w;
    p.h = h;
    p.m.m[0] = x.x; p.m.m[1] = x.y; p.m.m[2] = x.z;
    p.m.m[4] = y.x; p.m.m[5] = y.y; p.m.m[6] = y.z;
    p.m.m[8] = z.x; p.m.m[9] = z.y; p.m.m[10] = z.z;
    p.m.m[12] = o.x; p.m.m[13] = o.y; p.m.m[14] = o.z;
    return p;
}

// A head-locked panel: (cx, cy) = its centre on a plane `dist` in front of the eyes (x right, y up).
static UiPanel headPanel(const Pose& head, float dist, float cx, float cy, float w, float h) {
    UiPanel p;
    p.w = w;
    p.h = h;
    p.m = trs(head.p + rotate(head.q, Vec3(cx - 0.5f * w, cy + 0.5f * h, -dist)), head.q, Vec3(1, 1, 1));
    return p;
}

static void plate(Ui& ui, const UiPanel& p) {
    ui.rect(p, 0, 0, p.w, p.h, kVoid.with(0.93f));
    ui.frame(p, 0, 0, p.w, p.h, 0.0012f, kRule);
}

// Section head: a small square node, a mono upper-case label, a hairline. Returns the y below it.
float App::section(const UiPanel& p, float x, float y, float w, const std::string& name, float s) {
    float size = 0.0115f * s;
    ui_.rect(p, x, y + 0.0045f * s, 0.006f * s, 0.006f * s, kAccent);
    ui_.text(p, x + 0.013f * s, y, name, size, kMono, kDim, kLeft, 0.06f);
    ui_.rect(p, x, y + 0.021f * s, w, 0.001f, kRule);
    return y + 0.030f * s;
}

// The server's status: a shape and words, never colour alone. Recording = filled disc, caution = outlined square,
// ok = filled square, none = small dim square.
void App::statusBadge(const UiPanel& p, float x, float y, float s, float size) {
    const PanelInfo& pi = scene_.panel;
    float r = 0.42f * size, cy = y + 0.5f * Ui::lineHeight(size);
    Rgba ink = kInk;
    std::string text = pi.valid ? subst(pi.statusText) : std::string("No status");
    if (pi.statusStyle == 1) {
        float pulse = 0.65f + 0.35f * std::sin(static_cast<float>(last_.time) * 4.0f);
        ui_.disc(p, x + r, cy, r, kThermal.with(pulse));
        ink = kThermal;
    } else if (pi.statusStyle == 2) {
        ui_.frame(p, x, cy - r, 2 * r, 2 * r, 0.0025f * s, kStraw);
        ink = kStraw;
    } else if (pi.statusStyle == 3) {
        ui_.rect(p, x, cy - r, 2 * r, 2 * r, kAccent);
    } else {
        ui_.rect(p, x + 0.5f * r, cy - 0.5f * r, r, r, kDim);
        ink = kMuted;
    }
    ui_.text(p, x + 2 * r + 0.5f * size, y, text, size, kDisplay, ink);
}

// One button. Returns true in the frame it is activated. `key` identifies it for hover / hold tracking.
bool App::button(const UiPanel& p, float x, float y, float w, float h, const std::string& label, const std::string& hint,
                 int style, bool enabled, bool confirm, bool active, float s, int key, const FrameInput& in) {
    const Pointer* hov = nullptr;
    for (const Pointer& q : pointers_)
        if (q.on && q.x >= x && q.x <= x + w && q.y >= y && q.y <= y + h) hov = &q;
    if (!enabled) hov = nullptr;
    Rgba ink = kInk, border = kInk.with(0.36f);
    if (style == 1) {                                             // primary: filled
        ui_.rect(p, x, y, w, h, enabled ? kInk : kInk.with(0.25f));
        ink = kVoid;
    } else {
        ui_.rect(p, x, y, w, h, hov ? kInk.with(0.10f) : kSurface.with(0.9f));
        if (style == 2) { border = kThermal; ink = kThermal; }
        if (active) { ui_.rect(p, x, y, w, h, kAccent.with(0.20f)); border = kAccent; }
        if (!enabled) { ink = kDim; border = kInk.with(0.14f); }
        ui_.frame(p, x, y, w, h, 0.0013f, border);
    }
    float ts = 0.019f * s, ty = y + 0.5f * (h - Ui::lineHeight(ts));
    float tx = x + 0.014f * s;
    if (active) {                                                 // "on" is also a shape: a filled node
        ui_.rect(p, tx, y + 0.5f * h - 0.004f * s, 0.008f * s, 0.008f * s, style == 1 ? kVoid : kAccent);
        tx += 0.016f * s;
    }
    std::string hintText = hint;
    if (confirm && hov) hintText = hov->press ? "KEEP HOLDING" : "HOLD THE TRIGGER";      // how to confirm, in the button
    float hintW = hintText.empty() ? 0.0f : ui_.textWidth(hintText, 0.0125f * s, kMono, 0.04f) + 0.014f * s;
    ui_.text(p, tx, ty, ui_.fit(label, x + w - 0.012f * s - hintW - tx, ts, kDisplay), ts, kDisplay, ink);
    if (!hintText.empty()) {
        float hs = 0.0125f * s;
        Rgba hc = style == 1 ? kVoid.with(0.75f) : ((confirm && hov) ? kStraw : kMuted);
        ui_.text(p, x + w - 0.012f * s, y + 0.5f * (h - Ui::lineHeight(hs)), hintText, hs, kMono, hc, kRight, 0.04f);
    }
    bool fired = false;
    if (hov) {
        hoverSeen_ = key;
        ui_.frame(p, x - 0.003f, y - 0.003f, w + 0.006f, h + 0.006f, 0.002f, kAccent);          // focus ring
        if (!confirm) {
            fired = hov->click;
        } else if (hov->press) {
            if (holdKey_ != key) {
                holdKey_ = key;
                holdStart_ = in.time;
                holdFired_ = false;
            }
            float f = std::min(1.0f, static_cast<float>(in.time - holdStart_) / 0.9f);
            ui_.rect(p, x, y + h - 0.006f * s, w * f, 0.006f * s, style == 2 ? kThermal : kAccent);
            if (f >= 1.0f && !holdFired_) {
                holdFired_ = true;
                fired = true;
            }
        } else if (holdKey_ == key) {
            holdKey_ = -1;
        }
        if (fired) localHaptic(hov->hand, 0.7f, 0.03f);
    } else if (holdKey_ == key) {
        holdKey_ = -1;
    }
    return fired;
}

void App::toast(const FrameInput& in, const std::string& text, float dy, float size, const Rgba& ink) {
    std::vector<std::string> lines;
    size_t start = 0;
    float wmax = 0;
    while (true) {
        size_t e = text.find('\n', start);
        lines.push_back(subst(text.substr(start, e == std::string::npos ? std::string::npos : e - start)));
        wmax = std::max(wmax, ui_.textWidth(lines.back(), size, kDisplay));
        if (e == std::string::npos) break;
        start = e + 1;
    }
    float pad = 0.8f * size, lh = Ui::lineHeight(size);
    UiPanel p = headPanel(in.head, 1.0f, 0, dy, wmax + 2 * pad, lh * static_cast<float>(lines.size()) + 2 * pad * 0.7f);
    plate(ui_, p);
    ui_.rect(p, 0, 0, 0.004f, p.h, ink);
    for (size_t i = 0; i < lines.size(); ++i)
        ui_.text(p, 0.5f * p.w, 0.7f * pad + lh * static_cast<float>(i), lines[i], size, kDisplay, ink, kCenter);
}

void App::buildWrist(const FrameInput& in, float s) {
    const HandInput& hd = in.hands[phys(0)];                 // the off hand carries the card
    if (!hd.active || !hd.gripValid) return;
    const PanelInfo& pi = scene_.panel;
    struct R3 { std::string label, value; Rgba ink; };
    std::vector<R3> rows;
    if (pi.valid)
        for (const PanelRow& r : pi.rows)
            if (r.style & 0x80) {
                int st = r.style & 0x7f;
                rows.push_back({subst(r.label), subst(r.value), st == 3 ? kThermal : (st == 2 ? kStraw : (st == 1 ? kAccent : kInk))});
            }
    char buf[160];
    std::snprintf(buf, sizeof(buf), "%s  %.0f ms", net_.tcpOnly() ? "USB" : "NET", latencyAvg_);
    rows.push_back({"LINK", net_.state() == NetClient::CONNECTED ? buf : "not connected", latencyAvg_ > 90 ? kStraw : kInk});
    const std::string hint = "MENU BUTTON: RECORD, SETTINGS";
    const float pad = 0.011f * s, rh = 0.0195f * s, ls = 0.0105f * s, vs = 0.0145f * s, bs = 0.017f * s, hs = 0.0085f * s;
    // the card is as wide as its widest line (within limits); longer values are shortened to fit
    std::string status = pi.valid ? subst(pi.statusText) : std::string("No status");
    float labelW = 0, need = ui_.textWidth(status, bs, kDisplay) + 2.2f * bs;
    need = std::max(need, ui_.textWidth(hint, hs, kMono, 0.04f));
    for (const R3& r : rows) labelW = std::max(labelW, ui_.textWidth(r.label, ls, kMono, 0.04f));
    for (const R3& r : rows) need = std::max(need, labelW + 0.012f * s + ui_.textWidth(r.value, vs, kDisplay));
    const float w = std::min(0.34f * s, std::max(0.17f * s, need + 2 * pad));
    const float h = 2 * pad + 0.028f * s + rh * static_cast<float>(rows.size()) + 0.016f * s;
    UiPanel p = billboard(hd.grip.p + Vec3(0, 0.07f, 0), in.head.p, w, h);
    plate(ui_, p);
    float y = pad;
    {   // status line, shortened if it would not fit
        PanelInfo& mut = scene_.panel;
        std::string keep = mut.statusText;
        if (pi.valid) mut.statusText = ui_.fit(status, w - 2 * pad - 2.2f * bs, bs, kDisplay);
        statusBadge(p, pad, y, s, bs);
        mut.statusText = keep;
    }
    y += 0.028f * s;
    ui_.rect(p, pad, y - 0.004f * s, w - 2 * pad, 0.001f, kRule);
    for (const R3& r : rows) {
        ui_.text(p, pad, y + 0.002f * s, r.label, ls, kMono, kDim, kLeft, 0.04f);
        ui_.text(p, w - pad, y, ui_.fit(r.value, w - 2 * pad - labelW - 0.012f * s, vs, kDisplay), vs, kDisplay, r.ink, kRight);
        y += rh;
    }
    ui_.text(p, pad, y + 0.003f * s, ui_.fit(hint, w - 2 * pad, hs, kMono, 0.04f), hs, kMono, kMuted, kLeft, 0.04f);
}

void App::buildMenu(const FrameInput& in, float s) {
    const PanelInfo& pi = scene_.panel;
    const float pad = 0.026f * s, cw = 0.42f * s, gap = 0.036f * s, rh = 0.0275f * s, bh = 0.048f * s, bgap = 0.010f * s;
    const float ls = 0.013f * s, vs = 0.0165f * s, ds = 0.0150f * s, colGap = 0.016f * s;
    const float W = 2 * pad + 2 * cw + gap;
    char buf[200];

    // ---- contents first (so that columns can be sized to them), then the layout
    struct R3 { std::string label, value; Rgba ink; };
    std::vector<R3> conn, sess;
    NetClient::State st = net_.state();
    // host = "name  address" as the server reports itself (over USB the socket address is only the loopback)
    std::string hostName = pi.valid ? pi.host : "", hostAddr;
    size_t sp = hostName.find("  ");
    if (sp != std::string::npos) {
        hostAddr = hostName.substr(sp + 2);
        hostName = hostName.substr(0, sp);
    }
    conn.push_back({"LINK", st == NetClient::CONNECTED ? linkName() : (st == NetClient::CONNECTING ? "connecting" : "searching"),
                    st == NetClient::CONNECTED ? kInk : kStraw});
    conn.push_back({"COMPUTER", !hostName.empty() ? hostName : "-", kInk});
    conn.push_back({"SERVER", st == NetClient::CONNECTED ? net_.serverName() : "-", kInk});
    conn.push_back({"ADDRESS", st != NetClient::CONNECTED ? "-" : (!hostAddr.empty() ? hostAddr : net_.serverAddress()), kMuted});
    std::snprintf(buf, sizeof(buf), "%.0f ms", latencyAvg_);
    conn.push_back({"LATENCY", buf, latencyAvg_ > 90 ? kStraw : kInk});
    std::snprintf(buf, sizeof(buf), "%.0f fps", fps_);
    conn.push_back({"DISPLAY", buf, kInk});
    std::snprintf(buf, sizeof(buf), "%.0f updates/s", stateHz_);
    conn.push_back({"SIMULATION", buf, kInk});
    std::snprintf(buf, sizeof(buf), "%.0f kbit/s   %.1f lost/s", kbps_, lostPerS_);
    conn.push_back({"TRAFFIC", buf, lostPerS_ > 2 ? kStraw : kInk});
    if (pi.valid)
        for (const PanelRow& r : pi.rows) {
            int rs = r.style & 0x7f;
            sess.push_back({subst(r.label), subst(r.value), rs == 3 ? kThermal : (rs == 2 ? kStraw : (rs == 1 ? kAccent : kInk))});
        }
    std::vector<std::pair<std::string, std::string>> legend;
    if (pi.valid)
        for (const auto& l : pi.legend) legend.emplace_back(subst(l.first), subst(l.second));
    legend.emplace_back("MENU (LEFT)", "close this panel, resume");
    legend.emplace_back("TRIGGER", "press the button you point at");
    legend.emplace_back("GRIP / BOTH GRIPS", "drag, turn / scale the scene");
    legend.emplace_back("STICKS", "slide / turn, raise the scene");
    // label columns as wide as their longest label; a legend whose keys and texts do not fit side by side is stacked
    float labelW = 0, keyW = 0, textW = 0;
    for (const R3& r : conn) labelW = std::max(labelW, ui_.textWidth(r.label, ls, kMono, 0.05f));
    for (const R3& r : sess) labelW = std::max(labelW, ui_.textWidth(r.label, ls, kMono, 0.05f));
    labelW = std::min(labelW, 0.45f * cw);
    for (const auto& l : legend) {
        keyW = std::max(keyW, ui_.textWidth(l.first, ls, kMono, 0.04f));
        textW = std::max(textW, ui_.textWidth(l.second, ds, kBody));
    }
    const bool stacked = keyW + colGap + textW > cw;
    const float legH = stacked ? 0.046f * s : rh;
    size_t nAct = pi.valid ? pi.actions.size() : 0;
    float hLeft = 0.118f * s + 0.036f * s + rh * static_cast<float>(conn.size()) + 0.014f * s + 0.036f * s + rh * static_cast<float>(sess.size());
    float hRight = 0.036f * s + (bh + bgap) * static_cast<float>(nAct) + 0.012f * s + 0.036f * s + legH * static_cast<float>(legend.size()) +
                   0.014f * s + 0.036f * s + (bh * 0.85f + bgap) * 4;
    const float H = 2 * pad + std::max(hLeft, hRight) + 0.022f * s;
    Quat q = yawQuat(menuYaw_);
    menu_.w = W;
    menu_.h = H;
    menu_.m = trs(menuTop_ + rotate(q, Vec3(-0.5f * W, 0.5f * H, 0)), q, Vec3(1, 1, 1));    // menuTop_ = its centre

    // pointers: a ray from each controller; the trigger presses
    for (int h = 0; h < 2; ++h) {
        Pointer& pt = pointers_[h];
        const HandInput& hd = in.hands[h];
        pt = Pointer();
        pt.hand = h;
        if (!hd.active || !hd.aimValid) continue;
        Vec3 dir = rotate(hd.aim.q, Vec3(0, 0, -1));
        float x, y, dist;
        if (!menu_.hit(hd.aim.p, dir, x, y, dist) || x < -0.02f || y < -0.02f || x > W + 0.02f || y > H + 0.02f || dist > 6.0f) continue;
        pt.on = true;
        pt.x = x;
        pt.y = y;
        pt.from = hd.aim.p;
        pt.to = hd.aim.p + dir * dist;
        pt.press = hd.trigger > 0.55f;
        pt.click = hd.trigger > 0.55f && prevTrigger_[h] <= 0.55f;
    }
    if (debugPointer) {
        Pointer& pt = pointers_[1];
        static bool prevPress = false;
        pt.on = true;
        pt.x = debugX;
        pt.y = debugY;
        pt.press = debugPress;
        pt.click = debugPress && !prevPress;
        prevPress = debugPress;
        pt.from = pt.to = transformPoint(menu_.m, Vec3(pt.x, -pt.y, 0));
    }

    plate(ui_, menu_);
    ui_.rect(menu_, 0, 0, W, 0.003f, kAccent);
    float x = pad, y = pad;
    // ---- left column: what am I connected to, what is running
    std::string title = pi.valid && !pi.title.empty() ? subst(pi.title) : (scene_.loaded ? scene_.name : "Quest Teleop");
    ui_.text(menu_, x, y, ui_.fit(title, cw, 0.036f * s, kDisplay), 0.036f * s, kDisplay, kInk);
    y += 0.046f * s;
    ui_.text(menu_, x, y, ui_.fit(pi.valid ? subst(pi.subtitle) : "", cw, 0.0165f * s, kBody), 0.0165f * s, kBody, kMuted);
    y += 0.032f * s;
    {
        PanelInfo& mut = scene_.panel;
        std::string keep = mut.statusText;
        if (pi.valid) mut.statusText = ui_.fit(subst(keep), cw - 2.2f * 0.024f * s, 0.024f * s, kDisplay);
        statusBadge(menu_, x, y, s, 0.024f * s);
        mut.statusText = keep;
    }
    y += 0.040f * s;
    auto row = [&](const R3& r) {
        ui_.text(menu_, x, y + 0.003f * s, ui_.fit(r.label, labelW, ls, kMono, 0.05f), ls, kMono, kDim, kLeft, 0.05f);
        float vx = x + labelW + colGap;
        ui_.text(menu_, vx, y, ui_.fit(r.value, x + cw - vx, vs, kDisplay), vs, kDisplay, r.ink);
        ui_.rect(menu_, x, y + rh - 0.002f * s, cw, 0.0008f, kRuleSoft);
        y += rh;
    };
    y = section(menu_, x, y, cw, "CONNECTION", s);
    for (const R3& r : conn) row(r);
    y += 0.014f * s;
    y = section(menu_, x, y, cw, "SESSION", s);
    for (const R3& r : sess) row(r);

    // ---- right column: what can I do
    x = pad + cw + gap;
    y = pad;
    y = section(menu_, x, y, cw, "ACTIONS", s);
    if (pi.valid)
        for (size_t i = 0; i < pi.actions.size(); ++i) {
            const PanelAction& a = pi.actions[i];
            if (debugAction == static_cast<int>(i)) { debugX = x + 0.5f * cw; debugY = y + 0.5f * bh; }
            if (button(menu_, x, y, cw, bh, subst(a.label), subst(a.hint), a.style, a.flags & 1, a.flags & 2, a.flags & 4, s,
                       kKeyAction0 + static_cast<int>(i), in))
                net_.sendMsg(M_ACTION, reinterpret_cast<const uint8_t*>(&a.id), 2);
            y += bh + bgap;
        }
    y += 0.012f * s;
    y = section(menu_, x, y, cw, "CONTROLS", s);
    for (const auto& l : legend) {
        if (stacked) {
            ui_.text(menu_, x, y, ui_.fit(l.first, cw, ls, kMono, 0.04f), ls, kMono, kInk, kLeft, 0.04f);
            ui_.text(menu_, x + 0.012f * s, y + 0.019f * s, ui_.fit(l.second, cw - 0.012f * s, ds, kBody), ds, kBody, kMuted);
        } else {
            ui_.text(menu_, x, y + 0.003f * s, l.first, ls, kMono, kInk, kLeft, 0.04f);
            ui_.text(menu_, x + keyW + colGap, y, l.second, ds, kBody, kMuted);
        }
        y += legH;
    }
    y += 0.014f * s;
    y = section(menu_, x, y, cw, "SETTINGS", s);
    float hw = 0.5f * (cw - bgap), sb = bh * 0.85f;
    if (button(menu_, x, y, hw, sb, "Larger text", largeText ? "ON" : "OFF", 0, true, false, largeText, s, kKeyLarge, in)) {
        largeText = !largeText;
        saveSettings();
    }
    if (button(menu_, x + hw + bgap, y, hw, sb, "Left-handed", leftHanded ? "ON" : "OFF", 0, true, false, leftHanded, s, kKeyLeft, in)) {
        leftHanded = !leftHanded;
        saveSettings();
    }
    y += sb + bgap;
    if (button(menu_, x, y, hw, sb, "Passthrough", !in.passthroughAvailable ? "N/A" : (passthrough ? "ON" : "OFF"), 0,
               in.passthroughAvailable, false, passthrough && in.passthroughAvailable, s, kKeyPass, in)) {
        passthrough = !passthrough;
        saveSettings();
    }
    if (button(menu_, x + hw + bgap, y, hw, sb, "Shadows", renderer_.shadowsEnabled ? "ON" : "OFF", 0, true, false,
               renderer_.shadowsEnabled, s, kKeyShadows, in)) {
        renderer_.shadowsEnabled = !renderer_.shadowsEnabled;
        saveSettings();
    }
    y += sb + bgap;
    if (button(menu_, x, y, hw, sb, "Scene smaller", "-", 0, scene_.loaded, false, false, s, kKeySmaller, in)) zoomAboutFocus(0.8f);
    if (button(menu_, x + hw + bgap, y, hw, sb, "Scene bigger", "+", 0, scene_.loaded, false, false, s, kKeyBigger, in)) zoomAboutFocus(1.25f);
    y += sb + bgap;
    if (button(menu_, x, y, hw, sb, "Reset placement", "", 0, scene_.loaded, false, false, s, kKeyPlace, in)) autoPlace(in.head);
    if (button(menu_, x + hw + bgap, y, hw, sb, "Close menu", "MENU", 1, true, false, false, s, kKeyClose, in)) {
        editMode = false;
        grabMode_ = 0;
    }
    std::snprintf(buf, sizeof(buf), "SCENE SCALE %.2f", placeScale);
    ui_.text(menu_, pad, H - pad - 0.008f * s, buf, 0.011f * s, kMono, kDim, kLeft, 0.05f);

    for (const Pointer& pt : pointers_)
        if (pt.on) {
            ui_.disc(menu_, pt.x, pt.y, 0.0075f, kVoid);
            ui_.disc(menu_, pt.x, pt.y, 0.0055f, pt.press ? kAccent : kInk);
        }
}

void App::buildUi(const FrameInput& in) {
    ui_.begin();
    hoverSeen_ = -1;
    for (Pointer& p : pointers_) p = Pointer();
    const float s = largeText ? 1.6f : 1.2f;      // text sizes are chosen for VR: nothing below about 0.8 degrees
    NetClient::State st = net_.state();
    const PanelInfo& pi = scene_.panel;

    if (!editMode && (st != NetClient::CONNECTED || !scene_.loaded)) {
        UiPanel p = headPanel(in.head, 1.1f, 0, 0, 0.62f * s, 0.215f * s);
        plate(ui_, p);
        ui_.rect(p, 0, 0, p.w, 0.003f, kAccent);
        float x = 0.03f * s, y = 0.026f * s;
        ui_.text(p, x, y, "Quest Teleop", 0.040f * s, kDisplay, kInk);
        y += 0.058f * s;
        std::string l1, l2;
        if (st == NetClient::SEARCHING) {
            l1 = "Looking for a server on this network";
            l2 = "Start the server on the computer. Same Wi-Fi, or the USB cable with adb reverse.";
            ui_.frame(p, x, y + 0.006f * s, 0.014f * s, 0.014f * s, 0.0025f * s, kStraw);
        } else if (st == NetClient::CONNECTING) {
            l1 = "Connecting to " + net_.serverAddress();
            ui_.frame(p, x, y + 0.006f * s, 0.014f * s, 0.014f * s, 0.0025f * s, kStraw);
        } else {
            l1 = "Connected to " + net_.serverName();
            l2 = "Waiting for the scene.";
            ui_.rect(p, x, y + 0.006f * s, 0.014f * s, 0.014f * s, kAccent);
        }
        ui_.text(p, x + 0.026f * s, y, l1, 0.022f * s, kDisplay, kInk);
        y += 0.036f * s;
        ui_.text(p, x, y, l2, 0.0145f * s, kBody, kMuted);
        y += 0.034f * s;
        ui_.text(p, x, y, "LEFT MENU BUTTON: PANEL AND SETTINGS", 0.013f * s, kMono, kDim, kLeft, 0.05f);
    }

    // the menu shows everything the wrist card does, so the card is hidden while the menu is open
    if (editMode) buildMenu(in, s);
    else buildWrist(in, s * 0.85f);

    // recording is also shown head-locked, so it is seen wherever the user looks
    bool lookingAtWrist = false;
    {
        const HandInput& off = in.hands[phys(0)];
        if (off.active && off.gripValid) {
            Vec3 d = normalize(off.grip.p + Vec3(0, 0.12f, 0) - in.head.p);
            lookingAtWrist = dot(d, rotate(in.head.q, Vec3(0, 0, -1))) > 0.78f;          // within about 39 degrees
        }
    }
    if (!editMode && pi.valid && pi.statusStyle == 1 && !lookingAtWrist) {
        float size = 0.020f * s;
        float w = ui_.textWidth(subst(pi.statusText), size, kDisplay) + 2.9f * size;
        UiPanel p = headPanel(in.head, 1.0f, -0.20f, 0.20f, w, Ui::lineHeight(size) + 0.012f * s);
        plate(ui_, p);
        statusBadge(p, 0.6f * size, 0.006f * s, s, size);
    }
    if (!editMode && scene_.loaded && in.time < hintUntil_)
        toast(in, "Left MENU button: panel, recording, settings", -0.30f, 0.018f * s, kMuted);

    // the server's texts: head-locked ones as toasts, the others as cards at their anchor
    float hudY = 0.24f;
    for (TextItem& t : scene_.texts) {
        if (!t.used) continue;
        if (t.expires > 0 && in.time > t.expires) { t.used = false; continue; }
        Rgba ink{t.rgba[0] / 255.0f, t.rgba[1] / 255.0f, t.rgba[2] / 255.0f, 1.0f};
        float size = std::max(0.014f, t.size) * s;
        if (t.anchor == 0) {
            toast(in, t.text, t.pos.y != 0 ? t.pos.y : hudY, size, ink);
            hudY -= 0.07f;
            continue;
        }
        int pa = t.anchor - 1;
        if (pa > 6 || (pa == P_SCENE && !scene_.loaded)) continue;
        std::vector<std::string> lines;
        size_t start = 0;
        float wmax = 0;
        std::string txt = subst(t.text);
        while (true) {
            size_t e = txt.find('\n', start);
            lines.push_back(txt.substr(start, e == std::string::npos ? std::string::npos : e - start));
            wmax = std::max(wmax, ui_.textWidth(lines.back(), size, kDisplay));
            if (e == std::string::npos) break;
            start = e + 1;
        }
        float pad = 0.6f * size, lh = Ui::lineHeight(size);
        Vec3 w = transformPoint(parents_[pa], t.pos) + Vec3(0, 0.05f, 0);
        UiPanel p = billboard(w, in.head.p, wmax + 2 * pad, lh * static_cast<float>(lines.size()) + 1.2f * pad);
        plate(ui_, p);
        ui_.rect(p, 0, 0, 0.003f, p.h, ink);
        for (size_t i = 0; i < lines.size(); ++i) ui_.text(p, pad, 0.6f * pad + lh * static_cast<float>(i), lines[i], size, kDisplay, ink);
    }

    if (hoverSeen_ != hoverKey_) {                    // a light tick when the pointer lands on a button
        if (hoverSeen_ >= 0)
            for (const Pointer& p : pointers_)
                if (p.on) localHaptic(p.hand, 0.18f, 0.012f);
        hoverKey_ = hoverSeen_;
    }
    if (hoverSeen_ < 0) holdKey_ = -1;
}

void App::render(const Mat4& view, const Mat4& proj, const Vec3& eye, bool gammaOut) {
    DrawView v;
    v.view = view;
    v.proj = proj;
    v.eye = eye;
    v.gammaOut = gammaOut;
    bool seeRoom = passthrough && last_.passthroughAvailable;
    if (!seeRoom) renderer_.drawGrid(v, 0.35f);
    renderer_.drawScene(scene_, v, parents_);
    for (int h = 0; h < 2; ++h) {
        const HandInput& hd = last_.hands[h];
        if (!hd.active || !hd.gripValid) continue;
        // the controller in the left role is violet, the right one straw; in the menu both are ink
        int role = leftHanded ? 1 - h : h;
        const float colL[4] = {kAccent.r, kAccent.g, kAccent.b, 1.0f}, colR[4] = {kStraw.r, kStraw.g, kStraw.b, 1.0f},
                    colE[4] = {kInk.r, kInk.g, kInk.b, 1.0f};
        const float* c = editMode ? colE : (role == 0 ? colL : colR);
        renderer_.drawSphere(v, trs(hd.grip.p, hd.grip.q, Vec3(0.011f, 0.011f, 0.011f)), c);
        if (hd.aimValid)
            renderer_.drawBox(v, poseMatrix(hd.aim) * trs(Vec3(0, 0, -0.045f), Quat(), Vec3(0.0022f, 0.0022f, 0.035f)), c);
    }
    if (editMode)
        for (const Pointer& p : pointers_) {
            if (!p.on || debugPointer) continue;
            Vec3 d = p.to - p.from;
            float len = length(d);
            if (len < 1e-3f) continue;
            // a thin beam from the controller to the panel
            Vec3 z = d * (1.0f / len), x = normalize(cross(std::fabs(z.y) < 0.9f ? Vec3(0, 1, 0) : Vec3(1, 0, 0), z)), y = cross(z, x);
            Mat4 m;
            const float t = 0.0012f;
            m.m[0] = x.x * t; m.m[1] = x.y * t; m.m[2] = x.z * t;
            m.m[4] = y.x * t; m.m[5] = y.y * t; m.m[6] = y.z * t;
            m.m[8] = z.x * len * 0.5f; m.m[9] = z.y * len * 0.5f; m.m[10] = z.z * len * 0.5f;
            Vec3 c = (p.from + p.to) * 0.5f;
            m.m[12] = c.x; m.m[13] = c.y; m.m[14] = c.z;
            const float col[4] = {kAccent.r, kAccent.g, kAccent.b, 1.0f};
            renderer_.drawBox(v, m, col, true);
        }
    ui_.draw(proj * view, gammaOut);
}

}  // namespace qt
