// Immediate-mode 2D drawing on panels placed in 3D: filled rectangles, discs and text in three faces.
// Visual language: dark, square corners, hairline rules, a small square "node" in
// front of items, mono upper-case labels, display-face titles.
#pragma once
#include <GLES3/gl3.h>
#include <string>
#include <vector>

#include "mathx.h"

namespace qt {

struct Rgba {
    float r, g, b, a;
    Rgba with(float alpha) const { return {r, g, b, alpha}; }
};

namespace theme {                                   // colour tokens (sRGB)
constexpr Rgba kVoid{0.024f, 0.027f, 0.035f, 1.0f};        // #060709
constexpr Rgba kSurface{0.047f, 0.051f, 0.067f, 1.0f};     // #0c0d11
constexpr Rgba kInk{0.953f, 0.957f, 0.965f, 1.0f};         // #f3f4f6
constexpr Rgba kMuted{0.663f, 0.675f, 0.702f, 1.0f};       // #a9acb3
constexpr Rgba kDim{0.478f, 0.494f, 0.529f, 1.0f};         // #7a7e87
constexpr Rgba kAccent{0.553f, 0.565f, 1.0f, 1.0f};        // #8d90ff
constexpr Rgba kStraw{0.910f, 0.780f, 0.494f, 1.0f};       // #e8c77e  boundary / caution
constexpr Rgba kThermal{0.941f, 0.443f, 0.227f, 1.0f};     // #f0713a  failure / recording
constexpr Rgba kRule{0.953f, 0.957f, 0.965f, 0.22f};
constexpr Rgba kRuleSoft{0.953f, 0.957f, 0.965f, 0.11f};
}  // namespace theme

enum Face { kDisplay = 0, kBody = 1, kMono = 2 };
enum Align { kLeft = 0, kCenter = 1, kRight = 2 };

// A rectangle in space. Local coordinates: x to the right, y DOWN, origin at the top-left corner, metres.
struct UiPanel {
    Mat4 m;                    // maps (x, -y, 0) to stage space
    float w = 0, h = 0;
    // Where a ray meets the panel, in panel coordinates; false when it misses the plane or points away.
    bool hit(const Vec3& origin, const Vec3& dir, float& x, float& y, float& dist) const;
};

class Ui {
public:
    bool init();
    void shutdown();
    void begin();                                                   // start a new frame's draw list
    void rect(const UiPanel& p, float x, float y, float w, float h, const Rgba& c);
    void frame(const UiPanel& p, float x, float y, float w, float h, float t, const Rgba& c);   // outline
    void disc(const UiPanel& p, float cx, float cy, float r, const Rgba& c);
    // y = top of the line box; size = em height in metres. Returns the advance width.
    float text(const UiPanel& p, float x, float y, const std::string& s, float size, Face f, const Rgba& c, Align a = kLeft,
               float tracking = 0.0f);
    float textWidth(const std::string& s, float size, Face f, float tracking = 0.0f) const;
    // The string shortened with "..." so that it is at most maxWidth wide.
    std::string fit(const std::string& s, float maxWidth, float size, Face f, float tracking = 0.0f) const;
    static float lineHeight(float size) { return size * 1.22f; }
    void draw(const Mat4& viewProj, bool gammaOut);                 // once per eye

private:
    void quad(const UiPanel& p, float x0, float y0, float x1, float y1, float u0, float v0, float u1, float v1, const Rgba& c,
              float mode);
    GLuint prog_ = 0, tex_ = 0, vao_ = 0, vbo_ = 0;
    GLint uMVP_ = -1, uTex_ = -1, uGamma_ = -1;
    std::vector<float> verts_;          // x y z u v r g b a mode
    bool dirty_ = false;
};

}  // namespace qt
