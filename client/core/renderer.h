// OpenGL ES 3.0 renderer for the received scene plus the client's own overlays (text, floor grid, controllers).
#pragma once
#include <GLES3/gl3.h>
#include <string>
#include <vector>

#include "mathx.h"
#include "scene.h"

namespace qt {

struct DrawView {
    Mat4 view, proj;
    Vec3 eye;
    bool gammaOut = false;     // true: the target is not an sRGB framebuffer, the shader encodes
};

class Renderer {
public:
    bool init();
    void shutdown();

    // Once per frame, before drawing: create / update the GPU buffers of the scene.
    void sync(Scene& scene);
    // parents[P_*]: transform of each parent frame into stage space (parents[P_SCENE] includes the scene scale).
    void drawScene(const Scene& scene, const DrawView& v, const Mat4 parents[7]);

    void drawGrid(const DrawView& v, float alpha);
    void drawBox(const DrawView& v, const Mat4& model, const float rgba[4], bool unlit = false);
    void drawSphere(const DrawView& v, const Mat4& model, const float rgba[4], bool unlit = false);

    // Once per frame after sync(): the shadow map of the key light over the given region (stage space).
    void renderShadow(const Scene& scene, const Mat4 parents[7], const Vec3& center, float radius);
    bool shadowsEnabled = true;

private:
    struct GpuMesh {
        GLuint vao = 0, pos = 0, nrm = 0, col = 0, idx = 0;
        GLsizei count = 0;
        bool hasNormals = false, hasColors = false;
    };
    struct GpuNode {
        GLuint vao = 0, vbo = 0, idx = 0;
        GLsizei count = 0;
        size_t points = 0;
    };
    void freeScene();
    void uploadMesh(const MeshData& m, GpuMesh& g);
    void updateDeform(const Scene& scene, Node& n, GpuNode& g);
    void updateTube(Node& n, GpuNode& g);
    void setCommon(const DrawView& v, const Mat4& model, const float rgba[4], int mode);
    GpuMesh makeBuiltin(const std::vector<float>& pos, const std::vector<float>& nrm, const std::vector<uint32_t>& idx);

    static constexpr int kShadowSize = 2048;
    GLuint prog_ = 0, depthProg_ = 0, shadowTex_ = 0, shadowFbo_ = 0;
    GLint uMVP_ = -1, uModel_ = -1, uNrm_ = -1, uColor_ = -1, uEye_ = -1, uMode_ = -1;
    GLint uShadowVP_ = -1, uShadow_ = -1, uShadowParam_ = -1, dMVP_ = -1;
    Mat4 shadowVP_;
    float shadowTexel_ = 0.001f;
    bool shadowValid_ = false;
    uint32_t generation_ = 0xFFFFFFFF;
    std::vector<GpuMesh> meshes_;
    std::vector<GpuNode> nodes_;
    GpuMesh box_, sphere_;
    GLuint gridVao_ = 0, gridVbo_ = 0;
    GLsizei gridCount_ = 0;
    std::vector<float> scratch_;
    std::vector<uint32_t> scratchIdx_;
};

}  // namespace qt
