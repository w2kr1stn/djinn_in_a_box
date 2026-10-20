package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

func connectControl(t *testing.T, handle func(request) response, accepted ...*atomic.Int32) string {
	t.Helper()
	return connectControlServer(t, handle, nil, accepted...)
}

func connectControlServer(t *testing.T, handle func(request) response, eof chan<- struct{}, accepted ...*atomic.Int32) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "dh")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "c")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			if len(accepted) != 0 {
				accepted[0].Add(1)
			}
			go func() {
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
				var r request
				if json.NewDecoder(conn).Decode(&r) == nil {
					if r.Operation != "status" || r.Generation == "" {
						t.Errorf("unexpected preflight: %+v", r)
					}
					_ = json.NewEncoder(conn).Encode(handle(r))
					if eof != nil {
						var data [1]byte
						if _, err := conn.Read(data[:]); err == io.EOF {
							eof <- struct{}{}
						}
					}
				}
			}()
		}
	}()
	return path
}

func connectTCP(t *testing.T, handle func(net.Conn)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var accepted net.Conn
	closed := false
	t.Cleanup(func() {
		_ = listener.Close()
		mu.Lock()
		defer mu.Unlock()
		closed = true
		if accepted != nil {
			_ = accepted.Close()
		}
	})
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		mu.Lock()
		accepted = conn
		if closed {
			mu.Unlock()
			return
		}
		mu.Unlock()
		_ = conn.SetDeadline(time.Now().Add(2 * time.Second))
		handle(conn)
	}()
	return listener.Addr().String()
}

func connectTestEnv(t *testing.T, c *controller) *atomic.Int32 {
	t.Helper()
	oldPath, oldDial := controlSocketPath, dialRelay
	oldTimeout, oldGrace := setupTimeout, stdinEOFGrace
	t.Cleanup(func() {
		controlSocketPath, dialRelay = oldPath, oldDial
		setupTimeout, stdinEOFGrace = oldTimeout, oldGrace
	})
	setupTimeout, stdinEOFGrace = 300*time.Millisecond, 80*time.Millisecond
	controlSocketPath = connectControl(t, c.update)
	calls := &atomic.Int32{}
	dialRelay = func(string, time.Time) (net.Conn, error) {
		calls.Add(1)
		return nil, syscall.ECONNREFUSED
	}
	return calls
}

func connectAdmitted(t *testing.T) *controller {
	t.Helper()
	c := fixture(t)
	if err := persist(c.path, c.w); err != nil {
		t.Fatal(err)
	}
	if reply := c.update(request{Operation: "admit", Generation: c.w.Generation,
		Routes: map[string]string{"host-a.example.ts.net": "100.64.0.1"}}); reply.Error != "" {
		t.Fatal(reply.Error)
	}
	return c
}

func connectTo(addr string) func(string, time.Time) (net.Conn, error) {
	return func(_ string, deadline time.Time) (net.Conn, error) {
		return net.DialTimeout("tcp", addr, time.Until(deadline))
	}
}

func connectResult(t *testing.T, args []string, input io.Reader, want string) string {
	t.Helper()
	output := &lockedLog{}
	done := make(chan error, 1)
	go func() { done <- connect(args, input, output) }()
	select {
	case err := <-done:
		got := ""
		if err != nil {
			got = err.Error() + "\n"
		}
		if got != want {
			t.Fatalf("diagnostic = %q, want %q", got, want)
		}
		if want != "" && output.String() != "" {
			t.Fatalf("refusal stdout = %q, want empty", output.String())
		}
		return output.String()
	case <-time.After(900 * time.Millisecond):
		t.Fatal("connect watchdog: did not return")
		return ""
	}
}

func connectArgs(c *controller) []string { return []string{c.w.Generation, "host-a.example.ts.net"} }

func TestConnectDefaults(t *testing.T) {
	if setupTimeout != 10*time.Second || stdinEOFGrace != 2*time.Second || controlSocketPath != socketPath {
		t.Fatal("production setup, grace or control socket default changed")
	}
}

