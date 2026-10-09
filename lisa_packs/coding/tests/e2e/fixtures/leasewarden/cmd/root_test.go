package cmd

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestCommandMapping(t *testing.T) {
	tests := []struct {
		args         []string
		method, path string
	}{
		{[]string{"leases"}, "GET", "/leases"},
		{[]string{"clients"}, "GET", "/clients"},
		{[]string{"addresses", "aabbccddee01"}, "GET", "/addresses/aabbccddee01"},
		{[]string{"devices", "192.168.1.10"}, "GET", "/devices/192.168.1.10"},
		{[]string{"reservations", "list"}, "GET", "/reservations"},
		{[]string{"reservations", "get", "aabbccddee01"}, "GET", "/reservations/aabbccddee01"},
		{[]string{"reservations", "add", "aabbccddee01", "192.168.1.10", "nas"}, "POST", "/reservations"},
		{[]string{"reservations", "update", "aabbccddee01", "192.168.1.11"}, "PUT", "/reservations/aabbccddee01"},
		{[]string{"reservations", "delete", "aabbccddee01"}, "DELETE", "/reservations/aabbccddee01"},
	}
	for _, test := range tests {
		method, path, _, err := commandRequest(test.args)
		if err != nil || method != test.method || path != test.path {
			t.Errorf("%v: %s %s, %v", test.args, method, path, err)
		}
	}
	for _, args := range [][]string{{"unknown"}, {"leases", "extra"}, {"reservations"}, {"reservations", "add"}} {
		if _, _, _, err := commandRequest(args); err == nil {
			t.Errorf("accepted %v", args)
		}
	}
}

func TestFlagsOverrideEnvironment(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Header.Get("Authorization") != "Bearer unit-token" {
			t.Error("token not propagated")
		}
		_, _ = w.Write([]byte(`[{"mac":"aa:bb:cc:dd:ee:01","ip":"192.168.1.10"}]`))
	}))
	defer server.Close()
	t.Setenv("LEASEWARDEN_SERVER", "http://127.0.0.1:1")
	t.Setenv("LEASEWARDEN_OUTPUT", "table")
	t.Setenv("LEASEWARDEN_TOKEN", "unit-token")
	var stdout, stderr bytes.Buffer
	err := Run(context.Background(), []string{"--server", server.URL, "--output", "json", "leases"}, &stdout, &stderr)
	if err != nil || !strings.Contains(stdout.String(), `"mac"`) || requests != 1 {
		t.Fatalf("Run: %s, %v", &stdout, err)
	}
}

func TestHelpAndInvalidFlags(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if err := Run(context.Background(), nil, &stdout, &stderr); err != nil || !strings.Contains(stdout.String(), "reservations") {
		t.Fatal("help failed")
	}
	if err := Run(context.Background(), []string{"--help"}, &stdout, &stderr); err != nil {
		t.Fatal(err)
	}
	if err := Run(context.Background(), []string{"--output", "xml", "leases"}, &stdout, &stderr); err == nil {
		t.Fatal("bad format accepted")
	}
	if err := Run(context.Background(), []string{"--timeout", "0s", "leases"}, &stdout, &stderr); err == nil {
		t.Fatal("bad timeout accepted")
	}
}
