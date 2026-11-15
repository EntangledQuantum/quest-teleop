#include "renderer.h"

#include <cmath>

#include "log.h"

namespace qt {

static const char* kMeshVS = R"(#version 300 es
layout(location = 0) in vec3 aPos;
layout(location = 1) in vec4 aNrm;
layout(location = 2) in vec4 aCol;
uniform mat4 uMVP;
uniform mat4 uModel;
uniform mat3 uNrm;
out vec3 vWorld;
out vec3 vNrm;
out vec4 vCol;
void main() {
    vec4 w = uModel * vec4(aPos, 1.0);
    vWorld = w.xyz;
    vNrm = uNrm * aNrm.xyz;
    vCol = aCol;
    gl_Position = uMVP * vec4(aPos, 1.0);
}
)";

// uMode: bit 0 = normals from the triangle (screen-space derivatives), bit 1 = unlit, bit 2 = encode to sRGB here,
// bit 3 = highlighted (accent rim), bit 4 = receives the shadow map.
static const char* kMeshFS = R"(#version 300 es
precision highp float;
precision highp sampler2DShadow;
in vec3 vWorld;
in vec3 vNrm;
in vec4 vCol;
uniform vec4 uColor;
uniform vec3 uEye;
uniform int uMode;
uniform mat4 uShadowVP;
uniform sampler2DShadow uShadow;
uniform vec2 uShadowParam;      // world size of one shadow texel, 1 / shadow map size
out vec4 o;
float shadowAt(vec3 world, vec3 n) {
    vec3 q = world + n * (uShadowParam.x * 1.8);
    vec4 s = uShadowVP * vec4(q, 1.0);
    vec3 p = s.xyz * 0.5 + 0.5;
    vec2 e = abs(p.xy - 0.5) * 2.0;
    float edge = max(e.x, e.y);
    if (edge >= 1.0 || p.z >= 1.0) return 1.0;
    p.z -= 0.0012;
    float t = uShadowParam.y, sum = 0.0;
    sum += texture(uShadow, p + vec3(-0.75 * t, -0.25 * t, 0.0));
    sum += texture(uShadow, p + vec3(0.25 * t, -0.75 * t, 0.0));
    sum += texture(uShadow, p + vec3(0.75 * t, 0.25 * t, 0.0));
    sum += texture(uShadow, p + vec3(-0.25 * t, 0.75 * t, 0.0));
    return mix(sum * 0.25, 1.0, smoothstep(0.8, 1.0, edge));
}
void main() {
    vec4 base = vCol * uColor;
    vec3 c = pow(base.rgb, vec3(2.2));
    if ((uMode & 2) == 0) {
        vec3 n = ((uMode & 1) != 0) ? cross(dFdx(vWorld), dFdy(vWorld)) : vNrm;
        n = normalize(n);
        vec3 v = normalize(uEye - vWorld);
        if (dot(n, v) < 0.0) n = -n;
        vec3 l1 = normalize(vec3(0.35, 0.85, 0.40));
        vec3 l2 = normalize(vec3(-0.50, 0.30, -0.60));
        float ndl = max(dot(n, l1), 0.0);
        float sh = ((uMode & 16) != 0 && ndl > 0.0) ? shadowAt(vWorld, n) : 1.0;
        vec3 amb = mix(vec3(0.13, 0.12, 0.13), vec3(0.36, 0.39, 0.45), 0.5 + 0.5 * n.y);
        vec3 key = vec3(1.00, 0.96, 0.90) * (0.95 * ndl * sh);
        float fill = 0.16 * max(dot(n, l2), 0.0);
        vec3 h = normalize(l1 + v);
        float fres = 0.04 + 0.96 * pow(1.0 - max(dot(n, v), 0.0), 5.0);
        float spec = (0.25 + 0.75 * fres) * 0.35 * pow(max(dot(n, h), 0.0), 48.0) * sh;
        c = c * (amb + key + fill) + spec;
        if ((uMode & 8) != 0) {
            float rim = pow(1.0 - max(dot(n, v), 0.0), 2.0);
            c += vec3(0.26, 0.27, 0.95) * (0.10 + 0.9 * rim);
        }
        c = c / (1.0 + 0.18 * c);          // gentle highlight roll-off
    }
    if ((uMode & 4) != 0) c = pow(c, vec3(1.0 / 2.2));
    o = vec4(c, base.a);
}
)";

