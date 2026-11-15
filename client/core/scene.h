// The received scene: meshes, nodes and their current state. Pure data + protocol decoding, no graphics calls.
#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include "mathx.h"
#include "net.h"
#include "protocol.h"

namespace qt {

struct MeshData {
    uint16_t flags = 0;
    std::vector<float> pos;        // 3 per vertex
    std::vector<int8_t> nrm;       // 4 per vertex (xyz * 127), empty without MF_NORMALS
    std::vector<uint8_t> col;      // 4 per vertex, empty without MF_COLORS
    std::vector<uint32_t> idx;
    bool valid = false;
};

struct Node {
    uint8_t kind = 0, parent = 0;
    uint16_t mesh = 0xFFFF, flags = 0;
    uint8_t rgba[4] = {255, 255, 255, 255};
    Pose pose;
    Vec3 scale{1, 1, 1};
    float param = 0;               // TUBE: radius
    std::string name;
    std::vector<float> pts;        // DEFORM: vertex positions, TUBE: polyline points (3 per point)
    bool ptsDirty = true;
};

struct TextItem {
    bool used = false;
    uint8_t anchor = 0;
    uint8_t rgba[4] = {255, 255, 255, 255};
    float size = 0.03f;
    Vec3 pos;
    double expires = 0;            // 0: never
    std::string text;
};

// The server's description of its control panel (M_PANEL): what the client shows in the menu and on the wrist.
struct PanelRow {
    uint8_t style = 0;             // 0 normal, 1 accent, 2 caution, 3 failure; bit 7: also on the wrist card
    std::string label, value;
};
struct PanelAction {
    uint16_t id = 0;
    uint8_t style = 0;             // 0 normal, 1 primary, 2 danger
    uint8_t flags = 0;             // bit 0 enabled, bit 1 hold to confirm, bit 2 active (toggled on)
    std::string label, hint;
};
struct PanelInfo {
    bool valid = false;
    std::string title, subtitle, host, statusText;
    uint8_t statusStyle = 0;       // 0 none, 1 recording, 2 caution, 3 ok
    std::vector<PanelRow> rows;
    std::vector<PanelAction> actions;
    std::vector<std::pair<std::string, std::string>> legend;
};

struct HapticState {
    uint16_t seq = 0;
    float amplitude = 0, duration = 0, frequency = 0;
    bool pending = false;          // set when a new command arrived; the platform layer clears it
};

struct Scene {
    bool loaded = false;           // SCENE_END received
    uint32_t generation = 0;       // bumped by every SCENE_BEGIN: graphics resources of older generations are stale
    uint16_t id = 0;
    int upAxis = 1;
    Vec3 lo, hi, focusCenter, viewDir;
    float focusRadius = 1;
    std::string name;
    std::vector<MeshData> meshes;
    std::vector<Node> nodes;
    TextItem texts[256];
    PanelInfo panel;
    HapticState haptics[2];

    // stream bookkeeping
    uint32_t newestFrame = 0, ackFrame = 0;
    uint32_t fragCount = 0, fragSeen = 0;
    std::vector<bool> fragMask;
    uint32_t echoSeq = 0;
    uint64_t echoClientTimeUs = 0;
    uint32_t echoHoldUs = 0;
    bool newEcho = false;          // a datagram of a new frame arrived since the flag was last cleared
    uint64_t statFrames = 0, statDatagrams = 0, statBytes = 0, statLostFrames = 0;

    // Returns true when the message completed a scene (SCENE_END): the caller answers SCENE_READY.
    bool onMessage(const NetMsg& m, double now);
    void onDatagram(const uint8_t* data, size_t size);
    void clear();
};

}  // namespace qt