func TestConnectGenerationRefusal(t *testing.T) {
	for _, name := range []string{"server-error", "returned-generation"} {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			calls := connectTestEnv(t, c)
			args := connectArgs(c)
			if name == "server-error" {
				args[0] = "old-generation"
			} else {
				controlSocketPath = connectControl(t, func(request) response {
					w := c.w
					w.Generation = "other-generation"
					return response{Window: &w}
				})
			}
			connectResult(t, args, strings.NewReader(""), "hostctl connect: generation changed\n")
			if calls.Load() != 0 {
				t.Fatal("gated-dial count must be 0")
			}
		})
	}
}

func TestConnectWindowRefusal(t *testing.T) {
	for _, name := range []string{"not-admitted", "paused", "closed", "utc-expired", "boot-expired", "wrong-boot", "missing-disk", "corrupt-disk", "mismatched-disk", "invalid-before-generation"} {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			calls := connectTestEnv(t, c)
			args := connectArgs(c)
			c.mu.Lock()
			switch name {
			case "not-admitted":
				c.w.Admission = false
			case "paused":
				c.w.Paused = true
			case "closed", "invalid-before-generation":
				c.w.Closed = true
				if name == "invalid-before-generation" {
					args[0] = "old-generation"
				}
			case "utc-expired":
				c.w.Deadline = c.now()
			case "boot-expired":
				c.w.BootDeadline = c.boot()
			case "wrong-boot":
				c.w.BootID = "other-boot"
			}
			if err := persist(c.path, c.w); err != nil {
				t.Fatal(err)
			}
			c.mu.Unlock()
			switch name {
			case "missing-disk":
				_ = os.Remove(c.path)
			case "corrupt-disk":
				_ = os.WriteFile(c.path, []byte("{"), 0600)
			case "mismatched-disk":
				w := c.w
				w.Generation = "other-generation"
				_ = persist(c.path, w)
			}
			connectResult(t, args, strings.NewReader(""), "hostctl connect: window not open\n")
			if calls.Load() != 0 {
				t.Fatal("gated-dial count must be 0")
			}
		})
	}
}

func TestConnectRelayRefusal(t *testing.T) {
	for _, name := range []string{"undeclared", "pause-after-preflight", "expiry-after-preflight", "REP4"} {
		t.Run(name, func(t *testing.T) {
			c, addr, _ := relayFixture(t, true)
			connectTestEnv(t, c)
			args := connectArgs(c)
			want := "hostctl connect: relay refused destination\n"
			switch name {
			case "undeclared":
				args[1] = "undeclared.example.ts.net"
			case "REP4":
				addr = connectTCP(t, func(conn net.Conn) {
					if _, _, err := readDestination(conn); err == nil {
						socksReply(conn, 4)
					}
				})
				want = "hostctl connect: relay could not reach destination\n"
			}
			dialRelay = func(_ string, deadline time.Time) (net.Conn, error) {
				c.mu.Lock()
				if name == "pause-after-preflight" {
					c.w.Paused = true
					_ = persist(c.path, c.w)
				}
				if name == "expiry-after-preflight" {
					c.w.Deadline = c.now()
					_ = persist(c.path, c.w)
				}
				c.mu.Unlock()
				return connectTo(addr)("", deadline)
			}
			connectResult(t, args, strings.NewReader(""), want)
		})
	}
}