static const char* kDepthVS = R"(#version 300 es
layout(location = 0) in vec3 aPos;
uniform mat4 uMVP;
void main() { gl_Position = uMVP * vec4(aPos, 1.0); }
)";
static const char* kDepthFS = R"(#version 300 es
precision mediump float;
void main() {}
)";

static GLuint compile(GLenum type, const char* src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[2048];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        QT_LOG("shader compile failed: %s", log);
    }
    return s;
}

static GLuint link(const char* vs, const char* fs) {
    GLuint p = glCreateProgram(), a = compile(GL_VERTEX_SHADER, vs), b = compile(GL_FRAGMENT_SHADER, fs);
    glAttachShader(p, a);
    glAttachShader(p, b);
    glLinkProgram(p);
    GLint ok = 0;
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[2048];
        glGetProgramInfoLog(p, sizeof(log), nullptr, log);
        QT_LOG("program link failed: %s", log);
    }
    glDeleteShader(a);
    glDeleteShader(b);
    return p;
}

Renderer::GpuMesh Renderer::makeBuiltin(const std::vector<float>& pos, const std::vector<float>& nrm,
                                        const std::vector<uint32_t>& idx) {
    GpuMesh g;
    glGenVertexArrays(1, &g.vao);
    glBindVertexArray(g.vao);
    glGenBuffers(1, &g.pos);
    glBindBuffer(GL_ARRAY_BUFFER, g.pos);
    glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(pos.size() * 4), pos.data(), GL_STATIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, nullptr);
    glGenBuffers(1, &g.nrm);
    glBindBuffer(GL_ARRAY_BUFFER, g.nrm);
    glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(nrm.size() * 4), nrm.data(), GL_STATIC_DRAW);
    glEnableVertexAttribArray(1);
    glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 0, nullptr);
    glGenBuffers(1, &g.idx);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g.idx);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, static_cast<GLsizeiptr>(idx.size() * 4), idx.data(), GL_STATIC_DRAW);
    g.count = static_cast<GLsizei>(idx.size());
    g.hasNormals = true;
    glBindVertexArray(0);
    return g;
}

