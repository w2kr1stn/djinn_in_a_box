package main

import (
	"bytes"
	"encoding/json"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"testing"
	"time"
)

type lockedLog struct {
	mu   sync.Mutex
	data bytes.Buffer
}

func (l *lockedLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.data.Write(p)
}
func (l *lockedLog) String() string { l.mu.Lock(); defer l.mu.Unlock(); return l.data.String() }

func relayFixture(t *testing.T, ready bool) (*controller, string, *lockedLog) {
	t.Helper()
	c := fixture(t)
	if err := persist(c.path, c.w); err != nil {
		t.Fatal(err)
	}
	c.routes = map[string]string{"host-a.example.ts.net": "100.64.0.1"}
	if ready {
		reply := c.update(request{Operation: "admit", Generation: c.w.Generation, Routes: map[string]string{"host-a.example.ts.net": "100.64.0.1", "100.64.0.1": "100.64.0.1", "fd7a:115c:a1e0::1": "100.64.0.1"}})
		if reply.Error != "" {
			t.Fatal(reply.Error)
		}
	}
	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = raw.Close() })
	go func() {
		for {
			conn, err := raw.Accept()
			if err != nil {
				return
			}
			go func() {
				defer conn.Close()
				host, port, err := readDestination(conn)
				if err != nil || host != "100.64.0.1" || port != 22 {
					return
				}
				socksReply(conn, 0)
				_, _ = io.Copy(conn, conn)
			}()
		}
	}()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	logs := &lockedLog{}
	go serveRelay(listener, c, raw.Addr().String(), logs)
	t.Cleanup(func() { _ = listener.Close(); _ = c.close("off") })
	return c, listener.Addr().String(), logs
}

func socksClient(t *testing.T, addr string, frame []byte) (net.Conn, byte) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
	// Fragment frames to require complete reads instead of assuming packet boundaries.
	for _, value := range []byte{5, 1, 0} {
		if _, err := conn.Write([]byte{value}); err != nil {
			t.Fatal(err)
		}
	}
	greeting := make([]byte, 2)
	if _, err := io.ReadFull(conn, greeting); err != nil || !bytes.Equal(greeting, []byte{5, 0}) {
		t.Fatalf("greeting: %v %v", greeting, err)
	}
	if _, err := conn.Write(frame); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 10)
	if _, err := io.ReadFull(conn, reply); err != nil {
		t.Fatal(err)
	}
	return conn, reply[1]
}

func domainFrame(host string, port byte) []byte {
	return append(append([]byte{5, 1, 0, 3, byte(len(host))}, []byte(host)...), 0, port)
}

func TestRelayAdmission(t *testing.T) {
	for _, name := range []string{"not-ready", "undeclared", "port", "malformed", "missing-state", "expired", "expires-during-dial"} {
		t.Run(name, func(t *testing.T) {
			c, addr, _ := relayFixture(t, name != "not-ready")
			frame := domainFrame("host-a.example.ts.net", 22)
			switch name {
			case "undeclared":
				frame = domainFrame("host-b.example.ts.net", 22)
			case "port":
				frame = domainFrame("host-a.example.ts.net", 23)
			case "malformed":
				frame[2] = 1
			case "missing-state":
				_ = os.Remove(c.path)
			case "expired":
				c.mu.Lock()
				c.w.Deadline = c.now()
				_ = persist(c.path, c.w)
				c.mu.Unlock()
			case "expires-during-dial":
				// Valid at the first admission check, expired at the recheck after the dial.
				c.mu.Lock()
				valid, checks := c.now(), 0
				c.now = func() time.Time {
					checks++
					if checks == 1 {
						return valid
					}
					return c.w.Deadline
				}
				c.mu.Unlock()
			}
			_, code := socksClient(t, addr, frame)
			if code == 0 {
				t.Fatal("refused stream admitted")
			}
		})
	}
}

func TestRelayStreamsAndLogs(t *testing.T) {
	for _, reason := range []string{"off", "expiry"} {
		t.Run(reason, func(t *testing.T) {
			c, addr, logs := relayFixture(t, true)
			frames := [][]byte{domainFrame("host-a.example.ts.net", 22), {5, 1, 0, 1, 100, 64, 0, 1, 0, 22}, append([]byte{5, 1, 0, 4}, append(net.ParseIP("fd7a:115c:a1e0::1").To16(), 0, 22)...)}
			clients := []net.Conn{}
			for _, frame := range frames {
				conn, code := socksClient(t, addr, frame)
				if code != 0 {
					t.Fatalf("valid stream refused: %d", code)
				}
				payload := []byte("secret payload; multiple SSH channels share this TCP stream")
				if _, err := conn.Write(payload); err != nil {
					t.Fatal(err)
				}
				echoed := make([]byte, len(payload))
				if _, err := io.ReadFull(conn, echoed); err != nil || !bytes.Equal(echoed, payload) {
					t.Fatal("stream corrupted")
				}
				clients = append(clients, conn)
			}
			if err := c.close(reason); err != nil {
				t.Fatal(err)
			}
			for _, client := range clients {
				if _, err := client.Read(make([]byte, 1)); err == nil {
					t.Fatal("active stream survived cutoff")
				}
			}
			rows := strings.Split(strings.TrimSpace(logs.String()), "\n")
			if len(rows) != 6 {
				t.Fatalf("missing end records: %s", logs.String())
			}
			ids := map[string]int{}
			for _, line := range rows {
				var row map[string]any
				if err := json.Unmarshal([]byte(line), &row); err != nil {
					t.Fatal(err)
				}
				id := row["id"].(string)
				ids[id]++
				if row["destination"] != "100.64.0.1:22" || row["start"] == nil {
					t.Fatal(row)
				}
				if row["event"] == "connection-end" && (row["end"] == nil || row["reason"] != reason) {
					t.Fatal(row)
				}
			}
			if len(ids) != 3 || strings.Contains(logs.String(), "secret payload") {
				t.Fatal("invalid IDs or payload log")
			}
			for _, count := range ids {
				if count != 2 {
					t.Fatal("unmatched connection")
				}
			}
		})
	}
}

