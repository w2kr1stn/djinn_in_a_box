// Linux PID 1 owns the window; the host observer never enforces its deadline.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

const stateDir = "/var/lib/tailscale/djinn-hostctl"
const socketPath = "/run/djinn-hostctl/control.sock"
const tailscaleSocket = "/run/djinn-hostctl/tailscaled.sock"

type window struct {
	Generation   string    `json:"generation"`
	BootID       string    `json:"boot_id"`
	Deadline     time.Time `json:"deadline"`
	BootDeadline int64     `json:"boottime_deadline_ns"`
	Closed       bool      `json:"closed"`
	Reason       string    `json:"reason,omitempty"`
	Admission    bool      `json:"admission"`
}

type request struct {
	Operation  string            `json:"operation"`
	Generation string            `json:"generation"`
	Minutes    int               `json:"minutes"`
	Routes     map[string]string `json:"routes,omitempty"`
}

type response struct {
	Window *window `json:"window,omitempty"`
	Error  string  `json:"error,omitempty"`
}

func boottime() int64 {
	var ts syscall.Timespec
	_, _, errno := syscall.RawSyscall(syscall.SYS_CLOCK_GETTIME, 7, uintptr(unsafe.Pointer(&ts)), 0)
	if errno != 0 {
		panic(errno)
	}
	return ts.Nano()
}

func bootID() string {
	data, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil {
		panic(err)
	}
	return strings.TrimSpace(string(data))
}

func (w window) valid(now time.Time, boot int64, id string) bool {
	return w.Generation != "" && w.BootID == id && !w.Closed &&
		!w.Deadline.IsZero() && now.Before(w.Deadline) && boot < w.BootDeadline
}

func persist(path string, w window) error {
	data, err := json.Marshal(w)
	if err != nil {
		return err
	}
	f, err := os.OpenFile(path+".tmp", os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return err
	}
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(path+".tmp", path); err != nil {
		return err
	}
	d, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}

type controller struct {
	mu          sync.Mutex
	w           window
	path        string
	now         func() time.Time
	boot        func() int64
	bootID      string
	routes      map[string]string
	streams     map[*stream]bool
	connections sync.WaitGroup
	sequence    uint64
}

func (c *controller) update(r request) response {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.w.valid(c.now(), c.boot(), c.bootID) || (c.w.Admission && !c.diskMatches()) {
		return response{Error: "window is closed, expired or its state is unavailable"}
	}
	if r.Generation != c.w.Generation {
		return response{Error: "generation changed"}
	}
	if r.Operation == "set-deadline" {
		if r.Minutes < 1 || r.Minutes > 1440 {
			return response{Error: "minutes must be 1..1440"}
		}
		candidate := c.w
		duration := time.Duration(r.Minutes) * time.Minute
		candidate.Deadline = c.now().UTC().Add(duration)
		candidate.BootDeadline = c.boot() + int64(duration)
		if err := persist(c.path, candidate); err != nil {
			return response{Error: err.Error()}
		}
		c.w = candidate
	} else if r.Operation == "admit" {
		if !c.diskMatches() {
			return response{Error: "window state is unavailable or changed"}
		}
		if c.w.Admission {
			return response{Error: "trust is frozen for this generation"}
		}
		if err := validateRoutes(r.Routes); err != nil {
			return response{Error: err.Error()}
		}
		candidate := c.w
		candidate.Admission = true
		if err := persist(c.path, candidate); err != nil {
			return response{Error: err.Error()}
		}
		c.routes = r.Routes
		c.w = candidate
	} else if r.Operation != "status" {
		return response{Error: "unknown operation"}
	}
	copy := c.w
	return response{Window: &copy}
}

func (c *controller) close(reason string) error {
	c.mu.Lock()
	c.w.Closed = true
	c.w.Admission = false
	c.w.Reason = reason
	for stream := range c.streams {
		stream.reason = reason
		_ = stream.client.Close()
		if stream.upstream != nil {
			_ = stream.upstream.Close()
		}
	}
	err := persist(c.path, c.w)
	c.mu.Unlock()
	c.connections.Wait()
	// No login URL, key, payload or account credential is included in events.
	returnErr := json.NewEncoder(os.Stdout).Encode(map[string]any{
		"event": reason, "generation": c.w.Generation, "deadline": c.w.Deadline,
		"time": c.now().UTC(), "state_error": err != nil,
	})
	if err != nil {
		return err
	}
	return returnErr
}

var authURL = regexp.MustCompile(`https?://[^\s"<>]+`)

func redact(reader io.Reader) {
	scanner := bufio.NewScanner(reader)
	for scanner.Scan() {
		fmt.Fprintln(os.Stderr, authURL.ReplaceAllString(scanner.Text(), "[URL redacted]"))
	}
}

func serve(listener net.Listener, c *controller) {
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		go func() {
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
			var r request
			if err := json.NewDecoder(io.LimitReader(conn, 1024*1024)).Decode(&r); err != nil {
				_ = json.NewEncoder(conn).Encode(response{Error: "invalid request"})
				return
			}
			_ = json.NewEncoder(conn).Encode(c.update(r))
		}()
	}
}

