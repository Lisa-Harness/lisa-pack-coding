package macs

import "testing"

func TestNormalize(t *testing.T) {
	for _, raw := range []string{"AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "AABBCCDDEEFF", " Aa:bB:cc:DD:ee:ff "} {
		got, err := Normalize(raw)
		if err != nil || got != "aa:bb:cc:dd:ee:ff" {
			t.Errorf("%q: %q, %v", raw, got, err)
		}
	}
	for _, raw := range []string{"", "aa:bb:cc:dd:ee", "gg:bb:cc:dd:ee:ff", "aa:bb-cc:dd:ee:ff", "aabb.ccdd.eeff", "../../etc/passwd", "aa::bb:cc:dd:ee:ff"} {
		if _, err := Normalize(raw); err == nil {
			t.Errorf("accepted %q", raw)
		}
	}
}

func TestFilename(t *testing.T) {
	got, err := Filename("AABBCCDDEE01")
	if err != nil || got != "aa-bb-cc-dd-ee-01.conf" {
		t.Fatalf("filename: %q, %v", got, err)
	}
	if _, err := Filename("../escape"); err == nil {
		t.Fatal("unsafe filename")
	}
}
