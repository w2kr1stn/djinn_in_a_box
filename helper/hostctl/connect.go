package main

import (
	"errors"
	"io"
	"net"
	"time"
)

var controlSocketPath = socketPath
var setupTimeout = 10 * time.Second
var stdinEOFGrace = 2 * time.Second
var dialRelay = func(addr string, deadline time.Time) (net.Conn, error) {
	return net.DialTimeout("tcp", addr, time.Until(deadline))
}

func connectFailure(err error) error {
	var timeout net.Error
	if errors.As(err, &timeout) && timeout.Timeout() {
		return errors.New("hostctl connect: setup timed out")
	}
	var refused *socksRefusal
	if errors.As(err, &refused) {
		if refused.code == 2 {
			return errors.New("hostctl connect: relay refused destination")
		}
		return errors.New("hostctl connect: relay could not reach destination")
	}
	return errors.New("hostctl connect: setup failed")
}

func connect(args []string, stdin io.Reader, stdout io.Writer) error {
	deadline := time.Now().Add(setupTimeout)
	if len(args) != 2 || args[0] == "" {
		return connectFailure(nil)
	}
	gen, selector := args[0], args[1]
	if _, err := selectorAddress(selector); err != nil {
		return connectFailure(err)
	}
	reply, err := controlRequest(controlSocketPath, request{Operation: "status", Generation: gen}, deadline)
	if err != nil {
		return connectFailure(err)
	}
	switch reply.Error {
	case "generation changed":
		return errors.New("hostctl connect: generation changed")
	case "window is closed, expired or its state is unavailable", "window state is unavailable or changed":
		return errors.New("hostctl connect: window not open")
	case "":
	default:
		return connectFailure(nil)
	}
	w := reply.Window
	if w == nil {
		return connectFailure(nil)
	}
	if w.Generation != gen {
		return errors.New("hostctl connect: generation changed")
	}
	if !w.Admission || w.Paused {
		return errors.New("hostctl connect: window not open")
	}
	conn, err := dialRelay(connectRelayAddr, deadline)
	if err != nil {
		return connectFailure(err)
	}
	defer conn.Close()
	if err = conn.SetDeadline(deadline); err != nil {
		return connectFailure(err)
	}
	if err = socksConnect(conn, selector); err != nil {
		return connectFailure(err)
	}
	if err = conn.SetDeadline(time.Time{}); err != nil {
		return connectFailure(err)
	}

	inputDone := make(chan error, 1)
	outputDone := make(chan error, 1)
	go func() {
		_, err := io.Copy(conn, stdin)
		if tcp, ok := conn.(interface{ CloseWrite() error }); ok {
			_ = tcp.CloseWrite()
		}
		inputDone <- err
	}()
	go func() {
		_, err := io.Copy(stdout, conn)
		if output, ok := stdout.(interface{ CloseWrite() error }); ok {
			_ = output.CloseWrite()
		}
		outputDone <- err
	}()
	select {
	case <-outputDone:
		return nil
	case err := <-inputDone:
		if err != nil {
			return nil
		}
	}
	grace := time.NewTimer(stdinEOFGrace)
	defer grace.Stop()
	select {
	case <-outputDone:
	case <-grace.C:
	}
	return nil
}