func TestConnectSetupFailure(t *testing.T) {
	for _, name := range []string{"missing-control", "malformed-JSON", "nil-ack", "unknown-error", "state-error", "dial-refused"} {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			calls := connectTestEnv(t, c)
			want := "hostctl connect: setup failed\n"
			switch name {
			case "missing-control":
				controlSocketPath += "missing"
			case "malformed-JSON":
				// JSON array is not a control response.
				controlSocketPath = connectUnixBytes(t, "[]\n")
			case "nil-ack":
				controlSocketPath = connectControl(t, func(request) response { return response{} })
			case "unknown-error", "state-error":
				message := "invalid request"
				if name == "state-error" {
					message = "window state is unavailable or changed"
					want = "hostctl connect: window not open\n"
				}
				controlSocketPath = connectControl(t, func(request) response { return response{Window: &c.w, Error: message} })
			}
			connectResult(t, connectArgs(c), strings.NewReader("must not copy"), want)
			if name != "dial-refused" && calls.Load() != 0 {
				t.Fatal("gated-dial count must be 0")
			}
		})
	}
	for _, selector := range []string{"", strings.Repeat("a", 256), "a\x00b", "a b", "a\tb", "a\nb", "a\x01b"} {
		t.Run("selector-"+selector, func(t *testing.T) {
			c := connectAdmitted(t)
			calls := connectTestEnv(t, c)
			connectResult(t, []string{c.w.Generation, selector}, strings.NewReader(""), "hostctl connect: setup failed\n")
			if calls.Load() != 0 {
				t.Fatal("invalid selector reached dial")
			}
		})
	}
	for _, args := range [][]string{nil, {"gen"}, {"gen", "host", "22"}, {"", "host"}} {
		t.Run("argv-"+strings.Join(args, "-"), func(t *testing.T) {
			c := connectAdmitted(t)
			calls := connectTestEnv(t, c)
			connectResult(t, args, strings.NewReader(""), "hostctl connect: setup failed\n")
			if calls.Load() != 0 {
				t.Fatal("invalid argv reached dial")
			}
		})
	}
}

func connectUnixBytes(t *testing.T, data string) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "dh")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	path := filepath.Join(dir, "c")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		var r request
		_ = json.NewDecoder(conn).Decode(&r)
		_, _ = io.WriteString(conn, data)
	}()
	return path
}

func TestConnectMalformedSOCKS(t *testing.T) {
	cases := map[string][]byte{
		"greeting-version": {4, 0, 5, 0, 0, 1, 0, 0, 0, 0, 0, 0}, "greeting-refused": {5, 255, 5, 0, 0, 1, 0, 0, 0, 0, 0, 0}, "greeting-short": {5},
		"reply-version":  {5, 0, 4, 0, 0, 1, 0, 0, 0, 0, 0, 0},
		"reply-reserved": {5, 0, 5, 0, 1, 1, 0, 0, 0, 0, 0, 0},
		"reply-atyp":     {5, 0, 5, 0, 0, 9, 0, 22}, "reply-short": {5, 0, 5, 0, 0, 1, 0},
		"reply-empty-domain": {5, 0, 5, 0, 0, 3, 0, 0, 0},
		"refusal-malformed":  {5, 0, 4, 2, 0, 1, 0, 0, 0, 0, 0, 0},
		"refusal-short":      {5, 0, 5, 2, 0, 1, 0},
	}
	for name, reply := range cases {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			addr := connectTCP(t, func(conn net.Conn) {
				var greeting [3]byte
				_, _ = io.ReadFull(conn, greeting[:])
				_, _ = conn.Write(reply[:min(2, len(reply))])
				if len(reply) > 2 {
					var frame [4]byte
					if _, err := io.ReadFull(conn, frame[:]); err != nil {
						return
					}
					var length [1]byte
					if _, err := io.ReadFull(conn, length[:]); err != nil {
						return
					}
					if _, err := io.CopyN(io.Discard, conn, int64(length[0])+2); err != nil {
						return
					}
					_, _ = conn.Write(reply[2:])
				}
			})
			dialRelay = connectTo(addr)
			connectResult(t, connectArgs(c), strings.NewReader(""), "hostctl connect: setup failed\n")
		})
	}
}

