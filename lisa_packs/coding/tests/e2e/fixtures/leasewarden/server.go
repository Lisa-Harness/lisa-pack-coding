package main

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
)

// Publisher asks dnsmasq to reload after a durable reservation change.
type Publisher interface{ Publish() error }

type routerDeps struct {
	Leases    *LeaseFile
	Store     *Store
	Publisher Publisher
	Token     string
}

type api struct {
	deps   routerDeps
	writes sync.Mutex
}

// newRouter is shared by process wiring and in-process handler tests.
func newRouter(deps routerDeps) http.Handler {
	a := &api{deps: deps}
	private := http.NewServeMux()
	private.HandleFunc("GET /leases", a.listLeases)
	private.HandleFunc("GET /clients", a.listClients)
	private.HandleFunc("GET /addresses/{mac}", a.listAddresses)
	private.HandleFunc("GET /devices/{ip}", a.listDevices)
	private.HandleFunc("GET /reservations", a.listReservations)
	private.HandleFunc("GET /reservations/{mac}", a.getReservation)
	private.HandleFunc("POST /reservations", a.createReservation)
	private.HandleFunc("PUT /reservations/{mac}", a.updateReservation)
	private.HandleFunc("DELETE /reservations/{mac}", a.deleteReservation)
	public := http.NewServeMux()
	public.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	public.Handle("/", bearerAuth(deps.Token, private))
	return public
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if body != nil {
		_ = json.NewEncoder(w).Encode(body)
	}
}

func writeError(w http.ResponseWriter, status int, code string) {
	writeJSON(w, status, map[string]string{"error": code})
}

func storeError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, errNotFound):
		writeError(w, http.StatusNotFound, "not_found")
	case errors.Is(err, errAlreadyReserved):
		writeError(w, http.StatusConflict, "already_reserved")
	default:
		writeError(w, http.StatusInternalServerError, "storage_unavailable")
	}
}

func decodeBody(w http.ResponseWriter, r *http.Request, target any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 16*1024)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	err := decoder.Decode(target)
	if err == nil {
		var extra any
		if decoder.Decode(&extra) != io.EOF {
			err = errors.New("trailing JSON")
		}
	}
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			writeError(w, http.StatusRequestEntityTooLarge, "too_large")
		} else {
			writeError(w, http.StatusBadRequest, "bad_request")
		}
		return false
	}
	return true
}
