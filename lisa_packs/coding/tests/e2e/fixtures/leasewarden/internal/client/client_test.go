package client

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequestAndResponse(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Header.Get("Authorization") != "Bearer secret" {
			t.Error("missing bearer")
		}
		if r.Method != "POST" || r.URL.Path != "/reservations" {
			t.Error("wrong request")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(201)
		_, _ = w.Write([]byte(`{"mac":"aa:bb:cc:dd:ee:01"}`))
	}))
	defer server.Close()
	client, err := New(Config{Server: server.URL, Token: "secret"})
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if err := client.Do(context.Background(), "POST", "/reservations", map[string]string{"ip": "192.168.1.10"}, &result); err != nil {
		t.Fatal(err)
	}
	if result["mac"] != "aa:bb:cc:dd:ee:01" || calls != 1 {
		t.Fatalf("result: %+v, calls %d", result, calls)
	}
}

func TestErrorStatusAndNoRetry(t *testing.T) {
	for _, status := range []int{401, 409, 503} {
		calls := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			w.WriteHeader(status)
			_, _ = w.Write([]byte(`{"error":"rejected"}`))
		}))
		client, err := New(Config{Server: server.URL})
		if err != nil {
			t.Fatal(err)
		}
		err = client.Do(context.Background(), "GET", "/reservations", nil, nil)
		server.Close()
		var apiErr *APIError
		if !errors.As(err, &apiErr) || apiErr.Status != status || apiErr.Code != "rejected" || calls != 1 {
			t.Fatalf("status %d: %v, calls %d", status, err, calls)
		}
	}
}

func TestClientConfiguration(t *testing.T) {
	for _, url := range []string{"localhost:8068", "file:///hosts", "http://user:secret@localhost", "http://localhost?x=y"} {
		if _, err := New(Config{Server: url}); err == nil {
			t.Errorf("accepted %q", url)
		}
	}
}