bool Renderer::init() {
    prog_ = link(kMeshVS, kMeshFS);
    uMVP_ = glGetUniformLocation(prog_, "uMVP");
    uModel_ = glGetUniformLocation(prog_, "uModel");
    uNrm_ = glGetUniformLocation(prog_, "uNrm");
    uColor_ = glGetUniformLocation(prog_, "uColor");
    uEye_ = glGetUniformLocation(prog_, "uEye");
    uMode_ = glGetUniformLocation(prog_, "uMode");
    uShadowVP_ = glGetUniformLocation(prog_, "uShadowVP");
    uShadow_ = glGetUniformLocation(prog_, "uShadow");
    uShadowParam_ = glGetUniformLocation(prog_, "uShadowParam");
    depthProg_ = link(kDepthVS, kDepthFS);
    dMVP_ = glGetUniformLocation(depthProg_, "uMVP");
    // shadow map: one depth texture with hardware comparison
    glGenTextures(1, &shadowTex_);
    glBindTexture(GL_TEXTURE_2D, shadowTex_);
    glTexStorage2D(GL_TEXTURE_2D, 1, GL_DEPTH_COMPONENT24, kShadowSize, kShadowSize);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_COMPARE_MODE, GL_COMPARE_REF_TO_TEXTURE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_COMPARE_FUNC, GL_LEQUAL);
    glGenFramebuffers(1, &shadowFbo_);
    glBindFramebuffer(GL_FRAMEBUFFER, shadowFbo_);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_TEXTURE_2D, shadowTex_, 0);
    GLenum none = GL_NONE;
    glDrawBuffers(1, &none);
    glReadBuffer(GL_NONE);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) QT_LOG("shadow framebuffer incomplete");
    glBindFramebuffer(GL_FRAMEBUFFER, 0);

    // unit box (half extent 1)
    {
        std::vector<float> p, n;
        std::vector<uint32_t> ix;
        for (int axis = 0; axis < 3; ++axis)
            for (int s = -1; s <= 1; s += 2) {
                int a = (axis + 1) % 3, b = (axis + 2) % 3;
                uint32_t base = static_cast<uint32_t>(p.size() / 3);
                const int c[4][2] = {{-1, -1}, {1, -1}, {1, 1}, {-1, 1}};
                for (auto& k : c) {
                    float v[3], nn[3] = {0, 0, 0};
                    v[axis] = static_cast<float>(s);
                    v[a] = static_cast<float>(k[0]);
                    v[b] = static_cast<float>(k[1]);
                    nn[axis] = static_cast<float>(s);
                    p.insert(p.end(), v, v + 3);
                    n.insert(n.end(), nn, nn + 3);
                }
                for (uint32_t k : {0u, 1u, 2u, 0u, 2u, 3u}) ix.push_back(base + k);
            }
        box_ = makeBuiltin(p, n, ix);
    }
    // unit sphere
    {
        std::vector<float> p;
        std::vector<uint32_t> ix;
        const int rings = 10, sectors = 16;
        for (int i = 0; i <= rings; ++i)
            for (int j = 0; j <= sectors; ++j) {
                float th = 3.14159265f * i / rings, ph = 6.2831853f * j / sectors;
                p.push_back(std::sin(th) * std::cos(ph));
                p.push_back(std::cos(th));
                p.push_back(std::sin(th) * std::sin(ph));
            }
        for (int i = 0; i < rings; ++i)
            for (int j = 0; j < sectors; ++j) {
                uint32_t a = static_cast<uint32_t>(i * (sectors + 1) + j), b = a + sectors + 1;
                for (uint32_t k : {a, b, a + 1, a + 1, b, b + 1}) ix.push_back(k);
            }
        sphere_ = makeBuiltin(p, p, ix);
    }
    // floor grid: 8 m x 8 m, 0.5 m cells
    {
        std::vector<float> g;
        const float h = 4.0f;
        for (int i = -8; i <= 8; ++i) {
            float c = 0.5f * static_cast<float>(i);
            float l[12] = {-h, 0, c, h, 0, c, c, 0, -h, c, 0, h};
            g.insert(g.end(), l, l + 12);
        }
        glGenVertexArrays(1, &gridVao_);
        glBindVertexArray(gridVao_);
        glGenBuffers(1, &gridVbo_);
        glBindBuffer(GL_ARRAY_BUFFER, gridVbo_);
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(g.size() * 4), g.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, nullptr);
        gridCount_ = static_cast<GLsizei>(g.size() / 3);
        glBindVertexArray(0);
    }
    return prog_ != 0 && depthProg_ != 0;
}

void Renderer::freeScene() {
    for (auto& g : meshes_) {
        if (g.vao) glDeleteVertexArrays(1, &g.vao);
        GLuint b[4] = {g.pos, g.nrm, g.col, g.idx};
        glDeleteBuffers(4, b);
    }
    for (auto& g : nodes_) {
        if (g.vao) glDeleteVertexArrays(1, &g.vao);
        GLuint b[2] = {g.vbo, g.idx};
        glDeleteBuffers(2, b);
    }
    meshes_.clear();
    nodes_.clear();
}

void Renderer::shutdown() {
    freeScene();
    glDeleteProgram(prog_);
    glDeleteProgram(depthProg_);
    prog_ = depthProg_ = 0;
}

