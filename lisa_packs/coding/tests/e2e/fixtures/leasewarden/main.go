package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/signal"
	"syscall"
	"time"

	"example.invalid/leasewarden/cmd"
)

var version = "development"

type serverConfig struct {
	Listen     string
	Leases     string
	HostDir    string
	Subnet     string
	PIDFile    string
	DNSPIDFile string
	Token      string
	Shutdown   time.Duration
}

func parseServerConfig(args []string, stderr io.Writer) (serverConfig, error) {
	cfg := serverConfig{Token: os.Getenv("LEASEWARDEN_TOKEN")}
	flags := flag.NewFlagSet("leasewarden server", flag.ContinueOnError)
	flags.SetOutput(stderr)
	flags.StringVar(&cfg.Listen, "listen", "127.0.0.1:8068", "HTTP bind address")
	flags.StringVar(&cfg.Leases, "leases", "/var/lib/misc/dnsmasq.leases", "dnsmasq lease file")
	flags.StringVar(&cfg.HostDir, "hostdir", "/etc/dnsmasq.hosts.d", "reservation directory")
	flags.StringVar(&cfg.Subnet, "subnet", "192.168.1.0/24", "managed IPv4 subnet")
	flags.StringVar(&cfg.PIDFile, "pid-file", "", "optional exclusive leasewarden PID file")
	flags.StringVar(&cfg.DNSPIDFile, "dnsmasq-pid", "", "optional dnsmasq PID file for reload")
	flags.DurationVar(&cfg.Shutdown, "shutdown-timeout", 5*time.Second, "graceful shutdown budget")
	if err := flags.Parse(args); err != nil {
		return cfg, err
	}
	if flags.NArg() != 0 {
		return cfg, fmt.Errorf("unexpected server arguments")
	}
	return cfg, nil
}

func (cfg serverConfig) validate() (netip.Prefix, error) {
	if cfg.Token == "" {
		return netip.Prefix{}, fmt.Errorf("LEASEWARDEN_TOKEN must be set")
	}
	if cfg.HostDir == "" || cfg.Leases == "" {
		return netip.Prefix{}, fmt.Errorf("lease and host paths are required")
	}
	if _, _, err := net.SplitHostPort(cfg.Listen); err != nil {
		return netip.Prefix{}, fmt.Errorf("listen address: %w", err)
	}
	subnet, err := netip.ParsePrefix(cfg.Subnet)
	if err != nil || !subnet.Addr().Is4() {
		return netip.Prefix{}, fmt.Errorf("subnet must be an IPv4 CIDR")
	}
	if cfg.Shutdown <= 0 {
		return netip.Prefix{}, fmt.Errorf("shutdown timeout must be positive")
	}
	file, err := os.Open(cfg.Leases)
	if err != nil {
		return netip.Prefix{}, fmt.Errorf("open lease file at startup: %w", err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return netip.Prefix{}, err
	}
	if !info.Mode().IsRegular() {
		return netip.Prefix{}, fmt.Errorf("lease path must be a regular file")
	}
	return subnet.Masked(), nil
}

func runServer(ctx context.Context, cfg serverConfig) error {
	subnet, err := cfg.validate()
	if err != nil {
		return err
	}
	store, err := NewStore(cfg.HostDir)
	if err != nil {
		return err
	}
	cleanup, err := acquirePIDFile(cfg.PIDFile)
	if err != nil {
		return err
	}
	defer cleanup()
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	server := &http.Server{
		Handler: newRouter(routerDeps{
			Leases: &LeaseFile{Path: cfg.Leases, Subnet: subnet},
			Store:  store, Publisher: signalPublisher{PIDFile: cfg.DNSPIDFile}, Token: cfg.Token,
		}),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       time.Minute,
	}
	return serveUntilCancelled(ctx, server, listener, cfg.Shutdown)
}

func main() {
	args := os.Args[1:]
	if len(args) == 1 && args[0] == "version" {
		fmt.Println(version)
		return
	}
	if len(args) == 0 || args[0] != "server" {
		if err := cmd.Run(context.Background(), args, os.Stdout, os.Stderr); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}
	cfg, err := parseServerConfig(args[1:], os.Stderr)
	if err == flag.ErrHelp {
		return
	}
	if err == nil {
		ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
		defer stop()
		err = runServer(ctx, cfg)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
