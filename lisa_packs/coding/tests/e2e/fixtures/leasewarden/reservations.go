package main

import (
	"net/http"

	"example.invalid/leasewarden/internal/macs"
)

type reservationInput struct {
	MAC      string `json:"mac"`
	IP       string `json:"ip"`
	Hostname string `json:"hostname"`
}

func (a *api) listReservations(w http.ResponseWriter, r *http.Request) {
	records, err := a.deps.Store.List()
	if err != nil {
		storeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, records)
}

func (a *api) getReservation(w http.ResponseWriter, r *http.Request) {
	key, err := macs.Normalize(r.PathValue("mac"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	record, err := a.deps.Store.Get(key)
	if err != nil {
		storeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, record)
}

func (a *api) createReservation(w http.ResponseWriter, r *http.Request) {
	a.saveReservation(w, r, false)
}

func (a *api) updateReservation(w http.ResponseWriter, r *http.Request) {
	a.saveReservation(w, r, true)
}

func (a *api) saveReservation(w http.ResponseWriter, r *http.Request, replace bool) {
	var input *reservationInput
	if !decodeBody(w, r, &input) {
		return
	}
	if input == nil {
		writeError(w, http.StatusBadRequest, "bad_request")
		return
	}
	key := input.MAC
	if replace {
		key = r.PathValue("mac")
	}
	mac, err := macs.Normalize(key)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	if replace && input.MAC != "" {
		bodyMAC, err := macs.Normalize(input.MAC)
		if err != nil || bodyMAC != mac {
			writeError(w, http.StatusBadRequest, "invalid")
			return
		}
	}
	if input.IP == "" {
		status := http.StatusBadRequest
		if replace {
			status = http.StatusUnprocessableEntity
		}
		writeError(w, status, "invalid")
		return
	}
	ip, err := normalizeIP(input.IP, a.deps.Leases.Subnet)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	hostname, err := normalizeHostname(input.Hostname)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	a.writes.Lock()
	defer a.writes.Unlock()
	record, err := a.deps.Store.Put(Reservation{mac, ip, hostname}, replace)
	if err != nil {
		storeError(w, err)
		return
	}
	if !a.publish(w) {
		return
	}
	status := http.StatusCreated
	if replace {
		status = http.StatusOK
	}
	writeJSON(w, status, record)
}

func (a *api) deleteReservation(w http.ResponseWriter, r *http.Request) {
	mac, err := macs.Normalize(r.PathValue("mac"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	a.writes.Lock()
	defer a.writes.Unlock()
	if err := a.deps.Store.Delete(mac); err != nil {
		storeError(w, err)
		return
	}
	if !a.publish(w) {
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (a *api) publish(w http.ResponseWriter) bool {
	if a.deps.Publisher != nil {
		if err := a.deps.Publisher.Publish(); err != nil {
			// The file is already durable; report reload failure without hiding it.
			writeError(w, http.StatusServiceUnavailable, "publish_failed")
			return false
		}
	}
	return true
}
