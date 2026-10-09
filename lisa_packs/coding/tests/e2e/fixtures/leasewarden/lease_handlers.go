package main

import (
	"net/http"
	"sort"

	"example.invalid/leasewarden/internal/macs"
)

func (a *api) listLeases(w http.ResponseWriter, r *http.Request) {
	leases, err := a.deps.Leases.All()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "leases_unavailable")
		return
	}
	writeJSON(w, http.StatusOK, leases)
}

type clientSummary struct {
	MAC       string   `json:"mac"`
	Hostname  string   `json:"hostname"`
	Addresses []string `json:"addresses"`
}

func (a *api) listClients(w http.ResponseWriter, r *http.Request) {
	leases, err := a.deps.Leases.All()
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "leases_unavailable")
		return
	}
	byMAC := make(map[string]*clientSummary)
	for _, lease := range leases {
		item := byMAC[lease.MAC]
		if item == nil {
			item = &clientSummary{MAC: lease.MAC, Addresses: make([]string, 0)}
			byMAC[lease.MAC] = item
		}
		if lease.Hostname != "" {
			item.Hostname = lease.Hostname
		}
		seen := false
		for _, address := range item.Addresses {
			if address == lease.IP {
				seen = true
				break
			}
		}
		if !seen {
			item.Addresses = append(item.Addresses, lease.IP)
		}
	}
	records := make([]clientSummary, 0, len(byMAC))
	for _, record := range byMAC {
		sort.Strings(record.Addresses)
		records = append(records, *record)
	}
	sort.Slice(records, func(i, j int) bool { return records[i].MAC < records[j].MAC })
	writeJSON(w, http.StatusOK, records)
}

func (a *api) listAddresses(w http.ResponseWriter, r *http.Request) {
	mac, err := macs.Normalize(r.PathValue("mac"))
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	records, err := a.deps.Leases.ByMAC(mac)
	a.writeLeaseMatches(w, records, err)
}

func (a *api) listDevices(w http.ResponseWriter, r *http.Request) {
	ip, err := normalizeIP(r.PathValue("ip"), a.deps.Leases.Subnet)
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid")
		return
	}
	records, err := a.deps.Leases.ByIP(ip)
	a.writeLeaseMatches(w, records, err)
}

func (a *api) writeLeaseMatches(w http.ResponseWriter, records []Lease, err error) {
	if err != nil {
		writeError(w, http.StatusServiceUnavailable, "leases_unavailable")
		return
	}
	if len(records) == 0 {
		writeError(w, http.StatusNotFound, "not_found")
		return
	}
	writeJSON(w, http.StatusOK, records)
}
