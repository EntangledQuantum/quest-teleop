#include "ui.h"

#include <cmath>

#include "font_data.h"
#include "log.h"

namespace qt {

static const char* kVS = R"(#version 300 es
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec2 aUV;
layout(location = 2) in vec4 aCol;
layout(location = 3) in float aMode;
uniform mat4 uMVP;
out vec2 vUV;
out vec4 vCol;
out float vMode;
void main() {
    vUV = aUV;
    vCol = aCol;
    vMode = aMode;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
)";

// mode 0: solid, 1: glyph coverage from the atlas, 2: disc (uv in -1..1)
static const char* kFS = R"(#version 300 es
precision mediump float;
in vec2 vUV;
in vec4 vCol;
in float vMode;
uniform sampler2D uTex;
uniform int uGamma;
out vec4 o;
void main() {
    float a = 1.0;
    if (vMode > 1.5) {
        float d = length(vUV);
        a = clamp((1.0 - d) / max(fwidth(d), 1e-4), 0.0, 1.0);
    } else if (vMode > 0.5) {
        a = texture(uTex, vUV).r;
    }
    vec3 c = uGamma != 0 ? vCol.rgb : pow(vCol.rgb, vec3(2.2));
    o = vec4(c, vCol.a * a);
}
)";

bool UiPanel::hit(const Vec3& origin, const Vec3& dir, float& x, float& y, float& dist) const {
    Vec3 o(m.m[12], m.m[13], m.m[14]);
    Vec3 ax(m.m[0], m.m[1], m.m[2]), ay(m.m[4], m.m[5], m.m[6]), n(m.m[8], m.m[9], m.m[10]);
    float denom = dot(dir, n);
    if (std::fabs(denom) < 1e-5f) return false;
    dist = dot(o - origin, n) / denom;
    if (dist <= 0.0f) return false;
    Vec3 p = origin + dir * dist - o;
    x = dot(p, ax) / dot(ax, ax);
    y = -dot(p, ay) / dot(ay, ay);
    return true;
}

static GLuint compile(GLenum type, const char* src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[1024];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        QT_LOG("ui shader: %s", log);
    }
    return s;
}

bool Ui::init() {
    prog_ = glCreateProgram();
    GLuint a = compile(GL_VERTEX_SHADER, kVS), b = compile(GL_FRAGMENT_SHADER, kFS);
    glAttachShader(prog_, a);
    glAttachShader(prog_, b);
    glLinkProgram(prog_);
    glDeleteShader(a);
    glDeleteShader(b);
    uMVP_ = glGetUniformLocation(prog_, "uMVP");
    uTex_ = glGetUniformLocation(prog_, "uTex");
    uGamma_ = glGetUniformLocation(prog_, "uGamma");
    std::vector<uint8_t> px(static_cast<size_t>(kFontAtlasW) * kFontAtlasH);
    size_t o = 0;
    for (int i = 0; i + 1 < kFontRleSize && o < px.size(); i += 2)
        for (int k = 0; k < kFontRle[i] && o < px.size(); ++k) px[o++] = kFontRle[i + 1];
    glGenTextures(1, &tex_);
    glBindTexture(GL_TEXTURE_2D, tex_);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, kFontAtlasW, kFontAtlasH, 0, GL_RED, GL_UNSIGNED_BYTE, px.data());
    glGenerateMipmap(GL_TEXTURE_2D);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glGenVertexArrays(1, &vao_);
    glBindVertexArray(vao_);
    glGenBuffers(1, &vbo_);
    glBindBuffer(GL_ARRAY_BUFFER, vbo_);
    const GLsizei stride = 10 * 4;
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride, nullptr);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void*>(12));
    glEnableVertexAttribArray(2);
    glVertexAttribPointer(2, 4, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void*>(20));
    glEnableVertexAttribArray(3);
    glVertexAttribPointer(3, 1, GL_FLOAT, GL_FALSE, stride, reinterpret_cast<void*>(36));
    glBindVertexArray(0);
    return prog_ != 0;
}

void Ui::shutdown() {
    glDeleteProgram(prog_);
    glDeleteTextures(1, &tex_);
    glDeleteBuffers(1, &vbo_);
    glDeleteVertexArrays(1, &vao_);
    prog_ = 0;
}

void Ui::begin() {
    verts_.clear();
    dirty_ = true;
}

