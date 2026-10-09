package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func acquirePIDFile(path string) (func(), error) {
	if path == "" {
		return func() {}, nil
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0640)
	if err != nil {
		return nil, fmt.Errorf("claim PID file: %w", err)
	}
	_, writeErr := fmt.Fprintf(file, "%d\n", os.Getpid())
	closeErr := file.Close()
	if writeErr != nil || closeErr != nil {
		_ = os.Remove(path)
		return nil, fmt.Errorf("write PID file: %w", errors.Join(writeErr, closeErr))
	}
	return func() { _ = os.Remove(path) }, nil
}

type signalPublisher struct{ PIDFile string }

func (p signalPublisher) Publish() error {
	if p.PIDFile == "" {
		return nil
	}
	data, err := os.ReadFile(p.PIDFile)
	if err != nil {
		return fmt.Errorf("read dnsmasq PID: %w", err)
	}
	pid, err := strconv.Atoi(strings.TrimSpace(string(data)))
	if err != nil || pid <= 1 {
		return fmt.Errorf("invalid dnsmasq PID")
	}
	process, err := os.FindProcess(pid)
	if err != nil {
		return fmt.Errorf("find dnsmasq: %w", err)
	}
	defer process.Release()
	if err := process.Signal(syscall.SIGHUP); err != nil {
		return fmt.Errorf("reload dnsmasq: %w", err)
	}
	return nil
}

func serveUntilCancelled(ctx context.Context, server *http.Server, listener net.Listener, timeout time.Duration) error {
	stopped := make(chan error, 1)
	go func() { stopped <- server.Serve(listener) }()
	select {
	case err := <-stopped:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), timeout)
		defer cancel()
		err := server.Shutdown(shutdownCtx)
		if err != nil {
			_ = server.Close()
		}
		serveErr := <-stopped
		if serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			return errors.Join(err, serveErr)
		}
		return err
	}
}