func TestSOCKSSelectorEncoding(t *testing.T) {
	for _, selector := range []string{"host-a.example.ts.net", "HOST-A.Example.TS.NET", "100.64.0.1", "fd7a:115c:a1e0::1", strings.Repeat("a", 255)} {
		t.Run(selector, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			_ = client.SetDeadline(time.Now().Add(time.Second))
			done := make(chan error, 1)
			go func() {
				defer server.Close()
				var greeting [3]byte
				_, err := io.ReadFull(server, greeting[:])
				if err != nil || greeting != [3]byte{5, 1, 0} {
					done <- errors.New("greeting encoding changed")
					return
				}
				_, _ = server.Write([]byte{5})
				_, _ = server.Write([]byte{0})
				expected := []byte{5, 1, 0}
				if ip := net.ParseIP(selector); ip != nil {
					if v4 := ip.To4(); v4 != nil {
						expected = append(expected, append([]byte{1}, v4...)...)
					} else {
						expected = append(expected, append([]byte{4}, ip.To16()...)...)
					}
				} else {
					expected = append(expected, 3, byte(len(selector)))
					expected = append(expected, []byte(selector)...)
				}
				expected = append(expected, 0, 22)
				frame := make([]byte, len(expected))
				_, err = io.ReadFull(server, frame)
				if err != nil || !bytes.Equal(frame, expected) {
					done <- errors.New("selector, ATYP or port differs from literal input")
					return
				}
				for _, reply := range []byte{5, 0, 0, 3, 3, 'b', 'n', 'd', 0, 22} {
					_, _ = server.Write([]byte{reply})
				}
				_, _ = server.Write([]byte("payload"))
				done <- nil
			}()
			if err := socksConnect(client, selector); err != nil {
				t.Fatal(err)
			}
			payload, _ := io.ReadAll(client)
			if string(payload) != "payload" {
				t.Fatalf("reply framing leaked into payload: %q", payload)
			}
			if err := <-done; err != nil {
				t.Fatal(err)
			}
		})
	}
	for _, selector := range []string{"", strings.Repeat("a", 256), "a\x00b", "a b", "a\tb", "a\nb", "a\x01b", "a\u00a0b"} {
		t.Run("invalid-"+selector, func(t *testing.T) {
			if _, err := selectorAddress(selector); err == nil {
				t.Fatal("invalid selector accepted")
			}
		})
	}
}

type connectBlockingReader struct{ release chan struct{} }

func (r *connectBlockingReader) Read([]byte) (int, error) { <-r.release; return 0, io.EOF }
func (r *connectBlockingReader) Close() error             { return nil }

func connectBlockedInput(t *testing.T, kind string) io.Reader {
	t.Helper()
	if kind == "pipe" {
		r, w, err := os.Pipe()
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = w.Close(); _ = r.Close() })
		return r
	}
	r := &connectBlockingReader{release: make(chan struct{})}
	t.Cleanup(func() { close(r.release) })
	return r
}

func TestConnectStdoutOnlyPayload(t *testing.T) {
	c, addr, _ := relayFixture(t, true)
	connectTestEnv(t, c)
	dialRelay = connectTo(addr)
	payload := "SSH-2.0-OpenSSH_9.2p1\r\n\x00\xffopaque stream"
	if got := connectResult(t, connectArgs(c), strings.NewReader(payload), ""); got != payload {
		t.Fatalf("stdout = %q, want payload %q", got, payload)
	}
}

func TestConnectHalfClose(t *testing.T) {
	c := connectAdmitted(t)
	connectTestEnv(t, c)
	received := make(chan string, 1)
	addr := connectTCP(t, func(conn net.Conn) {
		_, _, _ = readDestination(conn)
		socksReply(conn, 0)
		data, err := io.ReadAll(conn)
		if err != nil {
			received <- err.Error()
			return
		}
		received <- string(data)
		time.Sleep(15 * time.Millisecond)
		_, _ = io.WriteString(conn, "final bytes after FIN")
	})
	dialRelay = connectTo(addr)
	if got := connectResult(t, connectArgs(c), strings.NewReader("input payload"), ""); got != "final bytes after FIN" {
		t.Fatalf("half-close lost final response: %q", got)
	}
	if got := <-received; got != "input payload" {
		t.Fatalf("upstream input = %q", got)
	}
}

func TestConnectReturnsWithoutStdinEOF(t *testing.T) {
	for _, kind := range []string{"ignores-close", "pipe"} {
		t.Run(kind, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			addr := connectTCP(t, func(conn net.Conn) {
				_, _, _ = readDestination(conn)
				socksReply(conn, 0)
				_, _ = io.WriteString(conn, "remote EOF")
			})
			dialRelay = connectTo(addr)
			if got := connectResult(t, connectArgs(c), connectBlockedInput(t, kind), ""); got != "remote EOF" {
				t.Fatalf("stdout = %q", got)
			}
		})
	}
}

