package main

import (
	"errors"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"example.invalid/leasewarden/internal/macs"
)

var errNotFound = errors.New("reservation not found")
var errAlreadyReserved = errors.New("MAC already reserved")

// Reservation is the persistent and public representation of a device pin.
type Reservation struct {
	MAC      string `json:"mac"`
	IP       string `json:"ip"`
	Hostname string `json:"hostname"`
}

// Store owns only MAC-named .conf files in Dir. Other files are untouched.
type Store struct {
	Dir string
	mu  sync.RWMutex
}

// NewStore prepares a private directory for reservation records.
func NewStore(dir string) (*Store, error) {
	if dir == "" {
		return nil, fmt.Errorf("host directory is required")
	}
	if err := os.MkdirAll(dir, 0750); err != nil {
		return nil, fmt.Errorf("create hostsdir: %w", err)
	}
	return &Store{Dir: dir}, nil
}

func (s *Store) path(mac string) (string, error) {
	filename, err := macs.Filename(mac)
	if err != nil {
		return "", err
	}
	return filepath.Join(s.Dir, filename), nil
}

func parseReservation(data string) (Reservation, error) {
	fields := strings.Split(strings.TrimSpace(data), ",")
	if len(fields) < 2 || len(fields) > 3 {
		return Reservation{}, fmt.Errorf("invalid host record")
	}
	mac, err := macs.Normalize(fields[0])
	if err != nil {
		return Reservation{}, err
	}
	ip, err := normalizeIP(fields[1], netip.Prefix{})
	if err != nil {
		return Reservation{}, err
	}
	hostname := ""
	if len(fields) == 3 {
		hostname, err = normalizeHostname(fields[2])
		if err != nil {
			return Reservation{}, err
		}
	}
	return Reservation{mac, ip, hostname}, nil
}

func hostLine(res Reservation) string {
	fields := []string{res.MAC, res.IP}
	if res.Hostname != "" {
		fields = append(fields, res.Hostname)
	}
	return strings.Join(fields, ",") + "\n"
}

func (s *Store) read(path string) (Reservation, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return Reservation{}, errNotFound
	}
	if err != nil {
		return Reservation{}, fmt.Errorf("read reservation: %w", err)
	}
	res, err := parseReservation(string(data))
	if err != nil {
		return Reservation{}, fmt.Errorf("parse %s: %w", filepath.Base(path), err)
	}
	expected, _ := s.path(res.MAC)
	if expected != path {
		return Reservation{}, fmt.Errorf("reservation filename does not match MAC")
	}
	return res, nil
}

// Get returns one device after normalizing its key.
func (s *Store) Get(mac string) (Reservation, error) {
	path, err := s.path(mac)
	if err != nil {
		return Reservation{}, err
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.read(path)
}

// List returns managed reservations sorted by MAC for stable API output.
func (s *Store) List() ([]Reservation, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return nil, fmt.Errorf("list reservations: %w", err)
	}
	out := make([]Reservation, 0)
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".conf") {
			continue
		}
		key := strings.TrimSuffix(entry.Name(), ".conf")
		filename, err := macs.Filename(key)
		if err != nil || filename != entry.Name() {
			continue
		}
		res, err := s.read(filepath.Join(s.Dir, entry.Name()))
		if err != nil {
			return nil, err
		}
		out = append(out, res)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].MAC < out[j].MAC })
	return out, nil
}

// Put creates a record, or replaces an existing record when replace is true.
func (s *Store) Put(res Reservation, replace bool) (Reservation, error) {
	key, err := macs.Normalize(res.MAC)
	if err != nil {
		return Reservation{}, err
	}
	res.MAC = key
	res.IP, err = normalizeIP(res.IP, netip.Prefix{})
	if err != nil {
		return Reservation{}, err
	}
	res.Hostname, err = normalizeHostname(res.Hostname)
	if err != nil {
		return Reservation{}, err
	}
	path, _ := s.path(key)
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err = os.Stat(path)
	if err == nil && !replace {
		return Reservation{}, errAlreadyReserved
	}
	if os.IsNotExist(err) && replace {
		return Reservation{}, errNotFound
	}
	if err != nil && !os.IsNotExist(err) {
		return Reservation{}, fmt.Errorf("stat reservation: %w", err)
	}
	file, err := os.CreateTemp(s.Dir, ".pending-")
	if err != nil {
		return Reservation{}, fmt.Errorf("stage reservation: %w", err)
	}
	defer os.Remove(file.Name())
	if err := file.Chmod(0640); err != nil {
		file.Close()
		return Reservation{}, err
	}
	if _, err := file.WriteString(hostLine(res)); err != nil {
		file.Close()
		return Reservation{}, err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return Reservation{}, err
	}
	if err := file.Close(); err != nil {
		return Reservation{}, err
	}
	if err := os.Rename(file.Name(), path); err != nil {
		return Reservation{}, fmt.Errorf("install reservation: %w", err)
	}
	return res, nil
}

// Delete withdraws the device's file; a missing device is not successful.
func (s *Store) Delete(mac string) error {
	path, err := s.path(mac)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := os.Remove(path); err != nil {
		if os.IsNotExist(err) {
			return errNotFound
		}
		return fmt.Errorf("withdraw reservation: %w", err)
	}
	return nil
}