func TestRelayEOF(t *testing.T) {
	c, addr, logs := relayFixture(t, true)
	conn, code := socksClient(t, addr, domainFrame("host-a.example.ts.net", 22))
	if code != 0 {
		t.Fatal(code)
	}
	_ = conn.(*net.TCPConn).CloseWrite()
	if _, err := conn.Read(make([]byte, 1)); err != io.EOF {
		t.Fatal(err)
	}
	c.connections.Wait()
	if !strings.Contains(logs.String(), `"reason":"eof"`) {
		t.Fatal(logs.String())
	}
}

func TestAdmissionFreeze(t *testing.T) {
	c, _, _ := relayFixture(t, true)
	if reply := c.update(request{Operation: "admit", Generation: c.w.Generation, Routes: map[string]string{"host-b": "100.64.0.2"}}); reply.Error == "" {
		t.Fatal("in-window trust update accepted")
	}
	if c.routes["100.64.0.1"] != "100.64.0.1" {
		t.Fatal("trust changed")
	}
}

func TestRouteValidation(t *testing.T) {
	for _, name := range []string{"empty", "LAN", "malformed-state", "old-generation"} {
		t.Run(name, func(t *testing.T) {
			c := fixture(t)
			_ = persist(c.path, c.w)
			request := request{Operation: "admit", Generation: c.w.Generation, Routes: map[string]string{"host-a": "100.64.0.1"}}
			switch name {
			case "empty":
				request.Routes = nil
			case "LAN":
				request.Routes["host-a"] = "192.168.1.1"
			case "malformed-state":
				_ = os.WriteFile(c.path, []byte("bad state"), 0600)
			case "old-generation":
				request.Generation = "old"
			}
			if reply := c.update(request); reply.Error == "" || c.w.Admission {
				t.Fatal("unsafe admission acknowledged")
			}
		})
	}
}

func TestRelayReset(t *testing.T) {
	c, addr, logs := relayFixture(t, true)
	conn, code := socksClient(t, addr, domainFrame("host-a.example.ts.net", 22))
	if code != 0 {
		t.Fatal(code)
	}
	_ = conn.(*net.TCPConn).SetLinger(0)
	_ = conn.Close()
	c.connections.Wait()
	if !strings.Contains(logs.String(), `"reason":"reset"`) {
		t.Fatal(logs.String())
	}
}

func TestDamagedAdmittedState(t *testing.T) {
	c, _, _ := relayFixture(t, true)
	_ = os.Remove(c.path)
	if reply := c.update(request{Operation: "status", Generation: c.w.Generation}); reply.Error == "" {
		t.Fatal("damaged state reported admitted")
	}
	if reply := c.update(request{Operation: "set-deadline", Generation: c.w.Generation, Minutes: 1}); reply.Error == "" {
		t.Fatal("damaged grant restored by limit")
	}
}

func TestAdmissionPauseResume(t *testing.T) {
	t.Run("pause", func(t *testing.T) {
		c, _, _ := relayFixture(t, true)
		before := c.w
		reply := c.update(request{Operation: "pause", Generation: c.w.Generation})
		if reply.Error != "" || !c.w.Paused || !c.w.Admission || c.admissionValid() {
			t.Fatal("pause did not close admission while retaining frozen trust", reply)
		}
		if c.w.Deadline != before.Deadline || c.w.BootDeadline != before.BootDeadline {
			t.Fatal("pause changed the deadline")
		}
		if r := c.update(request{Operation: "admit", Generation: c.w.Generation,
			Routes: map[string]string{"replacement": "100.64.0.2"}}); r.Error == "" {
			t.Fatal("pause allowed replacement trust")
		}
	})
	t.Run("resume", func(t *testing.T) {
		c, _, _ := relayFixture(t, true)
		before := c.w
		if r := c.update(request{Operation: "pause", Generation: c.w.Generation}); r.Error != "" {
			t.Fatal(r.Error)
		}
		reply := c.update(request{Operation: "resume", Generation: c.w.Generation})
		if reply.Error != "" || c.w.Paused || !c.admissionValid() || c.w != before {
			t.Fatal("resume did not restore the same grant", reply)
		}
		if c.routes["host-a.example.ts.net"] != "100.64.0.1" {
			t.Fatal("resume changed frozen destinations")
		}
	})
	t.Run("pending", func(t *testing.T) {
		c, _, _ := relayFixture(t, false)
		if r := c.update(request{Operation: "pause", Generation: c.w.Generation}); r.Error != "" {
			t.Fatal(r.Error)
		}
		if r := c.update(request{Operation: "resume", Generation: c.w.Generation}); r.Error != "" {
			t.Fatal(r.Error)
		}
		if c.w.Admission || c.admissionValid() {
			t.Fatal("resume admitted before enrollment/trust")
		}
	})
	t.Run("cancelled", func(t *testing.T) {
		c, _, _ := relayFixture(t, true)
		if r := c.update(request{Operation: "resume", Generation: "old"}); r.Error == "" {
			t.Fatal("old generation resumed admission")
		}
		c.now = func() time.Time { return c.w.Deadline.Add(time.Second) }
		if r := c.update(request{Operation: "resume", Generation: c.w.Generation}); r.Error == "" {
			t.Fatal("expired window resumed admission")
		}
	})
}
