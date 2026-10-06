package main

import (
	"encoding/json"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func fixture(t *testing.T) *controller {
	t.Helper()
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	return &controller{w: window{Generation: "generation-a", BootID: "boot-a",
		Deadline: now.Add(10 * time.Minute), BootDeadline: int64(10 * time.Minute)},
		path: filepath.Join(t.TempDir(), "window.json"), now: func() time.Time { return now },
		boot: func() int64 { return 0 }, bootID: "boot-a"}
}

func TestClockBoundaries(t *testing.T) {
	c := fixture(t)
	if !c.w.valid(c.now(), 0, "boot-a") {
		t.Fatal("valid window refused")
	}
	for _, tc := range []struct {
		name string
		now  time.Time
		boot int64
		id   string
	}{
		{"UTC", c.w.Deadline, 0, "boot-a"},
		{"boottime", c.now().Add(-time.Hour), c.w.BootDeadline, "boot-a"},
		{"suspend", c.now(), c.w.BootDeadline + int64(time.Hour), "boot-a"},
		{"reboot", c.now(), 0, "boot-b"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if c.w.valid(tc.now, tc.boot, tc.id) {
				t.Fatal("expired/reboot window accepted")
			}
		})
	}
	c.w.Closed = true
	if c.w.valid(c.now(), 0, "boot-a") {
		t.Fatal("closed window accepted")
	}
	c.w.Closed = false
	c.w.Generation = ""
	if c.w.valid(c.now(), 0, "boot-a") {
		t.Fatal("missing generation accepted")
	}
}

func TestDeadlineResetPersistenceAndAck(t *testing.T) {
	c := fixture(t)
	c.boot = func() int64 { return int64(time.Minute) }
	r := c.update(request{Operation: "set-deadline", Generation: "generation-a", Minutes: 2})
	if r.Error != "" || r.Window == nil {
		t.Fatalf("missing ack: %+v", r)
	}
	if !r.Window.Deadline.Equal(c.now().Add(2*time.Minute)) || r.Window.BootDeadline != int64(3*time.Minute) {
		t.Fatalf("deadline was not reset: %+v", r.Window)
	}
	var saved window
	data, err := os.ReadFile(c.path)
	if err != nil || json.Unmarshal(data, &saved) != nil || saved != *r.Window {
		t.Fatal("ack differs from persisted state")
	}
	info, err := os.Stat(c.path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatal("state is not private")
	}
}

func TestUpdaterRefusals(t *testing.T) {
	for _, tc := range []struct {
		name    string
		change  func(*controller)
		request request
	}{
		{"old-generation", func(c *controller) {}, request{"set-deadline", "old", 1}},
		{"over-max", func(c *controller) {}, request{"set-deadline", "generation-a", 1441}},
		{"zero", func(c *controller) {}, request{"set-deadline", "generation-a", 0}},
		{"closed", func(c *controller) { c.w.Closed = true }, request{"set-deadline", "generation-a", 1}},
		{"expired", func(c *controller) { c.boot = func() int64 { return c.w.BootDeadline } }, request{"set-deadline", "generation-a", 1}},
		{"operation", func(c *controller) {}, request{"open", "generation-a", 1}},
		{"write-failure", func(c *controller) { c.path = filepath.Join(t.TempDir(), "missing/window.json") }, request{"set-deadline", "generation-a", 1}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := fixture(t)
			tc.change(c)
			old := c.w
			if r := c.update(tc.request); r.Error == "" || r.Window != nil {
				t.Fatal("refusal acknowledged success")
			}
			if c.w != old {
				t.Fatal("failed update changed authoritative state")
			}
		})
	}
}

func TestCloseUpdateRaceNeverResurrects(t *testing.T) {
	c := fixture(t)
	var wg sync.WaitGroup
	wg.Add(2)
	go func() { defer wg.Done(); _ = c.close("expiry") }()
	go func() { defer wg.Done(); c.update(request{"set-deadline", "generation-a", 1}) }()
	wg.Wait()
	if !c.w.Closed {
		t.Fatal("racing update reopened window")
	}
	if r := c.update(request{"set-deadline", "generation-a", 1}); r.Error == "" {
		t.Fatal("closed generation acknowledged")
	}
}

func TestIPCGenerationAndAcknowledgement(t *testing.T) {
	c := fixture(t)
	path := filepath.Join(t.TempDir(), "control.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	go serve(listener, c)
	conn, err := net.Dial("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(time.Second))
	if err = json.NewEncoder(conn).Encode(request{"set-deadline", "generation-a", 1}); err != nil {
		t.Fatal(err)
	}
	var reply response
	if err = json.NewDecoder(conn).Decode(&reply); err != nil {
		t.Fatal(err)
	}
	if reply.Window == nil || reply.Error != "" || reply.Window.Generation != "generation-a" {
		t.Fatalf("invalid IPC ack: %+v", reply)
	}
}

func TestLogRedaction(t *testing.T) {
	if authURL.ReplaceAllString("visit https://login.tailscale.com/a/example", "[URL redacted]") != "visit [URL redacted]" {
		t.Fatal("login URL leaked")
	}
}

func TestMonitorProcess(t *testing.T) {
	if mode := os.Getenv("DJINN_TEST_CLOCK"); mode != "" {
		c := fixture(t)
		if mode == "utc" {
			c.w.Deadline = c.now()
		} else {
			c.w.BootDeadline = 0
		}
		if reason := monitor(c, make(chan error), make(chan os.Signal)); reason != "expiry" || !c.w.Closed {
			os.Exit(2)
		}
		if r := c.update(request{"set-deadline", "generation-a", 1}); r.Error == "" {
			os.Exit(3)
		}
		os.Exit(0)
	}
	for _, mode := range []string{"utc", "boot"} {
		child := exec.Command(os.Args[0], "-test.run=^TestMonitorProcess$")
		child.Env = append(os.Environ(), "DJINN_TEST_CLOCK="+mode)
		done := make(chan error, 1)
		if err := child.Start(); err != nil {
			t.Fatal(err)
		}
		go func() { done <- child.Wait() }()
		select {
		case err := <-done:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(2 * time.Second):
			_ = child.Process.Kill()
			<-done
			t.Fatal("PID 1 timer failed to exit independently")
		}
	}
}

func TestRestartState(t *testing.T) {
	c := fixture(t)
	if err := initialize(c.path, c.w); err != nil {
		t.Fatal(err)
	}
	if err := initialize(c.path, c.w); err == nil {
		t.Fatal("restart reopened prior generation")
	}
	replacement := c.w
	replacement.Generation = "generation-b"
	if err := initialize(c.path, replacement); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(c.path, []byte("malformed"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := initialize(c.path, replacement); err == nil {
		t.Fatal("malformed state admitted")
	}
}

func TestIncompleteState(t *testing.T) {
	c := fixture(t)
	if err := os.WriteFile(c.path, []byte(`{}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := initialize(c.path, c.w); err == nil {
		t.Fatal("incomplete state admitted")
	}
}
