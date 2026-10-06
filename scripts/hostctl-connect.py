#!/usr/bin/env python3
"""OpenSSH ProxyCommand: one SOCKS5 stream through Djinn's gated helper."""

import ipaddress
import os
import socket
import struct
import sys
import threading


def receive(connection, length):
    data = bytearray()
    while len(data) < length:
        part = connection.recv(length - len(data))
        if not part:
            raise OSError("relay closed during SOCKS negotiation")
        data.extend(part)
    return bytes(data)


def connect(host, port):
    if port != 22:
        raise ValueError("hostctl permits TCP port 22 only")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode("ascii")
        if not 1 <= len(name) <= 255 or any(char.isspace() for char in host) or "\x00" in host:
            raise ValueError("invalid destination") from None
        destination = bytes((3, len(name))) + name
    else:
        destination = bytes((1 if address.version == 4 else 4,)) + address.packed
    connection = socket.create_connection(("djinn-hostctl", 1080), timeout=5)
    try:
        connection.sendall(b"\x05\x01\x00")
        if receive(connection, 2) != b"\x05\x00":
            raise OSError("relay refused SOCKS authentication")
        connection.sendall(b"\x05\x01\x00" + destination + struct.pack(">H", port))
        reply = receive(connection, 4)
        if reply[:3] != b"\x05\x00\x00":
            raise OSError("relay refused admission or destination")
        if reply[3] == 1:
            size = 4
        elif reply[3] == 4:
            size = 16
        elif reply[3] == 3:
            size = receive(connection, 1)[0]
        else:
            raise OSError("malformed SOCKS response")
        receive(connection, size + 2)
        connection.settimeout(None)
        return connection
    except BaseException:
        connection.close()
        raise


def stream(connection):
    def upload():
        try:
            while data := os.read(0, 65536):
                connection.sendall(data)
            connection.shutdown(socket.SHUT_WR)
        except OSError:
            connection.close()

    threading.Thread(target=upload, daemon=True).start()
    while data := connection.recv(65536):
        view = memoryview(data)
        while view:
            view = view[os.write(1, view):]


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("usage: djinn-hostctl-connect <host> <port>")
        with connect(sys.argv[1], int(sys.argv[2])) as connection:
            stream(connection)
    except (OSError, ValueError) as exc:
        print(f"djinn-hostctl-connect: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
