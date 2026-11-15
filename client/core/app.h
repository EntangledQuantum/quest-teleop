// The platform-independent application: network + scene + renderer + user interface.
// A platform layer (client/quest: OpenXR, client/desktop: X11 window or off-screen) feeds it tracking data and
// buttons once per frame, asks it to draw each eye, and plays the haptics it reports.
//
// User interface (all of it works with the controllers' buttons, seated, and never relies on colour alone):
//   * wrist card on the off hand: recording / status badge, the server's pinned values, link and latency;
//   * a head-locked REC badge while the server reports "recording";
//   * the menu (left MENU button): a panel in front of the user with connection, session, the server's actions as
//     large buttons (point with either controller, trigger to press; dangerous ones need a hold), the control
//     legend, and settings (larger text, left-handed, passthrough, scene size, placement). While the menu is open
//     the grips move / turn / scale the scene and the server is told to pause the robot.
#pragma once
#include <string>
#include <vector>

#include "mathx.h"
#include "net.h"
#include "renderer.h"
#include "scene.h"
#include "ui.h"

namespace qt {

struct HandInput {
    bool active = false, gripValid = false, aimValid = false, tracked = false;
    Pose grip, aim;                       // stage space
    Vec3 velocity, angularVelocity;
    float trigger = 0, squeeze = 0, stickX = 0, stickY = 0;
    uint16_t buttons = 0;                 // protocol B_* bits
};

struct FrameInput {
    double time = 0;                      // seconds, monotonic
    double dt = 1.0 / 72.0;
    Pose head;                            // stage space
    HandInput hands[2];                   // physical: 0 left, 1 right
    bool focused = true;
    bool passthroughAvailable = false;
};

struct HapticCommand {
    bool fire = false;
    float amplitude = 0, duration = 0, frequency = 0;
};

class App {
public:
    void init(const NetConfig& net, const std::string& settingsFile = "");   // needs a current GL context
    void shutdown();
    // Connection on / off. The platform turns it off while the app cannot be seen (headset off, Quest menu in
    // front, app in the background): the session is closed with BYE, so the server never keeps a client that
    // renders nothing and sends no input, and nothing piles up unread. Back in front, the app reconnects.
    void setOnline(bool on);
    bool online() const { return online_; }

    // Once per frame: receive, apply, run the UI, send the controller sample, render the shadow map.
    // Leaves an unspecified framebuffer bound: bind the eye target afterwards.
    void update(const FrameInput& in);
    // Once per eye, with the target framebuffer bound and the viewport set.
    void render(const Mat4& view, const Mat4& proj, const Vec3& eye, bool gammaOut);

    HapticCommand haptics[2];             // physical hands; the platform plays those with fire == true
    bool passthrough = true;              // setting; the platform applies it if it can
    bool largeText = false, leftHanded = false;
    bool editMode = false;                // the menu is open

    // Placement of the scene in the stage: p_stage = pos + scale * rotate(yaw about +y, upFix * p_scene).
    Vec3 placePos{0, 1.0f, -0.5f};
    float placeYaw = 0, placeScale = 1;
    Quat placementQuat() const;
    void zoomAboutFocus(float factor);    // scale the scene about its focus point
    float latencyMs() const { return latencyMs_; }
    const Scene& scene() const { return scene_; }
    NetClient& net() { return net_; }
    Renderer& renderer() { return renderer_; }
    float clearColor[4] = {0.024f, 0.027f, 0.035f, 1.0f};

    // Test hook (desktop client): point at panel coordinates instead of using the controllers' rays.
    bool debugPointer = false, debugPress = false;
    float debugX = 0, debugY = 0;
    int debugAction = -1;                 // >= 0: aim at this action button of the menu

private:
    struct Pointer {
        bool on = false, press = false, click = false;
        float x = 0, y = 0;
        Vec3 from, to;
        int hand = 0;
    };
    void autoPlace(const Pose& head);
    void editPlacement(const FrameInput& in);
    void sendInput(const FrameInput& in);
    void buildUi(const FrameInput& in);
    void buildMenu(const FrameInput& in, float s);
    void buildWrist(const FrameInput& in, float s);
    bool button(const UiPanel& p, float x, float y, float w, float h, const std::string& label, const std::string& hint,
                int style, bool enabled, bool confirm, bool active, float s, int key, const FrameInput& in);
    float section(const UiPanel& p, float x, float y, float w, const std::string& name, float s);
    void statusBadge(const UiPanel& p, float x, float y, float s, float size);
    void toast(const FrameInput& in, const std::string& text, float dy, float size, const Rgba& ink);
    std::string subst(const std::string& s) const;
    std::string linkName() const;
    void loadSettings();
    void saveSettings();
    Quat upFix() const;
    int phys(int role) const { return leftHanded ? 1 - role : role; }
    void localHaptic(int hand, float amplitude, float duration);

    NetClient net_;
    NetConfig netCfg_;
    bool online_ = false;
    Scene scene_;
    Renderer renderer_;
    Ui ui_;
    FrameInput last_;
    Mat4 parents_[7];
    std::string settingsFile_;
    bool placed_ = false;
    std::string placedFor_;
    uint32_t inputSeq_ = 0;
    float latencyMs_ = 0, latencyAvg_ = 0;
    uint32_t latencyUs_ = 0;
    uint16_t prevButtons_[2] = {0, 0};
    float prevTrigger_[2] = {0, 0};
    double hintUntil_ = 0;
    // menu
    UiPanel menu_;
    Vec3 menuTop_;                        // top centre of the menu panel (stage), set when the menu opens
    float menuYaw_ = 0;
    Pointer pointers_[2];
    int hoverKey_ = -1, holdKey_ = -1;
    double holdStart_ = 0;
    bool holdFired_ = false;
    int hoverSeen_ = -1;
    // grab state for placing the scene
    int grabMode_ = 0;                    // 0 none, 1 left, 2 right, 3 both
    Vec3 grabAnchor_, grabPos0_;
    float grabAngle0_ = 0, grabDist0_ = 1, grabYaw0_ = 0, grabScale0_ = 1;
    // statistics
    double statTime_ = 0;
    uint64_t statFrames0_ = 0, statBytes0_ = 0, statLost0_ = 0;
    float stateHz_ = 0, kbps_ = 0, fps_ = 0, lostPerS_ = 0;
    int frameCount_ = 0;
};

}  // namespace qt
