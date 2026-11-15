// Network side of the client: finds the server, keeps the TCP session and the UDP socket, and hands complete
// messages and datagrams to the render thread through queues. One background thread; POSIX sockets only.
#pragma once
#include <atomic>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace qt {

struct NetConfig {
    std::string host;            // empty: discover on the LAN (and try 127.0.0.1, for `adb reverse`)
    int port = 47800;
    bool tcpOnly = false;        // tunnel datagrams through TCP
    std::string clientName = "quest-teleop";
    std::string device = "unknown";
    float displayHz = 72.0f;
};

struct NetMsg {
    uint16_t type = 0;
    std::vector<uint8_t> data;   // payload; for MESH the vertex block is already inflated (see scene.cpp)
};

class NetClient {
public:
    enum State { SEARCHING = 0, CONNECTING = 1, CONNECTED = 2 };

    ~NetClient() { stop(); }
    void start(const NetConfig& cfg);
    void stop();

    // Render thread: take everything that arrived since the last call.
    void drain(std::vector<NetMsg>& msgs, std::vector<std::vector<uint8_t>>& datagrams);
    // Render thread: send one datagram (UDP, or wrapped in TCP in TCP-only mode) / one reliable message.
    void sendDatagram(const uint8_t* data, size_t size);
    void sendMsg(uint16_t type, const uint8_t* data, size_t size);

    State state() const { return static_cast<State>(state_.load()); }
    uint32_t session() const { return session_.load(); }
    std::string serverAddress();
    std::string serverName();
    void setDisplayHz(float hz) { cfg_.displayHz = hz; }
    bool tcpOnly() const { return tcpOnlyActive_.load(); }      // datagrams tunnelled through TCP (USB / adb reverse)

private:
    void run();
    bool connectTo(const std::string& host, int port, bool tcpOnly);
    bool discover(std::string& host, int& port);
    void runSession();
    void closeSockets();
    bool sendAll(const uint8_t* data, size_t size);
    void push(NetMsg&& m);

    NetConfig cfg_;
    std::thread thread_;
    std::atomic<bool> running_{false};
    std::atomic<int> state_{SEARCHING};
    std::atomic<uint32_t> session_{0};
    std::atomic<bool> tcpOnlyActive_{false};
    int tcp_ = -1, udp_ = -1, beacon_ = -1;
    std::mutex sendMutex_;       // TCP writes and socket lifetime
    std::mutex queueMutex_;
    std::deque<NetMsg> msgs_;
    std::deque<std::vector<uint8_t>> dgrams_;
    std::mutex infoMutex_;
    std::string serverAddr_, serverName_;
    int discoverRound_ = 0;
};

}  // namespace qt