func initialize(path string, w window) error {
	data, err := os.ReadFile(path)
	if err == nil {
		var previous window
		if json.Unmarshal(data, &previous) != nil {
			return errors.New("malformed window state; refusing startup")
		}
		if previous.Generation == "" || previous.BootID == "" || previous.Deadline.IsZero() || previous.BootDeadline <= 0 {
			return errors.New("incomplete window state; refusing startup")
		}
		// An explicit on uses a fresh generation. Restart never reopens one.
		if previous.Generation == w.Generation {
			return errors.New("generation already started; explicit on required")
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	return persist(path, w)
}

func run(args []string) error {
	flags := flag.NewFlagSet("run", flag.ContinueOnError)
	w := window{}
	var deadline string
	flags.StringVar(&w.Generation, "generation", "", "new window generation")
	flags.StringVar(&w.BootID, "boot-id", "", "host boot ID")
	flags.StringVar(&deadline, "deadline", "", "UTC deadline")
	flags.Int64Var(&w.BootDeadline, "boottime-deadline", 0, "CLOCK_BOOTTIME deadline in ns")
	if err := flags.Parse(args); err != nil {
		return err
	}
	var err error
	w.Deadline, err = time.Parse(time.RFC3339Nano, deadline)
	if err != nil {
		return err
	}
	if !w.valid(time.Now(), boottime(), bootID()) {
		return errors.New("invalid or expired initial window")
	}
	if err = os.MkdirAll(stateDir, 0700); err != nil {
		return err
	}
	if err = os.MkdirAll(filepath.Dir(socketPath), 0700); err != nil {
		return err
	}
	path := stateDir + "/window.json"
	if err = initialize(path, w); err != nil {
		return err
	}
	c := &controller{w: w, path: path, now: time.Now, boot: boottime, bootID: bootID()}
	_ = os.Remove(socketPath)
	listener, err := net.Listen("unix", socketPath)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err = os.Chmod(socketPath, 0600); err != nil {
		return err
	}
	go serve(listener, c)
	relay, err := net.Listen("tcp", ":1080")
	if err != nil {
		return err
	}
	defer relay.Close()
	go serveRelay(relay, c, "127.0.0.1:1055", os.Stdout)
	child := exec.Command("/usr/local/bin/tailscaled", "--tun=userspace-networking",
		"--statedir=/var/lib/tailscale", "--socket="+tailscaleSocket,
		"--socks5-server=127.0.0.1:1055")
	stdout, err := child.StdoutPipe()
	if err != nil {
		return err
	}
	stderr, err := child.StderrPipe()
	if err != nil {
		return err
	}
	if err = child.Start(); err != nil {
		_ = c.close("failure")
		return err
	}
	go redact(stdout)
	go redact(stderr)
	done := make(chan error, 1)
	go func() { done <- child.Wait() }()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	reason := monitor(c, done, signals)
	_ = relay.Close()
	if reason == "failure" {
		_ = c.close(reason)
		return errors.New("tailscaled exited")
	}

	err = c.close(reason)
	_ = listener.Close()
	_ = child.Process.Signal(syscall.SIGTERM)
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		_ = child.Process.Kill()
		<-done
	}
	return err
}

func monitor(c *controller, done <-chan error, signals <-chan os.Signal) string {
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-signals:
			return "off"
		case <-done:
			return "failure"
		case <-ticker.C:
			c.mu.Lock()
			valid := c.w.valid(c.now(), c.boot(), c.bootID)
			if !valid {
				c.w.Closed = true
			}
			c.mu.Unlock()
			if !valid {
				return "expiry"
			}
		}
	}
}

func client(args []string) error {
	if len(args) < 2 {
		return errors.New("expected operation generation [minutes]")
	}
	r := request{Operation: args[0], Generation: args[1]}
	if r.Operation == "admit" {
		if len(args) != 3 {
			return errors.New("expected frozen routes")
		}
		if err := json.Unmarshal([]byte(args[2]), &r.Routes); err != nil {
			return err
		}
	}
	if r.Operation == "set-deadline" {
		if len(args) != 3 {
			return errors.New("expected minutes")
		}
		if _, err := fmt.Sscanf(args[2], "%d", &r.Minutes); err != nil {
			return err
		}
	}
	conn, err := net.DialTimeout("unix", socketPath, 2*time.Second)
	if err != nil {
		return err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
	if err = json.NewEncoder(conn).Encode(r); err != nil {
		return err
	}
	var reply response
	if err = json.NewDecoder(conn).Decode(&reply); err != nil {
		return err
	}
	if reply.Error != "" {
		return errors.New(reply.Error)
	}
	if reply.Window == nil || reply.Window.Generation != r.Generation {
		return errors.New("invalid acknowledgement")
	}
	return json.NewEncoder(os.Stdout).Encode(reply.Window)
}

func main() {
	var err error
	if len(os.Args) < 2 {
		err = errors.New("expected run, status or set-deadline")
	} else if os.Args[1] == "run" {
		err = run(os.Args[2:])
	} else {
		err = client(os.Args[1:])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
