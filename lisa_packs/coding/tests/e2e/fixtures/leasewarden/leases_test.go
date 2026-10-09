package main

import (
	"net/netip"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseLease(t *testing.T) {
	tests := []struct {
		name, line string
		valid      bool
	}{
		{"ordinary", "4102444800 AA:BB:CC:DD:EE:01 192.168.1.20 Laptop 01:aa:bb", true},
		{"infinite", "0 aa-bb-cc-dd-ee-02 192.168.1.21 * *", true},
		{"blank", "", false},
		{"missing field", "1 aa:bb:cc:dd:ee:01 192.168.1.20 laptop", false},
		{"negative expiry", "-1 aa:bb:cc:dd:ee:01 192.168.1.20 laptop *", false},
		{"non numeric", "forever aa:bb:cc:dd:ee:01 192.168.1.20 laptop *", false},
		{"bad MAC", "0 unknown 192.168.1.20 laptop *", false},
		{"IPv6", "0 aa:bb:cc:dd:ee:01 ::1 laptop *", false},
		{"bad hostname", "0 aa:bb:cc:dd:ee:01 192.168.1.20 laptop.local *", false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := parseLease(test.line)
			if (err == nil) != test.valid {
				t.Fatalf("parse %q: %v", test.line, err)
			}
		})
	}
	lease, err := parseLease(tests[0].line)
	if err != nil {
		t.Fatal(err)
	}
	if lease.MAC != "aa:bb:cc:dd:ee:01" || lease.Hostname != "laptop" || lease.ClientID != "01:aa:bb" {
		t.Fatalf("not normalized: %+v", lease)
	}
	infinite, _ := parseLease(tests[1].line)
	if infinite.Expires != 0 || infinite.Hostname != "" || infinite.ClientID != "" {
		t.Fatalf("star fields: %+v", infinite)
	}
}

func TestLeaseActivity(t *testing.T) {
	now := time.Unix(100, 0)
	for _, test := range []struct {
		expiry int64
		active bool
	}{{0, true}, {99, false}, {100, false}, {101, true}} {
		if got := (Lease{Expires: test.expiry}).Active(now); got != test.active {
			t.Errorf("expiry %d: %v", test.expiry, got)
		}
	}
}

func TestLeaseSnapshots(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "leases")
	data := "4102444800 AA:BB:CC:DD:EE:01 192.168.1.20 Laptop *\n" +
		"1000 aa-bb-cc-dd-ee-01 192.168.1.99 old *\n" +
		"0 aa:bb:cc:dd:ee:02 10.0.0.2 elsewhere *\ngarbage\n"
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	file := &LeaseFile{Path: path, Subnet: netip.MustParsePrefix("192.168.1.0/24")}
	all, err := file.All()
	if err != nil || len(all) != 2 {
		t.Fatalf("All: %+v, %v", all, err)
	}
	found, err := file.ByMAC("AABBCCDDEE01")
	if err != nil || len(found) != 2 {
		t.Fatalf("ByMAC: %+v, %v", found, err)
	}
	found, err = file.ByIP("192.168.1.20")
	if err != nil || len(found) != 1 {
		t.Fatalf("ByIP: %+v, %v", found, err)
	}
	if _, err := file.ByMAC("bad"); err == nil {
		t.Fatal("bad MAC accepted")
	}
	if err := os.WriteFile(path, nil, 0600); err != nil {
		t.Fatal(err)
	}
	all, err = file.All()
	if err != nil || len(all) != 0 || all == nil {
		t.Fatalf("fresh empty snapshot: %+v, %v", all, err)
	}
	missing, err := (&LeaseFile{Path: filepath.Join(root, "absent")}).All()
	if err != nil || len(missing) != 0 {
		t.Fatalf("missing file: %+v, %v", missing, err)
	}
}

func TestHostnameAndIPValidation(t *testing.T) {
	for _, raw := range []string{"999.1.1.1", "0.0.0.0", "127.0.0.1", "224.0.0.1", "::ffff:192.168.1.2"} {
		if _, err := normalizeIP(raw, netip.Prefix{}); err == nil {
			t.Errorf("accepted %s", raw)
		}
	}
	if _, err := normalizeIP("10.0.0.2", netip.MustParsePrefix("192.168.1.0/24")); err == nil {
		t.Fatal("off-subnet IP accepted")
	}
	for _, raw := range []string{"-nas", "nas-", "nas.local", "a,b", "a\nb"} {
		if _, err := normalizeHostname(raw); err == nil {
			t.Errorf("accepted hostname %q", raw)
		}
	}
}