func TestConnectSilentUpstreamEndsAfterGrace(t *testing.T) {
	for _, kind := range []string{"reader", "pipe"} {
		t.Run(kind, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			release := make(chan struct{})
			t.Cleanup(func() { close(release) })
			addr := connectTCP(t, func(conn net.Conn) {
				_, _, _ = readDestination(conn)
				socksReply(conn, 0)
				_, _ = io.Copy(io.Discard, conn)
				<-release // A target may ignore FIN and never send another byte.
			})
			dialRelay = connectTo(addr)
			var input io.Reader = strings.NewReader("")
			if kind == "pipe" {
				r, w, err := os.Pipe()
				if err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { _ = r.Close() })
				_ = w.Close()
				input = r
			}
			start := time.Now()
			connectResult(t, connectArgs(c), input, "")
			elapsed := time.Since(start)
			if elapsed < stdinEOFGrace || elapsed > stdinEOFGrace+250*time.Millisecond {
				t.Fatalf("EOF drain duration %v, grace %v", elapsed, stdinEOFGrace)
			}
		})
	}
}

func TestConnectResetAfterAdmission(t *testing.T) {
	c := connectAdmitted(t)
	connectTestEnv(t, c)
	addr := connectTCP(t, func(conn net.Conn) {
		_, _, _ = readDestination(conn)
		socksReply(conn, 0)
		_ = conn.(*net.TCPConn).SetLinger(0)
	})
	dialRelay = connectTo(addr)
	connectResult(t, connectArgs(c), connectBlockedInput(t, "ignores-close"), "")
}

type connectDeadlineConn struct {
	net.Conn
	deadline  time.Time
	cleared   atomic.Bool
	failSet   bool
	failClear bool
}

func (c *connectDeadlineConn) SetDeadline(deadline time.Time) error {
	if deadline.IsZero() {
		if c.failClear {
			return syscall.EIO
		}
		c.cleared.Store(true)
	} else {
		if c.failSet {
			return syscall.EIO
		}
		if !deadline.Equal(c.deadline) {
			return errors.New("setup deadline changed between stages")
		}
	}
	return c.Conn.SetDeadline(deadline)
}

func (c *connectDeadlineConn) CloseWrite() error {
	return c.Conn.(*net.TCPConn).CloseWrite()
}

type connectCheckedReader struct {
	conn *connectDeadlineConn
	read atomic.Bool
}

func (r *connectCheckedReader) Read([]byte) (int, error) {
	r.read.Store(true)
	if !r.conn.cleared.Load() {
		return 0, errors.New("copy began before clearing deadline")
	}
	return 0, io.EOF
}

func TestConnectClearsDeadline(t *testing.T) {
	for _, name := range []string{"before-copy", "clear-error", "set-error", "beyond-budget"} {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			setupTimeout = 50 * time.Millisecond
			addr := connectTCP(t, func(conn net.Conn) {
				_, _, err := readDestination(conn)
				if err != nil {
					return
				}
				socksReply(conn, 0)
				if name == "beyond-budget" {
					time.Sleep(100 * time.Millisecond)
				} else {
					_, _ = io.Copy(io.Discard, conn)
				}
				_, _ = io.WriteString(conn, "after deadline")
			})
			var wrapped *connectDeadlineConn
			reader := &connectCheckedReader{}
			dialRelay = func(_ string, deadline time.Time) (net.Conn, error) {
				conn, err := connectTo(addr)("", deadline)
				if err != nil {
					return nil, err
				}
				wrapped = &connectDeadlineConn{Conn: conn, deadline: deadline, failSet: name == "set-error", failClear: name == "clear-error"}
				reader.conn = wrapped
				return wrapped, nil
			}
			var input io.Reader = reader
			if name == "beyond-budget" {
				input = connectBlockedInput(t, "ignores-close")
			}
			want := ""
			if name == "clear-error" || name == "set-error" {
				want = "hostctl connect: setup failed\n"
			}
			got := connectResult(t, connectArgs(c), input, want)
			if want != "" {
				if reader.read.Load() {
					t.Fatal("copy started after SetDeadline failed")
				}
			} else if got != "after deadline" || !wrapped.cleared.Load() {
				t.Fatalf("deadline not cleared before copying: stdout %q", got)
			}
		})
	}
}

