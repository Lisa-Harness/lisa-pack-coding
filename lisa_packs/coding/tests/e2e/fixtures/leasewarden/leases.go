package main

import (
	"bufio"
	"fmt"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"time"

	"example.invalid/leasewarden/internal/macs"
)

// Lease is one dnsmasq IPv4 record; Expires is an epoch second, or zero.
type Lease struct {
	Expires  int64  `json:"expires"`
	MAC      string `json:"mac"`
	IP       string `json:"ip"`
	Hostname string `json:"hostname"`
	ClientID string `json:"client_id"`
}

// Active reports whether the record is valid at the supplied instant.
func (l Lease) Active(at time.Time) bool { return l.Expires == 0 || l.Expires > at.Unix() }

func parseLease(line string) (Lease, error) {
	fields := strings.Fields(line)
	if len(fields) != 5 {
		return Lease{}, fmt.Errorf("lease requires five fields")
	}
	expiry, err := strconv.ParseInt(fields[0], 10, 64)
	if err != nil || expiry < 0 {
		return Lease{}, fmt.Errorf("invalid lease expiry")
	}
	mac, err := macs.Normalize(fields[1])
	if err != nil {
		return Lease{}, err
	}
	ip, err := normalizeIP(fields[2], netip.Prefix{})
	if err != nil {
		return Lease{}, err
	}
	hostname, err := normalizeHostname(fields[3])
	if err != nil {
		return Lease{}, err
	}
	clientID := fields[4]
	if clientID == "*" {
		clientID = ""
	}
	return Lease{expiry, mac, ip, hostname, clientID}, nil
}

// LeaseFile reads fresh snapshots; it never writes dnsmasq's lease file.
type LeaseFile struct {
	Path   string
	Subnet netip.Prefix
}

// All returns valid, subnet-scoped records in source order, including history.
func (f *LeaseFile) All() ([]Lease, error) {
	leases := make([]Lease, 0)
	file, err := os.Open(f.Path)
	if os.IsNotExist(err) {
		return leases, nil
	}
	if err != nil {
		return nil, fmt.Errorf("open leases: %w", err)
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	scanner.Buffer(make([]byte, 4096), 256*1024)
	for scanner.Scan() {
		lease, err := parseLease(scanner.Text())
		if err != nil {
			continue
		}
		addr, _ := netip.ParseAddr(lease.IP)
		if f.Subnet.IsValid() && !f.Subnet.Contains(addr) {
			continue
		}
		leases = append(leases, lease)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read leases: %w", err)
	}
	return leases, nil
}

// ByMAC keeps every matching address rather than arbitrarily selecting one.
func (f *LeaseFile) ByMAC(raw string) ([]Lease, error) {
	key, err := macs.Normalize(raw)
	if err != nil {
		return nil, err
	}
	leases, err := f.All()
	if err != nil {
		return nil, err
	}
	found := make([]Lease, 0)
	for _, lease := range leases {
		if lease.MAC == key {
			found = append(found, lease)
		}
	}
	return found, nil
}

// ByIP returns all device records for an address in the managed network.
func (f *LeaseFile) ByIP(raw string) ([]Lease, error) {
	key, err := normalizeIP(raw, f.Subnet)
	if err != nil {
		return nil, err
	}
	leases, err := f.All()
	if err != nil {
		return nil, err
	}
	found := make([]Lease, 0)
	for _, lease := range leases {
		if lease.IP == key {
			found = append(found, lease)
		}
	}
	return found, nil
}
