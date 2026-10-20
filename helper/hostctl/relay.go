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
	"unicode"
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
	if !c.w.Admission || c.w.Paused || !c.w.valid(c.now(), c.boot(), c.bootID) {
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

var errSOCKSGreeting = errors.New("SOCKS greeting refused")
var errSOCKSReply = errors.New("malformed SOCKS response")
var errSOCKSAddress = errors.New("malformed SOCKS address")

type socksRefusal struct{ code byte }

func (e *socksRefusal) Error() string { return "SOCKS CONNECT refused" }

func selectorAddress(selector string) ([]byte, error) {
	if len(selector) == 0 || len(selector) > 255 || strings.ContainsFunc(selector, func(r rune) bool {
		return unicode.IsSpace(r) || unicode.IsControl(r)
	}) {
		return nil, errSOCKSAddress
	}
	if ip := net.ParseIP(selector); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			return append([]byte{1}, v4...), nil
		}
		return append([]byte{4}, ip.To16()...), nil
	}
	return append([]byte{3, byte(len(selector))}, []byte(selector)...), nil
}

func socksConnect(conn net.Conn, selector string) error {
	address, err := selectorAddress(selector)
	if err != nil {
		return err
	}
	if _, err = conn.Write([]byte{5, 1, 0}); err != nil {
		return err
	}
	var greeting [2]byte
	if _, err = io.ReadFull(conn, greeting[:]); err != nil {
		return err
	}
	if greeting != [2]byte{5, 0} {
		return errSOCKSGreeting
	}
	frame := append([]byte{5, 1, 0}, address...)
	frame = append(frame, 0, 22)
	if _, err = conn.Write(frame); err != nil {
		return err
	}
	var reply [4]byte
	if _, err = io.ReadFull(conn, reply[:]); err != nil {
		return err
	}
	if reply[0] != 5 || reply[2] != 0 {
		return errSOCKSReply
	}
	size := 0
	switch reply[3] {
	case 1:
		size = 4
	case 4:
		size = 16
	case 3:
		var length [1]byte
		if _, err = io.ReadFull(conn, length[:]); err != nil {
			return err
		}
		if length[0] == 0 {
			return errSOCKSAddress
		}
		size = int(length[0])
	default:
		return errSOCKSAddress
	}
	if _, err = io.CopyN(io.Discard, conn, int64(size+2)); err != nil {
		return err
	}
	if reply[1] != 0 {
		return &socksRefusal{code: reply[1]}
	}
	return nil
}

func rawConnect(conn net.Conn, destination string) error {
	err := socksConnect(conn, destination)
	var refused *socksRefusal
	switch {
	case errors.Is(err, errSOCKSGreeting):
		return errors.New("raw SOCKS greeting refused")
	case errors.Is(err, errSOCKSReply), errors.As(err, &refused):
		return errors.New("raw SOCKS CONNECT refused")
	case errors.Is(err, errSOCKSAddress):
		return errors.New("malformed raw SOCKS response")
	}
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
