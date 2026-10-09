// Package cmd owns client commands, independently of server process wiring.
package cmd

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"time"

	"example.invalid/leasewarden/internal/client"
	"example.invalid/leasewarden/internal/output"
)

const help = `leasewarden [--server URL] [--output table|json] COMMAND

Commands:
  leases                         list lease records
  clients                        summarize devices
  addresses MAC                  show device addresses
  devices IP                     show address holders
  reservations list              list persistent reservations
  reservations get MAC           inspect one reservation
  reservations add MAC IP [NAME]  create a reservation
  reservations update MAC IP [NAME]
  reservations delete MAC        withdraw a reservation

Global flags precede commands. The server subcommand has separate flags.
`

func envDefault(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

// Run executes a client command without changing process-global flag state.
func Run(ctx context.Context, args []string, stdout, stderr io.Writer) error {
	flags := flag.NewFlagSet("leasewarden", flag.ContinueOnError)
	flags.SetOutput(stderr)
	server := flags.String("server", envDefault("LEASEWARDEN_SERVER", "http://127.0.0.1:8068"), "server URL")
	format := flags.String("output", envDefault("LEASEWARDEN_OUTPUT", "table"), "table or json")
	timeout := flags.Duration("timeout", 5*time.Second, "HTTP request deadline")
	flags.Usage = func() { _, _ = io.WriteString(stderr, help); flags.PrintDefaults() }
	if err := flags.Parse(args); err != nil {
		if err == flag.ErrHelp {
			return nil
		}
		return err
	}
	args = flags.Args()
	if len(args) == 0 {
		_, err := io.WriteString(stdout, help)
		return err
	}
	if *format != "table" && *format != "json" {
		return fmt.Errorf("output must be table or json")
	}
	if *timeout <= 0 {
		return fmt.Errorf("timeout must be positive")
	}
	method, path, body, err := commandRequest(args)
	if err != nil {
		return err
	}
	transport, err := client.New(client.Config{Server: *server, Token: os.Getenv("LEASEWARDEN_TOKEN"), Timeout: *timeout})
	if err != nil {
		return err
	}
	var response any
	if err := transport.Do(ctx, method, path, body, &response); err != nil {
		return err
	}
	return output.Write(stdout, *format, response)
}

func commandRequest(args []string) (string, string, any, error) {
	switch args[0] {
	case "leases", "clients":
		if len(args) == 1 {
			return http.MethodGet, "/" + args[0], nil, nil
		}
	case "addresses", "devices":
		if len(args) == 2 {
			return http.MethodGet, "/" + args[0] + "/" + url.PathEscape(args[1]), nil, nil
		}
	case "reservations":
		return reservationRequest(args[1:])
	}
	return "", "", nil, fmt.Errorf("unknown command or wrong arguments; use --help")
}

func reservationRequest(args []string) (string, string, any, error) {
	if len(args) == 0 {
		return "", "", nil, fmt.Errorf("reservation action required")
	}
	switch args[0] {
	case "list":
		if len(args) == 1 {
			return http.MethodGet, "/reservations", nil, nil
		}
	case "get", "delete":
		if len(args) == 2 {
			method := http.MethodGet
			if args[0] == "delete" {
				method = http.MethodDelete
			}
			return method, "/reservations/" + url.PathEscape(args[1]), nil, nil
		}
	case "add", "update":
		if len(args) == 3 || len(args) == 4 {
			body := map[string]string{"mac": args[1], "ip": args[2]}
			if len(args) == 4 {
				body["hostname"] = args[3]
			}
			if args[0] == "add" {
				return http.MethodPost, "/reservations", body, nil
			}
			return http.MethodPut, "/reservations/" + url.PathEscape(args[1]), body, nil
		}
	}
	return "", "", nil, fmt.Errorf("wrong reservation arguments; use --help")
}
