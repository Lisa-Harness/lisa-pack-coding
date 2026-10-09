package main

import (
	"net/http/httptest"
	"testing"
)

func TestBearerParsing(t *testing.T) {
	for _, test := range []struct{ header, token string }{
		{"Bearer secret", "secret"}, {"bearer secret", "secret"},
		{"Bearer\tsecret", "secret"}, {"Basic secret", ""},
		{"Bearer", ""}, {"Bearer secret extra", ""}, {"", ""},
	} {
		if got := bearerValue(test.header); got != test.token {
			t.Errorf("header %q: %q", test.header, got)
		}
	}
}

func TestTokenComparison(t *testing.T) {
	for _, test := range []struct {
		configured, candidate string
		valid                 bool
	}{
		{"secret", "secret", true}, {"secret", "wrong", false},
		{"", "", false}, {"secret", "", false}, {"", "secret", false},
	} {
		if got := tokenMatches(test.configured, test.candidate); got != test.valid {
			t.Errorf("token comparison: %v", got)
		}
	}
}

func TestAuthenticationRoutes(t *testing.T) {
	f := newHandlerFixture(t)
	for _, header := range []string{"", "Basic unit-token", "Bearer wrong"} {
		request := httptest.NewRequest("GET", "/reservations", nil)
		request.Header.Set("Authorization", header)
		response := httptest.NewRecorder()
		f.router.ServeHTTP(response, request)
		assertStatus(t, response, 401)
		if response.Header().Get("WWW-Authenticate") == "" {
			t.Fatal("missing challenge")
		}
	}
	response := httptest.NewRecorder()
	f.router.ServeHTTP(response, httptest.NewRequest("GET", "/health", nil))
	assertStatus(t, response, 200)
}
