package main

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestStoreLifecycle(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "hosts.d")
	store, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	input := Reservation{"AA-BB-CC-DD-EE-01", "192.168.1.10", "NAS"}
	got, err := store.Put(input, false)
	if err != nil {
		t.Fatal(err)
	}
	if got.MAC != "aa:bb:cc:dd:ee:01" || got.Hostname != "nas" {
		t.Fatalf("normalized: %+v", got)
	}
	path := filepath.Join(dir, "aa-bb-cc-dd-ee-01.conf")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "aa:bb:cc:dd:ee:01,192.168.1.10,nas\n" {
		t.Fatalf("hostsdir line: %q", data)
	}
	got, err = store.Get("AABBCCDDEE01")
	if err != nil || got.IP != "192.168.1.10" {
		t.Fatalf("Get: %+v, %v", got, err)
	}
	if _, err := store.Put(input, false); !errors.Is(err, errAlreadyReserved) {
		t.Fatalf("duplicate: %v", err)
	}
	input.IP = "192.168.1.11"
	input.Hostname = ""
	got, err = store.Put(input, true)
	if err != nil || got.IP != input.IP {
		t.Fatalf("replace: %+v, %v", got, err)
	}
	all, err := store.List()
	if err != nil || len(all) != 1 {
		t.Fatalf("List: %+v, %v", all, err)
	}
	if err := store.Delete(input.MAC); err != nil {
		t.Fatal(err)
	}
	if _, err := store.Get(input.MAC); !errors.Is(err, errNotFound) {
		t.Fatalf("Get deleted: %v", err)
	}
	if err := store.Delete(input.MAC); !errors.Is(err, errNotFound) {
		t.Fatalf("Delete twice: %v", err)
	}
	if _, err := store.Put(input, true); !errors.Is(err, errNotFound) {
		t.Fatalf("replace missing: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil || len(entries) != 0 {
		t.Fatalf("staging leftovers: %v, %v", entries, err)
	}
}

func TestStoreIgnoresUnmanagedFiles(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"README", "operator.conf", ".pending-test"} {
		if err := os.WriteFile(filepath.Join(store.Dir, name), []byte("operator data"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	all, err := store.List()
	if err != nil || len(all) != 0 {
		t.Fatalf("unmanaged files: %+v, %v", all, err)
	}
	if _, err := store.Get("../../outside"); err == nil {
		t.Fatal("unsafe key accepted")
	}
	if _, err := store.Put(Reservation{"aa:bb:cc:dd:ee:01", "999.1.1.1", ""}, false); err == nil {
		t.Fatal("bad IP accepted")
	}
}

func TestStoreDetectsDamagedManagedRecord(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(store.Dir, "aa-bb-cc-dd-ee-01.conf")
	for _, data := range []string{"broken", "aa:bb:cc:dd:ee:02,192.168.1.10\n"} {
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := store.List(); err == nil {
			t.Fatalf("accepted damaged record %q", data)
		}
	}
}

func TestStoreConcurrentCreation(t *testing.T) {
	store, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	results := make(chan error, 8)
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := store.Put(Reservation{"aa:bb:cc:dd:ee:01", "192.168.1.10", ""}, false)
			results <- err
		}()
	}
	wg.Wait()
	close(results)
	created := 0
	for err := range results {
		if err == nil {
			created++
		} else if !errors.Is(err, errAlreadyReserved) {
			t.Fatal(err)
		}
	}
	if created != 1 {
		t.Fatalf("created %d times", created)
	}
	entries, err := os.ReadDir(store.Dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".pending-") {
			t.Fatal("staged file left behind")
		}
	}
}
