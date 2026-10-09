package main

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	lwAcceptanceToken  = "acceptance-token"
	lwAcceptanceFuture = "4102444800"
	lwAcceptancePast   = "1000000000"
	lwAcceptanceMAC1   = "aa:bb:cc:dd:ee:01"
	lwAcceptanceMAC2   = "aa:bb:cc:dd:ee:02"
	lwAcceptanceMAC3   = "aa:bb:cc:dd:ee:03"
)

type lwAcceptancePublisher struct{ calls atomic.Int64 }

func (p *lwAcceptancePublisher) Publish() error { p.calls.Add(1); return nil }

type lwAcceptanceFixture struct {
	t               *testing.T
	leases, hostdir string
	server          *httptest.Server
	publisher       *lwAcceptancePublisher
}

type lwAcceptanceResult struct {
	status int
	body   map[string]any
}

func lwAcceptanceNew(t *testing.T, lines ...string) *lwAcceptanceFixture {
	t.Helper()
	root := t.TempDir()
	f := &lwAcceptanceFixture{t: t, leases: filepath.Join(root, "leases"), hostdir: filepath.Join(root, "hosts.d"), publisher: &lwAcceptancePublisher{}}
	f.writeLeases(lines...)
	store, err := NewStore(f.hostdir)
	if err != nil {
		t.Fatal(err)
	}
	f.server = httptest.NewServer(newRouter(routerDeps{
		Leases: &LeaseFile{Path: f.leases, Subnet: netip.MustParsePrefix("192.168.1.0/24")},
		Store:  store, Publisher: f.publisher, Token: lwAcceptanceToken,
	}))
	f.server.Client().Timeout = 5 * time.Second
	t.Cleanup(f.server.Close)
	return f
}

func (f *lwAcceptanceFixture) writeLeases(lines ...string) {
	f.t.Helper()
	if err := os.WriteFile(f.leases, []byte(strings.Join(lines, "\n")+"\n"), 0600); err != nil {
		f.t.Fatal(err)
	}
}

func (f *lwAcceptanceFixture) call(method, path string, body any) lwAcceptanceResult {
	f.t.Helper()
	var data io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			f.t.Fatal(err)
		}
		data = bytes.NewReader(encoded)
	}
	req, err := http.NewRequest(method, f.server.URL+path, data)
	if err != nil {
		f.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+lwAcceptanceToken)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := f.server.Client().Do(req)
	if err != nil {
		f.t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		f.t.Fatal(err)
	}
	result := lwAcceptanceResult{status: resp.StatusCode}
	if len(bytes.TrimSpace(raw)) != 0 {
		if err := json.Unmarshal(raw, &result.body); err != nil {
			f.t.Fatalf("%s %s returned non-object JSON: %s (%v)", method, path, raw, err)
		}
	}
	return result
}

func lwAcceptanceCanonical(raw string) string {
	digits := strings.ToLower(strings.TrimSpace(raw))
	digits = strings.ReplaceAll(strings.ReplaceAll(digits, ":", ""), "-", "")
	if len(digits) != 12 {
		return "invalid:" + raw
	}
	octets, err := hex.DecodeString(digits)
	if err != nil {
		return "invalid:" + raw
	}
	parts := make([]string, len(octets))
	for i, value := range octets {
		parts[i] = fmt.Sprintf("%02x", value)
	}
	return strings.Join(parts, ":")
}

func lwAcceptanceField(body map[string]any, key string) string {
	value, _ := body[key].(string)
	return value
}

func (f *lwAcceptanceFixture) files() map[string]string {
	f.t.Helper()
	entries, err := os.ReadDir(f.hostdir)
	if err != nil {
		f.t.Fatal(err)
	}
	files := make(map[string]string)
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(f.hostdir, entry.Name()))
		if err != nil {
			f.t.Fatal(err)
		}
		files[entry.Name()] = string(data)
	}
	return files
}

func (f *lwAcceptanceFixture) assertRecord(result lwAcceptanceResult, status int, mac, ip, hostname string) {
	f.t.Helper()
	if result.status != status {
		f.t.Fatalf("status %d, want %d: %+v", result.status, status, result.body)
	}
	if lwAcceptanceCanonical(lwAcceptanceField(result.body, "mac")) != mac || lwAcceptanceField(result.body, "ip") != ip || lwAcceptanceField(result.body, "hostname") != hostname {
		f.t.Fatalf("reservation fields: %+v; want %s %s %q", result.body, mac, ip, hostname)
	}
}

func (f *lwAcceptanceFixture) assertStored(mac, ip, hostname string) {
	f.t.Helper()
	f.assertRecord(f.call("GET", "/reservations/"+mac, nil), 200, mac, ip, hostname)
	found := 0
	for _, data := range f.files() {
		for _, line := range strings.Split(strings.TrimSpace(data), "\n") {
			fields := strings.Split(strings.TrimPrefix(line, "dhcp-host="), ",")
			if len(fields) < 2 || lwAcceptanceCanonical(fields[0]) != mac {
				continue
			}
			name := ""
			if len(fields) > 2 {
				name = fields[2]
			}
			if fields[1] != ip || name != hostname {
				f.t.Fatalf("stored hostsdir record differs: %q", line)
			}
			found++
		}
	}
	if found != 1 {
		f.t.Fatalf("want one published record for %s; found %d in %+v", mac, found, f.files())
	}
}

