package main

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"strings"
	"time"
)

type stream struct {
	client     net.Conn
	upstream   net.Conn
	reason     string // guarded by the controller mutex
	generation string
}

func validateRoutes(routes map[string]string) error {
	if len(routes) == 0 || len(routes) > 4096 {
		return errors.New("missing or excessive declared routes")
	}
	_, v4, _ := net.ParseCIDR("100.64.0.0/10")
	_, v6, _ := net.ParseCIDR("fd7a:115c:a1e0::/48")
	for name, destination := range routes {
		ip := net.ParseIP(destination)
		if name == "" || len(name) > 255 || strings.ContainsAny(name, " \t\r\n\x00") || ip == nil || (!v4.Contains(ip) && !v6.Contains(ip)) {
			return errors.New("invalid declared tailnet route")
		}
	}
	return nil
}

func (c *controller) admissionValid() bool {
	if !c.w.Admission || !c.w.valid(c.now(), c.boot(), c.bootID) {
		return false
	}
	return c.diskMatches()
}

func (c *controller) diskMatches() bool {
	data, err := os.ReadFile(c.path)
	var disk window
	return err == nil && json.Unmarshal(data, &disk) == nil && disk == c.w
}

func readDestination(conn net.Conn) (string, uint16, error) {
	var header [2]byte
	if _, err := io.ReadFull(conn, header[:]); err != nil {
		return "", 0, err
	}
	if header[0] != 5 || header[1] == 0 {
		return "", 0, errors.New("invalid SOCKS greeting")
	}
	methods := make([]byte, int(header[1]))
	if _, err := io.ReadFull(conn, methods); err != nil {
		return "", 0, err
	}
	allowed := false
	for _, method := range methods {
		if method == 0 {
			allowed = true
		}
	}
	if !allowed {
		_, _ = conn.Write([]byte{5, 255})
		return "", 0, errors.New("no supported authentication")
	}
	if _, err := conn.Write([]byte{5, 0}); err != nil {
		return "", 0, err
	}
	var request [4]byte
	if _, err := io.ReadFull(conn, request[:]); err != nil {
		return "", 0, err
	}
	if request[0] != 5 || request[1] != 1 || request[2] != 0 {
		return "", 0, errors.New("CONNECT required")
	}
	var address string
	switch request[3] {
	case 1, 4:
		size := 4
		if request[3] == 4 {
			size = 16
		}
		value := make([]byte, size)
		if _, err := io.ReadFull(conn, value); err != nil {
			return "", 0, err
		}
		address = net.IP(value).String()
	case 3:
		var length [1]byte
		if _, err := io.ReadFull(conn, length[:]); err != nil {
			return "", 0, err
		}
		if length[0] == 0 {
			return "", 0, errors.New("empty destination")
		}
		value := make([]byte, int(length[0]))
		if _, err := io.ReadFull(conn, value); err != nil {
			return "", 0, err
		}
		address = strings.ToLower(strings.TrimSuffix(string(value), "."))
	default:
		return "", 0, errors.New("unsupported address type")
	}
	var port [2]byte
	_, err := io.ReadFull(conn, port[:])
	return address, binary.BigEndian.Uint16(port[:]), err
}

func socksReply(conn net.Conn, code byte) { _, _ = conn.Write([]byte{5, code, 0, 1, 0, 0, 0, 0, 0, 0}) }

func rawConnect(conn net.Conn, destination string) error {
	if _, err := conn.Write([]byte{5, 1, 0}); err != nil {
		return err
	}
	var greeting [2]byte
	if _, err := io.ReadFull(conn, greeting[:]); err != nil {
		return err
	}
	if greeting != [2]byte{5, 0} {
		return errors.New("raw SOCKS greeting refused")
	}
	ip := net.ParseIP(destination)
	request := []byte{5, 1, 0, 4}
	if v4 := ip.To4(); v4 != nil {
		request[3] = 1
		ip = v4
	}
	request = append(request, ip...)
	request = append(request, 0, 22)
	if _, err := conn.Write(request); err != nil {
		return err
	}
	var response [4]byte
	if _, err := io.ReadFull(conn, response[:]); err != nil {
		return err
	}
	if response[0] != 5 || response[1] != 0 || response[2] != 0 {
		return errors.New("raw SOCKS CONNECT refused")
	}
	size := 0
	switch response[3] {
	case 1:
		size = 4
	case 4:
		size = 16
	case 3:
		var length [1]byte
		if _, err := io.ReadFull(conn, length[:]); err != nil {
			return err
		}
		size = int(length[0])
	default:
		return errors.New("malformed raw SOCKS response")
	}
	_, err := io.CopyN(io.Discard, conn, int64(size+2))
	return err
}

