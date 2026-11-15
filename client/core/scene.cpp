#include "scene.h"

#include <algorithm>

#include "log.h"

namespace qt {

void Scene::clear() {
    loaded = false;
    ++generation;
    meshes.clear();
    nodes.clear();
    newestFrame = ackFrame = 0;
    fragCount = fragSeen = 0;
    fragMask.clear();
}

bool Scene::onMessage(const NetMsg& m, double now) {
    Reader r(m.data.data(), m.data.size());
    switch (m.type) {
        case M_SCENE_BEGIN: {
            clear();
            id = r.get<uint16_t>();
            upAxis = r.get<uint8_t>();
            r.get<uint8_t>();
            lo = r.vec3();
            hi = r.vec3();
            focusCenter = r.vec3();
            focusRadius = r.get<float>();
            viewDir = r.vec3();
            uint16_t nMesh = r.get<uint16_t>(), nNode = r.get<uint16_t>();
            name = r.str();
            if (!r.ok) { QT_LOG("bad SCENE_BEGIN"); return false; }
            meshes.resize(nMesh);
            nodes.reserve(nNode);
            QT_LOG("scene '%s' (id %u): %u meshes, %u nodes", name.c_str(), id, nMesh, nNode);
            return false;
        }
        case M_MESH: {
            uint16_t mid = r.get<uint16_t>(), flags = r.get<uint16_t>();
            uint32_t nv = r.get<uint32_t>(), ni = r.get<uint32_t>();
            Vec3 a = r.vec3(), b = r.vec3();
            r.get<uint32_t>();
            if (!r.ok || mid >= meshes.size()) return false;
            MeshData& me = meshes[mid];
            me = MeshData();
            me.flags = flags;
            const uint8_t* q = r.bytes(static_cast<size_t>(nv) * 6);
            if (!q) return false;
            me.pos.resize(static_cast<size_t>(nv) * 3);
            const float ext[3] = {b.x - a.x, b.y - a.y, b.z - a.z}, org[3] = {a.x, a.y, a.z};
            for (size_t i = 0; i < static_cast<size_t>(nv) * 3; ++i) {
                uint16_t u;
                std::memcpy(&u, q + 2 * i, 2);
                me.pos[i] = org[i % 3] + ext[i % 3] * (u / 65535.0f);
            }
            if (flags & MF_NORMALS) {
                const uint8_t* n = r.bytes(static_cast<size_t>(nv) * 4);
                if (!n) return false;
                me.nrm.resize(static_cast<size_t>(nv) * 4);
                std::memcpy(me.nrm.data(), n, me.nrm.size());
            }
            if (flags & MF_COLORS) {
                const uint8_t* c = r.bytes(static_cast<size_t>(nv) * 4);
                if (!c) return false;
                me.col.assign(c, c + static_cast<size_t>(nv) * 4);
            }
            me.idx.resize(ni);
            if (flags & MF_INDEX32) {
                const uint8_t* ix = r.bytes(static_cast<size_t>(ni) * 4);
                if (!ix) return false;
                std::memcpy(me.idx.data(), ix, static_cast<size_t>(ni) * 4);
            } else {
                const uint8_t* ix = r.bytes(static_cast<size_t>(ni) * 2);
                if (!ix) return false;
                for (uint32_t i = 0; i < ni; ++i) {
                    uint16_t u;
                    std::memcpy(&u, ix + 2 * i, 2);
                    me.idx[i] = u;
                }
            }
            for (uint32_t i : me.idx)
                if (i >= nv) { QT_LOG("mesh %u: index out of range", mid); return false; }
            me.valid = true;
            return false;
        }
        case M_NODE: {
            uint16_t nid = r.get<uint16_t>();
            Node n;
            n.kind = r.get<uint8_t>();
            n.parent = r.get<uint8_t>();
            n.mesh = r.get<uint16_t>();
            n.flags = r.get<uint16_t>();
            for (auto& c : n.rgba) c = r.get<uint8_t>();
            n.pose.p = r.vec3();
            n.pose.q = normalize(r.quat());
            n.scale = r.vec3();
            n.param = r.get<float>();
            uint16_t np = r.get<uint16_t>();
            if (n.kind == K_TUBE) {
                const uint8_t* p = r.bytes(static_cast<size_t>(np) * 12);
                if (!p) return false;
                n.pts.resize(static_cast<size_t>(np) * 3);
                std::memcpy(n.pts.data(), p, n.pts.size() * 4);
            }
            n.name = r.str();
            if (!r.ok || nid != nodes.size()) { QT_LOG("bad NODE %u", nid); return false; }
            bool hasMesh = n.mesh < meshes.size() && meshes[n.mesh].valid;
            if (n.kind == K_DEFORM) {
                if (!hasMesh) { n.kind = 0; } else n.pts = meshes[n.mesh].pos;
            } else if (n.kind == K_MESH && !hasMesh) {
                n.kind = 0;                      // nothing to draw; keeps the ids dense
            }
            nodes.push_back(std::move(n));
            return false;
        }
        case M_SCENE_END: {
            uint16_t sid = r.get<uint16_t>();
            if (sid != id) return false;
            loaded = true;
            return true;
        }
        case M_TEXT: {
            uint8_t slot = r.get<uint8_t>();
            TextItem t;
            t.anchor = r.get<uint8_t>();
            r.get<uint16_t>();
            for (auto& c : t.rgba) c = r.get<uint8_t>();
            t.size = r.get<float>();
            t.pos = r.vec3();
            uint32_t ttl = r.get<uint32_t>();
            t.text = r.str();
            if (!r.ok) return false;
            t.used = !t.text.empty();
            t.expires = ttl ? now + ttl / 1000.0 : 0.0;
            texts[slot] = t;
            return false;
        }
        case M_PANEL: {
            PanelInfo pi;
            pi.title = r.str();
            pi.subtitle = r.str();
            pi.host = r.str();
            pi.statusStyle = r.get<uint8_t>();
            pi.statusText = r.str();
            uint8_t n = r.get<uint8_t>();
            for (int i = 0; i < n && r.ok; ++i) {
                PanelRow row;
                row.style = r.get<uint8_t>();
                row.label = r.str();
                row.value = r.str();
                pi.rows.push_back(row);
            }
            n = r.get<uint8_t>();
            for (int i = 0; i < n && r.ok; ++i) {
                PanelAction a;
                a.id = r.get<uint16_t>();
                a.style = r.get<uint8_t>();
                a.flags = r.get<uint8_t>();
                a.label = r.str();
                a.hint = r.str();
                pi.actions.push_back(a);
            }
            n = r.get<uint8_t>();
            for (int i = 0; i < n && r.ok; ++i) {
                std::string k = r.str(), t = r.str();
                pi.legend.emplace_back(k, t);
            }
            if (!r.ok) return false;
            pi.valid = true;
            panel = pi;
            return false;
        }
        default:
            return false;
    }
}

void Scene::onDatagram(const uint8_t* data, size_t size) {
    if (!loaded || size < static_cast<size_t>(kStateOverhead)) return;
    Reader r(data, size);
    r.off = kDgramHeader;
    uint16_t sid = r.get<uint16_t>();
    uint32_t frame = r.get<uint32_t>();
    uint8_t frag = r.get<uint8_t>(), nfrag = r.get<uint8_t>();
    r.get<uint64_t>();
    uint32_t eSeq = r.get<uint32_t>();
    uint64_t eTime = r.get<uint64_t>();
    uint32_t eHold = r.get<uint32_t>();
    if (sid != id || frame < newestFrame || nfrag == 0 || frag >= nfrag) return;
    ++statDatagrams;
    statBytes += size;
    if (frame > newestFrame) {
        if (newestFrame && fragSeen && ackFrame != newestFrame) ++statLostFrames;
        newestFrame = frame;
        fragCount = nfrag;
        fragSeen = 0;
        fragMask.assign(nfrag, false);
        echoSeq = eSeq;
        echoClientTimeUs = eTime;
        echoHoldUs = eHold;
        newEcho = true;
        for (int h = 0; h < 2; ++h) {
            Reader hr(data + kDgramHeader + kStateHeader + h * kHapticSize, kHapticSize);
            uint16_t seq = hr.get<uint16_t>();
            uint8_t amp = hr.get<uint8_t>();
            hr.get<uint8_t>();
            uint16_t dur = hr.get<uint16_t>(), freq = hr.get<uint16_t>();
            HapticState& hs = haptics[h];
            if (seq != hs.seq) {
                hs.seq = seq;
                hs.amplitude = amp / 255.0f;
                hs.duration = dur / 1000.0f;
                hs.frequency = freq;
                hs.pending = true;
            }
        }
    }
    r.off = kStateOverhead;
    const float sLo[3] = {lo.x, lo.y, lo.z}, sExt[3] = {hi.x - lo.x, hi.y - lo.y, hi.z - lo.z};
    const float lLo[3] = {-kLocalBound, -kLocalBound, -kLocalBound}, lExt[3] = {2 * kLocalBound, 2 * kLocalBound, 2 * kLocalBound};
    while (r.left() >= 4) {
        uint8_t type = r.get<uint8_t>();
        r.get<uint8_t>();
        uint16_t nid = r.get<uint16_t>();
        if (nid >= nodes.size()) return;
        Node& n = nodes[nid];
        const float* bLo = n.parent == P_SCENE ? sLo : lLo;
        const float* bExt = n.parent == P_SCENE ? sExt : lExt;
        if (type == C_POSE) {
            uint16_t p[3];
            int16_t q[4];
            for (auto& v : p) v = r.get<uint16_t>();
            for (auto& v : q) v = r.get<int16_t>();
            if (!r.ok) return;
            n.pose.p = Vec3(bLo[0] + bExt[0] * (p[0] / 65535.0f), bLo[1] + bExt[1] * (p[1] / 65535.0f),
                            bLo[2] + bExt[2] * (p[2] / 65535.0f));
            n.pose.q = dequantizeQuat(q[0], q[1], q[2], q[3]);
        } else if (type == C_APPEARANCE) {
            for (auto& c : n.rgba) c = r.get<uint8_t>();
            uint16_t f = r.get<uint16_t>();
            r.get<uint16_t>();
            if (!r.ok) return;
            n.flags = f;
        } else if (type == C_VERTS) {
            uint16_t first = r.get<uint16_t>(), count = r.get<uint16_t>();
            const uint8_t* v = r.bytes(static_cast<size_t>(count) * 6);
            if (!v) return;
            if ((static_cast<size_t>(first) + count) * 3 > n.pts.size()) continue;
            float* dst = n.pts.data() + static_cast<size_t>(first) * 3;
            for (size_t i = 0; i < static_cast<size_t>(count) * 3; ++i) {
                uint16_t u;
                std::memcpy(&u, v + 2 * i, 2);
                dst[i] = bLo[i % 3] + bExt[i % 3] * (u / 65535.0f);
            }
            n.ptsDirty = true;
        } else {
            return;                              // unknown chunk: the rest of the datagram cannot be parsed
        }
    }
    if (!fragMask[frag]) {
        fragMask[frag] = true;
        if (++fragSeen == fragCount) {
            ackFrame = frame;
            ++statFrames;
        }
    }
}

}  // namespace qt