func (f *lwAcceptanceFixture) create(mac, ip string) {
	f.t.Helper()
	before := f.publisher.calls.Load()
	result := f.call("POST", "/reservations", map[string]string{"mac": mac, "ip": ip})
	f.assertRecord(result, 201, lwAcceptanceCanonical(mac), ip, "")
	f.assertStored(lwAcceptanceCanonical(mac), ip, "")
	if f.publisher.calls.Load() <= before {
		f.t.Fatal("successful creation was not published")
	}
}

func (f *lwAcceptanceFixture) conflict(method, path string, body any, owner string) {
	f.t.Helper()
	before := f.files()
	count := f.publisher.calls.Load()
	result := f.call(method, path, body)
	if result.status != 409 || lwAcceptanceField(result.body, "error") != "ip_conflict" || lwAcceptanceCanonical(lwAcceptanceField(result.body, "conflicting_mac")) != owner {
		f.t.Errorf("want 409 ip_conflict owned by %s; got %d %+v", owner, result.status, result.body)
	}
	if after := f.files(); !reflect.DeepEqual(before, after) {
		f.t.Errorf("conflict changed hostsdir: before %+v, after %+v", before, after)
	}
	if f.publisher.calls.Load() != count {
		f.t.Error("conflict invoked publisher")
	}
}

func TestAcceptanceReservationConflicts(t *testing.T) {
	t.Run("POST rejects another reservation", func(t *testing.T) {
		f := lwAcceptanceNew(t)
		f.create(lwAcceptanceMAC1, "192.168.1.10")
		f.conflict("POST", "/reservations", map[string]string{"mac": lwAcceptanceMAC2, "ip": "192.168.1.10"}, lwAcceptanceMAC1)
		if result := f.call("GET", "/reservations/"+lwAcceptanceMAC2, nil); result.status != 404 {
			t.Errorf("rejected MAC persisted: %+v", result)
		}
	})
	t.Run("PUT keeps original reservation", func(t *testing.T) {
		f := lwAcceptanceNew(t)
		f.create(lwAcceptanceMAC1, "192.168.1.10")
		f.create(lwAcceptanceMAC2, "192.168.1.11")
		f.conflict("PUT", "/reservations/"+lwAcceptanceMAC2, map[string]string{"ip": "192.168.1.10", "hostname": "changed"}, lwAcceptanceMAC1)
		f.assertStored(lwAcceptanceMAC2, "192.168.1.11", "")
	})
	t.Run("own reservation is not a conflict", func(t *testing.T) {
		f := lwAcceptanceNew(t)
		f.create(lwAcceptanceMAC1, "192.168.1.10")
		count := f.publisher.calls.Load()
		result := f.call("PUT", "/reservations/AABBCCDDEE01", map[string]string{"ip": "192.168.1.10", "hostname": "nas"})
		f.assertRecord(result, 200, lwAcceptanceMAC1, "192.168.1.10", "nas")
		f.assertStored(lwAcceptanceMAC1, "192.168.1.10", "nas")
		if f.publisher.calls.Load() <= count {
			t.Fatal("successful replacement not published")
		}
	})
}

func lwAcceptanceConflictLeases() []string {
	return []string{
		lwAcceptanceFuture + " AA:BB:CC:DD:EE:01 192.168.1.20 laptop *",
		"0 aa-bb-cc-dd-ee-01 192.168.1.21 laptop-wired *",
		lwAcceptancePast + " aa:bb:cc:dd:ee:01 192.168.1.22 laptop-old *",
		lwAcceptanceFuture + " aa:bb:cc:dd:ee:02 192.168.1.30 phone *",
	}
}

func TestAcceptanceLeaseConflicts(t *testing.T) {
	for _, test := range []struct{ name, ip string }{
		{"future lease of another MAC", "192.168.1.20"},
		{"infinite lease of another MAC", "192.168.1.21"},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := lwAcceptanceNew(t, lwAcceptanceConflictLeases()...)
			f.conflict("POST", "/reservations", map[string]string{"mac": lwAcceptanceMAC2, "ip": test.ip}, lwAcceptanceMAC1)
			if result := f.call("GET", "/reservations/"+lwAcceptanceMAC2, nil); result.status != 404 {
				t.Error("conflicting lease caused persistent reservation")
			}
		})
	}
	t.Run("expired lease does not conflict", func(t *testing.T) {
		f := lwAcceptanceNew(t, lwAcceptanceConflictLeases()...)
		f.create(lwAcceptanceMAC2, "192.168.1.22")
	})
	t.Run("own active lease does not conflict", func(t *testing.T) {
		f := lwAcceptanceNew(t, lwAcceptanceConflictLeases()...)
		f.create("AA-BB-CC-DD-EE-02", "192.168.1.30")
	})
	t.Run("PUT rejects another active lease", func(t *testing.T) {
		f := lwAcceptanceNew(t, lwAcceptanceConflictLeases()...)
		f.create(lwAcceptanceMAC3, "192.168.1.40")
		f.conflict("PUT", "/reservations/"+lwAcceptanceMAC3, map[string]string{"ip": "192.168.1.30"}, lwAcceptanceMAC2)
		f.assertStored(lwAcceptanceMAC3, "192.168.1.40", "")
	})
}

