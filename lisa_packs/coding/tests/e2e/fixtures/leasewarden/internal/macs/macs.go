// Package macs provides the canonical device key used by the API and files.
package macs

import (
	"encoding/hex"
	"fmt"
	"strings"
)

// Normalize accepts six octets in colon, dash, or unseparated notation.
func Normalize(value string) (string, error) {
	raw := strings.ToLower(strings.TrimSpace(value))
	if len(raw) == 17 {
		separator := raw[2]
		if separator != ':' && separator != '-' {
			return "", fmt.Errorf("invalid MAC %q", value)
		}
		for _, index := range []int{5, 8, 11, 14} {
			if raw[index] != separator {
				return "", fmt.Errorf("mixed MAC separators")
			}
		}
		raw = strings.ReplaceAll(raw, string(separator), "")
	}
	if len(raw) != 12 {
		return "", fmt.Errorf("MAC must contain six octets")
	}
	octets, err := hex.DecodeString(raw)
	if err != nil {
		return "", fmt.Errorf("decode MAC: %w", err)
	}
	parts := make([]string, len(octets))
	for i, octet := range octets {
		parts[i] = fmt.Sprintf("%02x", octet)
	}
	return strings.Join(parts, ":"), nil
}

// Filename returns the managed hostsdir filename for a validated MAC.
func Filename(value string) (string, error) {
	key, err := Normalize(value)
	if err != nil {
		return "", err
	}
	return strings.ReplaceAll(key, ":", "-") + ".conf", nil
}