func serveRelay(listener net.Listener, c *controller, raw string, logs io.Writer) {
	for {
		client, err := listener.Accept()
		if err != nil {
			return
		}
		c.mu.Lock()
		if c.w.Closed {
			c.mu.Unlock()
			_ = client.Close()
			continue
		}
		if c.streams == nil {
			c.streams = make(map[*stream]bool)
		}
		active := &stream{client: client, generation: c.w.Generation}
		c.streams[active] = true
		c.sequence++
		id := fmt.Sprintf("%s-%d", c.w.Generation, c.sequence)
		c.connections.Add(1)
		c.mu.Unlock()
		go relayStream(c, active, id, raw, logs)
	}
}

func relayStream(c *controller, active *stream, id, raw string, logs io.Writer) {
	start := time.Now().UTC()
	destination := "unknown"
	reason := "malformed"
	started := false
	log := func(event string) {
		row := map[string]any{"event": event, "id": id, "generation": active.generation, "destination": destination, "start": start}
		if event == "connection-end" {
			row["end"] = time.Now().UTC()
			row["reason"] = reason
		}
		_ = json.NewEncoder(logs).Encode(row)
	}
	defer func() {
		_ = active.client.Close()
		c.mu.Lock()
		if active.upstream != nil {
			_ = active.upstream.Close()
		}
		if active.reason != "" {
			reason = active.reason
		}
		delete(c.streams, active)
		c.mu.Unlock()
		if !started {
			log("connection-start")
		}
		log("connection-end")
		c.connections.Done()
	}()
	_ = active.client.SetDeadline(time.Now().Add(3 * time.Second))
	selector, port, err := readDestination(active.client)
	if err != nil {
		socksReply(active.client, 1)
		return
	}
	c.mu.Lock()
	target, declared := c.routes[selector]
	ready := c.admissionValid()
	c.mu.Unlock()
	if declared {
		destination = net.JoinHostPort(target, fmt.Sprint(port))
	} else {
		destination = "undeclared"
	}
	log("connection-start")
	started = true
	if !ready || !declared || port != 22 {
		reason = "admission-refused"
		socksReply(active.client, 2)
		return
	}
	reason = "upstream-refused"
	upstream, err := net.DialTimeout("tcp", raw, time.Second)
	if err != nil {
		socksReply(active.client, 5)
		return
	}
	c.mu.Lock()
	if !c.admissionValid() {
		c.mu.Unlock()
		_ = upstream.Close()
		socksReply(active.client, 2)
		return
	}
	active.upstream = upstream
	c.mu.Unlock()
	_ = upstream.SetDeadline(time.Now().Add(3 * time.Second))
	if err = rawConnect(upstream, target); err != nil {
		socksReply(active.client, 5)
		return
	}
	_ = upstream.SetDeadline(time.Time{})
	_ = active.client.SetDeadline(time.Time{})
	socksReply(active.client, 0)
	results := make(chan error, 2)
	copyStream := func(to, from net.Conn) {
		_, err := io.Copy(to, from)
		if tcp, ok := to.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
		results <- err
	}
	go copyStream(upstream, active.client)
	go copyStream(active.client, upstream)
	first := <-results
	if first != nil {
		_ = upstream.Close()
		_ = active.client.Close()
	}
	second := <-results
	reason = "eof"
	if first != nil || second != nil {
		reason = "reset"
	}
}