func lwAcceptancePinLeases() []string {
	return []string{
		lwAcceptancePast + " aa:bb:cc:dd:ee:02 192.168.1.29 phone-old *",
		lwAcceptanceFuture + " AA:BB:CC:DD:EE:02 192.168.1.30 Phone *",
		lwAcceptancePast + " aa:bb:cc:dd:ee:03 192.168.1.40 tablet *",
		lwAcceptanceFuture + " aa:bb:cc:dd:ee:04 10.0.0.4 offnet *",
		"0 aa:bb:cc:dd:ee:05 192.168.1.50 * *",
	}
}

func TestAcceptanceReserveFromLease(t *testing.T) {
	for _, test := range []struct {
		name, mac, ip, hostname string
		body                    map[string]string
	}{
		{"active lease supplies address and normalized hostname", lwAcceptanceMAC2, "192.168.1.30", "phone", map[string]string{"mac": "AABBCCDDEE02"}},
		{"body hostname overrides lease", lwAcceptanceMAC2, "192.168.1.30", "pixel", map[string]string{"mac": lwAcceptanceMAC2, "hostname": "pixel"}},
		{"infinite lease with no hostname", "aa:bb:cc:dd:ee:05", "192.168.1.50", "", map[string]string{"mac": "aa-bb-cc-dd-ee-05"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := lwAcceptanceNew(t, lwAcceptancePinLeases()...)
			result := f.call("POST", "/reservations", test.body)
			f.assertRecord(result, 201, test.mac, test.ip, test.hostname)
			f.assertStored(test.mac, test.ip, test.hostname)
			if f.publisher.calls.Load() == 0 {
				t.Fatal("pin not published")
			}
		})
	}
	for _, test := range []struct{ name, mac string }{
		{"only expired lease", lwAcceptanceMAC3},
		{"off-subnet lease", "aa:bb:cc:dd:ee:04"},
		{"unknown MAC", "aa:bb:cc:dd:ee:99"},
	} {
		t.Run(test.name, func(t *testing.T) {
			f := lwAcceptanceNew(t, lwAcceptancePinLeases()...)
			before := f.files()
			result := f.call("POST", "/reservations", map[string]string{"mac": test.mac})
			if result.status != 422 || lwAcceptanceField(result.body, "error") != "no_active_lease" {
				t.Errorf("want 422 no_active_lease; got %d %+v", result.status, result.body)
			}
			if !reflect.DeepEqual(before, f.files()) || f.publisher.calls.Load() != 0 {
				t.Error("missing active lease changed or published state")
			}
			if got := f.call("GET", "/reservations/"+test.mac, nil); got.status != 404 {
				t.Error("failed pin was persisted")
			}
		})
	}
	t.Run("inferred address still checks reservations using fresh snapshot", func(t *testing.T) {
		f := lwAcceptanceNew(t)
		f.create(lwAcceptanceMAC1, "192.168.1.30")
		f.writeLeases(lwAcceptancePinLeases()...)
		f.conflict("POST", "/reservations", map[string]string{"mac": lwAcceptanceMAC2}, lwAcceptanceMAC1)
		f.assertStored(lwAcceptanceMAC1, "192.168.1.30", "")
		if got := f.call("GET", "/reservations/"+lwAcceptanceMAC2, nil); got.status != 404 {
			t.Error("conflicting inferred pin persisted")
		}
	})
	t.Run("PUT still requires explicit IP", func(t *testing.T) {
		f := lwAcceptanceNew(t, lwAcceptancePinLeases()...)
		created := f.call("POST", "/reservations", map[string]string{"mac": lwAcceptanceMAC2})
		f.assertRecord(created, 201, lwAcceptanceMAC2, "192.168.1.30", "phone")
		before := f.files()
		count := f.publisher.calls.Load()
		result := f.call("PUT", "/reservations/"+lwAcceptanceMAC2, map[string]string{"hostname": "x"})
		if result.status != 422 || lwAcceptanceField(result.body, "error") != "invalid" {
			t.Errorf("want 422 invalid; got %d %+v", result.status, result.body)
		}
		if !reflect.DeepEqual(before, f.files()) || f.publisher.calls.Load() != count {
			t.Error("invalid PUT changed or published state")
		}
		f.assertStored(lwAcceptanceMAC2, "192.168.1.30", "phone")
	})
}