void Renderer::uploadMesh(const MeshData& m, GpuMesh& g) {
    glGenVertexArrays(1, &g.vao);
    glBindVertexArray(g.vao);
    glGenBuffers(1, &g.pos);
    glBindBuffer(GL_ARRAY_BUFFER, g.pos);
    glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(m.pos.size() * 4), m.pos.data(), GL_STATIC_DRAW);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, nullptr);
    if (!m.nrm.empty()) {
        glGenBuffers(1, &g.nrm);
        glBindBuffer(GL_ARRAY_BUFFER, g.nrm);
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(m.nrm.size()), m.nrm.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 4, GL_BYTE, GL_TRUE, 0, nullptr);
        g.hasNormals = true;
    }
    if (!m.col.empty()) {
        glGenBuffers(1, &g.col);
        glBindBuffer(GL_ARRAY_BUFFER, g.col);
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(m.col.size()), m.col.data(), GL_STATIC_DRAW);
        glEnableVertexAttribArray(2);
        glVertexAttribPointer(2, 4, GL_UNSIGNED_BYTE, GL_TRUE, 0, nullptr);
        g.hasColors = true;
    }
    glGenBuffers(1, &g.idx);
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g.idx);
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, static_cast<GLsizeiptr>(m.idx.size() * 4), m.idx.data(), GL_STATIC_DRAW);
    g.count = static_cast<GLsizei>(m.idx.size());
    glBindVertexArray(0);
}

void Renderer::updateDeform(const Scene& scene, Node& n, GpuNode& g) {
    const GpuMesh& gm = meshes_[n.mesh];
    if (!g.vao) {
        glGenVertexArrays(1, &g.vao);
        glBindVertexArray(g.vao);
        glGenBuffers(1, &g.vbo);
        glBindBuffer(GL_ARRAY_BUFFER, g.vbo);
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(n.pts.size() * 4), n.pts.data(), GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 0, nullptr);
        if (gm.col) {
            glBindBuffer(GL_ARRAY_BUFFER, gm.col);
            glEnableVertexAttribArray(2);
            glVertexAttribPointer(2, 4, GL_UNSIGNED_BYTE, GL_TRUE, 0, nullptr);
        }
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, gm.idx);
        g.count = gm.count;
        glBindVertexArray(0);
    } else {
        glBindBuffer(GL_ARRAY_BUFFER, g.vbo);
        glBufferSubData(GL_ARRAY_BUFFER, 0, static_cast<GLsizeiptr>(n.pts.size() * 4), n.pts.data());
    }
    (void)scene;
}