void Ui::quad(const UiPanel& p, float x0, float y0, float x1, float y1, float u0, float v0, float u1, float v1, const Rgba& c,
              float mode) {
    const float xs[6] = {x0, x1, x1, x0, x1, x0}, ys[6] = {y0, y0, y1, y0, y1, y1};
    const float us[6] = {u0, u1, u1, u0, u1, u0}, vs[6] = {v0, v0, v1, v0, v1, v1};
    for (int k = 0; k < 6; ++k) {
        Vec3 w = transformPoint(p.m, Vec3(xs[k], -ys[k], 0));
        const float v[10] = {w.x, w.y, w.z, us[k], vs[k], c.r, c.g, c.b, c.a, mode};
        verts_.insert(verts_.end(), v, v + 10);
    }
    dirty_ = true;
}

void Ui::rect(const UiPanel& p, float x, float y, float w, float h, const Rgba& c) {
    quad(p, x, y, x + w, y + h, 0, 0, 0, 0, c, 0.0f);
}

void Ui::frame(const UiPanel& p, float x, float y, float w, float h, float t, const Rgba& c) {
    rect(p, x, y, w, t, c);
    rect(p, x, y + h - t, w, t, c);
    rect(p, x, y + t, t, h - 2 * t, c);
    rect(p, x + w - t, y + t, t, h - 2 * t, c);
}

void Ui::disc(const UiPanel& p, float cx, float cy, float r, const Rgba& c) {
    quad(p, cx - r, cy - r, cx + r, cy + r, -1, -1, 1, 1, c, 2.0f);
}

float Ui::textWidth(const std::string& s, float size, Face f, float tracking) const {
    const FontFace& ff = kFonts[f];
    float k = size / ff.px, w = 0;
    for (char ch : s) {
        int c = static_cast<unsigned char>(ch);
        if (c < kFontFirst || c > kFontLast) c = '?';
        w += ff.g[c - kFontFirst].adv * k + tracking * size;
    }
    return w;
}

std::string Ui::fit(const std::string& s, float maxWidth, float size, Face f, float tracking) const {
    if (textWidth(s, size, f, tracking) <= maxWidth) return s;
    std::string o = s;
    while (!o.empty() && textWidth(o + "...", size, f, tracking) > maxWidth) o.pop_back();
    return o + "...";
}

float Ui::text(const UiPanel& p, float x, float y, const std::string& s, float size, Face f, const Rgba& c, Align a,
               float tracking) {
    const FontFace& ff = kFonts[f];
    float k = size / ff.px;
    float w = textWidth(s, size, f, tracking);
    if (a == kCenter) x -= 0.5f * w;
    if (a == kRight) x -= w;
    // centre the ascent..descent box in the line box
    float top = y + 0.5f * (lineHeight(size) - (ff.ascent + ff.descent) * k);
    for (char ch : s) {
        int ci = static_cast<unsigned char>(ch);
        if (ci < kFontFirst || ci > kFontLast) ci = '?';
        const FontGlyph& g = ff.g[ci - kFontFirst];
        if (g.w && g.h) {
            float x0 = x + g.ox * k, y0 = top + g.oy * k;
            quad(p, x0, y0, x0 + g.w * k, y0 + g.h * k, static_cast<float>(g.x) / kFontAtlasW, static_cast<float>(g.y) / kFontAtlasH,
                 static_cast<float>(g.x + g.w) / kFontAtlasW, static_cast<float>(g.y + g.h) / kFontAtlasH, c, 1.0f);
        }
        x += g.adv * k + tracking * size;
    }
    return w;
}

void Ui::draw(const Mat4& viewProj, bool gammaOut) {
    if (verts_.empty()) return;
    glUseProgram(prog_);
    glUniformMatrix4fv(uMVP_, 1, GL_FALSE, viewProj.m);
    glUniform1i(uTex_, 0);
    glUniform1i(uGamma_, gammaOut ? 1 : 0);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, tex_);
    glBindVertexArray(vao_);
    if (dirty_) {
        glBindBuffer(GL_ARRAY_BUFFER, vbo_);
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(verts_.size() * 4), verts_.data(), GL_DYNAMIC_DRAW);
        dirty_ = false;
    }
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glEnable(GL_BLEND);
    glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glDrawArrays(GL_TRIANGLES, 0, static_cast<GLsizei>(verts_.size() / 10));
    glBindVertexArray(0);
    glEnable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
}

}  // namespace qt
