package main

import (
	"fmt"
	"net/netip"
	"strings"
)

func normalizeIP(raw string, subnet netip.Prefix) (string, error) {
	addr, err := netip.ParseAddr(strings.TrimSpace(raw))
	if err != nil || !addr.Is4() || addr.IsUnspecified() || addr.IsLoopback() || addr.IsMulticast() {
		return "", fmt.Errorf("invalid IPv4 address %q", raw)
	}
	if subnet.IsValid() && !subnet.Contains(addr) {
		return "", fmt.Errorf("address outside managed subnet")
	}
	return addr.String(), nil
}

func normalizeHostname(raw string) (string, error) {
	name := strings.ToLower(strings.TrimSpace(raw))
	if name == "*" || name == "" {
		return "", nil
	}
	if len(name) > 63 || name[0] == '-' || name[len(name)-1] == '-' {
		return "", fmt.Errorf("invalid hostname")
	}
	for _, c := range name {
		if (c < 'a' || c > 'z') && (c < '0' || c > '9') && c != '-' {
			return "", fmt.Errorf("invalid hostname")
		}
	}
	return name, nil
}