// A tube around the polyline: rings of kSides vertices carried along with a twist-free (parallel transport) frame.
void Renderer::updateTube(Node& n, GpuNode& g) {
    const int kSides = 8;
    size_t np = n.pts.size() / 3;
    if (np < 2) { g.count = 0; return; }
    auto P = [&](size_t i) { return Vec3(n.pts[3 * i], n.pts[3 * i + 1], n.pts[3 * i + 2]); };
    scratch_.resize((np * kSides + 2) * 6);
    Vec3 t0 = normalize(P(1) - P(0));
    if (length(t0) < 0.5f) t0 = Vec3(0, 0, 1);
    Vec3 ref = std::fabs(t0.y) < 0.9f ? Vec3(0, 1, 0) : Vec3(1, 0, 0);
    Vec3 u = normalize(cross(ref, t0));
    Vec3 tPrev = t0;
    float r = n.param;
    for (size_t i = 0; i < np; ++i) {
        Vec3 t = i == 0 ? P(1) - P(0) : (i + 1 == np ? P(i) - P(i - 1) : P(i + 1) - P(i - 1));
        t = normalize(t);
        if (length(t) < 0.5f) t = tPrev;
        // rotate the frame by the rotation that takes the previous tangent to this one
        Vec3 ax = cross(tPrev, t);
        float s = length(ax), c = dot(tPrev, t);
        if (s > 1e-6f) u = rotate(axisAngle(ax, std::atan2(s, c)), u);
        u = normalize(u - t * dot(u, t));
        Vec3 w = cross(t, u);
        tPrev = t;
        Vec3 p = P(i);
        for (int k = 0; k < kSides; ++k) {
            float a = 6.2831853f * static_cast<float>(k) / kSides;
            Vec3 nn = u * std::cos(a) + w * std::sin(a);
            Vec3 v = p + nn * r;
            float* d = &scratch_[(i * kSides + static_cast<size_t>(k)) * 6];
            d[0] = v.x; d[1] = v.y; d[2] = v.z; d[3] = nn.x; d[4] = nn.y; d[5] = nn.z;
        }
        if (i == 0 || i + 1 == np) {
            float* d = &scratch_[(np * kSides + (i == 0 ? 0 : 1)) * 6];
            Vec3 nn = i == 0 ? -t : t;
            d[0] = p.x; d[1] = p.y; d[2] = p.z; d[3] = nn.x; d[4] = nn.y; d[5] = nn.z;
        }
    }
    bool fresh = !g.vao || g.points != np;
    if (!g.vao) {
        glGenVertexArrays(1, &g.vao);
        glGenBuffers(1, &g.vbo);
        glGenBuffers(1, &g.idx);
    }
    glBindVertexArray(g.vao);
    glBindBuffer(GL_ARRAY_BUFFER, g.vbo);
    if (fresh) {
        glBufferData(GL_ARRAY_BUFFER, static_cast<GLsizeiptr>(scratch_.size() * 4), scratch_.data(), GL_DYNAMIC_DRAW);
        glEnableVertexAttribArray(0);
        glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, 24, nullptr);
        glEnableVertexAttribArray(1);
        glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, 24, reinterpret_cast<void*>(12));
        scratchIdx_.clear();
        for (size_t i = 0; i + 1 < np; ++i)
            for (int k = 0; k < kSides; ++k) {
                uint32_t a = static_cast<uint32_t>(i * kSides + static_cast<size_t>(k));
                uint32_t b = static_cast<uint32_t>(i * kSides + static_cast<size_t>((k + 1) % kSides));
                for (uint32_t q : {a, b, a + kSides, b, b + kSides, a + kSides}) scratchIdx_.push_back(q);
            }
        uint32_t c0 = static_cast<uint32_t>(np * kSides), c1 = c0 + 1, last = static_cast<uint32_t>((np - 1) * kSides);
        for (uint32_t k = 0; k < kSides; ++k) {
            for (uint32_t q : {c0, (k + 1) % kSides, k}) scratchIdx_.push_back(q);
            for (uint32_t q : {c1, last + k, last + (k + 1) % kSides}) scratchIdx_.push_back(q);
        }
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, g.idx);
        glBufferData(GL_ELEMENT_ARRAY_BUFFER, static_cast<GLsizeiptr>(scratchIdx_.size() * 4), scratchIdx_.data(), GL_STATIC_DRAW);
        g.count = static_cast<GLsizei>(scratchIdx_.size());
        g.points = np;
    } else {
        glBufferSubData(GL_ARRAY_BUFFER, 0, static_cast<GLsizeiptr>(scratch_.size() * 4), scratch_.data());
    }
    glBindVertexArray(0);
}

void Renderer::sync(Scene& scene) {
    if (scene.generation != generation_) {
        freeScene();
        generation_ = scene.generation;
    }
    if (!scene.loaded) return;
    if (meshes_.size() != scene.meshes.size()) {
        meshes_.assign(scene.meshes.size(), GpuMesh());
        for (size_t i = 0; i < scene.meshes.size(); ++i)
            if (scene.meshes[i].valid) uploadMesh(scene.meshes[i], meshes_[i]);
    }
    if (nodes_.size() != scene.nodes.size()) nodes_.resize(scene.nodes.size());
    for (size_t i = 0; i < scene.nodes.size(); ++i) {
        Node& n = scene.nodes[i];
        if (!n.ptsDirty) continue;
        if (n.kind == K_DEFORM) updateDeform(scene, n, nodes_[i]);
        else if (n.kind == K_TUBE) updateTube(n, nodes_[i]);
        n.ptsDirty = false;
    }
    glBindBuffer(GL_ARRAY_BUFFER, 0);
}

