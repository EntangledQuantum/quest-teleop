// Wire format of quest-teleop, protocol version 1. See docs/PROTOCOL.md. Little-endian hosts only (arm64, x86-64).
#pragma once
#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "mathx.h"

namespace qt {

constexpr uint16_t kMagic = 0x5451;
constexpr uint8_t kVersion = 1;
constexpr int kDefaultPort = 47800;
constexpr int kBeaconPort = 47801;
constexpr int kMaxDatagram = 1200;

enum : uint8_t { D_DISCOVER = 1, D_OFFER = 2, D_INPUT = 3, D_STATE = 4 };
enum : uint16_t {
    M_HELLO = 1, M_WELCOME, M_SCENE_BEGIN, M_MESH, M_NODE, M_SCENE_END, M_SCENE_READY, M_TEXT, M_DGRAM, M_PING, M_PONG,
    M_BYE, M_PANEL, M_ACTION,
    M_LOCAL_DISCONNECTED = 0xFFFF,   // synthesised by the network thread
};
enum : uint8_t { C_POSE = 1, C_VERTS = 2, C_APPEARANCE = 3 };
enum : uint8_t { K_MESH = 1, K_DEFORM = 2, K_TUBE = 3 };
enum : uint8_t { P_SCENE = 0, P_STAGE, P_HEAD, P_LEFT_GRIP, P_RIGHT_GRIP, P_LEFT_AIM, P_RIGHT_AIM };
enum : uint16_t { F_VISIBLE = 1, F_BLEND = 2, F_UNLIT = 4, F_ON_TOP = 8, F_FLAT = 16, F_HIGHLIGHT = 32 };
enum : uint16_t { MF_NORMALS = 1, MF_COLORS = 2, MF_INDEX32 = 4, MF_DOUBLE_SIDED = 8 };
enum : uint16_t { HF_TCP_ONLY = 1 };
enum : uint16_t { IF_EDIT_MODE = 1, IF_FOCUSED = 2, IF_PASSTHROUGH = 4 };
enum : uint16_t { HAND_ACTIVE = 1, HAND_GRIP_VALID = 2, HAND_AIM_VALID = 4, HAND_TRACKED = 8 };
enum : uint16_t {
    B_PRIMARY = 1, B_SECONDARY = 2, B_STICK = 4, B_MENU = 8, B_TRIGGER_TOUCH = 16, B_STICK_TOUCH = 32,
    B_PRIMARY_TOUCH = 64, B_SECONDARY_TOUCH = 128, B_THUMBREST_TOUCH = 256,
};

constexpr int kDgramHeader = 8;
constexpr int kStateHeader = 32;
constexpr int kHapticSize = 8;
constexpr int kStateOverhead = kDgramHeader + kStateHeader + 2 * kHapticSize;
constexpr float kLocalBound = 2.0f;                 // pose range of nodes that are not in scene space [m]
constexpr float kQuatScale = 32767.0f * 1.41421356237f;

// Bounds-checked reader over a byte buffer. After a failed read `ok` is false and reads return zero.
struct Reader {
    const uint8_t* p;
    size_t n;
    size_t off = 0;
    bool ok = true;
    Reader(const uint8_t* data, size_t size) : p(data), n(size) {}
    template <typename T> T get() {
        T v{};
        if (off + sizeof(T) > n) { ok = false; off = n; return v; }
        std::memcpy(&v, p + off, sizeof(T));
        off += sizeof(T);
        return v;
    }
    const uint8_t* bytes(size_t count) {
        if (off + count > n) { ok = false; off = n; return nullptr; }
        const uint8_t* r = p + off;
        off += count;
        return r;
    }
    std::string str() {
        uint16_t len = get<uint16_t>();
        const uint8_t* b = bytes(len);
        return b ? std::string(reinterpret_cast<const char*>(b), len) : std::string();
    }
    Vec3 vec3() { float x = get<float>(), y = get<float>(), z = get<float>(); return {x, y, z}; }
    Quat quat() { float x = get<float>(), y = get<float>(), z = get<float>(), w = get<float>(); return {x, y, z, w}; }
    size_t left() const { return n - off; }
};

struct Writer {
    std::vector<uint8_t> buf;
    template <typename T> void put(T v) {
        size_t o = buf.size();
        buf.resize(o + sizeof(T));
        std::memcpy(buf.data() + o, &v, sizeof(T));
    }
    void str(const std::string& s) {
        put<uint16_t>(static_cast<uint16_t>(s.size()));
        buf.insert(buf.end(), s.begin(), s.end());
    }
    void vec3(const Vec3& v) { put(v.x); put(v.y); put(v.z); }
    void quat(const Quat& q) { put(q.x); put(q.y); put(q.z); put(q.w); }
    void datagramHeader(uint8_t type, uint32_t session) { put(kMagic); put(kVersion); put(type); put(session); }
};

inline Quat dequantizeQuat(int16_t a, int16_t b, int16_t c, int16_t dropped) {
    float v[3] = {a / kQuatScale, b / kQuatScale, c / kQuatScale};
    float q[4];
    int k = dropped & 3, j = 0;
    for (int i = 0; i < 4; ++i)
        if (i != k) q[i] = v[j++];
    float s = 1.0f - v[0] * v[0] - v[1] * v[1] - v[2] * v[2];
    q[k] = s > 0 ? std::sqrt(s) : 0.0f;
    return {q[0], q[1], q[2], q[3]};
}

}  // namespace qt