func TestConnectSetupTimeout(t *testing.T) {
	for _, name := range []string{"unix-dial", "control-reply", "tcp-dial", "SOCKS-reply", "SOCKS-greeting", "shared-budget"} {
		t.Run(name, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			setupTimeout = 60 * time.Millisecond
			started := time.Now()
			accepted := &atomic.Int32{}
			switch name {
			case "unix-dial":
				setupTimeout = -time.Second
				controlSocketPath = connectControl(t, c.update, accepted)
			case "control-reply":
				controlSocketPath = connectControl(t, func(r request) response {
					time.Sleep(120 * time.Millisecond)
					return c.update(r)
				})
			case "tcp-dial":
				dialRelay = func(_ string, deadline time.Time) (net.Conn, error) {
					if deadline.Before(started) || deadline.After(started.Add(setupTimeout+10*time.Millisecond)) {
						t.Error("not the one overall setup deadline")
					}
					return nil, &net.OpError{Op: "dial", Net: "tcp", Err: os.ErrDeadlineExceeded}
				}
			case "SOCKS-reply", "SOCKS-greeting", "shared-budget":
				if name == "shared-budget" {
					controlSocketPath = connectControl(t, func(r request) response {
						time.Sleep(30 * time.Millisecond)
						return c.update(r)
					})
				}
				addr := connectTCP(t, func(conn net.Conn) {
					if name == "SOCKS-greeting" {
						_, _ = io.Copy(io.Discard, conn)
						return
					}
					_, _, _ = readDestination(conn)
					_, _ = io.Copy(io.Discard, conn)
				})
				dialRelay = func(_ string, deadline time.Time) (net.Conn, error) {
					if deadline.Before(started) || deadline.After(started.Add(setupTimeout+10*time.Millisecond)) {
						t.Error("not the one overall setup deadline")
					}
					if name == "shared-budget" && time.Until(deadline) > 45*time.Millisecond {
						t.Error("control time did not consume setup budget")
					}
					conn, err := connectTo(addr)("", deadline)
					if err != nil {
						return nil, err
					}
					return &connectDeadlineConn{Conn: conn, deadline: deadline}, nil
				}
			}
			connectResult(t, connectArgs(c), strings.NewReader("no copying"), "hostctl connect: setup timed out\n")
			if name == "unix-dial" {
				time.Sleep(100 * time.Millisecond)
				if accepted.Load() != 0 {
					t.Fatal("expired setup budget still connected to control socket")
				}
			}
		})
	}
}

func TestConnectTargetsGatedRelay(t *testing.T) {
	c, addr, _ := relayFixture(t, true)
	connectTestEnv(t, c)
	var target string
	dialRelay = func(got string, deadline time.Time) (net.Conn, error) {
		target = got
		return connectTo(addr)(got, deadline)
	}
	connectResult(t, connectArgs(c), strings.NewReader(""), "")
	if target != "127.0.0.1:1080" || target == rawSocksAddr {
		t.Fatalf("production connect dial target %q bypasses gated relay", target)
	}
	if connectRelayAddr != "127.0.0.1"+relayListen {
		t.Fatal("connect address is not derived from relay listen address")
	}
}

func TestConnectServerOwnsValidity(t *testing.T) {
	c, addr, _ := relayFixture(t, true)
	connectTestEnv(t, c)
	controlSocketPath = connectControl(t, func(request) response {
		return response{Window: &window{Generation: c.w.Generation, Admission: true, Closed: true}}
	})
	dialRelay = connectTo(addr)
	if got := connectResult(t, connectArgs(c), strings.NewReader("payload"), ""); got != "payload" {
		t.Fatal("client added a local clock/validity check")
	}
}

func TestConnectDispatchProcess(t *testing.T) {
	if os.Getenv("DJINN_TEST_CONNECT_PROCESS") != "1" {
		return
	}
	controlSocketPath = os.Getenv("DJINN_TEST_CONTROL_SOCKET")
	dialRelay = connectTo(os.Getenv("DJINN_TEST_RELAY"))
	os.Args = append([]string{"supervisor"}, strings.Split(os.Getenv("DJINN_TEST_ARGS"), "|")...)
	main()
	os.Exit(0)
}

