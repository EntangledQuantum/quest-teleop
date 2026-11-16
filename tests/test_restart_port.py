"""A stopped server gives its port back at once: a new server on the same port starts right after stop() (a program
that stops and restarts its server, or hands the port to another process)."""
import socket
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))

from quest_teleop import TeleopServer  # noqa: E402


def test_port_free_right_after_stop():
    s = TeleopServer(port=0, beacon=True, verbose=False).start()
    port = s.port
    s.stop()
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):       # nothing holds the port any more
        k = socket.socket(socket.AF_INET, kind)
        k.bind(("0.0.0.0", port))
        k.close()
    s2 = TeleopServer(port=port, beacon=False, verbose=False).start()
    assert s2.port == port
    s2.stop()