void Renderer::setCommon(const DrawView& v, const Mat4& model, const float rgba[4], int mode) {
    Mat4 mvp = v.proj * v.view * model;
    glUniformMatrix4fv(uMVP_, 1, GL_FALSE, mvp.m);
    glUniformMatrix4fv(uModel_, 1, GL_FALSE, model.m);
    float nm[9];
    for (int c = 0; c < 3; ++c) {
        float x = model.m[c * 4], y = model.m[c * 4 + 1], z = model.m[c * 4 + 2];
        float l2 = x * x + y * y + z * z;
        float inv = l2 > 1e-20f ? 1.0f / l2 : 0.0f;
        nm[c * 3] = x * inv; nm[c * 3 + 1] = y * inv; nm[c * 3 + 2] = z * inv;
    }
    glUniformMatrix3fv(uNrm_, 1, GL_FALSE, nm);
    glUniform4fv(uColor_, 1, rgba);
    glUniform3f(uEye_, v.eye.x, v.eye.y, v.eye.z);
    glUniform1i(uMode_, mode | (v.gammaOut ? 4 : 0) | (shadowValid_ ? 16 : 0));
    glUniformMatrix4fv(uShadowVP_, 1, GL_FALSE, shadowVP_.m);
    glUniform2f(uShadowParam_, shadowTexel_, 1.0f / static_cast<float>(kShadowSize));
    glUniform1i(uShadow_, 1);
}

// Depth of the scene seen from the key light, for the shadow test in the main pass. center / radius: the region
// (stage space) the map covers.
void Renderer::renderShadow(const Scene& scene, const Mat4 parents[7], const Vec3& center, float radius) {
    shadowValid_ = false;
    if (!shadowsEnabled || !scene.loaded || nodes_.size() != scene.nodes.size()) return;
    Vec3 l = normalize(Vec3(0.35f, 0.85f, 0.40f));
    Vec3 z = l, x = normalize(cross(Vec3(0, 1, 0), z)), y = cross(z, x);
    Vec3 eye = center + l * (2.0f * radius);
    Mat4 view;
    view.m[0] = x.x; view.m[4] = x.y; view.m[8] = x.z; view.m[12] = -dot(x, eye);
    view.m[1] = y.x; view.m[5] = y.y; view.m[9] = y.z; view.m[13] = -dot(y, eye);
    view.m[2] = z.x; view.m[6] = z.y; view.m[10] = z.z; view.m[14] = -dot(z, eye);
    Mat4 proj;
    float zn = 0.05f, zf = 4.0f * radius;
    proj.m[0] = 1.0f / radius;
    proj.m[5] = 1.0f / radius;
    proj.m[10] = -2.0f / (zf - zn);
    proj.m[14] = -(zf + zn) / (zf - zn);
    shadowVP_ = proj * view;
    shadowTexel_ = 2.0f * radius / static_cast<float>(kShadowSize);
    glBindFramebuffer(GL_FRAMEBUFFER, shadowFbo_);
    glViewport(0, 0, kShadowSize, kShadowSize);
    glEnable(GL_DEPTH_TEST);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
    glDisable(GL_CULL_FACE);
    glClear(GL_DEPTH_BUFFER_BIT);
    glEnable(GL_POLYGON_OFFSET_FILL);
    glPolygonOffset(1.5f, 3.0f);
    glUseProgram(depthProg_);
    for (size_t i = 0; i < scene.nodes.size(); ++i) {
        const Node& n = scene.nodes[i];
        if (!(n.flags & F_VISIBLE) || n.kind == 0 || (n.flags & (F_BLEND | F_ON_TOP | F_UNLIT))) continue;
        GLuint vao = n.kind == K_MESH ? meshes_[n.mesh].vao : nodes_[i].vao;
        GLsizei count = n.kind == K_MESH ? meshes_[n.mesh].count : nodes_[i].count;
        if (!vao || count == 0) continue;
        Mat4 mvp = shadowVP_ * parents[n.parent < 7 ? n.parent : 0] * trs(n.pose.p, n.pose.q, n.scale);
        glUniformMatrix4fv(dMVP_, 1, GL_FALSE, mvp.m);
        glBindVertexArray(vao);
        glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, nullptr);
    }
    glBindVertexArray(0);
    glDisable(GL_POLYGON_OFFSET_FILL);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    shadowValid_ = true;
}