func TestConnectDispatchDoesNotUseJSONClient(t *testing.T) {
	c, addr, _ := relayFixture(t, true)
	connectTestEnv(t, c)
	for _, extra := range []bool{false, true} {
		t.Run(map[bool]string{false: "stream", true: "third-operand"}[extra], func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestConnectDispatchProcess$")
			args := "connect|" + c.w.Generation + "|host-a.example.ts.net"
			if extra {
				args += "|22"
			}
			cmd.Env = append(os.Environ(), "DJINN_TEST_CONNECT_PROCESS=1", "DJINN_TEST_CONTROL_SOCKET="+controlSocketPath,
				"DJINN_TEST_RELAY="+addr, "DJINN_TEST_ARGS="+args)
			cmd.Stdin = strings.NewReader("SSH stream only")
			var stdout, stderr bytes.Buffer
			cmd.Stdout, cmd.Stderr = &stdout, &stderr
			err := cmd.Run()
			if extra {
				var exit *exec.ExitError
				if !errors.As(err, &exit) || exit.ExitCode() != 1 || stderr.String() != "hostctl connect: setup failed\n" || stdout.Len() != 0 {
					t.Fatalf("third operand: err=%v stdout=%q stderr=%q", err, stdout.String(), stderr.String())
				}
			} else if err != nil || stdout.String() != "SSH stream only" || stderr.Len() != 0 {
				t.Fatalf("stream dispatch: err=%v stdout=%q stderr=%q", err, stdout.String(), stderr.String())
			}
		})
	}
}

func TestSOCKSReplyFraming(t *testing.T) {
	for _, bound := range [][]byte{{1, 1, 2, 3, 4}, append([]byte{4}, net.ParseIP("fd7a:115c:a1e0::1").To16()...), {3, 3, 'b', 'n', 'd'}} {
		t.Run(string(rune('0'+bound[0])), func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			_ = client.SetDeadline(time.Now().Add(time.Second))
			go func() {
				defer server.Close()
				_, _, _ = readDestination(server)
				reply := append([]byte{5, 0, 0}, bound...)
				reply = append(reply, 0, 22)
				for _, b := range reply {
					_, _ = server.Write([]byte{b})
				}
				_, _ = io.WriteString(server, "opaque")
			}()
			if err := socksConnect(client, "host-a.example.ts.net"); err != nil {
				t.Fatal(err)
			}
			data, _ := io.ReadAll(client)
			if string(data) != "opaque" {
				t.Fatalf("bound-address bytes leaked into stream: %q", data)
			}
		})
	}
}

func TestRawConnectErrorWording(t *testing.T) {
	for _, row := range []struct {
		reply []byte
		want  string
	}{
		{[]byte{5, 255}, "raw SOCKS greeting refused"},
		{[]byte{5, 0, 5, 4, 0, 1, 0, 0, 0, 0, 0, 0}, "raw SOCKS CONNECT refused"},
		{[]byte{5, 0, 4, 0, 0, 1, 0, 0, 0, 0, 0, 0}, "raw SOCKS CONNECT refused"},
		{[]byte{5, 0, 5, 0, 0, 9}, "malformed raw SOCKS response"},
	} {
		t.Run(row.want, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			_ = client.SetDeadline(time.Now().Add(time.Second))
			go func() {
				defer server.Close()
				var greeting [3]byte
				_, _ = io.ReadFull(server, greeting[:])
				_, _ = server.Write(row.reply[:2])
				if len(row.reply) > 2 {
					var frame [10]byte
					_, _ = io.ReadFull(server, frame[:])
					_, _ = server.Write(row.reply[2:])
				}
			}()
			if err := rawConnect(client, "100.64.0.1"); err == nil || err.Error() != row.want {
				t.Fatalf("raw error = %v, want %q", err, row.want)
			}
		})
	}
}

type connectErrorReader struct{}

func (connectErrorReader) Read([]byte) (int, error) { return 0, syscall.EIO }

type connectOutput struct {
	lockedLog
	closed atomic.Bool
	fail   bool
}

func (w *connectOutput) Write(data []byte) (int, error) {
	if w.fail {
		return 0, syscall.EIO
	}
	return w.lockedLog.Write(data)
}
func (w *connectOutput) CloseWrite() error { w.closed.Store(true); return nil }

