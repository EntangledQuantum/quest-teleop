#include "net.h"

#include <arpa/inet.h>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>
#include <zlib.h>

#include "log.h"
#include "protocol.h"

namespace qt {

static double nowSeconds() {
    using namespace std::chrono;
    return duration<double>(steady_clock::now().time_since_epoch()).count();
}

static void closeFd(int& fd) {
    if (fd >= 0) {
        ::shutdown(fd, SHUT_RDWR);
        ::close(fd);
        fd = -1;
    }
}

void NetClient::start(const NetConfig& cfg) {
    stop();
    cfg_ = cfg;
    running_ = true;
    thread_ = std::thread([this] { run(); });
}

void NetClient::stop() {
    if (!running_.exchange(false)) return;
    {
        std::lock_guard<std::mutex> lk(sendMutex_);
        if (tcp_ >= 0) ::shutdown(tcp_, SHUT_RDWR);      // wakes the poll in the network thread
    }
    if (thread_.joinable()) thread_.join();
    closeSockets();
}

void NetClient::closeSockets() {
    std::lock_guard<std::mutex> lk(sendMutex_);
    closeFd(tcp_);
    closeFd(udp_);
    closeFd(beacon_);
    session_ = 0;
}

std::string NetClient::serverAddress() {
    std::lock_guard<std::mutex> lk(infoMutex_);
    return serverAddr_;
}

std::string NetClient::serverName() {
    std::lock_guard<std::mutex> lk(infoMutex_);
    return serverName_;
}

void NetClient::push(NetMsg&& m) {
    std::lock_guard<std::mutex> lk(queueMutex_);
    msgs_.push_back(std::move(m));
}

void NetClient::drain(std::vector<NetMsg>& msgs, std::vector<std::vector<uint8_t>>& datagrams) {
    std::lock_guard<std::mutex> lk(queueMutex_);
    msgs.assign(std::make_move_iterator(msgs_.begin()), std::make_move_iterator(msgs_.end()));
    msgs_.clear();
    datagrams.assign(std::make_move_iterator(dgrams_.begin()), std::make_move_iterator(dgrams_.end()));
    dgrams_.clear();
}

bool NetClient::sendAll(const uint8_t* data, size_t size) {
    size_t off = 0;
    while (off < size) {
        ssize_t k = ::send(tcp_, data + off, size - off, MSG_NOSIGNAL);
        if (k <= 0) {
            if (k < 0 && (errno == EINTR)) continue;
            return false;
        }
        off += static_cast<size_t>(k);
    }
    return true;
}

void NetClient::sendMsg(uint16_t type, const uint8_t* data, size_t size) {
    std::lock_guard<std::mutex> lk(sendMutex_);
    if (tcp_ < 0 || state_.load() != CONNECTED) return;
    std::vector<uint8_t> buf(6 + size);
    uint32_t len = static_cast<uint32_t>(2 + size);
    std::memcpy(buf.data(), &len, 4);
    std::memcpy(buf.data() + 4, &type, 2);
    if (size) std::memcpy(buf.data() + 6, data, size);
    sendAll(buf.data(), buf.size());
}

void NetClient::sendDatagram(const uint8_t* data, size_t size) {
    if (state_.load() != CONNECTED) return;
    if (tcpOnlyActive_.load()) {
        sendMsg(M_DGRAM, data, size);
        return;
    }
    std::lock_guard<std::mutex> lk(sendMutex_);
    if (udp_ >= 0) ::send(udp_, data, size, MSG_NOSIGNAL | MSG_DONTWAIT);
}

// Waits up to about one second for a server OFFER (answer to our DISCOVER broadcast, or a server beacon).
bool NetClient::discover(std::string& host, int& port) {
    int s = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return false;
    int one = 1;
    ::setsockopt(s, SOL_SOCKET, SO_BROADCAST, &one, sizeof(one));
    if (beacon_ < 0) {
        beacon_ = ::socket(AF_INET, SOCK_DGRAM, 0);
        ::setsockopt(beacon_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_addr.s_addr = htonl(INADDR_ANY);
        a.sin_port = htons(kBeaconPort);
        if (::bind(beacon_, reinterpret_cast<sockaddr*>(&a), sizeof(a)) != 0) closeFd(beacon_);
    }
    Writer w;
    w.datagramHeader(D_DISCOVER, 0);
    for (const char* dst : {"255.255.255.255", "127.0.0.1"}) {
        sockaddr_in a{};
        a.sin_family = AF_INET;
        a.sin_port = htons(static_cast<uint16_t>(cfg_.port));
        ::inet_pton(AF_INET, dst, &a.sin_addr);
        ::sendto(s, w.buf.data(), w.buf.size(), 0, reinterpret_cast<sockaddr*>(&a), sizeof(a));
    }
    bool found = false;
    double tEnd = nowSeconds() + 1.0;
    while (running_ && !found && nowSeconds() < tEnd) {
        pollfd fds[2] = {{s, POLLIN, 0}, {beacon_, POLLIN, 0}};
        if (::poll(fds, beacon_ >= 0 ? 2 : 1, 100) <= 0) continue;
        for (auto& f : fds) {
            if (!(f.revents & POLLIN)) continue;
            uint8_t buf[512];
            sockaddr_in from{};
            socklen_t fl = sizeof(from);
            ssize_t k = ::recvfrom(f.fd, buf, sizeof(buf), 0, reinterpret_cast<sockaddr*>(&from), &fl);
            if (k < kDgramHeader + 2) continue;
            Reader r(buf, static_cast<size_t>(k));
            if (r.get<uint16_t>() != kMagic || r.get<uint8_t>() != kVersion || r.get<uint8_t>() != D_OFFER) continue;
            r.get<uint32_t>();
            port = r.get<uint16_t>();
            char ip[64];
            ::inet_ntop(AF_INET, &from.sin_addr, ip, sizeof(ip));
            host = ip;
            found = true;
            break;
        }
    }
    ::close(s);
    return found;
}

bool NetClient::connectTo(const std::string& host, int port, bool tcpOnly) {
    sockaddr_in a{};
    a.sin_family = AF_INET;
    a.sin_port = htons(static_cast<uint16_t>(port));
    if (::inet_pton(AF_INET, host.c_str(), &a.sin_addr) != 1) {
        QT_LOG("bad server address '%s' (IPv4 dotted form expected)", host.c_str());
        return false;
    }
    int s = ::socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return false;
    int fl = ::fcntl(s, F_GETFL, 0);
    ::fcntl(s, F_SETFL, fl | O_NONBLOCK);
    int rc = ::connect(s, reinterpret_cast<sockaddr*>(&a), sizeof(a));
    if (rc != 0 && errno == EINPROGRESS) {
        pollfd p{s, POLLOUT, 0};
        int err = 0;
        socklen_t el = sizeof(err);
        if (::poll(&p, 1, 1500) == 1 && ::getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &el) == 0 && err == 0) rc = 0;
    }
    if (rc != 0) {
        ::close(s);
        return false;
    }
    ::fcntl(s, F_SETFL, fl);
    int one = 1;
    ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    int u = ::socket(AF_INET, SOCK_DGRAM, 0);
    int rcv = 1 << 20;
    ::setsockopt(u, SOL_SOCKET, SO_RCVBUF, &rcv, sizeof(rcv));
    ::connect(u, reinterpret_cast<sockaddr*>(&a), sizeof(a));      // fixes the peer: send() / recv() only
    {
        std::lock_guard<std::mutex> lk(sendMutex_);
        tcp_ = s;
        udp_ = u;
    }
    tcpOnlyActive_ = tcpOnly;
    {
        std::lock_guard<std::mutex> lk(infoMutex_);
        serverAddr_ = host + ":" + std::to_string(port);
        serverName_.clear();
    }
    Writer w;
    w.put<uint32_t>(0);
    w.put<uint16_t>(M_HELLO);
    w.put<uint16_t>(kMagic);
    w.put<uint16_t>(kVersion);
    w.put<uint16_t>(tcpOnly ? HF_TCP_ONLY : 0);
    w.put<float>(cfg_.displayHz);
    w.str(cfg_.clientName);
    w.str(cfg_.device);
    uint32_t len = static_cast<uint32_t>(w.buf.size() - 4);
    std::memcpy(w.buf.data(), &len, 4);
    std::lock_guard<std::mutex> lk(sendMutex_);
    return sendAll(w.buf.data(), w.buf.size());
}

void NetClient::runSession() {
    std::vector<uint8_t> rx;
    rx.reserve(1 << 16);
    std::vector<uint8_t> tmp(1 << 16);
    double lastRx = nowSeconds(), lastPing = lastRx;
    while (running_) {
        pollfd fds[2] = {{tcp_, POLLIN, 0}, {udp_, POLLIN, 0}};
        int rc = ::poll(fds, 2, 50);
        double now = nowSeconds();
        if (rc < 0 && errno != EINTR) break;
        if (fds[0].revents & (POLLERR | POLLHUP | POLLNVAL)) break;
        if (fds[1].revents & POLLIN) {
            for (int burst = 0; burst < 64; ++burst) {
                ssize_t k = ::recv(udp_, tmp.data(), tmp.size(), MSG_DONTWAIT);
                if (k <= 0) break;
                if (k < kStateOverhead) continue;
                Reader r(tmp.data(), static_cast<size_t>(k));
                if (r.get<uint16_t>() != kMagic || r.get<uint8_t>() != kVersion || r.get<uint8_t>() != D_STATE) continue;
                if (r.get<uint32_t>() != session_.load()) continue;
                lastRx = now;
                std::lock_guard<std::mutex> lk(queueMutex_);
                if (dgrams_.size() > 1024) dgrams_.pop_front();
                dgrams_.emplace_back(tmp.begin(), tmp.begin() + k);
            }
        }
        if (fds[0].revents & POLLIN) {
            ssize_t k = ::recv(tcp_, tmp.data(), tmp.size(), 0);
            if (k <= 0) break;
            lastRx = now;
            rx.insert(rx.end(), tmp.begin(), tmp.begin() + k);
            size_t off = 0;
            bool bye = false;
            while (rx.size() - off >= 6) {
                uint32_t len;
                uint16_t type;
                std::memcpy(&len, rx.data() + off, 4);
                std::memcpy(&type, rx.data() + off + 4, 2);
                if (len < 2 || len > (1u << 28)) { bye = true; break; }
                if (rx.size() - off < 4 + static_cast<size_t>(len)) break;
                const uint8_t* body = rx.data() + off + 6;
                size_t n = len - 2;
                off += 4 + len;
                if (type == M_DGRAM) {
                    std::lock_guard<std::mutex> lk(queueMutex_);
                    if (dgrams_.size() > 1024) dgrams_.pop_front();
                    dgrams_.emplace_back(body, body + n);
                    continue;
                }
                NetMsg m;
                m.type = type;
                if (type == M_WELCOME) {
                    Reader r(body, n);
                    r.get<uint16_t>();
                    r.get<uint16_t>();
                    uint32_t sid = r.get<uint32_t>();
                    r.get<uint16_t>();
                    r.get<float>();
                    std::string name = r.str();
                    {
                        std::lock_guard<std::mutex> lk(infoMutex_);
                        serverName_ = name;
                    }
                    session_ = sid;
                    state_ = CONNECTED;
                    QT_LOG("connected to '%s', session %u", name.c_str(), sid);
                }
                if (type == M_MESH && n >= 40) {
                    // inflate here so the render thread never stalls on a big mesh
                    uint32_t rawSize;
                    std::memcpy(&rawSize, body + 36, 4);
                    m.data.resize(40 + static_cast<size_t>(rawSize));
                    std::memcpy(m.data.data(), body, 40);
                    uLongf dl = rawSize;
                    if (rawSize > (1u << 30) ||
                        ::uncompress(m.data.data() + 40, &dl, body + 40, static_cast<uLong>(n - 40)) != Z_OK || dl != rawSize) {
                        QT_LOG("mesh block failed to inflate");
                        continue;
                    }
                } else {
                    m.data.assign(body, body + n);
                }
                if (type == M_BYE) bye = true;
                push(std::move(m));
            }
            rx.erase(rx.begin(), rx.begin() + static_cast<long>(off));
            if (bye) break;
        }
        if (now - lastPing > 1.0 && state_.load() == CONNECTED) {
            lastPing = now;
            uint64_t t = static_cast<uint64_t>(now * 1e6);
            sendMsg(M_PING, reinterpret_cast<const uint8_t*>(&t), 8);
        }
        if (now - lastRx > 5.0) {
            QT_LOG("server silent for 5 s: reconnecting");
            break;
        }
    }
}

void NetClient::run() {
    while (running_) {
        state_ = SEARCHING;
        std::string host = cfg_.host;
        int port = cfg_.port;
        bool tcpOnly = cfg_.tcpOnly;
        bool ok = false;
        if (!host.empty()) {
            state_ = CONNECTING;
            ok = connectTo(host, port, tcpOnly);
        } else {
#ifdef __ANDROID__
            // USB link: `adb reverse tcp:PORT tcp:PORT` makes the PC's server reachable on the headset's loopback
            // (TCP only, so datagrams are tunnelled).
            if ((discoverRound_++ % 2) == 0 && connectTo("127.0.0.1", port, true)) ok = true;
#endif
            if (!ok && discover(host, port)) {
                state_ = CONNECTING;
                ok = connectTo(host, port, tcpOnly);
            }
        }
        if (!ok) {
            closeSockets();
            if (!cfg_.host.empty())
                for (int i = 0; i < 10 && running_; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
            continue;
        }
        runSession();
        state_ = SEARCHING;
        closeSockets();
        NetMsg m;
        m.type = M_LOCAL_DISCONNECTED;
        push(std::move(m));
        for (int i = 0; i < 5 && running_; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
}

}  // namespace qt
