package main

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type testPublisher struct {
	count int
	err   error
}

func (p *testPublisher) Publish() error { p.count++; return p.err }

type handlerFixture struct {
	router    http.Handler
	store     *Store
	publisher *testPublisher
	leases    string
}

func newHandlerFixture(t *testing.T) *handlerFixture {
	t.Helper()
	root := t.TempDir()
	leases := filepath.Join(root, "leases")
	if err := os.WriteFile(leases, nil, 0600); err != nil {
		t.Fatal(err)
	}
	store, err := NewStore(filepath.Join(root, "hosts.d"))
	if err != nil {
		t.Fatal(err)
	}
	publisher := &testPublisher{}
	router := newRouter(routerDeps{Leases: &LeaseFile{Path: leases, Subnet: netip.MustParsePrefix("192.168.1.0/24")}, Store: store, Publisher: publisher, Token: "unit-token"})
	return &handlerFixture{router, store, publisher, leases}
}

func (f *handlerFixture) call(t *testing.T, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	request.Header.Set("Authorization", "Bearer unit-token")
	request.Header.Set("Content-Type", "application/json")
	response := httptest.NewRecorder()
	f.router.ServeHTTP(response, request)
	return response
}

func assertStatus(t *testing.T, response *httptest.ResponseRecorder, status int) {
	t.Helper()
	if response.Code != status {
		t.Fatalf("status %d, want %d: %s", response.Code, status, response.Body.String())
	}
}

func TestReservationHTTP(t *testing.T) {
	f := newHandlerFixture(t)
	created := f.call(t, "POST", "/reservations", `{"mac":"AA-BB-CC-DD-EE-01","ip":"192.168.1.10","hostname":"NAS"}`)
	assertStatus(t, created, http.StatusCreated)
	var record Reservation
	if err := json.Unmarshal(created.Body.Bytes(), &record); err != nil {
		t.Fatal(err)
	}
	if record.MAC != "aa:bb:cc:dd:ee:01" || record.Hostname != "nas" {
		t.Fatalf("record: %+v", record)
	}
	if f.publisher.count != 1 {
		t.Fatalf("publish count %d", f.publisher.count)
	}
	assertStatus(t, f.call(t, "GET", "/reservations/AABBCCDDEE01", ""), 200)
	assertStatus(t, f.call(t, "GET", "/reservations", ""), 200)
	assertStatus(t, f.call(t, "POST", "/reservations", `{"mac":"aabbccddee01","ip":"192.168.1.11"}`), 409)
	if f.publisher.count != 1 {
		t.Fatal("duplicate published")
	}
	updated := f.call(t, "PUT", "/reservations/aabbccddee01", `{"ip":"192.168.1.11","hostname":"backup"}`)
	assertStatus(t, updated, 200)
	if !strings.Contains(updated.Body.String(), "backup") {
		t.Fatal("hostname not updated")
	}
	assertStatus(t, f.call(t, "DELETE", "/reservations/aa:bb:cc:dd:ee:01", ""), 204)
	assertStatus(t, f.call(t, "GET", "/reservations/aabbccddee01", ""), 404)
	assertStatus(t, f.call(t, "DELETE", "/reservations/aabbccddee01", ""), 404)
	if f.publisher.count != 3 {
		t.Fatalf("publish count %d", f.publisher.count)
	}
}

func TestReservationInvalidInput(t *testing.T) {
	tests := []struct{ name, body string }{
		{"bad MAC", `{"mac":"garbage","ip":"192.168.1.10"}`},
		{"missing MAC", `{"ip":"192.168.1.10"}`},
		{"malformed IP", `{"mac":"aabbccddee01","ip":"999.1.1.1"}`},
		{"IPv6", `{"mac":"aabbccddee01","ip":"2001:db8::1"}`},
		{"off subnet", `{"mac":"aabbccddee01","ip":"10.0.0.4"}`},
		{"injected hostname", `{"mac":"aabbccddee01","ip":"192.168.1.10","hostname":"nas,other"}`},
		{"array", `[1]`}, {"null", `null`}, {"bad JSON", `{`},
		{"wrong field type", `{"mac":12,"ip":"192.168.1.10"}`},
		{"extra document", `{} {}`}, {"unknown field", `{"surprise":true}`},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			f := newHandlerFixture(t)
			response := f.call(t, "POST", "/reservations", test.body)
			assertStatus(t, response, 400)
			if !strings.Contains(response.Body.String(), `"error"`) {
				t.Fatal("missing JSON error")
			}
			records, err := f.store.List()
			if err != nil || len(records) != 0 || f.publisher.count != 0 {
				t.Fatalf("invalid request changed state: %+v, %v", records, err)
			}
		})
	}
}

func TestReservationUpdateValidation(t *testing.T) {
	f := newHandlerFixture(t)
	assertStatus(t, f.call(t, "POST", "/reservations", `{"mac":"aabbccddee01","ip":"192.168.1.10"}`), 201)
	assertStatus(t, f.call(t, "PUT", "/reservations/aabbccddee01", `{"hostname":"new"}`), 422)
	assertStatus(t, f.call(t, "PUT", "/reservations/aabbccddee01", `{"mac":"aabbccddee02","ip":"192.168.1.11"}`), 400)
	assertStatus(t, f.call(t, "PUT", "/reservations/aabbccddee99", `{"ip":"192.168.1.11"}`), 404)
	if f.publisher.count != 1 {
		t.Fatal("invalid update published")
	}
}

func TestQueryHTTP(t *testing.T) {
	f := newHandlerFixture(t)
	data := "4102444800 AA:BB:CC:DD:EE:01 192.168.1.20 Laptop *\n" +
		"1000 aa-bb-cc-dd-ee-01 192.168.1.21 old *\n" +
		"0 aa:bb:cc:dd:ee:02 10.0.0.2 elsewhere *\n"
	if err := os.WriteFile(f.leases, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"/leases", "/clients", "/addresses/AABBCCDDEE01", "/devices/192.168.1.20"} {
		response := f.call(t, "GET", path, "")
		assertStatus(t, response, 200)
		if strings.Contains(response.Body.String(), "10.0.0.2") {
			t.Fatal("unscoped query")
		}
	}
	assertStatus(t, f.call(t, "GET", "/addresses/aabbccddee99", ""), 404)
	assertStatus(t, f.call(t, "GET", "/devices/192.168.1.99", ""), 404)
	assertStatus(t, f.call(t, "GET", "/addresses/nope", ""), 400)
	assertStatus(t, f.call(t, "GET", "/devices/999.1.1.1", ""), 400)
	assertStatus(t, f.call(t, "DELETE", "/reservations", ""), 405)
	assertStatus(t, f.call(t, "GET", "/unknown", ""), 404)
}

func TestPublishFailureIsVisible(t *testing.T) {
	f := newHandlerFixture(t)
	f.publisher.err = errors.New("reload unavailable")
	response := f.call(t, "POST", "/reservations", `{"mac":"aabbccddee01","ip":"192.168.1.10"}`)
	assertStatus(t, response, 503)
	if !strings.Contains(response.Body.String(), "publish_failed") {
		t.Fatal("reload failure hidden")
	}
	record, err := f.store.Get("aabbccddee01")
	if err != nil || record.IP != "192.168.1.10" {
		t.Fatalf("durable write lost: %+v, %v", record, err)
	}
}