func TestConnectCopyTermination(t *testing.T) {
	for _, kind := range []string{"input-error", "output-error", "stdout-half-close"} {
		t.Run(kind, func(t *testing.T) {
			c := connectAdmitted(t)
			connectTestEnv(t, c)
			peerDone := make(chan struct{})
			release := make(chan struct{})
			t.Cleanup(func() { close(release) })
			addr := connectTCP(t, func(conn net.Conn) {
				_, _, _ = readDestination(conn)
				socksReply(conn, 0)
				if kind != "input-error" {
					_, _ = io.WriteString(conn, "output")
					_ = conn.(*net.TCPConn).CloseWrite()
				}
				_, _ = io.Copy(io.Discard, conn)
				close(peerDone)
				if kind == "input-error" {
					<-release
				}
			})
			dialRelay = connectTo(addr)
			input := connectBlockedInput(t, "ignores-close")
			if kind == "input-error" {
				input = connectErrorReader{}
			}
			output := &connectOutput{fail: kind == "output-error"}
			done := make(chan error, 1)
			go func() { done <- connect(connectArgs(c), input, output) }()
			select {
			case err := <-done:
				if err != nil {
					t.Fatalf("admitted stream returned diagnostic: %v", err)
				}
			case <-time.After(900 * time.Millisecond):
				t.Fatal("copy error or remote EOF did not end connect")
			}
			select {
			case <-peerDone:
			case <-time.After(300 * time.Millisecond):
				t.Fatal("stream termination did not close upstream")
			}
			if kind == "stdout-half-close" && (!output.closed.Load() || output.String() != "output") {
				t.Fatal("upstream EOF did not half-close stdout after payload")
			}
		})
	}
}

func TestConnectDefaultDial(t *testing.T) {
	addr := connectTCP(t, func(conn net.Conn) { _, _ = io.Copy(io.Discard, conn) })
	conn, err := dialRelay(addr, time.Now().Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	conn, err = dialRelay(addr, time.Now().Add(-time.Second))
	if conn != nil {
		_ = conn.Close()
	}
	var timeout net.Error
	if !errors.As(err, &timeout) || !timeout.Timeout() {
		t.Fatalf("default dial ignored expired overall deadline: %v", err)
	}
}

func TestConnectReleasesControlBeforeDial(t *testing.T) {
	c := connectAdmitted(t)
	connectTestEnv(t, c)
	eof := make(chan struct{}, 1)
	controlSocketPath = connectControlServer(t, c.update, eof)
	dialRelay = func(string, time.Time) (net.Conn, error) {
		select {
		case <-eof:
		case <-time.After(200 * time.Millisecond):
			t.Error("control socket still open at relay dial")
		}
		return nil, syscall.ECONNREFUSED
	}
	connectResult(t, connectArgs(c), strings.NewReader(""), "hostctl connect: setup failed\n")
}

type connectHalfCloseRecorder struct {
	net.Conn
	writes atomic.Int32
}

func (c *connectHalfCloseRecorder) CloseWrite() error {
	c.writes.Add(1)
	return c.Conn.(*net.TCPConn).CloseWrite()
}

func TestConnectHalfCloseCallsCloseWrite(t *testing.T) {
	c := connectAdmitted(t)
	connectTestEnv(t, c)
	addr := connectTCP(t, func(conn net.Conn) {
		_, _, err := readDestination(conn)
		if err != nil {
			return
		}
		socksReply(conn, 0)
		_, _ = io.Copy(io.Discard, conn)
		_, _ = io.WriteString(conn, "response after input EOF")
	})
	var recorded *connectHalfCloseRecorder
	dialRelay = func(_ string, deadline time.Time) (net.Conn, error) {
		conn, err := connectTo(addr)("", deadline)
		if err != nil {
			return nil, err
		}
		recorded = &connectHalfCloseRecorder{Conn: conn}
		return recorded, nil
	}
	got := connectResult(t, connectArgs(c), strings.NewReader(""), "")
	if recorded.writes.Load() != 1 {
		t.Fatalf("stdin EOF called CloseWrite %d times, want 1", recorded.writes.Load())
	}
	if got != "response after input EOF" {
		t.Fatalf("half-close lost final bytes: %q", got)
	}
}
