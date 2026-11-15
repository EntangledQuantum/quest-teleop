// Small vector / quaternion / matrix helpers. Column-major 4x4 matrices (OpenGL convention), quaternions xyzw.
#pragma once
#include <cmath>
#include <cstring>

namespace qt {

struct Vec3 {
    float x = 0, y = 0, z = 0;
    Vec3() = default;
    Vec3(float x_, float y_, float z_) : x(x_), y(y_), z(z_) {}
    Vec3 operator+(const Vec3& o) const { return {x + o.x, y + o.y, z + o.z}; }
    Vec3 operator-(const Vec3& o) const { return {x - o.x, y - o.y, z - o.z}; }
    Vec3 operator*(float s) const { return {x * s, y * s, z * s}; }
    Vec3 operator-() const { return {-x, -y, -z}; }
    Vec3& operator+=(const Vec3& o) { x += o.x; y += o.y; z += o.z; return *this; }
};
inline float dot(const Vec3& a, const Vec3& b) { return a.x * b.x + a.y * b.y + a.z * b.z; }
inline Vec3 cross(const Vec3& a, const Vec3& b) { return {a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x}; }
inline float length(const Vec3& a) { return std::sqrt(dot(a, a)); }
inline Vec3 normalize(const Vec3& a) { float n = length(a); return n > 1e-12f ? a * (1.0f / n) : Vec3(0, 0, 0); }

struct Quat {
    float x = 0, y = 0, z = 0, w = 1;
    Quat() = default;
    Quat(float x_, float y_, float z_, float w_) : x(x_), y(y_), z(z_), w(w_) {}
};
inline Quat operator*(const Quat& a, const Quat& b) {
    return {a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y, a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x,
            a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w, a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z};
}
inline Quat conj(const Quat& q) { return {-q.x, -q.y, -q.z, q.w}; }
inline Quat normalize(const Quat& q) {
    float n = std::sqrt(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w);
    return n > 1e-12f ? Quat(q.x / n, q.y / n, q.z / n, q.w / n) : Quat();
}
inline Vec3 rotate(const Quat& q, const Vec3& v) {
    Vec3 u(q.x, q.y, q.z);
    Vec3 t = cross(u, v) * 2.0f;
    return v + t * q.w + cross(u, t);
}
inline Quat axisAngle(const Vec3& axis, float angle) {
    Vec3 a = normalize(axis);
    float s = std::sin(0.5f * angle);
    return {a.x * s, a.y * s, a.z * s, std::cos(0.5f * angle)};
}
// Heading of a pose about +y (0 = looking along -z), from its forward (-z) axis projected on the floor.
inline float yawOf(const Quat& q) {
    Vec3 f = rotate(q, Vec3(0, 0, -1));
    return std::atan2(-f.x, -f.z);
}

struct Pose {
    Vec3 p;
    Quat q;
};
inline Pose operator*(const Pose& a, const Pose& b) { return {a.p + rotate(a.q, b.p), a.q * b.q}; }
inline Pose inverse(const Pose& a) { Quat qi = conj(a.q); return {rotate(qi, -a.p), qi}; }

struct Mat4 {
    float m[16];
    Mat4() { std::memset(m, 0, sizeof(m)); m[0] = m[5] = m[10] = m[15] = 1.0f; }
    float& at(int row, int col) { return m[col * 4 + row]; }
    float at(int row, int col) const { return m[col * 4 + row]; }
};
inline Mat4 operator*(const Mat4& a, const Mat4& b) {
    Mat4 r;
    for (int c = 0; c < 4; ++c)
        for (int rw = 0; rw < 4; ++rw) {
            float s = 0;
            for (int k = 0; k < 4; ++k) s += a.m[k * 4 + rw] * b.m[c * 4 + k];
            r.m[c * 4 + rw] = s;
        }
    return r;
}
inline Mat4 trs(const Vec3& p, const Quat& q, const Vec3& s) {
    Mat4 r;
    Vec3 x = rotate(q, Vec3(1, 0, 0)) * s.x, y = rotate(q, Vec3(0, 1, 0)) * s.y, z = rotate(q, Vec3(0, 0, 1)) * s.z;
    r.m[0] = x.x; r.m[1] = x.y; r.m[2] = x.z;
    r.m[4] = y.x; r.m[5] = y.y; r.m[6] = y.z;
    r.m[8] = z.x; r.m[9] = z.y; r.m[10] = z.z;
    r.m[12] = p.x; r.m[13] = p.y; r.m[14] = p.z;
    return r;
}
inline Mat4 poseMatrix(const Pose& p) { return trs(p.p, p.q, Vec3(1, 1, 1)); }
inline Mat4 viewFromPose(const Pose& eye) { return poseMatrix(inverse(eye)); }
inline Vec3 transformPoint(const Mat4& m, const Vec3& v) {
    return {m.m[0] * v.x + m.m[4] * v.y + m.m[8] * v.z + m.m[12], m.m[1] * v.x + m.m[5] * v.y + m.m[9] * v.z + m.m[13],
            m.m[2] * v.x + m.m[6] * v.y + m.m[10] * v.z + m.m[14]};
}
// Projection from the tangents of the four half-angles (OpenXR XrFovf: left and down are negative).
inline Mat4 projectionFov(float tanL, float tanR, float tanD, float tanU, float zn, float zf) {
    Mat4 r;
    std::memset(r.m, 0, sizeof(r.m));
    r.m[0] = 2.0f / (tanR - tanL);
    r.m[5] = 2.0f / (tanU - tanD);
    r.m[8] = (tanR + tanL) / (tanR - tanL);
    r.m[9] = (tanU + tanD) / (tanU - tanD);
    r.m[10] = -(zf + zn) / (zf - zn);
    r.m[11] = -1.0f;
    r.m[14] = -2.0f * zf * zn / (zf - zn);
    return r;
}

}  // namespace qt