void Renderer::drawScene(const Scene& scene, const DrawView& v, const Mat4 parents[7]) {
    if (!scene.loaded || nodes_.size() != scene.nodes.size()) return;
    glUseProgram(prog_);
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, shadowTex_);
    glActiveTexture(GL_TEXTURE0);
    glDisable(GL_CULL_FACE);
    glEnable(GL_DEPTH_TEST);
    for (int pass = 0; pass < 3; ++pass) {
        if (pass == 0) {
            glDisable(GL_BLEND);
            glDepthMask(GL_TRUE);
        } else {
            glEnable(GL_BLEND);
            glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
            glDepthMask(GL_FALSE);
            if (pass == 2) glDisable(GL_DEPTH_TEST);
        }
        for (size_t i = 0; i < scene.nodes.size(); ++i) {
            const Node& n = scene.nodes[i];
            if (!(n.flags & F_VISIBLE) || n.kind == 0) continue;
            int p = (n.flags & F_ON_TOP) ? 2 : ((n.flags & F_BLEND) ? 1 : 0);
            if (p != pass) continue;
            GLuint vao = 0;
            GLsizei count = 0;
            int mode = ((n.flags & F_UNLIT) ? 2 : 0) | ((n.flags & F_HIGHLIGHT) ? 8 : 0);
            bool colors = false;
            if (n.kind == K_MESH) {
                const GpuMesh& g = meshes_[n.mesh];
                vao = g.vao;
                count = g.count;
                colors = g.hasColors;
                if (!g.hasNormals || (n.flags & F_FLAT)) mode |= 1;
            } else {
                const GpuNode& g = nodes_[i];
                vao = g.vao;
                count = g.count;
                if (n.kind == K_DEFORM) {
                    mode |= 1;
                    colors = meshes_[n.mesh].hasColors;
                }
            }
            if (!vao || count == 0) continue;
            Mat4 model = parents[n.parent < 7 ? n.parent : 0] * trs(n.pose.p, n.pose.q, n.scale);
            float rgba[4] = {n.rgba[0] / 255.0f, n.rgba[1] / 255.0f, n.rgba[2] / 255.0f, n.rgba[3] / 255.0f};
            setCommon(v, model, rgba, mode);
            glBindVertexArray(vao);
            if (!colors) glVertexAttrib4f(2, 1, 1, 1, 1);
            glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, nullptr);
        }
    }
    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glEnable(GL_DEPTH_TEST);
    glDisable(GL_BLEND);
}

void Renderer::drawGrid(const DrawView& v, float alpha) {
    glUseProgram(prog_);
    glEnable(GL_BLEND);
    glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glDepthMask(GL_FALSE);
    float rgba[4] = {0.45f, 0.55f, 0.65f, alpha};
    setCommon(v, Mat4(), rgba, 2);
    glBindVertexArray(gridVao_);
    glVertexAttrib4f(1, 0, 1, 0, 0);
    glVertexAttrib4f(2, 1, 1, 1, 1);
    glDrawArrays(GL_LINES, 0, gridCount_);
    glBindVertexArray(0);
    glDepthMask(GL_TRUE);
    glDisable(GL_BLEND);
}

void Renderer::drawBox(const DrawView& v, const Mat4& model, const float rgba[4], bool unlit) {
    glUseProgram(prog_);
    setCommon(v, model, rgba, unlit ? 2 : 0);
    glBindVertexArray(box_.vao);
    glVertexAttrib4f(2, 1, 1, 1, 1);
    glDrawElements(GL_TRIANGLES, box_.count, GL_UNSIGNED_INT, nullptr);
    glBindVertexArray(0);
}

void Renderer::drawSphere(const DrawView& v, const Mat4& model, const float rgba[4], bool unlit) {
    glUseProgram(prog_);
    setCommon(v, model, rgba, unlit ? 2 : 0);
    glBindVertexArray(sphere_.vao);
    glVertexAttrib4f(2, 1, 1, 1, 1);
    glDrawElements(GL_TRIANGLES, sphere_.count, GL_UNSIGNED_INT, nullptr);
    glBindVertexArray(0);
}

}  // namespace qt
